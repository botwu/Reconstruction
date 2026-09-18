"""依据用户验收义务生成隐藏测试和参考解；生成结果必须另经 RED-check。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelRequest,
    parse_json_object,
)

VERIFIER_PROMPT_VERSION = "terminal-universe-verifier-adaptation-v1"
VERIFIER_SYSTEM = """你是独立的 code/file 任务验证器构建者。参照 Terminal-Universe 附录 D：
只测试用户明确规定的接口和功能。期望值必须在测试中独立计算；不得运行待测实现
来产生 gold。至少一个 missing-capability 测试必须在当前完成态 workspace（bE）上失败；
禁止只断言文件/目录存在的 missing 测试。保护性测试必须通过。
oracle 只写评审类完成物，不得写注入器实现。不得把历史失败轨迹的实现当成正确参考解。
生成自足 pytest 文件，测试中的 workspace 根路径必须通过环境变量
TRACEFORGE_WORKSPACE 获取。测试文件只在独立 verifier 中可见。
同时给出至少两个可独立执行的合法参考解 shell 脚本和一个错误实现脚本，供验证器校准。
脚本在 /home/user/workspace 中执行且不得访问 /tests 或其他隐藏文件。
每个参考解应从初始 workspace 出发完成任务；错误实现要是有意义的错误实现，不能只删文件。
输出严格 JSON，字段：status(READY/REVIEW)、test_outputs_py、oracle_solutions
([{name,script,justification}])、mutation_solutions(同结构)、missing_capability_tests
([pytest函数名])、protective_tests([pytest函数名])、obligation_coverage
({用户验收义务ID:[pytest函数名]})、expected_value_strategy、open_questions。
证据不足时 status=REVIEW，列出缺口。生成不是通过校准，不得声称测试已执行。
"""


class VerifierSynthesisError(ValueError):
    """验证器候选结构或语义来源不满足最低要求。"""


_EXISTENCE_ATTRS = frozenset({"exists", "is_file", "is_dir", "isfile", "isdir", "islink"})
_PROTECTED_INJECTOR_NAMES = frozenset({"injector.cpp", "loader.cpp", "robloxdll.cpp"})
_PATH_WRITE = re.compile(
    r"""Path\s*\(\s*(?:[\w.]+\s*/\s*)?['\"]([^'\"]+)['\"]\s*\)\s*\.\s*write_(?:text|bytes)""",
    re.I,
)
_DIV_WRITE = re.compile(
    r"""/\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*write_(?:text|bytes)""",
    re.I,
)
_OPEN_WRITE = re.compile(
    r"""open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]w""",
    re.I,
)
_REDIRECT = re.compile(
    r"""(?:>>?|tee(?:\s+-a)?)\s+['\"]?([^\s'\";|&<>]+)""",
    re.I,
)
_PS_WRITE = re.compile(
    r"""(?:Set-Content|Add-Content|Out-File|New-Item)\s+(?:-\w+\s+)*['\"]?([^\s'\";]+)""",
    re.I,
)


def _assert_is_existence(node: ast.Assert) -> bool:
    for child in ast.walk(node.test):
        if isinstance(child, ast.Attribute) and child.attr in _EXISTENCE_ATTRS:
            return True
        if isinstance(child, ast.Name) and child.id in _EXISTENCE_ATTRS:
            return True
    return False


def existence_only_missing_tests(code: str, missing_names: tuple[str, ...]) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    by_name = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    bad: list[str] = []
    for name in missing_names:
        fn = by_name.get(name)
        if fn is None:
            continue
        asserts = [node for node in ast.walk(fn) if isinstance(node, ast.Assert)]
        if asserts and all(_assert_is_existence(item) for item in asserts):
            bad.append(name)
    return bad


def oracle_write_destinations(script: str) -> list[str]:
    """Literal paths a script writes to. Mentions in review text are ignored."""

    found: list[str] = []
    for pattern in (_PATH_WRITE, _DIV_WRITE, _OPEN_WRITE, _REDIRECT, _PS_WRITE):
        for match in pattern.finditer(script or ""):
            raw = match.group(1).replace("\\", "/").rstrip("/")
            if raw and raw not in found:
                found.append(raw)
    return found


def oracle_writes_injector(script: str) -> bool:
    """True only when the script writes an injector implementation file."""

    for dest in oracle_write_destinations(script):
        if PurePosixPath(dest).name.lower() in _PROTECTED_INJECTOR_NAMES:
            return True
    return False


def red_shape_errors(
    *,
    test_outputs_py: str,
    missing_capability_tests: tuple[str, ...],
    oracle_solutions: tuple[SolutionVariant, ...],
) -> list[str]:
    errors = [
        f"EXISTENCE_ONLY_MISSING:{name}"
        for name in existence_only_missing_tests(test_outputs_py, missing_capability_tests)
    ]
    for variant in oracle_solutions:
        if oracle_writes_injector(variant.script):
            errors.append(f"ORACLE_WRITES_INJECTOR:{variant.name}")
    return errors


@dataclass(frozen=True, slots=True)
class SolutionVariant:
    name: str
    script: str
    justification: str


@dataclass(frozen=True, slots=True)
class VerifierCandidate:
    schema_version: str
    candidate_id: str
    status: str
    test_outputs_py: str
    oracle_solutions: tuple[SolutionVariant, ...]
    mutation_solutions: tuple[SolutionVariant, ...]
    missing_capability_tests: tuple[str, ...]
    protective_tests: tuple[str, ...]
    obligation_coverage: dict[str, tuple[str, ...]]
    expected_value_strategy: str
    open_questions: tuple[str, ...]
    model: str
    prompt_version: str
    prompt_sha256: str
    response_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strings(value: Any, name: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise VerifierSynthesisError(f"{name} 必须为字符串数组")
    if required and not value:
        raise VerifierSynthesisError(f"{name} 不能为空")
    if len(set(value)) != len(value):
        raise VerifierSynthesisError(f"{name} 含重复条目")
    return tuple(value)


def _variants(value: Any, name: str, minimum: int) -> tuple[SolutionVariant, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise VerifierSynthesisError(f"{name} 至少需要 {minimum} 个")
    output = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "script", "justification"}:
            raise VerifierSynthesisError(f"{name} 条目字段不匹配")
        if any(not isinstance(item[k], str) or not item[k].strip() for k in item):
            raise VerifierSynthesisError(f"{name} 字段不能为空")
        if "/tests" in item["script"] or "/solution" in item["script"]:
            raise VerifierSynthesisError("参考解不能访问隐藏测试或解答目录")
        output.append(SolutionVariant(**item))
    if len({x.name for x in output}) != len(output):
        raise VerifierSynthesisError("参考解名称不能重复")
    if len({x.script for x in output}) != len(output):
        raise VerifierSynthesisError("参考解脚本不能完全相同")
    return tuple(output)


def synthesize_verifier(
    *,
    task: dict[str, Any],
    workspace_files: dict[str, str],
    model: ChatModel,
    model_name: str = "claude-opus-4-8",
) -> tuple[VerifierCandidate | None, dict[str, Any]]:
    """调用教师生成测试候选；只做语法校验，模型代码绝不在宿主机执行。"""
    obligations = task.get("acceptance_obligations")
    if not isinstance(obligations, list) or not obligations:
        raise VerifierSynthesisError("任务必须提供用户验收义务")
    ids = [item.get("id") for item in obligations if isinstance(item, dict)]
    if len(ids) != len(obligations) or any(not isinstance(x, str) or not x for x in ids):
        raise VerifierSynthesisError("每条验收义务必须包含非空 id")
    if len(set(ids)) != len(ids):
        raise VerifierSynthesisError("验收义务 ID 重复")
    prompt = json.dumps({"task": task, "initial_workspace": workspace_files}, ensure_ascii=False)
    digest = hashlib.sha256((VERIFIER_SYSTEM + prompt).encode()).hexdigest()
    request = ModelRequest(
        digest,
        model_name,
        VERIFIER_SYSTEM,
        prompt,
        "traceforge.verifier-candidate.v1",
        max_tokens=16000,
    )
    response = model.complete(request)
    payload = parse_json_object(response.text)
    audit = {
        "prompt_version": VERIFIER_PROMPT_VERSION,
        "prompt_sha256": digest,
        "model": model_name,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "attempts": response.attempts,
        "latency_seconds": response.latency_seconds,
        "raw_response": response.text,
        "response_sha256": response.content_sha256,
        "system": VERIFIER_SYSTEM,
        "prompt": prompt,
    }
    candidate, extra = candidate_from_payload(
        payload,
        obligation_ids=[str(item) for item in ids],
        model_name=model_name,
        prompt_sha256=digest,
        response_sha256=response.content_sha256,
    )
    audit.update(extra)
    return candidate, audit


def candidate_from_payload(
    payload: dict[str, Any],
    *,
    obligation_ids: list[str],
    model_name: str,
    prompt_sha256: str,
    response_sha256: str,
) -> tuple[VerifierCandidate | None, dict[str, Any]]:
    """把模型 JSON 校验成 VerifierCandidate；不执行测试代码。"""

    extra: dict[str, Any] = {}
    if payload.get("status") == "REVIEW":
        extra["status"] = "REVIEW"
        extra["open_questions"] = list(
            _strings(payload.get("open_questions"), "open_questions", required=True)
        )
        return None, extra
    if payload.get("status") != "READY":
        raise VerifierSynthesisError("status 必须是 READY 或 REVIEW")
    code = payload.get("test_outputs_py")
    if not isinstance(code, str) or not code.strip():
        raise VerifierSynthesisError("缺少 test_outputs_py")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise VerifierSynthesisError("测试代码语法无效") from exc
    tests = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    }
    missing = _strings(
        payload.get("missing_capability_tests"), "missing_capability_tests", required=True
    )
    protective = _strings(payload.get("protective_tests"), "protective_tests", required=True)
    coverage = payload.get("obligation_coverage")
    if not isinstance(coverage, dict) or set(coverage) != set(obligation_ids):
        raise VerifierSynthesisError("测试覆盖必须与用户验收义务完全对应")
    normalized = {
        key: _strings(value, "obligation_coverage", required=True)
        for key, value in coverage.items()
    }
    all_refs = (
        set(missing) | set(protective) | {x for values in normalized.values() for x in values}
    )
    if not all_refs <= tests:
        raise VerifierSynthesisError("测试引用不存在的 pytest 函数")
    if set(missing) & set(protective):
        raise VerifierSynthesisError("能力缺失与保护性测试不能重叠")
    strategy = payload.get("expected_value_strategy")
    if not isinstance(strategy, str) or not strategy.strip():
        raise VerifierSynthesisError("必须说明期望值独立计算方法")
    oracles = _variants(payload.get("oracle_solutions"), "oracle_solutions", 2)
    mutations = _variants(payload.get("mutation_solutions"), "mutation_solutions", 1)
    shape = red_shape_errors(
        test_outputs_py=code,
        missing_capability_tests=missing,
        oracle_solutions=oracles,
    )
    if shape:
        raise VerifierSynthesisError(shape[0])
    extra["status"] = "UNVALIDATED"
    return (
        VerifierCandidate(
            "traceforge.verifier-candidate.v1",
            prompt_sha256,
            "UNVALIDATED",
            code,
            oracles,
            mutations,
            missing,
            protective,
            normalized,
            strategy,
            _strings(payload.get("open_questions"), "open_questions"),
            model_name,
            VERIFIER_PROMPT_VERSION,
            prompt_sha256,
            response_sha256,
        ),
        extra,
    )


__all__ = [
    "SolutionVariant",
    "VerifierCandidate",
    "VerifierSynthesisError",
    "candidate_from_payload",
    "synthesize_verifier",
]
