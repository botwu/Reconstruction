"""通过 selector CLI 验证候选归类、覆盖审计和原始数据保护。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/select_terminal_domain.py"


def raw_record(name: str, arguments: object) -> bytes:
    return (
        json.dumps(
            {
                "domain_meta": {
                    "rubric": {"primary": {"code": "R04"}},
                    "operation_risk": {"code": "read_only"},
                },
                "messages": [
                    {"role": "user", "content": "检查本地项目"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": name, "arguments": arguments}}
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def select(source: Path, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(source), "--output", str(output), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def manifest(output: Path) -> dict:
    return json.loads(output.with_name(output.name + ".manifest.json").read_text())


@pytest.mark.parametrize(
    ("name", "arguments", "selected", "retrieval_only"),
    [
        ("functions.exec", {"code": "await tools.web_search({q: 'parser'});"}, 0, 1),
        ("functions.exec", {"code": "text('terminal');"}, 0, 0),
        ("exec", {}, 0, 0),
        ("functions.exec", {"code": "await tools.exec_command({cmd: 'pwd'});"}, 1, 0),
        ("exec", {"cmd": "ls src"}, 1, 0),
        ("functions.exec", json.dumps({"input": "await tools.read_file({path: 'a.py'});"}), 1, 0),
    ],
)
def test_exec_wrapper_requires_actual_terminal_call(
    tmp_path: Path, name: str, arguments: object, selected: int, retrieval_only: int
) -> None:
    source = tmp_path / "R04.jsonl"
    original = raw_record(name, arguments)
    source.write_bytes(original)
    output = tmp_path / "selected.jsonl"
    result = select(source, output)
    assert result.returncode == 0, result.stderr
    audit = manifest(output)
    assert audit["counts"].get("selected", 0) == selected
    assert audit["counts"].get("retrieval_only", 0) == retrieval_only
    assert output.read_bytes() == (original if selected else b"")
    assert source.read_bytes() == original
    assert audit["policy"]["eligibility_decision"] == "NOT_RUN"


@pytest.mark.parametrize("complete", [True, False])
def test_distribution_coverage_uses_actual_source_bytes(tmp_path: Path, complete: bool) -> None:
    source = tmp_path / "R04.jsonl"
    row = raw_record("exec_command", {"cmd": "pwd"})
    upstream = row if complete else row + row
    source.write_bytes(row)
    (tmp_path / "distribution.json").write_text(
        json.dumps(
            {
                "distribution": [
                    {
                        "code": "R04", "records": 1 if complete else 2,
                        "bytes": len(upstream),
                        "sha256": hashlib.sha256(upstream).hexdigest(),
                    }
                ]
            }
        )
    )
    output = tmp_path / "selected.jsonl"
    result = select(source, output)
    assert result.returncode == 0, result.stderr
    audit = manifest(output)["sources"][0]
    assert audit["physical_lines"] == 1
    assert audit["bytes"] == len(row)
    assert audit["coverage_complete"] is complete
    assert audit["matches_index"] is complete


def test_selector_scans_bad_final_json_even_after_candidate_limit(tmp_path: Path) -> None:
    source = tmp_path / "R04.jsonl"
    row = raw_record("exec_command", {"cmd": "pwd"})
    original = row + row + b'{"messages":'
    source.write_bytes(original)
    output = tmp_path / "selected.jsonl"
    result = select(source, output, "--limit", "1")
    assert result.returncode == 0, result.stderr
    audit = manifest(output)
    assert output.read_bytes() == row
    assert audit["counts"]["selected"] == 1
    assert audit["counts"]["limit_reached"] == 1
    assert audit["counts"]["invalid_json"] == 1
    assert audit["sources"][0]["physical_lines"] == 3
    assert audit["sources"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original


@pytest.mark.parametrize("destination", ["source", "existing_output", "existing_manifest"])
def test_selector_refuses_overwriting_any_existing_artifact(
    tmp_path: Path, destination: str
) -> None:
    source = tmp_path / "R04.jsonl"
    original = raw_record("exec_command", {"cmd": "pwd"})
    source.write_bytes(original)
    output = source if destination == "source" else tmp_path / "selected.jsonl"
    protected = output.with_name(output.name + ".manifest.json")
    if destination == "existing_output":
        output.write_bytes(b"PREVIOUS_OUTPUT")
    elif destination == "existing_manifest":
        protected.write_bytes(b"PREVIOUS_MANIFEST")
    result = select(source, output)
    assert result.returncode != 0
    assert source.read_bytes() == original
    if destination == "existing_output":
        assert output.read_bytes() == b"PREVIOUS_OUTPUT"
    elif destination == "existing_manifest":
        assert protected.read_bytes() == b"PREVIOUS_MANIFEST"
        assert not output.exists()
