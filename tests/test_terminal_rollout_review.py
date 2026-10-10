"""后审完整接收真实正文，并拒绝旧回执与缺失终态。"""

import copy
import json

import pytest
from test_unassessed_terminal_rollout import _terminal_trial

from traceforge.harbor_ags.results import HarborResultError, read_native_trial
from traceforge.reconstruction.terminal_rollout_review import build_terminal_rollout_evidence
from traceforge.workspace_snapshot import collect_workspace


def _evidence(tmp_path, monkeypatch):
    task, root, full = _terminal_trial(tmp_path, monkeypatch)
    full.update(
        system_prompt="原生 solver 的系统提示", tools=[{"name": "terminal"}],
        messages=[{"role": "assistant", "content": "未省略的中间推断"}],
    )
    (root / "agent/trajectory.full.json").write_text(json.dumps(full))
    final = root / "artifacts/logs/artifacts/traceforge/workspace"
    modified = tmp_path / "modified"
    text = "真实源码\n" * 3000
    (modified / "large.py").write_text(text)
    (modified / "opaque.bin").write_bytes(b"\x00\xff\x01")
    collect_workspace(modified, final, initial_paths=["main.py"], output_paths=[])
    native = read_native_trial(root, expected_task=task)
    assert native["completed"], native["errors"]
    return native, root, full, text


def test_review_preserves_complete_evidence_and_marks_binary(tmp_path, monkeypatch):
    native, _, full, text = _evidence(tmp_path, monkeypatch)
    original = copy.deepcopy(native)
    evidence = build_terminal_rollout_evidence([native])
    result = evidence["trials"][0]
    assert len(evidence["evidence_refs"]) == 9
    assert "trial/messages/0" in evidence["evidence_refs"]
    assert "trial/tools/t1" in evidence["evidence_refs"]
    assert "trial/initial/main.py" in evidence["evidence_refs"]
    assert "trial/files/opaque.bin" in evidence["evidence_refs"]
    assert result["answer"] == native["answer"]
    assert result["tool_events"] == native["tool_events"]
    assert result["trajectory"] == native["trajectory"]
    for field in ("messages", "system_prompt", "tools"):
        assert result["trajectory"][field] == full[field]
    before = {row["path"]: row for row in result["initial_files"]}
    after = {row["path"]: row for row in result["final_files"]}
    assert before["main.py"]["content"] == "print('initial')\n"
    assert after["main.py"]["content"] == "print('changed')\n"
    assert after["large.py"]["content"] == text
    assert after["opaque.bin"]["content_kind"] == "binary"
    assert "content" not in after["opaque.bin"]
    assert after["opaque.bin"]["size_bytes"] == 3
    assert native == original


@pytest.mark.parametrize("failure", ["missing", "changed", "stale_receipt", "answer", "symlink"])
def test_review_rejects_missing_or_changed_evidence(tmp_path, monkeypatch, failure):
    native, root, _, _ = _evidence(tmp_path, monkeypatch)
    final = root / "artifacts/logs/artifacts/traceforge/workspace/main.py"
    if failure == "missing":
        (final.parent.parent / "workspace-collection.json").unlink()
    elif failure == "changed":
        final.write_text("篡改正文")
    elif failure == "stale_receipt":
        native["receipt"]["evidence_files"]["agent/trajectory.full.json"] = "0" * 64
    elif failure == "answer":
        native["answer"] = "伪造回答"
    else:
        content = final.read_bytes()
        final.unlink()
        external = tmp_path / "external.py"
        external.write_bytes(content)
        final.symlink_to(external)
    with pytest.raises(HarborResultError, match="TERMINAL_REVIEW"):
        build_terminal_rollout_evidence([native])


def test_review_does_not_silently_omit_trials(tmp_path, monkeypatch):
    native, _, _, _ = _evidence(tmp_path, monkeypatch)
    with pytest.raises(HarborResultError, match="TRIALS_MISSING"):
        build_terminal_rollout_evidence([])
    with pytest.raises(HarborResultError, match="TRIAL_PATH_INVALID"):
        build_terminal_rollout_evidence([native, native])


def test_review_detects_changed_full_conversation(tmp_path, monkeypatch):
    native, root, full, _ = _evidence(tmp_path, monkeypatch)
    full["messages"].append({"role": "assistant", "content": "未经绑定的中间回答"})
    (root / "agent/trajectory.full.json").write_text(json.dumps(full))
    with pytest.raises(HarborResultError, match="TERMINAL_REVIEW"):
        build_terminal_rollout_evidence([native])
