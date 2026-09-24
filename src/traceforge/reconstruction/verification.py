"""把新重建主链接到隐藏 Verifier、Harbor 校准和 Hermes 复验。

模型代码只由 Harbor/AGS 执行。未执行、执行不完整或校准失败都不能返回 READY。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.response_receipt import (
    build_response_receipt_from_path,
)
from traceforge.harbor_ags.results import (
    HarborResultError,
    certify_hermes_job,
    read_rollout_results,
)
from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
    publish_rollout_bundle,
    redact_harbor_output,
)
from traceforge.reconstruction.model_gateway import ChatModel, ModelGatewayError
from traceforge.reconstruction.run_config import load_rollout_limits
from traceforge.task_instruction import acceptance_report_criterion_ids
from traceforge.reconstruction.environment_bindings import non_file_obligation_ids
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
    rollout_trials: int = 2
    max_rounds: int = 2
    config_path: Path | None = None
    channel: str = "claude"
    timeout_seconds: int = 900
    rollout_max_iterations: int = 60

    def validate(self) -> None:
        if self.execute_rollout and self.rollout_trials < 2:
            raise ValueError("真实复验要求至少两次 Hermes rollout")
        if self.execute_rollout and not self.should_run_red():
            raise ValueError("真实 rollout 要求同时执行 Harbor RED；请设置 --execute-red")
        if (
            isinstance(self.max_rounds, bool)
            or not isinstance(self.max_rounds, int)
            or self.max_rounds < 1
        ):
            raise ValueError("Verifier 迭代次数必须是正整数")
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
    candidate: VerifierCandidate, verdicts: list[dict[str, Any]]
) -> dict[str, Any]:
    """初始环境必须有缺失能力测试失败，且每个保护性测试都通过。"""
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
        if not any("FAIL" in statuses for statuses in missing):
            errors.append(f"MISSING_CAPABILITY_ALREADY_PASSES:{index}")
        if any(
            not statuses or any(status != "PASS" for status in statuses) for statuses in protective
        ):
            errors.append(f"PROTECTIVE_TEST_FAILED:{index}")
    return {"passed": not errors, "errors": errors, "verdict_count": len(verdicts)}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rollout_passed(result: dict[str, Any], expected_trials: int) -> bool:
    """只把完整且通过质量门的多次 Hermes 复验视为成功。"""
    rollout = result.get("rollout")
    if not isinstance(rollout, dict):
        return False
    execution = rollout.get("execution")
    if not isinstance(execution, dict) or execution.get("status") != "COMPLETED":
        return False
    results = rollout.get("results")
    gate = results.get("quality_gate") if isinstance(results, dict) else None
    if not isinstance(gate, dict) or gate.get("ok") is not True:
        return False
    trials = results.get("trials")
    return (
        isinstance(trials, list)
        and len(trials) == expected_trials
        and all(
            isinstance(trial, dict)
            and trial.get("status") == "PASS"
            and not isinstance(trial.get("reward"), bool)
            and trial.get("reward") == 1.0
            for trial in trials
        )
    )


def _set_rollout_eligibility(result: dict[str, Any], expected_trials: int) -> bool:
    """集中维护 rollout、NON_FILE 义务和 SFT 资格的关系。"""
    passed = _rollout_passed(result, expected_trials)
    unresolved = result.get("unverified_obligations") or []
    eligible = passed and not unresolved and not result.get("errors")
    result["sft_eligible"] = eligible
    if not eligible:
        result["status"] = "REVIEW"
    if passed and unresolved:
        result["errors"] = list(
            dict.fromkeys([*(result.get("errors") or []), "SFT_UNVERIFIED_OBLIGATIONS"])
        )
    return passed


def _certification_complete(result: dict[str, Any], expected_trials: int) -> bool:
    """全部 RED、复验和义务覆盖完成后才关闭认证。"""
    return (
        result.get("status") == "READY"
        and result.get("calibration") == "PASS"
        and _rollout_passed(result, expected_trials)
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



def _acceptance_report_obligation_ids(task: dict[str, Any]) -> list[str]:
    """只选择义务正文明确要求 acceptance-report 的非文件义务。"""

    non_file = set(non_file_obligation_ids(task))
    ids: list[str] = []
    for obligation in task.get("acceptance_obligations") or []:
        if not isinstance(obligation, dict):
            continue
        obligation_id = obligation.get("id")
        if not isinstance(obligation_id, str) or obligation_id not in non_file:
            continue
        text = str(obligation.get("text") or "").lower()
        if "acceptance-report" in text or "acceptance report" in text:
            ids.append(obligation_id)
    return ids


def _attach_response_receipts(
    rollout: dict[str, Any], expected_trials: int, task: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate terminal acceptance reports and store hash receipts only."""

    results = rollout.get("results")
    trials = results.get("trials") if isinstance(results, dict) else None
    if not isinstance(trials, list):
        return ["RESPONSE_RECEIPT_TRIALS_MISSING"], []
    if expected_trials < 1 or len(trials) != expected_trials:
        return [
            f"RESPONSE_RECEIPT_TRIAL_COUNT_MISMATCH:{len(trials)}:{expected_trials}"
        ], []
    errors: list[str] = []
    receipts: list[dict[str, Any]] = []
    for index, trial in enumerate(trials):
        # Receipts are evidence for successful completed responses. Failed,
        # timed out, or infrastructure-error trials retain their own diagnosis.
        if not isinstance(trial, dict):
            errors.append(f"RESPONSE_RECEIPT_TRIAL_INVALID:{index}")
            continue
        trial_status = trial.get("status")
        if trial_status != "PASS":
            trial["response_receipt_status"] = "SKIPPED"
            trial["response_receipt_skip_reason"] = (
                f"TRIAL_STATUS_{trial_status or 'UNKNOWN'}"
            )
            continue
        path = trial.get("trajectory_path")
        if not isinstance(path, str) or not path:
            errors.append(f"RESPONSE_RECEIPT_TRAJECTORY_MISSING:{index}")
            continue
        try:
            receipt = build_response_receipt_from_path(
                path, expected_criterion_ids=acceptance_report_criterion_ids(task)
            )
        except (ValueError, OSError) as exc:
            errors.append(f"RESPONSE_RECEIPT_INVALID:{index}:{exc}")
            continue
        summary = {
            key: receipt[key]
            for key in (
                "schema_version",
                "trajectory_sha256",
                "assistant_message_index",
                "response_sha256",
                "acceptance_report_sha256",
            )
        }
        trial["response_receipt"] = summary
        trial["response_receipt_status"] = "VERIFIED"
        receipts.append(summary)
    return errors, receipts


def _apply_response_receipts(
    result: dict[str, Any], rollout: dict[str, Any], task: dict[str, Any], expected_trials: int
) -> None:
    """Apply receipts without clearing unrelated NON_FILE obligations."""

    obligation_ids = _acceptance_report_obligation_ids(task)
    if not obligation_ids:
        return
    errors, receipts = _attach_response_receipts(rollout, expected_trials, task)
    result["response_receipts"] = receipts
    raw_results = rollout.get("results")
    trials = raw_results.get("trials") if isinstance(raw_results, dict) else None
    skipped = [
        {
            "index": index,
            "status": trial.get("status"),
            "reason": trial.get("response_receipt_skip_reason"),
        }
        for index, trial in enumerate(trials if isinstance(trials, list) else [])
        if isinstance(trial, dict) and trial.get("response_receipt_status") == "SKIPPED"
    ]
    if skipped:
        result["response_receipt_skipped"] = skipped
    if errors:
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        result["errors"] = list(
            dict.fromkeys(
                [*(result.get("errors") or []), *errors, "NON_FILE_RESPONSE_UNVERIFIED"]
            )
        )
        return
    # Do not clear the obligation until every trial has a successful receipt.
    if skipped or len(receipts) != expected_trials:
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        return
    result["unverified_obligations"] = [
        item
        for item in result.get("unverified_obligations") or []
        if item not in set(obligation_ids)
    ]


class HarborCalibrationExecutor:
    """每个候选都在独立 Harbor job 中校准，不执行宿主机 shell。"""

    def __init__(
        self, *, task: dict[str, Any], workspace: Path, root: Path, config: VerificationConfig,
        env_root: Path | None = None,
    ):
        self.task, self.workspace, self.root, self.config = task, workspace, root, config
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
        return record

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
                diagnostic["oracle_log_tail"] = redact_harbor_output(text[-2000:])
        return diagnostic

    @classmethod
    def _failure_diagnostics(cls, run: dict[str, Any]) -> dict[str, Any]:
        """为下一轮 Verifier 提供最小的失败证据，而不是只给 job 标签。"""

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
                            test_row["message"] = redact_harbor_output(item["message"][:2000])
                        row["tests"].append(test_row)
                row["exit_code"] = verdict.get("exit_code") if isinstance(verdict, dict) else None
            rows.append(row)
        if rows:
            diagnostics["trials"] = rows
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
        number = len(self.attempts) + 1
        prefix = f"round-{number:02d}"
        runs: dict[str, Any] = {}
        cases: list[RedCheckCase] = []
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
        initial = initial_red_check(candidate, verdicts)
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
        }


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
    """保留待验义务；非文件义务允许诊断执行，但阻止最终认证。

    已请求的 rollout 为响应校验提供真实证据。FILE 校准覆盖缺失仍须前置拒绝，
    不得以部分文件通过或合法 JSON 代替完整任务验收。
    """

    non_file = set(non_file_obligation_ids(task))
    recorded = list(result.get("unverified_obligations") or [])
    recorded.extend(sorted(non_file))
    raw = (audit or {}).get("unverified_obligations") or []
    if isinstance(raw, list):
        recorded.extend(str(item) for item in raw if item)
    elif raw:
        # A malformed non-empty audit must not silently erase the hard gate.
        recorded.append("INVALID_UNVERIFIED_OBLIGATIONS")
    unresolved = list(dict.fromkeys(recorded))
    result["unverified_obligations"] = unresolved
    blocking = [item for item in unresolved if item not in non_file]
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
) -> dict[str, Any]:
    """RED 记录校准通过；请求 rollout 后仅完整验收可 READY，否则 REVIEW。"""
    config.validate()
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
    try:
        candidate = None
        audit: dict[str, Any] = {}
        if _is_non_file_task(source, files):
            result["errors"] = ["NON_FILE_TASK"]
            _write_verification(root, result, config)
            return result
        # 非文件义务留给真实响应校验；不支持的义务始终保留为未验收。
        if _record_unverified_obligations(
            result, task=task
        ):
            _write_verification(root, result, config)
            return result
        if agent is not None and config.should_run_red():
            from traceforge.reconstruction.verifier_recovery import run_verifier_recovery
            executor = HarborCalibrationExecutor(
                task=task,
                workspace=workspace,
                root=root,
                config=config,
                env_root=Path(env_root) if env_root is not None else None,
            )
            iterations: list[dict[str, Any]] = []
            feedback: dict[str, Any] | None = None
            for round_number in range(1, config.max_rounds + 1):
                recovered, generated = run_verifier_recovery(
                    task=task,
                    workspace_root=workspace,
                    agent=agent,
                    output_root=root / "agent" / f"round-{round_number:02d}",
                    source=source,
                    feedback=feedback,
                    round_number=round_number,
                )
                audit = recovered
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
                    feedback = dict(prior_feedback) if isinstance(prior_feedback, dict) else {}
                    feedback["generation_errors"] = errors
                    continue
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
                    passed = _rollout_passed(result, config.rollout_trials)
                    _apply_response_receipts(result, rollout, task, config.rollout_trials)
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
            )
            audit = recovered
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
                    passed = _rollout_passed(result, config.rollout_trials)
                    _apply_response_receipts(result, rollout, task, config.rollout_trials)
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
                        passed = _rollout_passed(result, config.rollout_trials)
                        _apply_response_receipts(result, rollout, task, config.rollout_trials)
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
