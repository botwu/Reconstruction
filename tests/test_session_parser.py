from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.model_gateway import ModelResponse
from traceforge.reconstruction.session_parser import (
    SessionParserError,
    indexed_system_messages,
    materialize_interpretation,
    parse_session_tools,
    reference_views,
)


def _event(index: int, operations: list, effect: str = "read_only") -> dict:
    return {"event_index": index, "effect": effect, "ordering": "parallel",
            "action": "按参数请求读取", "observation": "按实际返回配对",
            "excluded_content": [], "operations": operations}


def _excluded(start: int, end: int, *, block: int = 0, path: list | None = None) -> dict:
    return {"content_ref": {"block_index": block, "json_path": path or [],
                            "start_line": start, "end_line": end},
            "kind": "wrapper",
            "reason": "调用包装产生的分隔或状态信息，不是文件正文"}


def test_file_reference_requires_complete_nonoverlapping_return_accounting():
    timeline = [{"pending": False, "result_blocks": [
        {"index": 0, "text": "---file---\nfirst\nlast\n"},
    ]}]
    op = {**_read(), "partial": True, "file_start_line": 380,
          "content_ref": {"block_index": 0, "json_path": [], "start_line": 2, "end_line": 2}}
    event = _event(0, [op])
    event["excluded_content"] = [_excluded(1, 1)]
    interpretation = {"workspace_root": "/workspace", "events": [event]}
    with pytest.raises(SessionParserError, match="未解释.*3"):
        materialize_interpretation(timeline, interpretation)
    op["content_ref"]["end_line"] = 3
    parsed = materialize_interpretation(timeline, interpretation)[0]["session_parse"]
    assert parsed["file_ops"][0]["content"] == "first\nlast\n"
    assert parsed["file_ops"][0]["line_numbers"] == [380, 381]
    assert parsed["ordering"] == "parallel"
    op["content_ref"]["start_line"] = 1
    with pytest.raises(SessionParserError, match="重叠.*1"):
        materialize_interpretation(timeline, interpretation)
    op["content_ref"]["start_line"] = 2
    event["excluded_content"][0]["content_ref"].pop("end_line")
    with pytest.raises(SessionParserError, match="必须明确起止行"):
        materialize_interpretation(timeline, interpretation)


def test_requested_range_and_truncation_anchor_reject_adjacent_source_loss():
    timeline = [{"pending": False, "result_blocks": [
        {"index": 0, "text": "first\nlast\n\n…8 tokens truncated…\n"},
    ]}]
    op = {**_read(), "partial": True, "file_start_line": 220, "requested_range": [220, 221],
          "content_ref": {"block_index": 0, "json_path": [], "start_line": 1, "end_line": 3}}
    event = _event(0, [op])
    event["excluded_content"] = [{**_excluded(4, 4), "kind": "truncation",
                                  "truncation_marker": "…8 tokens truncated…"}]
    data = {"workspace_root": "/workspace", "events": [event]}
    with pytest.raises(SessionParserError, match="最多返回 2 行，实际引用 3 行"):
        materialize_interpretation(timeline, data)
    op["content_ref"]["end_line"] = 2
    event["excluded_content"].append(_excluded(3, 3))
    parsed = materialize_interpretation(timeline, data)
    assert parsed[0]["session_parse"]["file_ops"][0]["content"] == "first\nlast\n"
    event["excluded_content"][0]["content_ref"]["start_line"] = 2
    with pytest.raises(SessionParserError, match="没有标记.*2"):
        materialize_interpretation(timeline, data)


def _read(path: str = "lib/a.py") -> dict:
    return {"kind": "file_text", "path": path, "partial": False, "file_start_line": 1,
            "requested_range": None,
            "content_ref": {"block_index": 1, "json_path": ["output"],
                            "start_line": 1, "end_line": None}}


def _source() -> dict:
    return {
        "domain_route": "terminal",
        "raw_session": {"messages": [{"role": "user", "content": "理解原任务"}]},
        "selected_span_ids": ["s1"],
        "tool_timeline": [
            {"call_id": "c0", "name": "another_harness.exec", "span_id": "s1",
             "arguments": {"input": "读取工作簿表头"}, "pending": False,
             "result_blocks": [{"index": 0, "text": '["店铺7.23 1.0"]'}]},
            {"call_id": "c1", "name": "another_harness.exec", "span_id": "s1",
             "arguments": {"input": "读取源码"}, "pending": False,
             "result_blocks": [{"index": 0, "text": "工具包装"},
                               {"index": 1, "text": json.dumps({"output": "x = 1\r\n"})}]},
        ],
    }


def _interpretation() -> dict:
    return {"workspace_root": "/workspace/project",
            "events": [_event(0, []), _event(1, [_read()])]}


class _Model:
    def __init__(self, payload: dict):
        self.payload = payload
        self.request = None

    def complete(self, request):
        self.request = request
        return ModelResponse(request.request_id, request.model, "fixture",
                             json.dumps(self.payload), 1, 0.1, 100, 50)


def test_system_messages_preserve_repeats_roles_and_nested_fields() -> None:
    message = {"role": "system", "content": [{"text": "  必须保留原约束\n"}],
               "reasoning_content": "历史字段"}
    raw = {"messages": [message, {"role": "user", "content": "任务"},
                        copy.deepcopy(message), {"role": "developer", "content": "后续约束"}]}
    before = copy.deepcopy(raw)
    result = indexed_system_messages(raw)
    assert result == [{"message_index": i, "message": raw["messages"][i]} for i in (0, 2, 3)]
    result[0]["message"]["content"][0]["text"] = "changed"
    assert raw == before
    assert indexed_system_messages(None) == []


def test_model_readonly_interpretation_preserves_bytes_and_later_files(tmp_path: Path) -> None:
    source = _source()
    source["raw_session"]["messages"][:0] = [
        {"role": "system", "content": "原始工具协议：包装调用会返回多个结果块。"},
        {"role": "developer", "content": "原环境声明只读；无返回时执行结果未知。"},
    ]
    source["raw_session"]["messages"].append({
        "role": "assistant", "content": "原始说明", "reasoning_content": "原始轨迹参考",
    })
    source["raw_session"]["meta"] = {"reasoning": {"effort": "high"}}
    before = copy.deepcopy(source)
    notes = [{"message_indices": [0, 1],
              "interpretation": "返回块需按包装协议对应；只读是权限声明，不证明调用结果。"}]
    model = _Model({**_interpretation(), "system_context": notes})
    parsed = parse_session_tools(source=source, model=model, output_root=tmp_path / "parse")
    replay = replay_from_timeline(parsed["tool_timeline"], tmp_path / "workspace")
    assert [f.path for f in replay.files] == ["lib/a.py"]
    assert (tmp_path / "workspace/lib/a.py").read_bytes() == b"x = 1\r\n"
    assert source == before
    assert json.loads((tmp_path / "parse/receipt.json").read_text())["status"] == "READY"
    assert parsed["domain_route"] == "terminal"
    assert parsed["raw_session"] == before["raw_session"]
    assert parsed["session_parser"]["system_context"] == notes
    saved = json.loads((tmp_path / "parse/interpretation.json").read_text())
    assert saved["system_context"] == notes
    context = json.loads(model.request.prompt)
    assert context["system_message_indices"] == [0, 1]
    assert context["domain_route"] == "terminal"
    assert context["session"]["messages"] == [
        {"message_index": i, "message": m}
        for i, m in enumerate(before["raw_session"]["messages"])
    ]
    assert context["session"]["meta"] == before["raw_session"]["meta"]
    assert '"domain":' not in model.request.system


@pytest.mark.parametrize("notes", [
    [],
    [{"message_indices": [0], "interpretation": "遗漏第二条系统指令"}],
    [{"message_indices": [0, 1, 2], "interpretation": "错误引用用户消息"}],
    [{"message_indices": [0, 1], "interpretation": " "}],
])
def test_system_context_requires_complete_original_references(tmp_path: Path, notes: list) -> None:
    source = _source()
    source["raw_session"]["messages"][:0] = [
        {"role": "system", "content": "原始协议"},
        {"role": "developer", "content": "原始环境"},
    ]
    with pytest.raises(SessionParserError, match="system_context"):
        parse_session_tools(source=source, output_root=tmp_path,
                            model=_Model({**_interpretation(), "system_context": notes}))
    assert not (tmp_path / "interpretation.json").exists()
    assert json.loads((tmp_path / "receipt.json").read_text())["status"] == "ERROR"


def test_model_cannot_override_input_domain(tmp_path: Path) -> None:
    source = _source()
    source["domain_route"] = "retrieval"
    model = _Model({**_interpretation(), "domain": "terminal"})
    parsed = parse_session_tools(source=source, model=model, output_root=tmp_path)
    assert parsed["domain_route"] == "retrieval"
    assert json.loads(model.request.prompt)["domain_route"] == "retrieval"
    assert "domain" not in parsed["session_parser"]


@pytest.mark.parametrize("change", [
    "missing_event", "wrong_block", "outside_path", "wrong_range", "invalid_effect",
    "ambiguous_read",
])
def test_invalid_model_references_stop_without_rule_fallback(tmp_path: Path, change: str) -> None:
    data = _interpretation()
    op = data["events"][1]["operations"][0]
    if change == "missing_event":
        data["events"].pop()
    elif change == "wrong_block":
        op["content_ref"]["block_index"] = 9
    elif change == "outside_path":
        op["path"] = "../secret"
    elif change == "invalid_effect":
        data["events"][1]["effect"] = []
    elif change == "ambiguous_read":
        op["kind"] = "read"
    else:
        op["content_ref"]["end_line"] = 9
    with pytest.raises(SessionParserError):
        parse_session_tools(source=_source(), model=_Model(data), output_root=tmp_path)
    assert len(list(tmp_path.glob("parser/attempt-*/response.txt"))) == 2
    assert json.loads((tmp_path / "receipt.json").read_text())["status"] == "ERROR"
    assert not (tmp_path / "parsed_timeline.json").exists()


def test_parallel_read_write_cannot_supply_initial_bytes() -> None:
    source, data = _source(), _interpretation()
    data["events"][1] = _event(1, [_read(), {"kind": "write", "path": "lib/a.py"}], "mutation")
    replay = replay_from_timeline(materialize_interpretation(source["tool_timeline"], data))
    assert not replay.files
    assert any(e["reason"] == "read_after_unparsed_mutation" for e in replay.partial_evidence)


def test_unknown_operation_keeps_mutation_barrier_without_command_regex() -> None:
    data = _interpretation()
    data["events"][0]["effect"] = "unknown"
    replay = replay_from_timeline(materialize_interpretation(_source()["tool_timeline"], data))
    assert not replay.files


def test_failed_nested_result_cannot_become_file() -> None:
    source = _source()
    source["tool_timeline"][1]["result_blocks"][1]["text"] = json.dumps({
        "exit_code": 1, "output": "错误信息，不是文件正文\n",
    })
    with pytest.raises(SessionParserError, match="失败的子调用"):
        materialize_interpretation(source["tool_timeline"], _interpretation())


def test_pending_patch_is_visible_for_semantics_but_never_materialized(tmp_path: Path) -> None:
    source, data = _source(), _interpretation()
    patch = "HIDDEN_FINAL_PATCH"
    source["raw_session"]["messages"].append({
        "role": "assistant", "tool_calls": [{"id": "p", "arguments": patch}],
    })
    source["tool_timeline"].append({
        "call_id": "p", "name": "apply_patch", "arguments": patch,
        "assistant_message_index": 1, "pending": True, "result_blocks": [],
    })
    data["events"].append(_event(2, [], "pending"))
    model = _Model(data)
    parsed = parse_session_tools(source=source, model=model, output_root=tmp_path)
    assert patch in model.request.prompt
    assert model.request.max_tokens == 128000
    assert parsed["tool_timeline"][2]["session_parse"]["file_ops"] == []
    assert source["tool_timeline"][2]["arguments"] == patch


def test_plain_text_harness_uses_exact_partial_reference() -> None:
    timeline = [{"call_id": "native", "name": "read", "pending": False,
                 "result_blocks": [{"index": 0, "text": "wrapper\n    value\nfooter\n"}]}]
    op = _read()
    op.update(partial=True, file_start_line=161)
    op["content_ref"] = {"block_index": 0, "json_path": [], "start_line": 2, "end_line": 2}
    data = {**_interpretation(), "events": [_event(0, [op])]}
    data["events"][0]["excluded_content"] = [_excluded(1, 1), _excluded(3, 3)]
    parsed = materialize_interpretation(timeline, data)[0]["session_parse"]["file_ops"][0]
    assert parsed["content"] == "    value\n"
    assert parsed["line_numbers"] == [161]
    assert parsed["total_lines"] is None


def test_numbered_reference_error_identifies_file_and_return_boundary() -> None:
    source, data = _source(), _interpretation()
    source["tool_timeline"][1]["result_blocks"][1]["text"] = json.dumps({
        "output": "1\tx = 1\n---next file---\n1\ty = 2\n",
    })
    op = data["events"][1]["operations"][0]
    op.update(path=None, source_path="../adjacent/a.py")
    op["content_ref"]["line_number_separator"] = "\t"
    with pytest.raises(SessionParserError) as error:
        materialize_interpretation(source["tool_timeline"], data)
    message = str(error.value)
    assert "../adjacent/a.py" in message
    assert "block_index=1" in message and "['output']" in message
    assert "返回第 2 行" in message and "期望显示行号 2" in message


@pytest.mark.parametrize("native_header", [True, False])
def test_truncated_exec_wrapper_is_indexed_and_cannot_enter_file_content(native_header: bool) -> None:
    body = ("Chunk ID: abc\nWall time: 0.001 seconds\nProcess exited with code 0\n"
            "Original token count: 100\nOutput:\n"
            "Warning: truncated output (original token count: 100)\nTotal output lines: 20\n\n"
            "first = 1\nbroken…3 tokens truncated…tail\nlast = 2\n")
    shift = 0 if native_header else 5
    if shift:
        body = "\n".join(body.splitlines()[shift:]) + "\n"
    timeline = [{"call_id": "native", "pending": False,
                 "result_blocks": [{"index": 0, "text": body}]}]
    assert reference_views(timeline)[0]["non_source_lines"] == [*range(1, 9 - shift), 10 - shift]
    op = {**_read(), "partial": True,
          "content_ref": {"block_index": 0, "json_path": [], "start_line": 9 - shift, "end_line": 9 - shift}}
    data = {"workspace_root": "/workspace", "events": [_event(0, [op])]}
    tail = copy.deepcopy(op)
    tail["content_ref"].update(start_line=11 - shift, end_line=11 - shift)
    tail["file_start_line"] = None
    data["events"][0]["operations"].append(tail)
    data["events"][0]["excluded_content"] = [
        _excluded(1, 8 - shift), _excluded(10 - shift, 10 - shift),
    ]
    read = materialize_interpretation(timeline, data)[0]["session_parse"]["file_ops"][0]
    assert read["content"] == "first = 1\n"
    for start, end in [(6, 9), (9, 11)]:
        op["content_ref"].update(start_line=start - shift, end_line=end - shift)
        with pytest.raises(SessionParserError, match="包装或截断.*返回行"):
            materialize_interpretation(timeline, data)
    # 没有已识别的工具包装时，正文中的相同字样必须原样保留。
    timeline[0]["result_blocks"][0]["text"] = "example = '…3 tokens truncated…'\n"
    op["content_ref"].update(start_line=1, end_line=1)
    data["events"][0]["operations"] = [op]
    data["events"][0]["excluded_content"] = []
    read = materialize_interpretation(timeline, data)[0]["session_parse"]["file_ops"][0]
    assert read["content"] == "example = '…3 tokens truncated…'\n"
    assert reference_views(timeline)[0]["non_source_lines"] == []


def test_numbered_ranges_are_format_hints_without_file_semantics() -> None:
    timeline = [{"result_blocks": [{"index": 0, "text":
        "head\n0\talpha\n1\tbeta\n---next---\n8: gamma\n9: delta\n12: gap\n"}]}]
    view = reference_views(timeline)[0]
    assert view["numbered_ranges"] == [
        {"start_line": 2, "end_line": 3, "display_start_line": 0, "line_number_separator": "\t"},
        {"start_line": 5, "end_line": 6, "display_start_line": 8, "line_number_separator": ": "},
        {"start_line": 7, "end_line": 7, "display_start_line": 12, "line_number_separator": ": "},
    ]
    assert "path" not in view and "kind" not in view


@pytest.mark.parametrize("partial,first", [(False, 1), (True, 18)])
def test_zero_based_read_preserves_first_line_and_file_coordinates(tmp_path: Path, partial, first):
    source, data = _source(), _interpretation()
    body = f"{first - 1}\tdef f():\r\n{first}\t    return 1\r\n"
    source["tool_timeline"][1]["result_blocks"][1]["text"] = json.dumps({"output": body})
    op = data["events"][1]["operations"][0]
    op.update(partial=partial, file_start_line=first)
    op["content_ref"].update(line_number_separator="\t", line_number_base=0)
    parsed = materialize_interpretation(source["tool_timeline"], data)
    read = parsed[1]["session_parse"]["file_ops"][0]
    assert read["content"] == "def f():\r\n    return 1\r\n"
    if partial:
        assert read["line_numbers"] == [18, 19]
    else:
        replay_from_timeline(parsed, tmp_path)
        assert (tmp_path / "lib/a.py").read_bytes() == b"def f():\r\n    return 1\r\n"
    op["content_ref"]["line_number_base"] = 1
    with pytest.raises(SessionParserError, match="行号"):
        materialize_interpretation(source["tool_timeline"], data)


@pytest.mark.parametrize("source_path", ["../adjacent/a.py", "abc123^:lib/a.py"])
def test_reference_file_is_preserved_without_workspace_materialization(tmp_path: Path, source_path: str):
    source, data = _source(), _interpretation()
    op = data["events"][1]["operations"][0]
    op.update(path=None, source_path=source_path)
    before = copy.deepcopy(source)
    parsed = parse_session_tools(source=source, model=_Model(data), output_root=tmp_path / "parser")
    observation = parsed["tool_timeline"][1]["session_parse"]
    assert observation["file_ops"] == []
    assert observation["reference_file_ops"][0]["source_path"] == source_path
    assert observation["reference_file_ops"][0]["content"] == "x = 1\r\n"
    assert not replay_from_timeline(parsed["tool_timeline"], tmp_path / "workspace").files
    assert source == before
    op.pop("source_path")
    with pytest.raises(SessionParserError, match="source_path"):
        materialize_interpretation(source["tool_timeline"], data)


def test_external_clone_does_not_hide_observed_workspace_files():
    source, data = _source(), _interpretation()
    data["events"][0] = _event(0, [{"kind": "write", "path": None,
                                    "source_path": "/tmp/cloned-repo"}], "mutation")
    parsed = materialize_interpretation(source["tool_timeline"], data)
    assert [f.path for f in replay_from_timeline(parsed).files] == ["lib/a.py"]
    assert parsed[0]["session_parse"]["reference_file_ops"][0]["kind"] == "write"


@pytest.mark.parametrize("first", [None, 1, 2])
def test_complete_read_needs_no_redundant_line_origin(tmp_path: Path, first) -> None:
    source, data = _source(), _interpretation()
    data["events"][1]["operations"][0]["file_start_line"] = first
    if first == 2:
        with pytest.raises(SessionParserError, match="全文读取"):
            materialize_interpretation(source["tool_timeline"], data)
        return
    parsed = materialize_interpretation(source["tool_timeline"], data)
    replay_from_timeline(parsed, tmp_path)
    assert (tmp_path / "lib/a.py").read_bytes() == b"x = 1\r\n"


@pytest.mark.parametrize("first", [None, 1])
def test_numbered_shell_output_preserves_source_bytes_and_replay(tmp_path: Path, first) -> None:
    source, data = _source(), _interpretation()
    source["tool_timeline"][1]["result_blocks"][1]["text"] = json.dumps({
        "main": "Exit code: 0\nWall time: 0.2 seconds\nOutput:\n"
                "   1: def existing():\r\n   2:     return 1\r\n   3: \r\n",
    })
    op = data["events"][1]["operations"][0]
    op["file_start_line"] = first
    op["content_ref"].update(json_path=["main"], start_line=4, end_line=6,
                             line_number_separator=": ")
    data["events"][1]["excluded_content"] = [_excluded(1, 3, block=1, path=["main"])]
    before = copy.deepcopy(source)
    parsed = materialize_interpretation(source["tool_timeline"], data)
    replay_from_timeline(parsed, tmp_path)
    assert (tmp_path / "lib/a.py").read_bytes() == b"def existing():\r\n    return 1\r\n\r\n"
    assert source == before


def test_wrong_request_range_cannot_discard_numbered_source_tail():
    source, data = _source(), _interpretation()
    item = source["tool_timeline"][1]
    item["arguments"] = {"cmd": "nl -ba lib/a.py | sed -n '25,27p'"}
    item["result_blocks"][1]["text"] = json.dumps({"output": "25\ta\n26\tb\n27\tc\n"})
    event = data["events"][1]
    op = event["operations"][0]
    op.update(partial=True, file_start_line=25, requested_range=[25, 26])
    op["content_ref"].update(start_line=1, end_line=2, line_number_separator="\t")
    event["excluded_content"] = [_excluded(3, 3, block=1, path=["output"])]
    with pytest.raises(SessionParserError, match="连续编号正文段.*被部分排除"):
        materialize_interpretation(source["tool_timeline"], data)
    op["content_ref"]["end_line"] = 3
    event["excluded_content"] = []
    with pytest.raises(SessionParserError, match="先核对 requested_range.*25,27p"):
        materialize_interpretation(source["tool_timeline"], data)
    op["requested_range"] = [25, 27]
    read = materialize_interpretation(source["tool_timeline"], data)[1]["session_parse"]["file_ops"][0]
    assert read["content"] == "a\nb\nc\n"
    assert read["line_numbers"] == [25, 26, 27]


def test_reference_index_exposes_nested_positions_without_rewriting_or_selecting_source():
    text = "Exit code: 0\nOutput:\n   1:     原文\r\n   2: \r\n"
    timeline = [{"result_blocks": [{"index": 2, "text": json.dumps({"results": [text]})}]},
                {"pending": True, "result_blocks": []}]
    before = copy.deepcopy(timeline)
    views = reference_views(timeline)
    assert views[0] == {"event_index": 0, "block_index": 2, "json_path": [],
                        "line_count": 1, "json_container": True}
    assert views[1] == {"event_index": 0, "block_index": 2, "json_path": ["results", 0],
                        "line_count": 4, "lines": list(enumerate(text.splitlines(keepends=True), 1)),
                        "non_source_lines": [], "numbered_ranges": [
                            {"start_line": 3, "end_line": 4, "display_start_line": 1,
                             "line_number_separator": ": "}]}
    assert timeline == before


@pytest.mark.parametrize("text,first,separator", [
    ("  2: x\n  4: y\n", 2, ": "),
    ("  2: x\n  3: y\n", 1, ": "),
    ("  2: x\nfooter\n", 2, ": "),
    ("  2: x\n", 0, ": "),
    ("  2: x\n", 2, ""),
])
def test_numbered_reference_rejects_gaps_wrappers_and_wrong_origin(text, first, separator):
    source, data = _source(), _interpretation()
    source["tool_timeline"][1]["result_blocks"][1]["text"] = json.dumps({"output": text})
    op = data["events"][1]["operations"][0]
    op.update(partial=True, file_start_line=first)
    op["content_ref"]["line_number_separator"] = separator
    with pytest.raises(SessionParserError, match="行号"):
        materialize_interpretation(source["tool_timeline"], data)


def test_search_observation_does_not_become_file_workspace() -> None:
    data = {**_interpretation(), "domain": "retrieval", "workspace_root": None,
            "events": [_event(0, []), _event(1, [])]}
    assert not replay_from_timeline(
        materialize_interpretation(_source()["tool_timeline"], data)
    ).files


def test_structural_correction_preserves_source_and_attempts(tmp_path: Path) -> None:
    wrong = _interpretation()
    wrong["events"][1]["operations"][0]["content_ref"]["block_index"] = 99

    class RepairModel(_Model):
        def complete(self, request):
            if "validation_feedback" in json.loads(request.prompt):
                self.payload = _interpretation()
            return super().complete(request)

    model = RepairModel(wrong)
    source = _source()
    before = copy.deepcopy(source)
    result = parse_session_tools(source=source, model=model, output_root=tmp_path)
    assert source == before
    assert len(list(tmp_path.glob("parser/attempt-*/response.txt"))) == 2
    assert result["session_parser"]["status"] == "READY"
    assert "content_ref" in json.loads(model.request.prompt)["validation_feedback"]["error"]


def test_reference_feedback_identifies_all_failed_events() -> None:
    data = _interpretation()
    data["events"][0]["operations"] = [_read("first.py")]
    data["events"][1]["operations"][0]["content_ref"]["end_line"] = 999
    with pytest.raises(SessionParserError) as caught:
        materialize_interpretation(_source()["tool_timeline"], data)
    assert "event_index=0" in str(caught.value)
    assert "event_index=1" in str(caught.value)


def test_unclosed_json_string_preserves_complete_lines_and_rejects_missing_tail():
    body = "Output:\n 1: first = 1\r\n 2: second = 2\r\n 3: incomplete"
    raw = '{"results":[' + json.dumps(body)[:-1]
    offset = len('{"results":[')
    timeline = [{"pending": False, "result_blocks": [{"index": 0, "text": raw}]}]
    views = reference_views(timeline)
    view = next(v for v in views if v.get("json_string_start") == offset)
    assert view["lines"][1][1] == " 1: first = 1\r\n"
    assert view["non_source_lines"] == [4]
    ref = {"block_index": 0, "json_path": [], "json_string_start": offset,
           "start_line": 2, "end_line": 3, "line_number_separator": ": "}
    event = _event(0, [{**_read(), "partial": True, "content_ref": ref}])
    event["excluded_content"] = [
        {"content_ref": {**ref, "start_line": n, "end_line": n}, "kind": "observation",
         "reason": "输出头或末尾未闭合字符串中的不完整行"} for n in (1, 4)
    ]
    data = {"events": [event], "workspace_root": "/work"}
    parsed = materialize_interpretation(timeline, data)
    op = parsed[0]["session_parse"]["file_ops"][0]
    assert op["content"] == "first = 1\r\nsecond = 2\r\n"
    assert op["line_numbers"] == [1, 2]
    assert timeline[0]["result_blocks"][0]["text"] == raw
    event["operations"][0]["partial"] = False
    with pytest.raises(SessionParserError, match="截断 JSON.*partial"):
        materialize_interpretation(timeline, data)
    event["operations"][0]["partial"] = True
    ref["end_line"] = 4
    with pytest.raises(SessionParserError, match="不完整"):
        materialize_interpretation(timeline, data)
    ref["end_line"] = 3
    ref["json_string_start"] = offset + 1
    with pytest.raises(SessionParserError, match="起点"):
        materialize_interpretation(timeline, data)


@pytest.mark.parametrize("raw", ['{"x":"bad\\q', '{"x": nope}', '{"x":"complete"}'])
def test_invalid_or_complete_json_is_not_repaired_as_unclosed_string(raw):
    views = reference_views([{"result_blocks": [{"index": 0, "text": raw}]}])
    assert not any("json_string_start" in v for v in views)
