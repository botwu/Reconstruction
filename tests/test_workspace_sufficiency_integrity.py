"""Sufficiency 的静态诊断必须可解释，不能把重建损坏误当成充分环境。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.workspace_integrity import inspect_workspace_integrity
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency


class _JudgmentRuntime:
    """固定模型判断，只验证诊断传递与门禁，不模拟真实执行。"""

    backend = "hermes-sandbox"
    model_name = "fixture"

    def __init__(
        self,
        classify: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.classify = classify
        self.report: dict[str, Any] = {}

    def run(self, *, role: Any, instruction: str, session: Any, output_root: Path) -> AgentResult:
        self.report = json.loads(instruction.split("STATIC_INTEGRITY_REPORT:\n", 1)[1])
        session.sandbox_started = True
        session.sandbox_stopped = True
        session.read_only_probe_blocked = True
        session.tool_events.append({"name": "read_file", "ok": True})
        payload: dict[str, Any] = {
            "label": "SUFFICIENT",
            "decision": "READY",
            "reason": "已检查任务上下文。",
            "missing_context": [],
            "confidence": 0.9,
        }
        if self.classify is not None:
            payload["integrity_classifications"] = self.classify(self.report)
        return AgentResult(
            role=role.name, backend=self.backend, payload=payload,
            completed=True, final_text=json.dumps(payload, ensure_ascii=False),
        )


def _task(path: str = "billing.py", instruction: str = "为账单函数添加折扣能力") -> dict[str, Any]:
    return {
        "task_instruction": instruction,
        "core_objective": instruction,
        "environment_bindings": [{
            "obligation_id": "billing", "verifier_kind": "FILE",
            "required_paths": [path], "observable": instruction,
        }],
    }


def _write(workspace: Path, relative: str, source: str) -> None:
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _judge(
    tmp_path: Path,
    *,
    task: dict[str, Any] | None = None,
    runtime: _JudgmentRuntime | None = None,
) -> dict[str, Any]:
    return run_workspace_sufficiency(
        task=task or _task(), workspace_root=tmp_path / "workspace",
        agent=runtime or _JudgmentRuntime(), output_root=tmp_path / "judge",
    )


def _classify(report: dict[str, Any], kind: str, reason: str) -> list[dict[str, Any]]:
    return [
        {"issue_id": issue["id"], "path": issue["path"], "classification": kind, "reason": reason}
        for issue in report["issues"]
    ]


def test_valid_bound_code_is_read_only_and_does_not_require_external_imports(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    side_effect = tmp_path / "must_not_exist"
    source = f"import unavailable_external_dependency\nopen({str(side_effect)!r}, 'w')\n"
    _write(workspace, "billing.py", source)
    result = _judge(tmp_path)
    assert result["status"] == "READY"
    assert result["integrity_report"]["issues"] == []
    assert result["integrity_report"]["python_version"]
    assert result["integrity_report"]["limitations"]
    assert result["execution_preflight"]["status"] == "REVIEW"
    assert "ENVIRONMENT_PROBES_REQUIRED" in result["execution_preflight"]["errors"]
    assert not side_effect.exists()
    assert (workspace / "billing.py").read_text(encoding="utf-8") == source


def test_unrelated_damaged_source_is_outside_the_binding_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "def bill(value): return value\n")
    _write(workspace, "unrelated.py", "def unavailable(\n")
    result = _judge(tmp_path)
    assert result["status"] == "READY"
    assert result["integrity_report"]["scope_paths"] == ["billing.py"]


@pytest.mark.parametrize("import_line", ["from pkg.helper import bill", "from .helper import bill"])
def test_truncated_local_dependency_blocks_unclassified_ready(
    tmp_path: Path, import_line: str,
) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "src/pkg/billing.py", import_line + "\n")
    _write(workspace, "src/pkg/helper.py", "def bill(value):\n")
    runtime = _JudgmentRuntime()
    result = _judge(tmp_path, task=_task("src/pkg/billing.py"), runtime=runtime)
    assert result["status"] == "REVIEW"
    issue = result["integrity_report"]["issues"][0]
    assert issue["code"] == "PYTHON_SYNTAX_ERROR"
    assert issue["path"] == "src/pkg/helper.py"
    assert issue["line"] == 1
    assert issue["reason"]
    assert "INTEGRITY_ISSUE_UNCLASSIFIED:" + issue["id"] in result["errors"]
    assert runtime.report["issues"][0]["path"] == "src/pkg/helper.py"


def test_reconstruction_gap_overrides_optimistic_model_decision(tmp_path: Path) -> None:
    _write(tmp_path / "workspace", "billing.py", "def bill(value):\n")
    runtime = _JudgmentRuntime(
        lambda report: _classify(
            report, "RECONSTRUCTION_GAP", "任务要添加折扣，但计费函数原有正文缺失，无法恢复计费规则。",
        )
    )
    result = _judge(tmp_path, runtime=runtime)
    assert result["status"] == "REVIEW"
    assert result["label"] == "INSUFFICIENT"
    issue = result["integrity_report"]["issues"][0]
    assert issue["classification"] == "RECONSTRUCTION_GAP"
    assert "WORKSPACE_RECONSTRUCTION_GAP:" + issue["id"] in result["errors"]


def test_task_defect_is_preserved_when_model_explains_the_task_relationship(tmp_path: Path) -> None:
    source = "def bill(value)\n    return value\n"
    _write(tmp_path / "workspace", "billing.py", source)
    runtime = _JudgmentRuntime(
        lambda report: _classify(
            report, "BASELINE_TASK_DEFECT", "用户要求给第一行补冒号；现有函数体完整，缺冒号即待修缺陷。",
        )
    )
    result = _judge(
        tmp_path, task=_task(instruction="给账单函数的第一行补上冒号"), runtime=runtime,
    )
    assert result["status"] == "READY"
    assert result["integrity_report"]["status"] == "CLASSIFIED"
    assert (tmp_path / "workspace/billing.py").read_text(encoding="utf-8") == source


def test_analytical_task_can_explain_why_trailing_damage_is_irrelevant(tmp_path: Path) -> None:
    _write(tmp_path / "workspace", "billing.py", "RATE = 0.9\ndef unrelated(\n")
    runtime = _JudgmentRuntime(
        lambda report: _classify(
            report, "IRRELEVANT", "任务仅解释首行的折扣比例，无需导入模块；尾部函数不影响该表达式。",
        )
    )
    result = _judge(tmp_path, task=_task(instruction="解释账单文件首行的折扣比例"), runtime=runtime)
    assert result["status"] == "READY"


@pytest.mark.parametrize(
    "source",
    [
        "# [PII_EN_PERSON_NAME_abc_LEN3]\nvalue = 1\n",
        "value = '[PII_EN_PERSON_NAME_abc_LEN3]'\n",
        'value = """说明\n[PII_EN_PERSON_NAME_abc_LEN3]\n"""\n',
    ],
)
def test_redaction_in_comments_and_strings_is_not_executable_damage(
    tmp_path: Path, source: str,
) -> None:
    _write(tmp_path / "workspace", "billing.py", source)
    result = _judge(tmp_path)
    assert result["status"] == "READY"
    assert result["integrity_report"]["issues"] == []


def test_redaction_in_executable_tokens_requires_classification(tmp_path: Path) -> None:
    _write(tmp_path / "workspace", "billing.py", "name = [PII_EN_PERSON_NAME_abc_LEN3].name\n")
    result = _judge(tmp_path)
    assert result["status"] == "REVIEW"
    assert [issue["code"] for issue in result["integrity_report"]["issues"]] == [
        "EXECUTABLE_REDACTION_PLACEHOLDER",
    ]


@pytest.mark.parametrize("invalid", ["path", "reason", "classification", "duplicate", "unknown"])
def test_invalid_issue_classification_cannot_yield_ready(tmp_path: Path, invalid: str) -> None:
    _write(tmp_path / "workspace", "billing.py", "def bill(value):\n")

    def classify(report: dict[str, Any]) -> list[dict[str, Any]]:
        items = _classify(report, "BASELINE_TASK_DEFECT", "此缺陷是任务要求修复的对象。")
        if invalid == "duplicate":
            items.append(dict(items[0]))
        elif invalid == "unknown":
            items[0]["issue_id"] = "unknown"
        else:
            items[0][invalid] = "" if invalid == "reason" else "invalid"
        return items

    result = _judge(tmp_path, runtime=_JudgmentRuntime(classify))
    assert result["status"] == "REVIEW"
    assert result["errors"]
    assert result["integrity_report"]["status"] == "REVIEW"


def test_python_compile_checks_errors_beyond_ast_parsing(tmp_path: Path) -> None:
    _write(tmp_path / "workspace", "billing.py", "return 1\n")
    report = inspect_workspace_integrity(tmp_path / "workspace", _task())
    assert report["issues"][0]["code"] == "PYTHON_SYNTAX_ERROR"


def test_package_initializers_are_part_of_local_import_closure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "from pkg.helper import bill\n")
    _write(workspace, "pkg/__init__.py", "def initializer(\n")
    _write(workspace, "pkg/helper.py", "def bill(value): return value\n")
    result = _judge(tmp_path)
    assert result["status"] == "REVIEW"
    assert result["integrity_report"]["issues"][0]["path"] == "pkg/__init__.py"


def test_observed_source_is_checked_beyond_intent_bindings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "RATE = 0.9\n")
    _write(workspace, "runner.py", "def run():\n")
    result = run_workspace_sufficiency(
        task=_task(), workspace_root=workspace, agent=_JudgmentRuntime(),
        output_root=tmp_path / "judge", observed_paths=["runner.py"],
    )
    assert result["status"] == "REVIEW"
    assert result["integrity_report"]["observed_paths"] == ["runner.py"]
    assert result["integrity_report"]["issues"][0]["path"] == "runner.py"
    assert "INTEGRITY_ISSUE_UNCLASSIFIED:integrity-001" in result["errors"]


def test_missing_observed_source_requires_explicit_classification(tmp_path: Path) -> None:
    _write(tmp_path / "workspace", "billing.py", "RATE = 0.9\n")
    result = run_workspace_sufficiency(
        task=_task(), workspace_root=tmp_path / "workspace", agent=_JudgmentRuntime(),
        output_root=tmp_path / "judge", observed_paths=["runner.py", "runner.py"],
    )
    assert result["status"] == "REVIEW"
    issues = result["integrity_report"]["issues"]
    assert len(issues) == 1
    assert issues[0]["code"] == "OBSERVED_PYTHON_SOURCE_MISSING"


def test_valid_observed_source_and_its_imports_are_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "RATE = 0.9\n")
    _write(workspace, "runner.py", "from helper import run\n")
    _write(workspace, "helper.py", "def run(): return 1\n")
    result = run_workspace_sufficiency(
        task=_task(), workspace_root=workspace, agent=_JudgmentRuntime(),
        output_root=tmp_path / "judge", observed_paths=["runner.py"],
    )
    assert result["status"] == "READY"
    assert result["integrity_report"]["scope_paths"] == ["billing.py", "helper.py", "runner.py"]


def test_observed_paths_cannot_read_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "RATE = 0.9\n")
    _write(tmp_path, "outside.py", "def truncated(\n")
    (workspace / "external.py").symlink_to(tmp_path / "outside.py")
    report = inspect_workspace_integrity(
        workspace, _task(), observed_paths=["../outside.py", "external.py"],
    )
    assert report["scope_paths"] == ["billing.py"]
    assert report["observed_paths"] == []
    assert report["issues"] == []


def test_mjcf_include_requires_local_asset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "def bill(value): return value\n")
    _write(workspace, "asset/scene.xml", '<mujoco><include file="robot.xml"/></mujoco>\n')
    runtime = _JudgmentRuntime(
        lambda report: _classify(report, "RECONSTRUCTION_GAP", "任务运行依赖的 MJCF include 文件缺失。")
    )
    result = _judge(tmp_path, runtime=runtime)
    assert result["status"] == "REVIEW"
    issue = result["integrity_report"]["issues"][0]
    assert issue["code"] == "REFERENCED_ASSET_MISSING"
    assert issue["reference"] == "robot.xml"


def test_mjcf_include_with_local_asset_is_not_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace, "billing.py", "def bill(value): return value\n")
    _write(workspace, "asset/scene.xml", '<mujoco><include file="robot.xml"/></mujoco>\n')
    _write(workspace, "asset/robot.xml", "<body/>\n")
    result = _judge(tmp_path)
    assert result["status"] == "READY"
    assert result["integrity_report"]["issues"] == []
