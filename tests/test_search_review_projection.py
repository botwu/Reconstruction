"""复核请求只共享完全相同的字节；原始资料与完整求解轨迹不变。"""

import copy
import json
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import search_environment as module
from traceforge.reconstruction.agents import AgentSession


def serialized(value):
    return json.dumps(value, ensure_ascii=False).encode()


def pointer(root, path):
    for part in path[1:].split("/") if path else []:
        part = part.replace("~1", "/").replace("~0", "~")
        root = root[int(part)] if isinstance(root, list) else root[part]
    return root


def expand(request):
    restored = copy.deepcopy(request)
    registration = restored.pop("request_references", None)
    if registration is None:
        return restored
    for target, source in registration["paths"].items():
        parent, key = target.rsplit("/", 1)
        owner = pointer(restored, parent)
        key = (int(key) if isinstance(owner, list)
               else key.replace("~1", "/").replace("~0", "~"))
        assert owner[key] == {"request_ref": source}
        owner[key] = copy.deepcopy(pointer(restored, source))
    return restored


def run_review(tmp_path, request, *, completion=False):
    original = copy.deepcopy(request)
    observed = {}

    def run(**kwargs):
        observed.update(json.loads(kwargs["instruction"]))
        return SimpleNamespace(completed=False, errors=["MODEL_RATE_LIMIT"])

    module._run_search_review(
        source={"raw_session": {"messages": []}}, request=request, task={},
        agent=SimpleNamespace(run=run), session=AgentSession(),
        role=module.SEARCH_REVIEW_ROLE, output_root=tmp_path, completion=completion,
    )
    assert request == original
    return observed


def fixture_request():
    body = "原始资料，不代表已经阅读。\n" * 200
    result = json.dumps({"text": body, "actual_window": "仅求解者所见"}, ensure_ascii=False)
    page = {"url": "https://example.org/guide", "raw_sha256": "原版本",
            "text": body, "links": [{"text": body, "url": "https://example.org/section"}]}
    return {
        "SOURCE_SESSION": {"messages": [{"role": "tool", "content": body}]},
        "environment": {"captures": [{"result_text": body, "call_id": "原调用"}],
                        "live_references": [page]},
        "trials": [{"trial": "原试次", "tool_events": [
            {"tool_call_id": "read-1", "result": result, "ok": True}],
            "trajectory": {"messages": [
                {"role": "tool", "tool_call_id": "read-1", "content": result}]}}],
        "actual_web_calls": [{"url": page["url"], "raw_sha256": "原版本",
                             "text": body, "links": copy.deepcopy(page["links"])}],
    }


@pytest.mark.parametrize("completion", [False, True])
def test_duplicate_derived_values_expand_to_exact_full_request(tmp_path, completion):
    request = fixture_request()
    observed = run_review(tmp_path, request, completion=completion)
    assert "request_references" in observed
    assert observed["trials"][0]["tool_events"][0]["result"] == {
        "request_ref": "/trials/0/trajectory/messages/0/content",
    }
    assert observed["SOURCE_SESSION"] == request["SOURCE_SESSION"]
    assert observed["environment"]["live_references"] == request["environment"]["live_references"]
    assert observed["trials"][0]["trajectory"] == request["trials"][0]["trajectory"]
    assert serialized(expand(observed)) == serialized(request)
    assert len(serialized(observed)) < len(serialized(request))


def test_changed_values_and_json_types_or_key_order_are_not_merged(tmp_path):
    request = fixture_request()
    value = "相同的长字符串" * 200
    request["SOURCE_SESSION"]["values"] = [
        {"a": value, "b": 1}, {"value": value, "flag": True},
    ]
    request["derived"] = [
        {"b": 1, "a": value}, {"value": value, "flag": 1},
        value + "不同末尾", value[:-1],
    ]
    observed = run_review(tmp_path, request)
    assert isinstance(observed["derived"][0], dict)
    assert list(observed["derived"][0]) == ["b", "a"]
    assert observed["derived"][1]["flag"] == 1
    assert type(observed["derived"][1]["flag"]) is int
    assert observed["derived"][2:] == request["derived"][2:]
    assert serialized(expand(observed)) == serialized(request)


def test_same_bytes_do_not_merge_source_or_version_metadata(tmp_path):
    request = fixture_request()
    request["environment"]["live_references"].append({
        **copy.deepcopy(request["environment"]["live_references"][0]),
        "url": "https://example.org/other-version", "raw_sha256": "另一个版本",
    })
    request["actual_web_calls"][0]["raw_sha256"] = "又一个版本"
    observed = run_review(tmp_path, request)
    assert observed["environment"]["live_references"] == request["environment"]["live_references"]
    assert observed["actual_web_calls"][0]["raw_sha256"] == "又一个版本"
    assert len(observed["actual_web_calls"]) == 1
    assert serialized(expand(observed)) == serialized(request)
    assert "不等于同一来源" in observed["request_references"]["description"]


def test_existing_reference_shapes_are_original_data_not_generated_refs(tmp_path):
    request = fixture_request()
    original_shape = {"request_ref": "/does/not/exist"}
    request["derived_original"] = original_shape
    request["SOURCE_SESSION"]["original_shape"] = original_shape
    observed = run_review(tmp_path, request)
    assert observed["derived_original"] == original_shape
    assert "/derived_original" not in observed["request_references"]["paths"]
    assert expand(observed)["derived_original"] == original_shape
    assert serialized(expand(observed)) == serialized(request)


def test_reserved_root_field_disables_projection_without_overwriting_data(tmp_path):
    request = fixture_request()
    request["request_references"] = {"paths": {"原字段": "不能当新生成引用"}}
    assert run_review(tmp_path, request) == request


def test_escaped_json_pointer_and_repeat_anchor_values_round_trip(tmp_path):
    value = "完整中文字段" * 200
    request = {"SOURCE_SESSION": {"a~/b": value, "same": value},
               "environment": {"live_references": []}, "x/y~": value}
    observed = run_review(tmp_path, request)
    assert observed["request_references"]["paths"]["/x~1y~0"] == "/SOURCE_SESSION/a~0~1b"
    assert serialized(expand(observed)) == serialized(request)


@pytest.mark.parametrize("value", ["短正文", "保留完整" * 40])
def test_small_values_and_unprofitable_total_keep_full_request(tmp_path, value):
    request = {"SOURCE_SESSION": {"text": value},
               "environment": {"live_references": []}, "derived": value}
    assert run_review(tmp_path, request) == request


def test_empty_json_pointer_segments_remain_original_fields(tmp_path):
    value = "必须保留空字段名" * 200
    request = {"SOURCE_SESSION": {"": {"": value}},
               "derived": {"": {"": value}}}
    observed = run_review(tmp_path, request)
    assert serialized(expand(observed)) == serialized(request)
