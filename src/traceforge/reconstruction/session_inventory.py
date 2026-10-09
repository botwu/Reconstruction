"""冻结调用方指定的原始字节，并为每个物理 session 建立可定位清单。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TextIO, TypedDict

SOURCE_SCHEMA = "traceforge.session-source-manifest.v1"
SESSION_SCHEMA = "traceforge.session-inventory.v1"


class SourceExpectation(TypedDict):
    """上游声明的完整源文件身份。"""

    records: int
    bytes: int
    sha256: str


class SourceInventory(TypedDict):
    """流式扫描得到的源文件事实。"""

    rubric: str
    source: str
    input: str
    records: int
    bytes: int
    sha256: str
    valid_records: int
    invalid_records: int
    duplicate_line_count: int
    expected: SourceExpectation
    coverage_complete: bool
    errors: list[str]


class SessionInventory(TypedDict):
    """一个物理行对应一个输入，不根据内容筛选或去重。"""

    schema: str
    input: str
    rubric: str
    line_number: int
    line_sha256: str
    byte_offset: int
    byte_length: int
    session_id: str
    label: str
    input_status: str
    errors: list[str]


class InventoryManifest(TypedDict):
    """只有覆盖校验全部通过的清单才可作为批处理输入。"""

    schema: str
    status: str
    domain: str
    created_at: str
    distribution: str
    distribution_sha256: str
    rubrics: list[str]
    coverage_complete: bool
    total_sessions: int
    pending_sessions: int
    invalid_sessions: int
    total_bytes: int
    duplicate_line_count: int
    sessions: str | None
    sources: list[SourceInventory]


def _expectations(path: Path, source_codes: Sequence[str]) -> tuple[dict[str, SourceExpectation], str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    entries = value.get("distribution") if isinstance(value, dict) else None
    if (not isinstance(entries, list) and isinstance(value, dict)
            and isinstance(value.get("datasets"), list)):
        entries = [
            {"code": item.get("name"), "records": item.get("physical_lines"),
             "bytes": item.get("bytes"), "sha256": item.get("sha256")}
            for item in value["datasets"] if isinstance(item, dict)
        ]
    if not isinstance(entries, list):
        raise ValueError(f"{path}: distribution 或 datasets 必须是数组")
    result: dict[str, SourceExpectation] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("code") not in source_codes:
            continue
        code = entry["code"]
        if code in result:
            raise ValueError(f"{path}: {code} 的覆盖声明重复")
        records, size, digest = entry.get("records"), entry.get("bytes"), entry.get("sha256")
        if (
            type(records) is not int
            or records < 0
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError(f"{path}: {code} 缺少有效 records/bytes/sha256")
        result[code] = {"records": records, "bytes": size, "sha256": digest}
    if set(result) != set(source_codes):
        raise ValueError(f"{path}: 必须声明全部指定数据源：{list(source_codes)}")
    return result, hashlib.sha256(raw).hexdigest()


def _input_errors(raw: bytes, source: Path, line_number: int) -> list[str]:
    location = f"{source}:{line_number}"
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"INVALID_JSON: {location}: {exc}"]
    if not isinstance(value, dict):
        return [f"INVALID_SESSION: {location}: session 必须是 JSON 对象"]
    if not isinstance(value.get("messages"), list):
        return [f"INVALID_SESSION: {location}: messages 必须是数组"]
    return []


def _scan_source(
    source: Path,
    frozen: Path,
    rubric: str,
    expected: SourceExpectation,
    frozen_stream: BinaryIO,
    session_stream: TextIO,
    seen_lines: set[str],
) -> SourceInventory:
    digest = hashlib.sha256()
    offset = records = invalid = duplicates = 0
    with source.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            digest.update(raw)
            frozen_stream.write(raw)
            line_hash = hashlib.sha256(raw).hexdigest()
            if line_hash in seen_lines:
                duplicates += 1
            seen_lines.add(line_hash)
            errors = _input_errors(raw, source, number)
            identity = f"{rubric}:{number}:{line_hash}".encode()
            row: SessionInventory = {
                "schema": SESSION_SCHEMA,
                "input": str(frozen),
                "rubric": rubric,
                "line_number": number,
                "line_sha256": line_hash,
                "byte_offset": offset,
                "byte_length": len(raw),
                "session_id": "session_" + hashlib.sha256(identity).hexdigest()[:24],
                "label": f"{rubric.lower()}-l{number:06d}",
                "input_status": "INVALID_INPUT" if errors else "PENDING",
                "errors": errors,
            }
            session_stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            records += 1
            invalid += bool(errors)
            offset += len(raw)
    actual: SourceExpectation = {
        "records": records,
        "bytes": offset,
        "sha256": digest.hexdigest(),
    }
    coverage_errors = [
        f"SOURCE_COVERAGE_MISMATCH: {source}: {key} 期望 {expected[key]}，实际 {actual[key]}"
        for key in ("records", "bytes", "sha256")
        if expected[key] != actual[key]
    ]
    return {
        "rubric": rubric,
        "source": str(source),
        "input": str(frozen),
        **actual,
        "valid_records": records - invalid,
        "invalid_records": invalid,
        "duplicate_line_count": duplicates,
        "expected": expected,
        "coverage_complete": not coverage_errors,
        "errors": coverage_errors,
    }


def _publish_once(temporary: Path, destination: Path) -> None:
    """以原子硬链接发布完整文件，目标已存在时明确失败而不覆盖。"""
    os.link(temporary, destination)
    temporary.unlink()


def prepare_session_batch(
    source_dir: str | Path,
    frozen_dir: str | Path,
    output_dir: str | Path,
    *,
    source_codes: Sequence[str],
    domain: str,
    distribution_path: str | Path | None = None,
) -> InventoryManifest:
    """按指定 domain 盘点源文件；覆盖不符时只发布失败报告。"""
    if domain not in {"search", "terminal"}:
        raise ValueError("domain 必须由调用方指定为 search 或 terminal")
    if (not source_codes or len(set(source_codes)) != len(source_codes)
            or any(not code or code in {".", ".."} or Path(code).name != code
                   for code in source_codes)):
        raise ValueError("数据源名称须非空、互不重复且不包含路径")
    source_root = Path(source_dir).resolve()
    frozen_root = Path(frozen_dir).resolve()
    output_root = Path(output_dir).resolve()
    distribution = (
        Path(distribution_path).resolve()
        if distribution_path
        else source_root / "distribution.json"
    )
    expected, distribution_hash = _expectations(distribution, source_codes)
    manifest_path = output_root / "source_manifest.json"
    sessions_path = output_root / "sessions.jsonl"
    destinations = [manifest_path, sessions_path, *(frozen_root / f"{r}.jsonl" for r in source_codes)]
    for destination in destinations:
        if destination.exists():
            raise FileExistsError(f"拒绝覆盖已有输入或清单：{destination}")
    for rubric in source_codes:
        if not (source_root / f"{rubric}.jsonl").is_file():
            raise FileNotFoundError(f"缺少完整源文件：{source_root / (rubric + '.jsonl')}")
    frozen_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    temporary_paths: list[Path] = []
    staged_sources: list[tuple[Path, Path]] = []
    sources: list[SourceInventory] = []
    seen_lines: set[str] = set()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_root, prefix=".sessions-", delete=False
        ) as sessions:
            session_temp = Path(sessions.name)
            temporary_paths.append(session_temp)
            for rubric in source_codes:
                frozen = frozen_root / f"{rubric}.jsonl"
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=frozen_root, prefix=f".{rubric}-", delete=False
                ) as staged:
                    source_temp = Path(staged.name)
                    temporary_paths.append(source_temp)
                    sources.append(
                        _scan_source(
                            source_root / f"{rubric}.jsonl",
                            frozen,
                            rubric,
                            expected[rubric],
                            staged,
                            sessions,
                            seen_lines,
                        )
                    )
                    staged.flush()
                    os.fsync(staged.fileno())
                    os.fchmod(staged.fileno(), 0o444)
                staged_sources.append((source_temp, frozen))
            sessions.flush()
            os.fsync(sessions.fileno())
        complete = all(source["coverage_complete"] for source in sources)
        manifest: InventoryManifest = {
            "schema": SOURCE_SCHEMA,
            "status": "READY" if complete else "PARTIAL_SOURCE",
            "domain": domain,
            "created_at": datetime.now(UTC).isoformat(),
            "distribution": str(distribution),
            "distribution_sha256": distribution_hash,
            "rubrics": list(source_codes),
            "coverage_complete": complete,
            "total_sessions": sum(source["records"] for source in sources),
            "pending_sessions": sum(source["valid_records"] for source in sources),
            "invalid_sessions": sum(source["invalid_records"] for source in sources),
            "total_bytes": sum(source["bytes"] for source in sources),
            "duplicate_line_count": sum(source["duplicate_line_count"] for source in sources),
            "sessions": str(sessions_path) if complete else None,
            "sources": sources,
        }
        if complete:
            for temporary, destination in staged_sources:
                _publish_once(temporary, destination)
            _publish_once(session_temp, sessions_path)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_root, prefix=".manifest-", delete=False
        ) as staged_manifest:
            manifest_temp = Path(staged_manifest.name)
            temporary_paths.append(manifest_temp)
            json.dump(manifest, staged_manifest, ensure_ascii=False, indent=2)
            staged_manifest.write("\n")
            staged_manifest.flush()
            os.fsync(staged_manifest.fileno())
        _publish_once(manifest_temp, manifest_path)
        return manifest
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
