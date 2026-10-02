"""把新重建主链接到隐藏 Verifier、Harbor 校准和 Hermes 复验。

模型代码只由 Harbor/AGS 执行。未执行、执行不完整或校准失败都不能返回 READY。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.response_acceptance import apply_file_semantic_receipts, apply_response_receipts
from traceforge.harbor_ags.results import (
    HarborResultError,
    build_file_artifact_snapshot,
    certify_hermes_job,
    read_rollout_results,
    rollout_passed,
)
from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
    publish_rollout_bundle,
)
from traceforge.reconstruction.artifact_review import review_file_artifact
from traceforge.reconstruction.environment_bindings import non_file_obligation_ids
from traceforge.reconstruction.model_gateway import ChatModel, ModelGatewayError
from traceforge.reconstruction.run_config import load_rollout_limits
from traceforge.task_instruction import grounded_response_contract
from traceforge.verifier.bundle import compile_bundle
from traceforge.verifier.iterative import synthesize_verifier_iterative
from traceforge.verifier.red_check import RedCheckCase, evaluate_red_check
from traceforge.verifier.synthesis import (
    VerifierCandidate,
    VerifierSynthesisError,
    synthesize_verifier,
    validate_solution_scripts,
)

VERIFICATION_SCHEMA = "traceforge.reconstruction-verification.v1"


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    harbor_root: Path
    model_name: str
    rollout_model: str
    execute: bool = False
    execute_red: bool = False
    execute_rollout: bool = False
    manual_response_review: bool = False
    rollout_trials: int = 2
    max_rounds: int | None = None
    config_path: Path | None = None
    hermes_home: Path | None = None
    channel: str = "claude"
    timeout_seconds: int = 900
    rollout_max_iterations: int = 60

    def validate(self) -> None:
        if self.execute_rollout and self.rollout_trials < 2:
            raise ValueError("真实复验要求至少两次 Hermes rollout")
        if self.execute_rollout and not self.should_run_red():
            raise ValueError("真实 rollout 要求同时执行 Harbor RED；请设置 --execute-red")
        if self.max_rounds is not None and (
            isinstance(self.max_rounds, bool)
            or not isinstance(self.max_rounds, int)
            or self.max_rounds < 1
        ):
            raise ValueError("Verifier 迭代次数必须是正整数或 None（不限制）")
        if not self.model_name.strip() or "/" not in self.rollout_model:
            raise ValueError("必须明确 verifier model 和 provider/model rollout 模型")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds 必须大于 0")
        if self.rollout_max_iterations < 1:
            raise ValueError("rollout_max_iterations 必须大于 0")

        if self.execute_rollout and self.config_path is not None:
            configured_timeout, configured_iterations = load_rollout_limits(self.config_path)
            if self.timeout_seconds < configured_timeout:
                raise ValueError(
                    "rollout timeout_seconds cannot be lower than reviewed config budget "
                    f"{configured_timeout}"
                )
            if self.rollout_max_iterations < configured_iterations:
                raise ValueError(
                    "rollout_max_iterations cannot be lower than reviewed config budget "
                    f"{configured_iterations}"
                )

    def should_run_red(self) -> bool:
        return bool(self.execute or self.execute_red)


def verifier_task(task: dict[str, Any]) -> dict[str, Any]:
    """只接受 Intent 产出的义务 ID，不再后贴 criterion-001。"""
    result = dict(task)
    instruction = task.get("task_instruction") or task.get("core_objective")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("Verifier 输入缺少任务指令")
    result["task_instruction"] = instruction.strip()
    obligations = task.get("acceptance_obligations")
    if not isinstance(obligations, list) or not obligations:
        raise ValueError("Verifier 输入缺少验收义务")
    ids = [item.get("id") for item in obligations if isinstance(item, dict)]
    if len(ids) != len(obligations) or any(not isinstance(item, str) or not item for item in ids):
        raise ValueError("每条验收义务必须包含非空 id")
    if len(set(ids)) != len(ids):
        raise ValueError("验收义务 ID 重复")
    result["acceptance_obligations"] = obligations
    return result


def initial_red_check(
    candidate: VerifierCandidate, verdicts: list[dict[str, Any]],
    semantic_reviews: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """初态须缺少行为或声明的 FILE 语义能力；所有保护性测试仍通过。"""
    errors: list[str] = []
    if not verdicts:
        errors.append("INITIAL_VERDICT_MISSING")
    for index, verdict in enumerate(verdicts):
        tests = verdict.get("tests")
        by_name: dict[str, list[str]] = {}
        if isinstance(tests, list):
            for row in tests:
                if isinstance(row, dict):
                    name = str(row.get("name", "")).split("[", 1)[0]
                    by_name.setdefault(name, []).append(str(row.get("status", "")))
        missing = [by_name.get(name, []) for name in candidate.missing_capability_tests]
        protective = [by_name.get(name, []) for name in candidate.protective_tests]
        if any(not statuses for statuses in missing + protective):
            errors.append(f"INITIAL_TEST_NOT_COLLECTED:{index}")
        if any(
            status not in {"PASS", "FAIL"} for statuses in by_name.values() for status in statuses
        ):
            errors.append(f"INITIAL_TEST_INCOMPLETE:{index}")
        semantic = (semantic_reviews or [])[index] if index < len(semantic_reviews or []) else {}
        semantic_failed = (bool(candidate.file_semantic_checks)
                           and semantic.get("status") == "REVISE" and not semantic.get("errors"))
        if not any("FAIL" in statuses for statuses in missing) and not semantic_failed:
            errors.append(f"MISSING_CAPABILITY_ALREADY_PASSES:{index}")
        if any(
            not statuses or any(status != "PASS" for status in statuses) for statuses in protective
        ):
            errors.append(f"PROTECTIVE_TEST_FAILED:{index}")
    return {"passed": not errors, "errors": errors, "verdict_count": len(verdicts)}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _set_rollout_eligibility(result: dict[str, Any], expected_trials: int) -> bool:
    """集中维护 rollout、NON_FILE 义务和 SFT 资格的关系。"""
    passed = rollout_passed(result.get("rollout"), expected_trials)
    unresolved = result.get("unverified_obligations") or []
    eligible = (
        passed and not unresolved and result.get("status") == "READY"
        and not (result.get("errors") or [])
    )
    result["sft_eligible"] = eligible
    if not eligible:
        result["status"] = "REVIEW"
    if unresolved:
        result["errors"] = list(
            dict.fromkeys([*(result.get("errors") or []), "SFT_UNVERIFIED_OBLIGATIONS"])
        )
    return passed


def _certification_complete(result: dict[str, Any], expected_trials: int) -> bool:
    """全部 RED、复验和义务覆盖完成后才关闭认证。"""
    return (
        result.get("status") == "READY"
        and result.get("calibration") == "PASS"
        and rollout_passed(result.get("rollout"), expected_trials)
        and not (result.get("unverified_obligations") or [])
        and not (result.get("errors") or [])
    )


def write_execution_manifest(
    root: Path, result: dict[str, Any], config: VerificationConfig
) -> None:
    """RED 认证写独立清单；不覆盖 compile 时的 UNVALIDATED。"""

    calibration = result.get("calibration")
    if not isinstance(calibration, str) or not calibration:
        calibration = "NOT_RUN" if not config.should_run_red() else "INCOMPLETE"
    _write(
        root / "execution_manifest.json",
        {
            "schema_version": "traceforge.execution-manifest.v1",
            "compile_verifier_status": "UNVALIDATED",
            "compile_status_immutable": True,
            "red_status": calibration,
            "reconstruction_status": result.get("status"),
            "sft_eligible": bool(result.get("sft_eligible")),
            "roles": {
                "verifier": config.model_name,
                "rollout": config.rollout_model,
            },
            "certification_closed": _certification_complete(result, config.rollout_trials),
        },
    )


def _write_verification(
    root: Path, result: dict[str, Any], config: VerificationConfig
) -> None:
    unresolved = set(result.get("unverified_obligations") or [])
    pending = set(result.get("pending_response_obligations") or []) | set(
        result.get("pending_file_semantic_obligations") or [])
    response_ready = set(result.get("response_verifier_ready_obligations") or []) | set(
        result.get("file_semantic_verifier_ready_obligations") or [])
    if unresolved:
        # 重建 READY 表示验收机制已就绪；真实响应仍须在独立 rollout 后验收。
        result["sft_eligible"] = False
    blocking = unresolved - (pending & response_ready)
    if blocking:
        result["status"] = "REVIEW"
        manual = set(result.get("manual_response_review", {}).get("obligation_ids", []))
        result["errors"] = list(dict.fromkeys([
            *(result.get("errors") or []),
            "MANUAL_RESPONSE_REVIEW_REQUIRED" if blocking <= manual else "UNVERIFIED_OBLIGATIONS",
            *(f"RESPONSE_VERIFIER_MISSING:{item}" for item in sorted((blocking & pending) - manual)),
        ]))
    _write(root / "verification.json", result)
    write_execution_manifest(root, result, config)


def _workspace_files(root: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Verifier workspace 不允许符号链接")
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = f"[binary size={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}]"
        output[path.relative_to(root).as_posix()] = text
    return output



class HarborCalibrationExecutor:
    """每个候选都在独立 Harbor job 中校准，不执行宿主机 shell。"""

    def __init__(
        self, *, task: dict[str, Any], workspace: Path, root: Path, config: VerificationConfig,
        env_root: Path | None = None, agent: Any | None = None,
        source: dict[str, Any] | None = None,
        baseline_observations: list[dict[str, Any]] | None = None,
    ):
        self.task, self.workspace, self.root, self.config = task, workspace, root, config
        from traceforge.reconstruction.researcher import ReconstructionRuntime

        self.review_agent = agent.agent if isinstance(agent, ReconstructionRuntime) else agent
        self.source, self.baseline_observations = source, baseline_observations
        self.semantic_candidate: VerifierCandidate | None = None
        self.env_root = Path(env_root).resolve() if env_root is not None else None
        self.attempts: list[dict[str, Any]] = []
        self.bundle: Path | None = None

    def _build_harbor_plan(self, bundle: Path, *, label: str, mode: str, trials: int) -> Path:
        """为同一份校准 bundle 建立不可变 Harbor 计划。"""
        return build_rollout_plan(
            HarborRolloutConfig(
                task_dir=bundle,
                harbor_root=self.config.harbor_root,
                output_root=self.root / "plans" / label,
                jobs_root=self.root / "jobs" / label,
                agent_mode=mode,
                model=self.config.rollout_model,
                trials=trials,
                timeout_seconds=self.config.timeout_seconds,
                agent_max_iterations=self.config.rollout_max_iterations,
            )
        )

    def _publish_harbor_bundle(self, bundle: Path, *, label: str, trials: int) -> dict[str, str]:
        """发布 task/environment/verifier 输入；发布本身不宣称 rollout 成功。"""
        plan = self._build_harbor_plan(bundle, label=label, mode="hermes", trials=trials)
        published = publish_rollout_bundle(
            plan, self.root / "deliverables" / label / "harbor_bundle"
        )
        return {"plan": str(plan), "harbor_bundle": str(published.resolve())}

    def _run_bundle(
        self, bundle: Path, *, label: str, mode: str, trials: int = 1,
        expected_test_sha256: str | None = None,
    ) -> dict[str, Any]:
        jobs = self.root / "jobs" / label
        record: dict[str, Any] = {"plan": None, "execution": None, "results": None}
        if self.semantic_candidate is not None and self.semantic_candidate.file_semantic_checks:
            record["file_semantic_checks"] = self.semantic_candidate.file_semantic_checks
        test_file = bundle / "tests/test_outputs.py"
        try:
            actual_test_sha256 = hashlib.sha256(test_file.read_bytes()).hexdigest()
        except OSError:
            record["error"] = "VERIFIER_TEST_FILE_MISSING"
            return record
        record["verifier_test_sha256"] = actual_test_sha256
        if expected_test_sha256 is not None and actual_test_sha256 != expected_test_sha256:
            record["error"] = "VERIFIER_TEST_BYTES_MISMATCH"
            return record
        try:
            plan = self._build_harbor_plan(bundle, label=label, mode=mode, trials=trials)
            if mode == "hermes":
                published = publish_rollout_bundle(
                    plan, self.root / "deliverables" / label / "harbor_bundle"
                )
                record["harbor_bundle"] = str(published.resolve())
                record["harbor_bundle_status"] = "PUBLISHED"
            execution = execute_rollout_plan(
                plan,
                config_path=self.config.config_path,
                channel=self.config.channel,
            )
        except (HarborRolloutError, OSError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}:{exc}"
            return record
        record.update(plan=str(plan), execution=execution)
        completed = execution.get("status") == "COMPLETED"
        if not completed:
            record["error"] = f"HARBOR_EXECUTION_{execution.get('status', 'UNKNOWN')}"
        try:
            plan_payload = json.loads((plan / "rollout_plan.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            record["error"] = "HARBOR_PLAN_UNREADABLE"
            return record
        job_name = plan_payload.get("job_name") or plan_payload.get("run_id")
        if not isinstance(job_name, str) or not job_name:
            record["error"] = "HARBOR_JOB_DIR_NOT_FOUND"
            return record
        job_dir = jobs / job_name
        if not job_dir.is_dir():
            record["error"] = "HARBOR_JOB_DIR_NOT_FOUND"
            return record
        if mode == "hermes" and completed:
            certify_hermes_job(job_dir, harbor_root=self.config.harbor_root)
        try:
            record["results"] = read_rollout_results(
                job_dir, agent_mode=mode, expected_trial_count=trials
            )
        except HarborResultError as exc:
            record["error"] = str(exc)
        if not completed and isinstance(record["results"], dict):
            gate = record["results"].setdefault("quality_gate", {})
            gate["ok"] = False
            gate["reasons"] = [
                *(gate.get("reasons") or []),
                f"HARBOR_EXECUTION_{execution.get('status', 'UNKNOWN')}",
            ]
        if record.get("file_semantic_checks"):
            self._review_artifacts(record)
        return record

    def _review_artifacts(self, run: dict[str, Any]) -> None:
        """对真实快照补充语义证据；pytest 状态和 reward 保留原值。"""
        checks = run["file_semantic_checks"]
        results = run.get("results") or {}
        trials = results.get("trials") or []
        run["file_semantic_reviews"], run["combined_verdicts"] = [], []
        for trial in trials:
            receipt: dict[str, Any] = {"status": "REVIEW", "errors": []}
            try:
                if trial.get("status") not in {"PASS", "FAIL"} or trial.get("content_valid") is not True:
                    detail = json.dumps({
                        "status": trial.get("status"), "content_errors": trial.get("content_errors") or [],
                    }, ensure_ascii=False)
                    raise HarborResultError(f"本次 trial 的执行或捕获证据不完整: {detail}")
                trial_dir = Path(trial["result_path"]).parent
                snapshot = build_file_artifact_snapshot(trial_dir)
                receipt = review_file_artifact(
                    snapshot=snapshot, task=self.task, checks=checks, agent=self.review_agent,
                    output_root=trial_dir / "verifier/file-semantic-review",
                    execution_evidence={
                        "pytest_status": trial.get("status"), "pytest_reward": trial.get("reward"),
                        "verdict": json.loads(Path(trial["verdict_path"]).read_text()),
                        "process": self._trial_process_diagnostic(trial),
                    },
                    source=self.source, baseline_observations=self.baseline_observations,
                )
                _write(trial_dir / "verifier/file-semantic-review.json", receipt)
            except (HarborResultError, OSError, ValueError, KeyError) as exc:
                receipt = {"status": "REVIEW", "errors": [f"FILE_SEMANTIC_REVIEW_FAILED:{exc}"]}
            semantic_status = receipt["status"]
            behavior_status = trial.get("status")
            combined = (
                "PASS" if behavior_status == "PASS" and semantic_status == "ACCEPT"
                else "FAIL" if behavior_status in {"PASS", "FAIL"} and semantic_status in {"ACCEPT", "REVISE"}
                else "REVIEW"
            )
            run["file_semantic_reviews"].append(receipt)
            run["combined_verdicts"].append({
                "status": combined, "pytest_status": behavior_status,
                "pytest_reward": trial.get("reward"), "file_semantic_status": semantic_status,
            })

    @staticmethod
    def _trial_process_diagnostic(trial: dict[str, Any]) -> dict[str, Any]:
        """读取 Oracle 的退出证据；绝不把完整 stdout/stderr 写入反馈。"""
        result_path = trial.get("result_path")
        if not isinstance(result_path, str) or not result_path:
            return {"exit_code": None, "process_error": "TRIAL_RESULT_PATH_MISSING"}
        trial_dir = Path(result_path).parent
        exit_path = trial_dir / "agent" / "exit-code.txt"
        if not exit_path.is_file():
            # Harbor Oracle only writes this marker for non-zero exits.
            return {"exit_code": 0}
        try:
            raw_code = exit_path.read_text(encoding="utf-8").strip()
            code = int(raw_code)
        except (OSError, UnicodeError, ValueError):
            return {"exit_code": None, "process_error": "ORACLE_EXIT_CODE_INVALID"}
        diagnostic: dict[str, Any] = {"exit_code": code}
        log_path = trial_dir / "agent" / "oracle.txt"
        if log_path.is_file():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if text:
                diagnostic["oracle_log_tail"] = text[-2000:]
        return diagnostic

    @classmethod
    def _failure_diagnostics(cls, run: dict[str, Any]) -> dict[str, Any]:
        """为下一轮 Verifier 保留完整测试错误，避免丢失首次异常。"""

        diagnostics: dict[str, Any] = {}
        if run.get("error"):
            diagnostics["error"] = str(run["error"])
        results = run.get("results") or {}
        trials = results.get("trials") or []
        rows: list[dict[str, Any]] = []
        for trial in trials:
            row: dict[str, Any] = {
                "status": trial.get("status"),
                "reward": trial.get("reward"),
                "process": cls._trial_process_diagnostic(trial),
            }
            verdict_path = trial.get("verdict_path")
            if isinstance(verdict_path, str) and Path(verdict_path).is_file():
                try:
                    verdict = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    verdict = {}
                tests = verdict.get("tests") if isinstance(verdict, dict) else None
                if isinstance(tests, list):
                    row["tests"] = []
                    for item in tests:
                        if not isinstance(item, dict):
                            continue
                        test_row = {"name": item.get("name"), "status": item.get("status")}
                        if isinstance(item.get("message"), str) and item["message"]:
                            test_row["message"] = item["message"]
                        row["tests"].append(test_row)
                row["exit_code"] = verdict.get("exit_code") if isinstance(verdict, dict) else None
            rows.append(row)
        if rows:
            diagnostics["trials"] = rows
        if run.get("file_semantic_reviews") is not None:
            diagnostics["file_semantic_reviews"] = run["file_semantic_reviews"]
            diagnostics["combined_verdicts"] = run.get("combined_verdicts", [])
        quality = results.get("quality_gate")
        if isinstance(quality, dict):
            diagnostics["quality_gate"] = {
                "ok": quality.get("ok"),
                "errors": quality.get("errors", quality.get("reasons", [])),
            }
        return diagnostics


    @classmethod
    def _case(cls, label: str, kind: str, run: dict[str, Any], expected: str) -> RedCheckCase:
        results = run.get("results") or {}
        trials = results.get("trials") or []
        quality = results.get("quality_gate") or {}
        reward = 1.0 if expected == "PASS" else 0.0
        valid = bool(trials) and quality.get("ok") is True
        process_ok = True
        if kind in {"oracle_pass", "mutation_fail"}:
            for trial in trials:
                process = cls._trial_process_diagnostic(trial)
                if process.get("process_error") or process.get("exit_code") != 0:
                    process_ok = False
        if run.get("file_semantic_checks"):
            combined = run.get("combined_verdicts") or []
            valid = (valid and len(combined) == len(trials)
                     and all(row.get("status") in {"PASS", "FAIL"} for row in combined))
            passed = valid and process_ok and all(row["status"] == expected for row in combined)
        else:
            passed = valid and process_ok and all(
                row.get("status") == expected and row.get("reward") == reward for row in trials
            )
        if not valid:
            status = "INFRA_ERROR"
        elif not process_ok or not passed:
            # A crashing oracle/mutation is a candidate defect, never semantic
            # evidence and never a reason to mark calibration successful.
            status = "MISMATCH"
        else:
            status = expected
        return RedCheckCase(label, kind, status, reward if passed else None, expected, reward)

    def publish_calibrated_bundle(self, *, label: str, trials: int) -> dict[str, str]:
        """发布已通过 RED 校准的交付输入，不执行 Hermes。"""
        if self.bundle is None:
            raise HarborRolloutError("校准 bundle 不存在")
        return self._publish_harbor_bundle(self.bundle, label=label, trials=trials)

    def run(self, candidate: VerifierCandidate) -> dict[str, Any]:
        self.semantic_candidate = candidate
        number = len(self.attempts) + 1
        prefix = f"round-{number:02d}"
        runs: dict[str, Any] = {}
        cases: list[RedCheckCase] = []
        if candidate.file_semantic_checks and self.review_agent is None:
            feedback = json.dumps({"errors": ["FILE_SEMANTIC_REVIEW_AGENT_MISSING"]})
            record = {"round": number, "candidate_id": candidate.candidate_id,
                      "status": "INFRA_ERROR", "feedback": feedback, "preflight": True}
            self.attempts.append(record)
            _write(self.root / f"{prefix}.json", record)
            return record
        try:
            validate_solution_scripts(candidate.oracle_solutions, candidate.mutation_solutions)
        except VerifierSynthesisError as exc:
            feedback = json.dumps(
                {"generation_errors": [str(exc)]}, ensure_ascii=False
            )
            record = {
                "round": number,
                "candidate_id": candidate.candidate_id,
                "status": "FAIL",
                "feedback": feedback,
                "preflight": True,
            }
            self.attempts.append(record)
            _write(self.root / f"{prefix}.json", record)
            return {"status": "FAIL", "feedback": feedback}
        # Compile one oracle bundle once.  The first execution is deliberately
        # NOP: this is the paper's initial RED calibration and prevents a
        # verifier that passes on the empty environment from being accepted.
        bundle_root = self.root / "bundles" / prefix
        published = compile_bundle(
            task=self.task,
            workspace_root=self.workspace,
            verifier=candidate,
            output_root=bundle_root,
            solution_index=0,
            env_root=self.env_root,
        )
        self.bundle = published / "task"
        nop_label = f"{prefix}-nop"
        expected_hash = hashlib.sha256(candidate.test_outputs_py.encode("utf-8")).hexdigest()
        runs[nop_label] = self._run_bundle(
            self.bundle, label=nop_label, mode="nop", expected_test_sha256=expected_hash
        )
        cases.append(self._case(nop_label, "nop_fail", runs[nop_label], "FAIL"))
        for index in range(len(candidate.oracle_solutions)):
            if index == 0:
                oracle_bundle = self.bundle
            else:
                published = compile_bundle(
                    task=self.task,
                    workspace_root=self.workspace,
                    verifier=candidate,
                    output_root=bundle_root,
                    solution_index=index,
                    env_root=self.env_root,
                )
                oracle_bundle = published / "task"
            label = f"{prefix}-oracle-{index:02d}"
            runs[label] = self._run_bundle(
                oracle_bundle, label=label, mode="oracle", expected_test_sha256=expected_hash
            )
            cases.append(self._case(label, "oracle_pass", runs[label], "PASS"))
        for index in range(len(candidate.mutation_solutions)):
            published = compile_bundle(
                task=self.task,
                workspace_root=self.workspace,
                verifier=candidate,
                output_root=bundle_root,
                mutation_index=index,
                env_root=self.env_root,
            )
            label = f"{prefix}-mutation-{index:02d}"
            runs[label] = self._run_bundle(
                published / "task", label=label, mode="oracle", expected_test_sha256=expected_hash
            )
            cases.append(self._case(label, "mutation_fail", runs[label], "FAIL"))
        verdicts = []
        for trial in (runs[nop_label].get("results") or {}).get("trials", []):
            path = trial.get("verdict_path")
            if isinstance(path, str) and Path(path).is_file():
                try:
                    verdicts.append(json.loads(Path(path).read_text(encoding="utf-8")))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pass
        initial = initial_red_check(candidate, verdicts, runs[nop_label].get("file_semantic_reviews"))
        report = evaluate_red_check(tuple(cases))
        passed = report.passed and initial["passed"]
        status = (
            "PASS"
            if passed
            else ("INFRA_ERROR" if any(c.status == "INFRA_ERROR" for c in cases) else "FAIL")
        )
        record = {
            "round": number,
            "candidate_id": candidate.candidate_id,
            "verifier_test_sha256": hashlib.sha256(candidate.test_outputs_py.encode("utf-8")).hexdigest(),
            "status": status,
            "initial_red": initial,
            "failed_cases": list(report.failed_case_ids),
            "runs": runs,
        }
        self.attempts.append(record)
        _write(self.root / f"{prefix}.json", record)
        failed_case_ids = list(report.failed_case_ids)
        feedback_payload = {
            "calibrated_candidate_id": candidate.candidate_id,
            "calibrated_test_sha256": expected_hash,
            "initial_red": initial,
            "failed_cases": failed_case_ids,
            "case_diagnostics": {
                label: self._failure_diagnostics(runs[label])
                for label in failed_case_ids
                if label in runs
            },
        }
        return {
            "status": status,
            "feedback": json.dumps(feedback_payload, ensure_ascii=False),
            "file_semantic_calibration": status if candidate.file_semantic_checks else "NOT_REQUIRED",
        }


def _adopt_reviewed_response_contract(
    task: dict[str, Any], audit: dict[str, Any], candidate: VerifierCandidate | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    """仅采纳有效候选同轮审查通过的合同；原始 Intent 保持不变。"""
    contract = audit.get("response_contract")
    review = audit.get("semantic_review")
    if (
        candidate is None or audit.get("status") != "READY" or audit.get("errors")
        or not isinstance(contract, dict) or not isinstance(review, dict)
        or review.get("status") != "ACCEPT" or review.get("errors")
    ):
        return task
    result["response_contract"] = contract
    return {**task, "response_contract": contract}


def _is_non_file_task(source: dict[str, Any] | None, files: dict[str, str]) -> bool:
    return (
        source is not None
        and not files
        and source.get("selected_span_has_file_ops") is False
    )


def _record_unverified_obligations(
    result: dict[str, Any],
    *,
    task: dict[str, Any],
    audit: dict[str, Any] | None = None,
) -> bool:
    """后验文件语义与响应义务在真实产物审查前保持未验收。"""

    non_file = set(non_file_obligation_ids(task))
    recorded = list(result.get("unverified_obligations") or [])
    if audit is not None:
        declared = (audit.get("verifier") or {}).get("file_semantic_checks") or {}
        review = audit.get("semantic_review") or {}
        pending_file = (
            list(audit.get("pending_file_semantic_obligations") or [])
            if audit.get("status") == "READY" and review.get("status") == "ACCEPT"
            and not review.get("errors") and set(declared) == set(
                audit.get("pending_file_semantic_obligations") or []) else []
        )
        old_pending = set(result.get("pending_file_semantic_obligations") or [])
        recorded = [item for item in recorded if item not in old_pending]
        result["pending_file_semantic_obligations"] = pending_file
        result["file_semantic_verifier_ready_obligations"] = pending_file
    recorded.extend(sorted(non_file))
    raw = (audit or {}).get("unverified_obligations") or []
    if isinstance(raw, list):
        recorded.extend(str(item) for item in raw if item)
    elif raw:
        # A malformed non-empty audit must not silently erase the hard gate.
        recorded.append("INVALID_UNVERIFIED_OBLIGATIONS")
    unresolved = list(dict.fromkeys(recorded))
    result["unverified_obligations"] = unresolved
    result["pending_response_obligations"] = [
        item for item in unresolved if item in non_file
    ]
    # 非文件义务不等于格式义务。只有已绑定公开要求且语义审查通过的
    # 完整响应契约，才能标为“机制就绪、等待真实响应”，不能清除待验义务。
    contract = grounded_response_contract(task)
    declared = task.get("response_contract")
    contract_ids: set[str] = set()
    if contract and isinstance(declared, dict) and contract["checks"] == declared.get("checks"):
        contract_ids = {check["obligation_id"] for check in contract["checks"]}
    review = (audit or {}).get("semantic_review")
    reviewed_ids: set[str] = set()
    if isinstance(review, dict) and review.get("status") == "ACCEPT" and not review.get("errors"):
        rows = review.get("obligation_reviews")
        if isinstance(rows, list):
            reviewed_ids = {
                row["obligation_id"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("obligation_id"), str)
                and row.get("covered") is True
            }
    result["response_verifier_ready_obligations"] = sorted(non_file & contract_ids & reviewed_ids)
    pending_file = set(result.get("pending_file_semantic_obligations") or [])
    blocking = [item for item in unresolved if item not in non_file | pending_file]
    if not blocking:
        return False
    result["status"] = "REVIEW"
    result["sft_eligible"] = False
    result["errors"] = list(dict.fromkeys([*(result.get("errors") or []), "UNVERIFIED_OBLIGATIONS"]))
    return True


def run_reconstruction_verification(
    *,
    task: dict[str, Any],
    workspace_root: str | Path,
    model: ChatModel | None,
    output_root: str | Path,
    config: VerificationConfig,
    agent: Any | None = None,
    source: dict[str, Any] | None = None,
    env_root: str | Path | None = None,
    initial_feedback: dict[str, Any] | None = None,
    start_round: int = 1,
    reviewed_candidate: tuple[VerifierCandidate, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """在已绑定初态上校准并复验；支持带既有反馈从失败轮次继续。"""
    config.validate()
    if isinstance(start_round, bool) or not isinstance(start_round, int) or start_round < 1:
        raise ValueError("Verifier 起始轮次必须是正整数")
    root, workspace = Path(output_root), Path(workspace_root)
    task = verifier_task(task)
    files = _workspace_files(workspace)
    result: dict[str, Any] = {
        "schema_version": VERIFICATION_SCHEMA,
        "status": "REVIEW",
        "errors": [],
        "sft_eligible": False,
        "rollout": "NOT_RUN",
        "unverified_obligations": [],
    }
    if config.manual_response_review:
        result["manual_response_review"] = {
            "status": "NOT_ASSESSED", "obligation_ids": non_file_obligation_ids(task),
        }
    try:
        candidate = None
        audit: dict[str, Any] = {}
        if _is_non_file_task(source, files):
            result["errors"] = ["NON_FILE_TASK"]
            _write_verification(root, result, config)
            return result
        # 响应结构必须在真实 rollout 产生后验收；未支持的语义义务不能被跳过。
        if _record_unverified_obligations(
            result, task=task
        ):
            _write_verification(root, result, config)
            return result
        if agent is not None and config.should_run_red():
            from traceforge.reconstruction.verifier_recovery import (
                VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION, run_verifier_recovery,
                verifier_behavior, verifier_input_binding,
            )
            resume = None
            if reviewed_candidate is not None:
                saved, saved_audit = reviewed_candidate
                review = saved_audit.get("semantic_review") or {}
                matched = (
                    saved_audit.get("status") == "READY" and not saved_audit.get("errors")
                    and saved_audit.get("input_binding") == verifier_input_binding(task, workspace, source)
                    and (saved_audit.get("agent") or {}).get("completed") is True
                    and review.get("status") == "ACCEPT" and not review.get("errors")
                    and (review.get("agent") or {}).get("completed") is True
                    and review.get("prompt_version") == VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION
                    and review.get("candidate_id") == saved.candidate_id
                    and review.get("test_sha256") == hashlib.sha256(
                        saved.test_outputs_py.encode("utf-8")).hexdigest()
                    and json.dumps(saved_audit.get("verifier"), sort_keys=True)
                    == json.dumps(saved.to_dict(), sort_keys=True)
                )
                result["candidate_resume"] = {
                    "status": "RESTORED" if matched else "REGENERATE",
                    "candidate_id": saved.candidate_id,
                }
                if matched:
                    resume = (saved_audit, saved)
                    _write(root / "resumed-candidate.json", saved_audit)
            executor = HarborCalibrationExecutor(
                task=task,
                workspace=workspace,
                root=root,
                config=config,
                env_root=Path(env_root) if env_root is not None else None,
                agent=agent, source=source,
                baseline_observations=(initial_feedback or {}).get("baseline_observations"),
            )
            iterations: list[dict[str, Any]] = []
            feedback = dict(initial_feedback) if initial_feedback is not None else None
            baseline_observations = (initial_feedback or {}).get("baseline_observations")
            previous_generation_failure = None
            rounds = count(start_round) if config.max_rounds is None else range(
                start_round, start_round + config.max_rounds,
            )
            for round_number in rounds:
                if baseline_observations is not None:
                    feedback = {**(feedback or {}), "baseline_observations": baseline_observations}
                if resume is not None:
                    recovered, generated = resume
                    resume = None
                else:
                    recovered, generated = run_verifier_recovery(
                        task=task,
                        workspace_root=workspace,
                        agent=agent,
                        output_root=root / "agent" / f"round-{round_number:02d}",
                        source=source,
                        feedback=feedback,
                        round_number=round_number,
                        manual_response_review=config.manual_response_review,
                    )
                audit = recovered
                task = _adopt_reviewed_response_contract(task, recovered, generated, result)
                executor.task = task
                if _record_unverified_obligations(
                    result, task=task, audit=recovered
                ):
                    iterations.append({
                        "round": round_number,
                        "status": "REVIEW",
                        "errors": list(result["errors"]),
                        "unverified_obligations": list(result["unverified_obligations"]),
                    })
                    break
                if generated is None:
                    errors = list(recovered.get("errors") or ["VERIFIER_REVIEW"])
                    iterations.append({"round": round_number, "status": "REVIEW", "errors": errors})
                    prior_feedback = recovered.get("feedback")
                    feedback = {**(feedback or {}), **(prior_feedback if isinstance(prior_feedback, dict) else {})}
                    feedback["generation_errors"] = errors
                    # 无进展或未完成的调用不是语义返修，保留原因等待恢复。
                    review_errors = (recovered.get("semantic_review") or {}).get("errors") or []
                    if (set(errors) & {
                        "VERIFIER_NO_PROGRESS", "AGENT_INCOMPLETE",
                        "VERIFIER_REVIEW_INCOMPLETE", "VERIFIER_REVIEW_INVALID",
                    } or any(error != "VERIFIER_SEMANTIC_REPAIR_REQUIRED" for error in review_errors)):
                        result["errors"] = errors
                        break
                    if not recovered.get("semantic_review"):
                        payload = feedback.get("previous_candidate")
                        if not isinstance(payload, dict) or not payload:
                            result["errors"] = errors
                            break
                        failure = (verifier_behavior(payload), errors)
                        if failure == previous_generation_failure:
                            result["errors"] = [*errors, "VERIFIER_NO_PROGRESS"]
                            iterations[-1]["errors"] = result["errors"]
                            break
                        previous_generation_failure = failure
                    else:
                        previous_generation_failure = None
                    continue
                previous_generation_failure = None
                outcome = executor.run(generated)
                iterations.append(
                    {
                        "round": round_number,
                        "status": outcome.get("status", "INFRA_ERROR"),
                        "feedback": outcome.get("feedback", ""),
                        "candidate_id": generated.candidate_id,
                    }
                )
                status = str(outcome.get("status", "INFRA_ERROR"))
                if status == "PASS":
                    candidate = generated
                    break
                if status == "INFRA_ERROR":
                    # Infrastructure failures are retried with the same
                    # immutable candidate once; do not ask the model to alter
                    # a verifier to compensate for a broken Harbor run.
                    if not any(
                        item.get("candidate_id") == generated.candidate_id and item.get("retry")
                        for item in iterations
                    ):
                        retry = executor.run(generated)
                        iterations.append(
                            {
                                "round": round_number,
                                "status": retry.get("status", "INFRA_ERROR"),
                                "feedback": retry.get("feedback", ""),
                                "candidate_id": generated.candidate_id,
                                "retry": True,
                            }
                        )
                        if retry.get("status") == "PASS":
                            candidate = generated
                            break
                        if retry.get("status") == "FAIL":
                            try:
                                feedback = json.loads(str(retry.get("feedback", "{}")))
                            except json.JSONDecodeError:
                                feedback = {"calibration_feedback": str(retry.get("feedback", ""))}
                            feedback["previous_candidate"] = generated.to_dict()
                            continue
                    result["errors"] = ["VERIFIER_CALIBRATION_INFRA_ERROR"]
                    break
                try:
                    feedback = json.loads(str(outcome.get("feedback", "{}")))
                except json.JSONDecodeError:
                    feedback = {"calibration_feedback": str(outcome.get("feedback", ""))}
                feedback["previous_candidate"] = generated.to_dict()
            result["iterations"] = iterations
            result["calibration_runs"] = executor.attempts
            if candidate is not None and executor.bundle is not None:
                result.update(
                    status="READY",
                    bundle=str(executor.bundle),
                    verifier=candidate.to_dict(),
                    calibration="PASS",
                )
                if config.execute_rollout:
                    rollout = executor._run_bundle(
                        executor.bundle,
                        label="hermes-replay",
                        mode="hermes",
                        trials=config.rollout_trials,
                        expected_test_sha256=hashlib.sha256(candidate.test_outputs_py.encode("utf-8")).hexdigest(),
                    )
                    result["rollout"] = rollout
                    passed = rollout_passed(result.get("rollout"), config.rollout_trials)
                    apply_response_receipts(result, rollout, task, config.rollout_trials)
                    apply_file_semantic_receipts(result, rollout, {
                        **task, "file_semantic_checks": candidate.file_semantic_checks,
                    }, config.rollout_trials)
                    if rollout.get("harbor_bundle"):
                        result["harbor_bundle"] = rollout["harbor_bundle"]
                        result["rollout_plan"] = rollout.get("plan")
                    _set_rollout_eligibility(result, config.rollout_trials)
                    if not passed:
                        result["errors"] = list(
                            dict.fromkeys(
                                [*(result.get("errors") or []), "HERMES_REPRODUCIBILITY_FAILED"]
                            )
                        )
                else:
                    try:
                        result.update(
                            executor.publish_calibrated_bundle(
                                label="hermes-replay", trials=config.rollout_trials
                            )
                        )
                    except (HarborRolloutError, OSError, ValueError) as exc:
                        result["status"] = "REVIEW"
                        result["errors"] = [
                            "HARBOR_BUNDLE_PUBLICATION_FAILED",
                            f"{type(exc).__name__}:{exc}",
                        ]
            elif not result.get("errors"):
                result["errors"] = ["VERIFIER_CALIBRATION_FAILED"]
            _write(root / "model_exchange.json", {"iterations": result.get("iterations")})
            _write_verification(root, result, config)
            return result
        if agent is not None:
            from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

            recovered, candidate = run_verifier_recovery(
                task=task,
                workspace_root=workspace,
                agent=agent,
                output_root=root / "agent" / "round-01",
                source=source,
                feedback=initial_feedback,
                manual_response_review=config.manual_response_review,
            )
            audit = recovered
            task = _adopt_reviewed_response_contract(task, recovered, candidate, result)
            if candidate is None:
                result["errors"] = list(recovered.get("errors") or ["VERIFIER_REVIEW"])
        elif model is not None and not config.should_run_red():
            candidate, audit = synthesize_verifier(
                task=task, workspace_files=files, model=model, model_name=config.model_name
            )
            if candidate is None:
                result["errors"] = list(audit.get("open_questions") or ["VERIFIER_REVIEW"])
        elif model is not None:
            executor = HarborCalibrationExecutor(
                task=task, workspace=workspace, root=root, config=config,
                env_root=Path(env_root) if env_root is not None else None,
                agent=agent, source=source,
                baseline_observations=(initial_feedback or {}).get("baseline_observations"),
            )
            iteration = synthesize_verifier_iterative(
                task=task,
                workspace_files=files,
                model=model,
                executor=executor,
                model_name=config.model_name,
                max_rounds=config.max_rounds,
            )
            result["iterations"] = list(iteration.attempts)
            result["calibration_runs"] = executor.attempts
            if iteration.status != "READY" or iteration.candidate is None or executor.bundle is None:
                result["errors"] = list(iteration.open_questions) or ["VERIFIER_CALIBRATION_FAILED"]
            else:
                candidate = iteration.candidate
                result.update(
                    status="READY",
                    bundle=str(executor.bundle),
                    verifier=candidate.to_dict(),
                    calibration="PASS",
                )
                if config.execute_rollout:
                    rollout = executor._run_bundle(
                        executor.bundle,
                        label="hermes-replay",
                        mode="hermes",
                        trials=config.rollout_trials,
                        expected_test_sha256=hashlib.sha256(candidate.test_outputs_py.encode("utf-8")).hexdigest(),
                    )
                    result["rollout"] = rollout
                    passed = rollout_passed(result.get("rollout"), config.rollout_trials)
                    apply_response_receipts(result, rollout, task, config.rollout_trials)
                    apply_file_semantic_receipts(result, rollout, {
                        **task, "file_semantic_checks": candidate.file_semantic_checks,
                    }, config.rollout_trials)
                    if rollout.get("harbor_bundle"):
                        result["harbor_bundle"] = rollout["harbor_bundle"]
                        result["rollout_plan"] = rollout.get("plan")
                    _set_rollout_eligibility(result, config.rollout_trials)
                    if not passed:
                        result["errors"] = list(
                            dict.fromkeys(
                                [*(result.get("errors") or []), "HERMES_REPRODUCIBILITY_FAILED"]
                            )
                        )
                else:
                    try:
                        result.update(
                            executor.publish_calibrated_bundle(
                                label="hermes-replay", trials=config.rollout_trials
                            )
                        )
                    except (HarborRolloutError, OSError, ValueError) as exc:
                        result["status"] = "REVIEW"
                        result["errors"] = [
                            "HARBOR_BUNDLE_PUBLICATION_FAILED",
                            f"{type(exc).__name__}:{exc}",
                        ]
            _write(root / "model_exchange.json", {"iterations": result.get("iterations")})
            _write_verification(root, result, config)
            return result
        else:
            result["errors"] = ["VERIFIER_SOURCE_MISSING"]
        if candidate is not None and candidate.file_semantic_checks and agent is None:
            result["unverified_obligations"].extend(candidate.file_semantic_checks)
            result["errors"].append("FILE_SEMANTIC_REVIEW_AGENT_MISSING")
            candidate = None
        if _record_unverified_obligations(
            result, task=task, audit=audit
        ):
            candidate = None
        if candidate is not None and result["status"] != "READY":
            bundle = compile_bundle(
                task=task,
                workspace_root=workspace,
                verifier=candidate,
                output_root=root / "bundles",
                env_root=Path(env_root) if env_root is not None else None,
            )
            result.update(
                status="PENDING_EXECUTION" if not config.should_run_red() else "REVIEW",
                bundle=str(bundle / "task"),
                verifier=candidate.to_dict(),
                calibration="NOT_RUN",
            )
            if config.should_run_red():
                executor = HarborCalibrationExecutor(
                    task=task, workspace=workspace, root=root, config=config,
                    env_root=Path(env_root) if env_root is not None else None,
                    agent=agent, source=source,
                    baseline_observations=(initial_feedback or {}).get("baseline_observations"),
                )
                outcome = executor.run(candidate)
                result["calibration_runs"] = executor.attempts
                if outcome.get("status") == "PASS" and executor.bundle is not None:
                    result.update(status="READY", bundle=str(executor.bundle), calibration="PASS")
                    if config.execute_rollout:
                        rollout = executor._run_bundle(
                            executor.bundle,
                            label="hermes-replay",
                            mode="hermes",
                            trials=config.rollout_trials,
                            expected_test_sha256=hashlib.sha256(candidate.test_outputs_py.encode("utf-8")).hexdigest(),
                        )
                        result["rollout"] = rollout
                        passed = rollout_passed(result.get("rollout"), config.rollout_trials)
                        apply_response_receipts(result, rollout, task, config.rollout_trials)
                        apply_file_semantic_receipts(result, rollout, {
                            **task, "file_semantic_checks": candidate.file_semantic_checks,
                        }, config.rollout_trials)
                        _set_rollout_eligibility(result, config.rollout_trials)
                        if not passed:
                            result["errors"] = list(
                                dict.fromkeys(
                                    [*(result.get("errors") or []), "HERMES_REPRODUCIBILITY_FAILED"]
                                )
                            )
                    else:
                        try:
                            result.update(
                                executor.publish_calibrated_bundle(
                                    label="hermes-replay", trials=config.rollout_trials
                                )
                            )
                        except (HarborRolloutError, OSError, ValueError) as exc:
                            result["status"] = "REVIEW"
                            result["errors"] = [
                                "HARBOR_BUNDLE_PUBLICATION_FAILED",
                                f"{type(exc).__name__}:{exc}",
                            ]
                else:
                    result["errors"] = ["VERIFIER_CALIBRATION_FAILED"]
                    result["status"] = "REVIEW"
        if audit:
            _write(root / "model_exchange.json", audit)
    except (OSError, ValueError, RuntimeError, ModelGatewayError, VerifierSynthesisError) as exc:
        result.update(
            status="REVIEW",
            sft_eligible=False,
            errors=[getattr(exc, "code", None) or type(exc).__name__, str(exc)],
        )
    _write_verification(root, result, config)
    return result
