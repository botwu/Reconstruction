from __future__ import annotations

import json
from types import SimpleNamespace

from traceforge.reconstruction.agents.roles import INTENT_ROLE
from traceforge.reconstruction.pipeline import execution_support_route
from traceforge.reconstruction.environment_bindings import (
    collect_allowed_paths,
    collect_file_binding_paths,
    file_obligation_ids,
    file_required_paths,
)
from traceforge.reconstruction.intent_recovery import (
    INTENT_PROMPT_VERSION,
    _gate,
    _prompt,
    deepen_requires_file,
)


def _l22_timeline() -> list[dict]:
    return [
        {
            "call_id": "c1",
            "name": "read_file",
            "arguments": {"path": "Injector.cpp"},
            "result_text": "int inject() {\n",
        },
        {
            "call_id": "c2",
            "name": "read_file",
            "arguments": {"path": "Loader.cpp"},
            "result_text": "int load() { return 1; }\n",
        },
    ]


def _l49_timeline() -> list[dict]:
    return [
        {
            "call_id": "r1",
            "name": "read_file",
            "arguments": {"path": "workflows/build_xhs_monitor.js"},
            "result_text": "module.exports = {}\n",
        },
        {
            "call_id": "r2",
            "name": "read_file",
            "arguments": {"path": "deploy/rsshub-xhs/patch-two-phase.cjs"},
            "result_text": "module.exports = {}\n",
        },
    ]


def test_deepen_file_gate_distinguishes_explicit_read_only_review() -> None:
    paths = ["src/parser.py"]
    assert deepen_requires_file(
        "只读代码评审 src/parser.py，不修改任何文件",
        paths,
        domain_route="terminal",
    ) is False
    assert deepen_requires_file(
        "read-only code review; do not modify source/tests",
        paths,
        domain_route="terminal",
    ) is False
    assert deepen_requires_file(
        "审查并修复 src/parser.py 的边界错误",
        paths,
        domain_route="terminal",
    ) is True
    assert deepen_requires_file(
        "review the parser and add a regression test",
        paths,
        domain_route="terminal",
    ) is True


def test_deepen_requires_file_on_read_code_or_code_file_route() -> None:
    paths = ["Injector.cpp"]
    assert deepen_requires_file("完全读取并了解注入器代码", paths) is True
    assert deepen_requires_file("在2026年這套注入器還可以使用嗎，完全讀取代碼", paths) is True
    assert deepen_requires_file("看看 Injector.cpp 还能不能用", paths) is True
    assert deepen_requires_file("补采本周数据并发布生产", paths, domain_route="code_file") is True
    assert deepen_requires_file("解释一下这段日志为什么超时", []) is False
    assert deepen_requires_file("解释一下这段日志为什么超时", paths) is False
    assert deepen_requires_file("重构 bar 服务", paths, domain_route="") is False


def test_l22_deepen_keeps_user_cite_and_file() -> None:
    source = {"tool_timeline": _l22_timeline()}
    allowed = collect_allowed_paths(source, [{"id": "user:1", "text": "在2026年這套注入器還可以使用嗎，完全讀取代碼，也可上網查看論壇"}])
    bindable = collect_file_binding_paths(source)
    payload = {
        "task_id": "task_828b37beaca277cc500e",
        "task_instruction": "在2026年這套注入器還可以使用嗎：读懂并评审 Injector.cpp / Loader.cpp，不把论坛调研当验收。",
        "core_objective": "基于回放代码给出架构与补丁评审",
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "上网查看 UnKnoWnCheaTs",
                "evidence_ref_ids": ["user:1"],
            },
            {
                "id": "obl-002",
                "text": "完全读取并了解注入器代码",
                "evidence_ref_ids": ["user:1"],
            },
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": [],
                "observable": "",
                "verifier_kind": "NON_FILE",
            },
            {
                "obligation_id": "obl-002",
                "required_paths": ["Injector.cpp", "Loader.cpp"],
                "observable": "代码解释覆盖用户指定的入口与最近修改",
                "verifier_kind": "FILE",
            },
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "task_828b37beaca277cc500e", "domain_route": "code_file"},
        {"user:1"},
        allowed_paths=allowed,
        user_blob="在2026年這套注入器還可以使用嗎，完全讀取代碼，也可上網查看論壇",
        file_binding_paths=bindable,
    )
    assert status == "READY", errors
    assert errors == []
    assert "user:1" in gated["acceptance_obligations"][1]["evidence_ref_ids"]
    assert "Injector.cpp" in file_required_paths(gated)
    assert file_obligation_ids(gated) == ["obl-002"]
    assert "在2026年這套注入器還可以使用嗎" in gated["task_instruction"]


def test_l22_all_non_file_with_tree_is_blocked() -> None:
    source = {"tool_timeline": _l22_timeline()}
    allowed = collect_allowed_paths(source, [{"id": "user:1", "text": "完全读取并了解注入器代码"}])
    payload = {
        "task_id": "t-l22",
        "task_instruction": "上网查论坛判断 2026 是否还能用",
        "core_objective": "调研",
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "查看逆向论坛",
                "evidence_ref_ids": ["user:1"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": [],
                "observable": "",
                "verifier_kind": "NON_FILE",
            }
        ],
    }
    status, errors, _gated = _gate(
        payload,
        {"task_id": "t-l22", "domain_route": "code_file"},
        {"user:1"},
        allowed_paths=allowed,
        user_blob="完全读取并了解注入器代码",
        file_binding_paths=collect_file_binding_paths(source),
    )
    assert status == "REVIEW"
    assert "FILE_OBLIGATION_REQUIRED" in errors


def test_l49_publish_is_context_file_scripts_ready() -> None:
    source = {"tool_timeline": _l49_timeline()}
    user = "先发布生产环境的 RSSHub 镜像，然后补采本周小红书作品数据"
    allowed = collect_allowed_paths(source, [{"id": "user:99", "text": user}])
    payload = {
        "task_id": "task_aad601e37eeae41e7404",
        "task_instruction": f"{user}：先把 workflows/build_xhs_monitor.js 与 RSSHub patch 补到本地可跑。",
        "core_objective": "为补采准备可本地验收的工作流脚本",
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "发布生产环境镜像",
                "evidence_ref_ids": ["user:99"],
            },
            {
                "id": "obl-002",
                "text": "补齐本周补采所需的本地工作流脚本",
                "evidence_ref_ids": ["user:99"],
            },
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": [],
                "observable": "",
                "verifier_kind": "NON_FILE",
            },
            {
                "obligation_id": "obl-002",
                "required_paths": [
                    "workflows/build_xhs_monitor.js",
                    "deploy/rsshub-xhs/patch-two-phase.cjs",
                ],
                "observable": "本地脚本具备补采本周数据所需的入口与配置",
                "verifier_kind": "FILE",
            },
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "task_aad601e37eeae41e7404", "domain_route": "code_file"},
        {"user:99"},
        allowed_paths=allowed,
        user_blob=user,
        file_binding_paths=collect_file_binding_paths(source),
    )
    assert status == "READY", errors
    assert file_obligation_ids(gated) == ["obl-002"]
    assert gated["environment_bindings"][0]["verifier_kind"] == "NON_FILE"
    assert "user:99" in gated["acceptance_obligations"][1]["evidence_ref_ids"]


def test_empty_tree_chat_may_stay_non_file() -> None:
    payload = {
        "task_id": "t-chat",
        "task_instruction": "解释一下这段日志为什么超时",
        "core_objective": "解释超时",
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "解释超时原因",
                "evidence_ref_ids": ["user:0"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": [],
                "observable": "",
                "verifier_kind": "NON_FILE",
            }
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "t-chat", "domain_route": "other"},
        {"user:0"},
        allowed_paths=[],
        user_blob="解释一下这段日志为什么超时",
        file_binding_paths=[],
    )
    assert status == "READY", errors
    assert gated["environment_bindings"][0]["verifier_kind"] == "NON_FILE"
    assert "FILE_OBLIGATION_REQUIRED" not in errors


def test_empty_retrieval_route_stays_none() -> None:
    result = execution_support_route(
        task={"domain_route": "retrieval"},
        source={"selected_span_has_file_ops": False},
        replay=SimpleNamespace(files=()),
    )
    assert result["route"] == "RETRIEVAL_UNSUPPORTED"
    assert result["env_origin"] == "NONE"
    assert result["allow_completion"] is False


def test_wrong_task_id_and_missing_user_cite_still_review() -> None:
    source = {"tool_timeline": _l22_timeline()}
    allowed = collect_allowed_paths(source, [{"id": "user:1", "text": "读 Injector.cpp"}])
    payload = {
        "task_id": "wrong",
        "task_instruction": "读 Injector.cpp",
        "core_objective": "读代码",
        "acceptance_obligations": [
            {
                "id": "obl-002",
                "text": "读取代码",
                "evidence_ref_ids": ["user:99"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-002",
                "required_paths": ["Injector.cpp"],
                "observable": "解释入口",
                "verifier_kind": "FILE",
            }
        ],
    }
    status, errors, _gated = _gate(
        payload,
        {"task_id": "t-l22", "domain_route": "code_file"},
        {"user:1"},
        allowed_paths=allowed,
        user_blob="读 Injector.cpp",
        file_binding_paths=collect_file_binding_paths(source),
    )
    assert status == "REVIEW"
    assert "TASK_ID_MISMATCH" in errors
    assert "OBLIGATION_EVIDENCE_REQUIRED:obl-002" in errors


def test_bar_service_on_shared_session_tree_stays_non_file() -> None:
    payload = {
        "task_id": "t-bar",
        "task_instruction": "重构 bar 服务",
        "core_objective": "重构无关服务",
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "重构 bar 服务",
                "evidence_ref_ids": ["user:4"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": [],
                "observable": "",
                "verifier_kind": "NON_FILE",
            }
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "t-bar", "domain_route": "other"},
        {"user:4"},
        allowed_paths=["foo.py"],
        user_blob="重构 bar 服务",
        file_binding_paths=["foo.py"],
    )
    assert status == "READY", errors
    assert gated["environment_bindings"][0]["verifier_kind"] == "NON_FILE"
    assert "FILE_OBLIGATION_REQUIRED" not in errors


def test_stub_observable_does_not_satisfy_file_gate() -> None:
    source = {"tool_timeline": _l22_timeline()}
    allowed = collect_allowed_paths(source, [{"id": "user:1", "text": "完全读取并了解注入器代码"}])
    payload = {
        "task_id": "t-l22",
        "task_instruction": "完全读取并了解注入器代码",
        "core_objective": "读代码",
        "acceptance_obligations": [
            {
                "id": "obl-002",
                "text": "读取代码",
                "evidence_ref_ids": ["user:1"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-002",
                "required_paths": ["Injector.cpp"],
                "observable": "replayed excerpts still present",
                "verifier_kind": "FILE",
            }
        ],
    }
    status, errors, _gated = _gate(
        payload,
        {"task_id": "t-l22", "domain_route": "code_file"},
        {"user:1"},
        allowed_paths=allowed,
        user_blob="完全读取并了解注入器代码",
        file_binding_paths=collect_file_binding_paths(source),
    )
    assert status == "REVIEW"
    assert "FILE_OBLIGATION_REQUIRED" in errors
    assert any(item.startswith("BINDING_TASK_OUTCOME_REQUIRED") for item in errors)


def test_intent_role_and_prompt_name_the_anchor() -> None:
    assert INTENT_PROMPT_VERSION == "intent-recovery-agent-v21-input-necessity"
    assert "original user query is the anchor" in INTENT_ROLE.identity
    assert "deepen" in INTENT_ROLE.identity
    assert "Research, forum lookup, production publish" in INTENT_ROLE.identity
    system_context = [{"message_indices": [0], "interpretation": "原环境声明只读。"}]
    text = _prompt(
        {"tool_timeline": _l22_timeline(),
         "raw_session": {"messages": [{"role": "system", "content": "原环境声明只读。"}]},
         "session_parser": {"system_context": system_context}},
        {"task_id": "t-l22", "domain_route": "code_file"},
        [{"id": "user:1", "message_index": 1, "text": "完全讀取代碼"}],
        ["Injector.cpp"],
        ["Injector.cpp"],
    )
    assert "The original user query is the anchor" in text
    assert "SOURCE_SYSTEM_CONTEXT=" + json.dumps(system_context, ensure_ascii=False) in text
    assert "SOURCE_SYSTEM_MESSAGES=" + json.dumps([
        {"message_index": 0, "message": {"role": "system", "content": "原环境声明只读。"}},
    ], ensure_ascii=False) in text
    assert "FILE_BINDING_PATHS" in text
    assert "Research, forum lookup, production publish" in text
    assert "at least one FILE obligation is required" in text
    assert "do not invent a project" in text.lower() or "When FILE_BINDING_PATHS is empty" in text


def test_explicit_bindings_missing_obligation_are_reviewed_not_inferred() -> None:
    source = {"tool_timeline": _l22_timeline()}
    allowed = collect_allowed_paths(source, [{"id": "user:1", "text": "修复 Injector.cpp"}])
    payload = {
        "task_id": "t-l22",
        "task_instruction": "修复 Injector.cpp",
        "core_objective": "修复代码",
        "acceptance_obligations": [
            {"id": "obl-001", "text": "提交验收报告", "evidence_ref_ids": ["user:1"]},
            {"id": "obl-002", "text": "修复代码行为", "evidence_ref_ids": ["user:1"]},
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-002",
                "required_paths": ["Injector.cpp"],
                "observable": "修复后的行为通过测试",
                "verifier_kind": "FILE",
            }
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "t-l22", "domain_route": "terminal"},
        {"user:1"},
        allowed_paths=allowed,
        user_blob="修复 Injector.cpp",
        file_binding_paths=collect_file_binding_paths(source),
    )
    assert status == "REVIEW"
    assert "BINDING_REQUIRED:obl-001" in errors
    assert [item["obligation_id"] for item in gated["environment_bindings"]] == ["obl-002"]
    assert "obl-001" not in file_obligation_ids(gated)
