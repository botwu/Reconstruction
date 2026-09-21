from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/inventory_terminal_candidates.py"


def _record(user: str, *, command: str = "cat src/app.py") -> bytes:
    payload = {
        "domain_meta": {
            "rubric": {"primary": {"code": "R04"}},
            "operation_risk": {"code": "local_reversible_write"},
        },
        "messages": [
            {"role": "user", "content": user},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "exec_command",
                            "arguments": {"cmd": command},
                        },
                    }
                ],
            },
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode() + b"\n"


def _run(source: Path, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(source), "--output", str(output), *extra],
        text=True,
        capture_output=True,
        check=False,
    )


def test_inventory_is_bounded_redacted_and_manifest_grounded(tmp_path: Path) -> None:
    first = _record("请修复 src/app.py，token=SECRET_VALUE_12345678")
    second = _record("请查看 src/other.py", command="git status --short")
    source = tmp_path / "selected.jsonl"
    source.write_bytes(first + second)
    manifest = tmp_path / "selected.jsonl.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "output_index": 1,
                        "source_file": "/data/R04.jsonl",
                        "source_line": 77,
                        "raw_line_sha256": hashlib.sha256(first).hexdigest(),
                        "rubric": "R04",
                        "risk": "local_reversible_write",
                    },
                    {
                        "output_index": 2,
                        "source_file": "/data/R04.jsonl",
                        "source_line": 78,
                        "raw_line_sha256": hashlib.sha256(second).hexdigest(),
                        "rubric": "R04",
                        "risk": "local_reversible_write",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "inventory.json"
    result = _run(source, output, "--manifest", str(manifest), "--limit", "2")
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "traceforge.terminal-candidate-inventory.v1"
    assert payload["policy"]["model_calls"] is False
    assert payload["counts"]["records"] == 2
    rows = payload["candidates"]
    first_row = next(row for row in rows if row["source"]["source_line"] == 77)
    assert first_row["preflight"]["eligible"] is True
    assert first_row["source"]["manifest_hash_match"] is True
    assert "src/app.py" in first_row["path_tokens"]
    assert "SECRET_VALUE_12345678" not in first_row["user_summary"]
    assert "<redacted>" in first_row["user_summary"]
    second_row = next(row for row in rows if row["source"]["source_line"] == 78)
    assert "NO_EXPLICIT_FILE_ACTION" in second_row["preflight"]["reason_codes"]
    assert source.read_bytes() == first + second


def test_inventory_redacts_colon_and_bearer_credentials(tmp_path: Path) -> None:
    source = tmp_path / "selected.jsonl"
    source.write_bytes(
        _record(
            "请修复 src/app.py，token: COLON_SECRET_12345678 "
            "Bearer abcdefghijklmnop Authorization: Bearer AUTH_SECRET_12345678"
        )
    )
    output = tmp_path / "inventory.json"
    result = _run(source, output)
    assert result.returncode == 0, result.stderr
    summary = json.loads(output.read_text(encoding="utf-8"))["candidates"][0]["user_summary"]
    assert "COLON_SECRET_12345678" not in summary
    assert "abcdefghijklmnop" not in summary
    assert "token: <redacted>" in summary
    assert "Bearer <redacted>" in summary
    assert "AUTH_SECRET_12345678" not in summary
    assert "Authorization: Bearer <redacted>" in summary


def test_inventory_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "selected.jsonl"
    source.write_bytes(_record("请修复 src/app.py"))
    output = tmp_path / "inventory.json"
    output.write_text("old", encoding="utf-8")
    result = _run(source, output)
    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == "old"


def test_inventory_provenance_does_not_shift_after_invalid_row(tmp_path: Path) -> None:
    source = tmp_path / "selected.jsonl"
    first = _record("请修复 src/first.py")
    second = _record("请修复 src/second.py")
    source.write_bytes(first + b"not-json\n" + second)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"records": [
        {"output_index": 1, "source_line": 11,
         "raw_line_sha256": hashlib.sha256(first).hexdigest()},
        {"output_index": 2, "source_line": 12, "raw_line_sha256": "invalid"},
        {"output_index": 3, "source_line": 13,
         "raw_line_sha256": hashlib.sha256(second).hexdigest()},
    ]}), encoding="utf-8")
    output = tmp_path / "inventory.json"
    result = _run(source, output, "--manifest", str(manifest))
    assert result.returncode == 0, result.stderr
    rows = json.loads(output.read_text(encoding="utf-8"))["candidates"]
    by_line = {row["line_number"]: row for row in rows}
    assert by_line[1]["source"]["source_line"] == 11
    assert by_line[3]["source"]["source_line"] == 13
    assert by_line[3]["source"]["manifest_hash_match"] is True


def test_inventory_requires_classified_risk_for_eligibility(tmp_path: Path) -> None:
    source = tmp_path / "selected.jsonl"
    raw = _record("fix src/app.py")
    source.write_bytes(raw)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"records": [
        {"output_index": 1, "source_line": 11,
         "raw_line_sha256": hashlib.sha256(raw).hexdigest()}
    ]}), encoding="utf-8")
    output = tmp_path / "inventory.json"
    result = _run(source, output, "--manifest", str(manifest))
    assert result.returncode == 0, result.stderr
    row = json.loads(output.read_text(encoding="utf-8"))["candidates"][0]
    assert row["preflight"]["eligible"] is False
    assert "RISK_UNCLASSIFIED" in row["preflight"]["reason_codes"]
    assert row["preflight"]["local_risk"] is False
