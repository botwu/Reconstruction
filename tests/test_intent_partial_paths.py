"""不把正文完整度当成意图绑定路径的准入条件。"""

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.pipeline import run_reconstruction
from traceforge.reconstruction.environment_bindings import normalize_environment_bindings

REQUEST = "增加智能识别表头功能，包含关键字即可匹配。"


class CaptureIntent:
    model_name = "fixture"
    backend = "fixture"

    def run(self, *, role, instruction, session, output_root):
        assert role.name == "intent"
        self.fields = {
            key: json.loads(value)
            for line in instruction.splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
            if key in {"FILE_BINDING_PATHS", "ALLOWED_OBSERVED_PATHS"}
        }
        return AgentResult(role=role.name, backend=self.backend, payload=None, completed=False)


def _read(call_id, path, *, partial=False, content="original\n"):
    return {
        "call_id": call_id, "name": "exec",
        "arguments": {"command": f"sed -n '1,2p' {path}" if partial else f"cat {path}"},
        "result_text": content,
    }


def _intent_candidates(tmp_path, timeline):
    runtime = CaptureIntent()
    source = {
        "entry_mode": "RAW_SESSION",
        "tasks": [{
            "task_id": "synthetic", "span_ids": ["span-1"],
            "message_indices": [0], "intake_selected": True, "domain_route": "terminal",
        }],
        "raw_session": {"messages": [{"role": "user", "content": REQUEST}]},
        "tool_timeline": timeline,
    }
    run_reconstruction(
        source=source,
        agent=runtime,
        output_root=tmp_path,
    )
    replay = json.loads((tmp_path / "tasks/synthetic/replay.json").read_text())
    return runtime.fields, replay


def test_partial_initial_source_reaches_intent_binding_without_claiming_complete(tmp_path: Path):
    fields, replay = _intent_candidates(tmp_path, [
        _read("source", "src/parser.py", partial=True),
        _read("support", "requirements.txt", content="pytest\n"),
    ])
    assert set(fields["FILE_BINDING_PATHS"]) == {"src/parser.py", "src/", "requirements.txt"}
    source_file = next(item for item in replay["files"] if item["path"] == "src/parser.py")
    assert source_file["completeness"] == "PARTIAL"
    assert source_file["content"] == "original\n"

    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": ["src/parser.py"], "observable": "表头关键字匹配符合要求",
        }]},
        [{"id": "o1", "text": REQUEST, "evidence_ref_ids": ["user:0"]}],
        fields["ALLOWED_OBSERVED_PATHS"], user_blob=REQUEST,

    )
    assert errors == []
    assert bindings[0]["initial_required_paths"] == ["src/parser.py"]
    assert bindings[0]["output_paths"] == []


@pytest.mark.parametrize("mutation", [
    {"call_id": "mutate", "name": "write",
     "arguments": {"path": "src/parser.py", "content": "solution\n"}, "result_text": "ok"},
    {"call_id": "mutate", "name": "exec",
     "arguments": {"command": "python -c 'change_files()'"}, "result_text": "ok"},
])
@pytest.mark.parametrize("observed_before_mutation", [False, True])
def test_mutation_boundary_preserves_only_prior_initial_body(
    tmp_path: Path, mutation, observed_before_mutation: bool,
):
    timeline = [_read("support", "requirements.txt", content="pytest\n")]
    if observed_before_mutation:
        timeline.append(_read("initial", "src/parser.py", partial=True))
    timeline.extend([mutation, _read("later", "src/parser.py", partial=True, content="solution\n")])
    fields, replay = _intent_candidates(tmp_path, timeline)
    assert ("src/parser.py" in fields["FILE_BINDING_PATHS"]) is observed_before_mutation
    files = {item["path"]: item for item in replay["files"]}
    if observed_before_mutation:
        assert files["src/parser.py"]["content"] == "original\n"
        assert files["src/parser.py"]["completeness"] == "PARTIAL"
    else:
        assert "src/parser.py" not in files
    assert "requirements.txt" in fields["FILE_BINDING_PATHS"]


def test_listing_absence_and_empty_partial_do_not_supply_initial_body(tmp_path: Path):
    fields, replay = _intent_candidates(tmp_path, [
        _read("support", "requirements.txt", content="pytest\n"),
        {"call_id": "listing", "name": "list_dir", "arguments": {"path": "."},
         "result_text": "listed.py\n"},
        {"call_id": "missing", "name": "read_file", "arguments": {"path": "absent.py"},
         "result_text": "File not found: absent.py"},
        _read("empty-range", "empty.py", partial=True, content=""),
    ])
    assert "listed.py" in fields["ALLOWED_OBSERVED_PATHS"]
    assert "absent.py" in fields["ALLOWED_OBSERVED_PATHS"]
    assert fields["FILE_BINDING_PATHS"] == ["requirements.txt"]
    assert any(
        item.get("reason") == "initial_read_not_found" for item in replay["partial_evidence"]
    )
