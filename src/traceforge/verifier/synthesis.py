"""依据用户验收义务生成隐藏测试和参考解；生成结果必须另经 RED-check。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelRequest,
    parse_json_object,
)

VERIFIER_PROMPT_VERSION = "terminal-universe-verifier-adaptation-v9-source-grounding"
VERIFIER_SYSTEM = """你是独立的 code/file 任务验证器构建者。参照 Terminal-Universe 附录 D：
只测试用户明确规定的接口和功能。期望值必须在测试中独立计算；不得运行待测实现
来产生 gold。可观察功能缺口由 missing-capability 测试在初态证明；保护性测试必须通过。
纯重构的业务行为可能原本就正确，不能为了制造初态失败而限定用户未规定的接口或调用栈。
此时用 file_semantic_checks 声明原义务中须对真实前后源码判定的要求，
允许 missing_capability_tests 为空；初态未完成、两参考解完成及错误变体未完成，
仍须由独立模型在各次真实执行归档上校准，不能靠声明直接通过。
可测试的行为继续用 pytest；语义判定不能代替这些行为。禁止存在性或关键词充当功能验证。
oracle 可写用户明确要求的任务文件；不得写入受保护的注入器实现文件（如 injector.cpp、loader.cpp、robloxdll.cpp）。不得把历史失败轨迹的实现当成正确参考解。
生成自足 pytest 文件，测试中的 workspace 根路径必须通过环境变量
TRACEFORGE_WORKSPACE 获取。测试文件只在独立 verifier 中可见。
同时给出恰好两个独立完整的合法参考解程序和恰好一个语义错误实现程序，供验证器校准。
每个参考解都必须独立完成全部 FILE 义务，不能把两个组件分作两个参考解。
返回的是修改工作区的安装程序，不是目标文件的源码。推荐标准库 Path.write_text 配合
repr 字符串写入文件；安装时不要导入目标程序的 ROS/仿真依赖或启动服务。
错误实现程序也必须正常执行并退出 0；语法、导入、权限、环境变量错误不是语义错误。
测试必须观察用户所要求的行为；禁止仅靠注释、关键词存在判定实现正确，或为了参考解通过而放宽断言。
报告类任务应依据实际输入核对结论、引用和用户判定规则，不能用关键词数量证明报告正确。
错误实现必须在合法输出路径保留合法格式，只破坏核心行为或事实；写错路径、漏标题和缺文件不能替代语义校准。
保护测试针对实际工作区与用户约束；不要在测试内嵌入参考安装器并用假项目自证正确。
每个 Python 参考解或错误实现都必须是独立文件可解析的合法 Python；返回前应按
`python -m py_compile` 检查，不能在引号内嵌入原始换行。
脚本在 /home/user/workspace 中执行且不得访问 /tests 或其他隐藏文件。
每个参考解应从初始 workspace 出发完成任务；它必须是会修改 workspace 的可执行 solver，不能只返回目标文件源码或只打印说明；错误实现要是有意义的错误实现，不能只删文件。
输出严格 JSON，字段：status(READY/REVIEW)、test_outputs_py、oracle_solutions
([{name,script,justification}])、mutation_solutions(同结构)、missing_capability_tests
([pytest函数名])、protective_tests([pytest函数名])、obligation_coverage
({每个FILE义务ID:[pytest函数名]})、file_semantic_checks
({需要产物语义判定的FILE义务ID:原任务支持的具体判据})、expected_value_strategy、open_questions。
仅另有语义判据的义务可以没有对应pytest；不得把FILE改为NON_FILE或删掉义务。
证据不足时 status=REVIEW，列出缺口。生成不是通过校准，不得声称测试已执行。
"""


class VerifierSynthesisError(ValueError):
    """验证器候选结构或语义来源不满足最低要求。"""


# 只检查脚本中的显式路径文字，不把字符串引用当作实际访问的证明。
_HIDDEN_ROOT_LITERAL = re.compile(
    r"""(?:^|[\s'"\x60=([{,:;|&<>])/(?:\./|/)*(?:tests|solution)(?=$|[/\s'"\x60)\]},;:|&<>])"""
)
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


def swallowed_assertion_tests(code: str, test_names: tuple[str, ...]) -> list[str]:
    """Reject mutation tests that swallow their own expected AssertionError.

    A common malformed RED test wraps the validator call and its deliberate
    raise AssertionError in except AssertionError: pass. That test passes even
    when the validator accepts the mutation, so it is not protection evidence.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    names = set(test_names)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
            continue
        for trial in ast.walk(node):
            if not isinstance(trial, ast.Try):
                continue
            deliberate_raise = any(
                isinstance(item, ast.Raise)
                and (
                    isinstance(item.exc, ast.Name)
                    and item.exc.id == "AssertionError"
                    or isinstance(item.exc, ast.Call)
                    and isinstance(item.exc.func, ast.Name)
                    and item.exc.func.id == "AssertionError"
                )
                for item in ast.walk(trial)
            )
            swallowed = any(
                isinstance(handler.type, ast.Name)
                and handler.type.id == "AssertionError"
                and any(isinstance(item, ast.Pass) for item in ast.walk(handler))
                for handler in trial.handlers
            )
            if deliberate_raise and swallowed:
                bad.append(node.name)
                break
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


def _task_requirement_text(task: dict[str, Any] | None) -> str:
    """提取任务原文；只有任务原文可以授权固定标题。"""

    if not isinstance(task, dict):
        return ""
    return json.dumps(task, ensure_ascii=False, sort_keys=True).lower()


_HEADING_TERMS = (
    ("STRENGTHS", re.compile(r"\bstrengths?\b", re.IGNORECASE)),
    ("RESIDUAL_RISKS", re.compile(r"\bresidual(?:\s+|\s*\*)risks?\b", re.IGNORECASE)),
)
_HEADING_CONTEXT = re.compile(
    r"\b(?:heading|headings|section|sections|title|titles|label|labels|"
    r"header|headers|field|fields|key|keys|exact|exactly|literal|verbatim|"
    r"fixed|named|called|wording|phrase)\b",
    re.IGNORECASE,
)
_HEADING_EQUIVALENT = re.compile(
    r"\b(?:equivalent|equivalence|semantic(?:s|ally)?|synonym(?:s)?|"
    r"similar|same meaning|or comparable|or alternative)\b",
    re.IGNORECASE,
)


def _task_explicitly_requires_heading(task_text: str, term: re.Pattern[str]) -> bool:
    """判断任务是否明确要求固定标题，而不是只要求相同语义。"""

    for match in term.finditer(task_text):
        window = task_text[max(0, match.start() - 100) : match.end() + 100]
        if _HEADING_EQUIVALENT.search(window):
            continue
        if _HEADING_CONTEXT.search(window):
            return True
        # 只有标题词本身被引用且邻近出现要求性动词，才授权字面契约。
        quoted_term = re.search(
            rf"""['"]\s*{re.escape(match.group())}\s*['"]""",
            window,
            re.IGNORECASE,
        )
        if quoted_term and re.search(
            r"\b(?:must|should|required|include|contain|write|use|return|provide)\b",
            window,
            re.IGNORECASE,
        ):
            return True
    return False


def _literal_heading_checks(test_outputs_py: str) -> set[str]:
    """找出测试代码中实际约束固定标题的调用和比较。"""

    try:
        tree = ast.parse(test_outputs_py)
    except SyntaxError:
        return set()
    found: set[str] = set()

    def add_for_text(value: str) -> None:
        # 正则字符串里的转义符不是报告内容；去掉后再识别标题词。
        normalized = re.sub(r"\\[A-Za-z]", " ", value)
        for code, pattern in _HEADING_TERMS:
            if pattern.search(normalized):
                found.add(code)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = (
                f"{callee.value.id}.{callee.attr}"
                if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
                else callee.id
                if isinstance(callee, ast.Name)
                else ""
            )
            if name in {"re.search", "re.match", "re.fullmatch", "re.findall", "re.finditer"}:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    add_for_text(node.args[0].value)
        elif isinstance(node, ast.Compare):
            # 覆盖 literal in report、lower 后比较，以及 not-in 形式。
            for operand in (node.left, *node.comparators):
                if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                    add_for_text(operand.value)
    return found


def unsupported_literal_heading_requirements(
    test_outputs_py: str,
    *,
    task: dict[str, Any] | None,
) -> list[str]:
    """拒绝任务未要求的固定报告标题，避免把编辑措辞当成行为契约。"""

    task_text = _task_requirement_text(task)
    if not task_text:
        return []
    errors: list[str] = []
    for code, pattern in _HEADING_TERMS:
        if code in _literal_heading_checks(test_outputs_py) and not _task_explicitly_requires_heading(
            task_text, pattern
        ):
            errors.append(f"UNSUPPORTED_LITERAL_REQUIREMENT:{code}")
    return errors


def red_shape_errors(
    *,
    test_outputs_py: str,
    missing_capability_tests: tuple[str, ...],
    oracle_solutions: tuple[SolutionVariant, ...],
    mutation_test_names: tuple[str, ...] = (),
    task: dict[str, Any] | None = None,
) -> list[str]:
    errors = [
        f"EXISTENCE_ONLY_MISSING:{name}"
        for name in existence_only_missing_tests(test_outputs_py, missing_capability_tests)
    ]
    errors.extend(unsupported_literal_heading_requirements(test_outputs_py, task=task))
    errors.extend(
        f"MUTATION_TEST_SWALLOWS_ASSERTION:{name}"
        for name in swallowed_assertion_tests(test_outputs_py, mutation_test_names)
    )
    for variant in oracle_solutions:
        if oracle_writes_injector(variant.script):
            errors.append(f"ORACLE_WRITES_INJECTOR:{variant.name}")
    return errors


@dataclass(frozen=True, slots=True)
class SolutionVariant:
    name: str
    script: str
    justification: str


_PYTHON_SHEBANG = re.compile(
    r"^#!\s*(?:/usr/bin/env(?:\s+-S)?\s+)?(?:\S*/)?"
    r"(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+(?:\.\d+)*)?)\b",
    re.IGNORECASE,
)
_SHELL_FIRST_WORDS = frozenset(
    {
        "[", "awk", "bash", "cat", "cd", "echo", "env", "exec", "false",
        "grep", "if", "mkdir", "mv", "perl", "printf", "python", "python3",
        "rm", "sed", "sh", "set", "source", "tee", "test", "then", "true",
        "command", "cp", "exit", "fi", "for", "function", "git", "install",
        "make", "node", "touch", "trap", "unset", "until", "while", "zsh",
    }
)
_PYTHON_FIRST_WORDS = frozenset(
    {"class", "def", "from", "import", "if", "for", "while", "try", "with",
     "async", "assert", "raise", "return"}
)


def _first_code_line(script: str) -> str:
    for line in script.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def is_python_solution(script: str) -> bool:
    """识别直接 Python 脚本，供语法校验与 bundle 入口共同使用。"""

    stripped = script.lstrip()
    if not stripped:
        return False
    first_line = stripped.splitlines()[0].strip()
    if _PYTHON_SHEBANG.match(first_line):
        return True
    if first_line.startswith("#!"):
        return False
    first_code = _first_code_line(script)
    if not first_code or first_code.startswith(("$", "#!")):
        return False
    first_word = first_code.split(None, 1)[0].rstrip(":=()").lower()
    if first_word in {"if", "for", "while"} and first_code.endswith(":"):
        return True
    if first_word in _SHELL_FIRST_WORDS or first_code.startswith("python3 - <<"):
        return False
    if first_word in _PYTHON_FIRST_WORDS:
        return True
    if first_code.startswith(("@", "Path(", "asyncio.", "subprocess.")):
        return True
    if re.search(r"(?m)^\s*(?:from|import|def|class)\b", script):
        return True
    if re.search(r"\b(?:Path|write_text|write_bytes)\s*\(", script):
        return True
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef,
                          ast.AsyncFunctionDef, ast.ClassDef, ast.With, ast.For, ast.While,
                          ast.Try, ast.If, ast.Call))
        for node in ast.walk(tree)
    )


def python_script_syntax_error(script: str) -> str | None:
    """返回编译器错误和附近原文，供作者修正；shell/heredoc 不会被误判。"""

    if not is_python_solution(script):
        return None
    try:
        ast.parse(script)
    except SyntaxError as exc:
        column = max(0, (exc.offset or 1) - 1)
        context = (exc.text or "").rstrip("\n")[max(0, column - 120):column + 120]
        return (
            f"line={exc.lineno or 0};offset={exc.offset or 0};"
            f"message={exc.msg};source={context!r}"
        )
    return None


def validate_solution_scripts(
    oracle_solutions: tuple[SolutionVariant, ...],
    mutation_solutions: tuple[SolutionVariant, ...],
) -> None:
    """在任何 Harbor job 前校验模型脚本，避免把语法错误当执行环境故障。"""

    for kind, variants in (("ORACLE", oracle_solutions), ("MUTATION", mutation_solutions)):
        for variant in variants:
            error = python_script_syntax_error(variant.script)
            if error is not None:
                raise VerifierSynthesisError(
                    f"{kind}_SCRIPT_SYNTAX_ERROR:{variant.name}:{error}"
                )


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
    file_semantic_checks: dict[str, str] = field(default_factory=dict)

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


def _variants(value: Any, name: str, expected: int) -> tuple[SolutionVariant, ...]:
    if not isinstance(value, list) or len(value) != expected:
        raise VerifierSynthesisError(f"{name} 必须恰好包含 {expected} 个")
    output = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "script", "justification"}:
            raise VerifierSynthesisError(f"{name} 条目字段不匹配")
        if any(not isinstance(item[k], str) or not item[k].strip() for k in item):
            raise VerifierSynthesisError(f"{name} 字段不能为空")
        if _HIDDEN_ROOT_LITERAL.search(item["script"]):
            raise VerifierSynthesisError("参考解包含隐藏测试或解答目录的绝对路径")
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
        task=task,
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
    task: dict[str, Any] | None = None,
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
    semantic = payload.get("file_semantic_checks", {})
    if (not isinstance(semantic, dict) or not set(semantic) <= set(obligation_ids)
            or any(not isinstance(value, str) or not value.strip() for value in semantic.values())):
        raise VerifierSynthesisError("file_semantic_checks 必须是已知FILE义务到非空判据的映射")
    missing = _strings(
        payload.get("missing_capability_tests"), "missing_capability_tests", required=not semantic
    )
    protective = _strings(payload.get("protective_tests"), "protective_tests", required=True)
    coverage = payload.get("obligation_coverage")
    if not isinstance(coverage, dict) or set(coverage) != set(obligation_ids):
        raise VerifierSynthesisError("测试覆盖必须与用户验收义务完全对应")
    normalized = {
        key: _strings(value, "obligation_coverage", required=key not in semantic)
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
    validate_solution_scripts(oracles, mutations)
    shape = red_shape_errors(
        test_outputs_py=code,
        missing_capability_tests=missing,
        oracle_solutions=oracles,
        mutation_test_names=protective,
        task=task,
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
            _strings(payload.get("open_questions", []), "open_questions"),
            model_name,
            VERIFIER_PROMPT_VERSION,
            prompt_sha256,
            response_sha256,
            dict(semantic),
        ),
        extra,
    )


__all__ = [
    "SolutionVariant",
    "VerifierCandidate",
    "VerifierSynthesisError",
    "candidate_from_payload",
    "unsupported_literal_heading_requirements",
    "swallowed_assertion_tests",
    "python_script_syntax_error",
    "is_python_solution",
    "validate_solution_scripts",
    "synthesize_verifier",
]
