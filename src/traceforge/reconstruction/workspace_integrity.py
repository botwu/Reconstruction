"""只读检查任务绑定的 Python 源码，诊断不等同于任务充分性结论。"""

from __future__ import annotations

import ast
import copy
import io
import re
import sys
import tokenize
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from traceforge.reconstruction.environment_bindings import file_required_paths

INTEGRITY_SCHEMA = "traceforge.workspace-integrity.v1"
_CLASSIFICATIONS = frozenset(
    {"BASELINE_TASK_DEFECT", "RECONSTRUCTION_GAP", "IRRELEVANT"}
)
_REDACTION_NAME = re.compile(r"PII_[A-Za-z0-9_]+\Z")

_INCLUDE_RE = re.compile(r'<include\b[^>]*\bfile\s*=\s*["\']([^"\']+)["\']', re.I)


def _referenced_asset_issues(workspace: Path) -> list[dict[str, Any]]:
    """检查已观测环境中的 MJCF/XML include 是否有本地资产。"""
    issues: list[dict[str, Any]] = []
    for source in sorted(workspace.rglob("*.xml")):
        if not _inside_file(source, workspace):
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for match in _INCLUDE_RE.finditer(text):
            reference = match.group(1).strip()
            if not reference or reference.startswith(("/", "$")):
                continue
            target = (source.parent / reference).resolve()
            if target.is_file() and target.is_relative_to(workspace):
                continue
            line = text.count("\n", 0, match.start()) + 1
            issues.append({
                "code": "REFERENCED_ASSET_MISSING",
                "path": source.relative_to(workspace).as_posix(),
                "line": line,
                "column": match.start() - text.rfind("\n", 0, match.start()),
                "reason": f"XML include 引用了缺失的本地资产：{reference}",
                "reference": reference,
            })
    return issues



def _inside_file(path: Path, workspace: Path) -> bool:
    return path.is_file() and path.resolve().is_relative_to(workspace)


def _bound_python_paths(workspace: Path, task: dict[str, Any]) -> set[Path]:
    paths: set[Path] = set()
    for relative in file_required_paths(task):
        bound = workspace / relative
        if not bound.resolve().is_relative_to(workspace):
            continue
        candidates = bound.rglob("*.py") if bound.is_dir() else (bound,)
        paths.update(
            path for path in candidates
            if path.suffix == ".py" and _inside_file(path, workspace)
        )
    return paths


def _module_paths(base: Path, module: str, names: tuple[str, ...]) -> list[Path] | None:
    target = base.joinpath(*module.split(".")) if module else base
    source = target.with_suffix(".py")
    if module and source.is_file():
        paths = [source]
    elif target.is_dir():
        paths = [target / "__init__.py"] if (target / "__init__.py").is_file() else []
        for name in names:
            if name == "*":
                continue
            child = target / name
            if child.with_suffix(".py").is_file():
                paths.append(child.with_suffix(".py"))
            elif (child / "__init__.py").is_file():
                paths.append(child / "__init__.py")
    else:
        return None
    parent = target.parent
    while parent != base and parent.is_relative_to(base):
        initializer = parent / "__init__.py"
        if initializer.is_file():
            paths.append(initializer)
        parent = parent.parent
    return paths


def _local_imports(tree: ast.AST, source: Path, workspace: Path) -> set[Path]:
    """只跟随能从导入文件祖先目录确定定位的本地模块，不解析 sys.path。"""

    found: set[Path] = set()
    roots = [parent for parent in source.parents if parent.is_relative_to(workspace)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            requests = [(alias.name, (), 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            requests = [(node.module or "", tuple(alias.name for alias in node.names), node.level)]
        else:
            continue
        for module, names, level in requests:
            if level:
                base = source.parent
                for _ in range(level - 1):
                    base = base.parent
                candidates = [base] if base.is_relative_to(workspace) else []
            else:
                candidates = roots
            for base in candidates:
                paths = _module_paths(base, module, names)
                if paths is not None:
                    found.update(path for path in paths if _inside_file(path, workspace))
                    break
    return found


def _redaction_positions(source: str) -> list[tuple[int, int]]:
    """仅识别可执行 token 中的脱敏占位符，字符串和注释不产生此类诊断。"""

    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            tokens.append(token)
    except (tokenize.TokenError, IndentationError):
        # 不完整文件仍可能暴露占位符；语法错误由独立的编译检查报告。
        pass
    return [
        token.start
        for previous, token, following in zip(tokens, tokens[1:], tokens[2:], strict=False)
        if token.type == tokenize.NAME
        and _REDACTION_NAME.fullmatch(token.string)
        and previous.string == "["
        and following.string == "]"
    ]


def inspect_workspace_integrity(
    workspace: Path, task: dict[str, Any], *, observed_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """静态诊断绑定、原轨迹观测源码及本地导入闭包；不执行项目代码。"""

    workspace = workspace.resolve()
    pending = _bound_python_paths(workspace, task)
    visited: set[Path] = set()
    issues: list[dict[str, Any]] = _referenced_asset_issues(workspace)
    observed: set[str] = set()
    for relative in observed_paths:
        path = workspace / relative
        if path.suffix != ".py" or not path.resolve().is_relative_to(workspace):
            continue
        relative = path.relative_to(workspace).as_posix()
        if relative in observed:
            continue
        observed.add(relative)
        if _inside_file(path, workspace):
            pending.add(path)
        else:
            issues.append({
                "code": "OBSERVED_PYTHON_SOURCE_MISSING", "path": relative,
                "line": 1, "column": 1,
                "reason": "原轨迹曾读取此源码，但候选环境缺失该文件；"
                "是否属于任务起点所需源码，需结合轨迹核查。",
            })
    while pending:
        path = min(pending)
        pending.remove(path)
        if path in visited:
            continue
        visited.add(path)
        relative = path.relative_to(workspace).as_posix()
        try:
            with tokenize.open(path) as handle:
                source = handle.read()
        except (OSError, UnicodeError, SyntaxError) as exc:
            issues.append({
                "code": "PYTHON_SOURCE_READ_ERROR", "path": relative, "line": 1,
                "column": 1, "reason": f"无法按 Python 源码编码读取：{exc}",
            })
            continue
        for line, column in _redaction_positions(source):
            issues.append({
                "code": "EXECUTABLE_REDACTION_PLACEHOLDER", "path": relative,
                "line": line, "column": column + 1,
                "reason": "可执行 token 中含有脱敏占位符，原始程序符号不可用。",
            })
        try:
            tree = ast.parse(source, filename=relative)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(tree, relative, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            issues.append({
                "code": "PYTHON_SYNTAX_ERROR", "path": relative,
                "line": getattr(exc, "lineno", None) or 1,
                "column": getattr(exc, "offset", None) or 1,
                "reason": str(getattr(exc, "msg", exc)),
            })
            continue
        pending.update(_local_imports(tree, path, workspace) - visited)
    issues.sort(key=lambda issue: (issue["path"], issue["line"], issue["column"], issue["code"]))
    for index, issue in enumerate(issues, 1):
        issue["id"] = f"integrity-{index:03d}"
    return {
        "schema_version": INTEGRITY_SCHEMA,
        "status": "DIAGNOSTICS" if issues else "PASS",
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "scope_paths": sorted(path.relative_to(workspace).as_posix() for path in visited),
        "observed_paths": sorted(observed),
        "issues": issues,
        "limitations": [
            "只使用宿主 Python 的语法；目标解释器版本不同必须由 Sufficiency 依据任务解释。",
            "只检查必要初态绑定、改动前回放观测的 Python 源码和可静态定位的本地导入；不执行代码。",
            "未验证动态导入、外部依赖、缺失导出符号或行为正确性；无诊断不代表环境完整。",
        ],
    }


def classify_integrity_issues(
    report: dict[str, Any], classifications: Any,
) -> tuple[dict[str, Any], list[str], bool]:
    """逐项核对模型的任务相关性解释，任何未解释的诊断都禁止 READY。"""

    issues = {issue["id"]: dict(issue) for issue in report["issues"]}
    errors: list[str] = []
    if classifications is None and not issues:
        classifications = []
    if not isinstance(classifications, list):
        errors.append("INTEGRITY_CLASSIFICATIONS_INVALID")
        classifications = []
    seen: set[str] = set()
    reconstruction_gap = False
    for item in classifications:
        if not isinstance(item, dict):
            errors.append("INTEGRITY_CLASSIFICATION_INVALID")
            continue
        issue_id = item.get("issue_id")
        if not isinstance(issue_id, str) or issue_id not in issues:
            errors.append("INTEGRITY_ISSUE_UNKNOWN")
            continue
        if issue_id in seen:
            errors.append(f"INTEGRITY_CLASSIFICATION_DUPLICATE:{issue_id}")
            continue
        seen.add(issue_id)
        issue = issues[issue_id]
        kind = item.get("classification")
        reason = item.get("reason")
        if (
            item.get("path") != issue["path"]
            or not isinstance(kind, str)
            or kind not in _CLASSIFICATIONS
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            errors.append(f"INTEGRITY_CLASSIFICATION_INVALID:{issue_id}")
            continue
        issue["classification"] = kind
        issue["classification_reason"] = reason.strip()
        if "classification_evidence_ref_ids" in item:
            # 原样保留供下游核验；不能过滤未知值或替模型补齐缺失引用。
            issue["classification_evidence_ref_ids"] = copy.deepcopy(
                item["classification_evidence_ref_ids"]
            )
        if kind == "RECONSTRUCTION_GAP":
            reconstruction_gap = True
            errors.append(f"WORKSPACE_RECONSTRUCTION_GAP:{issue_id}")
    for issue_id, issue in issues.items():
        if "classification" not in issue:
            errors.append(f"INTEGRITY_ISSUE_UNCLASSIFIED:{issue_id}")
    status = "REVIEW" if errors else ("CLASSIFIED" if issues else "PASS")
    return report | {"status": status, "issues": list(issues.values())}, errors, reconstruction_gap
