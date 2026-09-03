"""已发布 run 目录的产物完整性核验原语（各模块独立 validator 共用的物理层）。

这里只有**物理层**核验：manifest 自洽与文件集合闭合、逐文件重哈希/字节数/行数、目录 inventory、
canonical JSON + LF、隐私摘要兜底、控制文件无绝对路径/URL、运行回执字段与 Git 来源、以及
「期望集 ⇄ 自报集」的双向集合相等。它们在 M1C、M1D 的 validator 中曾各持一份逐字相同的实现，
共同语义已稳定，故按 [`AGENTS.md`] §1 DRY 抽为唯一来源；模块特有的字面量（契约类型、schema、
文件集合、错误码前缀）全部由调用方以参数传入。

信任边界：本模块**不含任何业务派生逻辑**。各模块的业务层——从上游已发布字段重建期望、bijection
的期望侧、不经 fold 的正交不变量、公共报告重算——仍在各模块 validator 内实现；本模块也不 import
任何模块的私有派生公式。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from traceforge.trajectory.contracts import ArtifactEntryV1
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    canonical_json_line,
    sha256_bytes,
    strict_json_loads,
)
from traceforge.trajectory.privacy import find_privacy_violations

RUN_RECEIPT_FIELDS = frozenset(
    {
        "artifact_manifest_sha256",
        "completed_at",
        "duration_seconds",
        "git_provenance",
        "git_provenance_verified_at_completion",
        "python",
        "run_id",
        "schema_version",
        "traceforge_version",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条不携带业务原文的校验错误。"""

    code: str
    location: str
    message: str


class IssueCollector:
    """限制错误量，避免损坏的大文件反向耗尽内存。"""

    def __init__(self, limit: int = 300) -> None:
        self.items: list[ValidationIssue] = []
        self.limit = limit
        self.dropped = 0

    def add(self, code: str, location: str, message: str) -> None:
        if len(self.items) < self.limit:
            self.items.append(ValidationIssue(code, location, message))
        else:
            self.dropped += 1

    def finish(self) -> tuple[ValidationIssue, ...]:
        if self.dropped:
            self.items.append(
                ValidationIssue(
                    "ERROR_LIMIT_REACHED",
                    "run",
                    f"另有 {self.dropped} 条错误未展开",
                )
            )
        return tuple(self.items)


# --- manifest / 文件字节 / inventory ----------------------------------------


def manifest_entries(
    root: Path,
    manifest: dict[str, Any] | None,
    *,
    contract: type[Any],
    schema: str,
    expected_files: frozenset[str],
    issues: IssueCollector,
) -> dict[str, dict[str, Any]]:
    """解析 artifact_manifest 的 files 项：契约闭合、路径不越界不重复、集合恰等于确定性文件集。"""

    if not check_contract(manifest, contract, schema, "artifact_manifest.json", issues):
        return {}
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        issues.add("MANIFEST_FILES_INVALID", "artifact_manifest.json/files", "files 必须是数组")
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(raw_entries):
        location = f"artifact_manifest.json/files/{index}"
        if not check_contract(entry, ArtifactEntryV1, None, location, issues):
            continue
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or not relative:
            issues.add("MANIFEST_PATH_INVALID", location, "relative_path 必须是非空字符串")
            continue
        try:
            (root / relative).resolve().relative_to(root.resolve())
        except ValueError:
            issues.add("MANIFEST_PATH_ESCAPES_RUN", location, "relative_path 越出 run 目录")
            continue
        if relative in entries:
            issues.add("MANIFEST_PATH_DUPLICATE", location, "relative_path 重复")
        entries[relative] = entry
    if frozenset(entries) != expected_files:
        issues.add(
            "MANIFEST_FILE_SET_MISMATCH",
            "artifact_manifest.json/files",
            "manifest 文件集合与当前契约不一致",
        )
    return entries


def check_artifact_bytes(
    root: Path,
    entries: Mapping[str, Mapping[str, Any]],
    issues: IssueCollector,
) -> int:
    """逐文件重算 SHA-256/字节数/JSONL 行数并与 manifest 比对；返回实际核验的文件数。"""

    checked = 0
    for relative, entry in sorted(entries.items()):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            issues.add("ARTIFACT_NOT_REGULAR_FILE", relative, "artifact 不存在或不是普通文件")
            continue
        digest = hashlib.sha256()
        size = lines = 0
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                lines += chunk.count(b"\n")
        checked += 1
        if digest.hexdigest() != entry.get("sha256"):
            issues.add("ARTIFACT_SHA256_MISMATCH", relative, "SHA-256 与 manifest 不一致")
        if size != entry.get("byte_length"):
            issues.add("ARTIFACT_SIZE_MISMATCH", relative, "字节数与 manifest 不一致")
        expected_records = entry.get("record_count")
        if relative.endswith(".jsonl"):
            if not is_int(expected_records) or lines != expected_records:
                issues.add(
                    "ARTIFACT_RECORD_COUNT_MISMATCH",
                    relative,
                    "JSONL 行数与 manifest 不一致",
                )
        elif expected_records is not None:
            issues.add(
                "ARTIFACT_RECORD_COUNT_INVALID",
                relative,
                "单体 JSON 的 record_count 必须为 null",
            )
    return checked


def check_inventory(root: Path, expected_files: frozenset[str], issues: IssueCollector) -> None:
    """run 目录内的文件（含符号链接）集合必须恰等于契约全集，不多不少。"""

    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if observed != expected_files:
        issues.add("RUN_FILE_SET_MISMATCH", "run", "run 文件集合与当前契约不一致")


# --- 运行回执与 Git 来源 ----------------------------------------------------


def check_receipt(
    root: Path,
    receipt: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    *,
    receipt_schema: str,
    manifest_run_id_field: str,
    issues: IssueCollector,
) -> None:
    """回执字段闭合、schema、Git 来源完成时一致、manifest 摘要与 run_id 回指。"""

    if not isinstance(receipt, dict):
        return
    if frozenset(receipt) != RUN_RECEIPT_FIELDS:
        issues.add("RUN_RECEIPT_SCHEMA_MISMATCH", "run_receipt.json", "运行回执字段不匹配")
    if receipt.get("schema_version") != receipt_schema:
        issues.add("RUN_RECEIPT_SCHEMA_INVALID", "run_receipt.json", "schema_version 不匹配")
    check_git_provenance(receipt.get("git_provenance"), issues)
    if receipt.get("git_provenance_verified_at_completion") is not True:
        issues.add(
            "RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED",
            "run_receipt.json/git_provenance_verified_at_completion",
            "正式 run 必须确认 Git 来源在完成时仍一致",
        )
    manifest_path = root / "artifact_manifest.json"
    if manifest_path.is_file() and receipt.get("artifact_manifest_sha256") != sha256_bytes(
        manifest_path.read_bytes()
    ):
        issues.add(
            "RUN_RECEIPT_MANIFEST_HASH_MISMATCH",
            "run_receipt.json",
            "artifact manifest 摘要不匹配",
        )
    if isinstance(manifest, dict) and receipt.get("run_id") != manifest.get(manifest_run_id_field):
        issues.add("RUN_RECEIPT_RUN_ID_MISMATCH", "run_receipt.json", "run_id 不匹配")


def check_git_provenance(value: Any, issues: IssueCollector) -> None:
    location = "run_receipt.json/git_provenance"
    if not isinstance(value, dict) or set(value) != {"available", "commit", "tree", "dirty"}:
        issues.add("RUN_RECEIPT_GIT_PROVENANCE_INVALID", location, "git_provenance 字段不闭合")
        return
    available = value.get("available")
    commit = value.get("commit")
    tree = value.get("tree")
    dirty = value.get("dirty")
    if available is True:
        if (
            not is_git_object_id(commit)
            or not is_git_object_id(tree)
            or len(commit) != len(tree)
            or not isinstance(dirty, bool)
        ):
            issues.add(
                "RUN_RECEIPT_GIT_PROVENANCE_INVALID",
                location,
                "available 来源必须携带同算法 commit/tree 与布尔 dirty",
            )
    elif available is False:
        if commit is not None or tree is not None or dirty is not None:
            issues.add(
                "RUN_RECEIPT_GIT_PROVENANCE_INVALID",
                location,
                "unavailable 来源必须显式使用 null",
            )
    else:
        issues.add("RUN_RECEIPT_GIT_PROVENANCE_INVALID", location, "available 必须是布尔值")


def scan_control_pathlike(value: Any, location: str, *, code: str, issues: IssueCollector) -> None:
    """控制文件不得泄漏绝对路径/URL/主机身份；`find_privacy_violations` 不查这些，故显式扫描。

    relative_path 等业务字段是 run 内相对路径（不以 ``/`` 起头），不会误伤。
    """

    if isinstance(value, str):
        if value.startswith("/") or "://" in value:
            issues.add(code, location, "控制文件出现绝对路径或 URL")
    elif isinstance(value, dict):
        for key, item in value.items():
            scan_control_pathlike(item, f"{location}/{key}", code=code, issues=issues)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            scan_control_pathlike(item, f"{location}/{index}", code=code, issues=issues)


# --- 期望集 ⇄ 自报集 --------------------------------------------------------


def bijection(
    prefix: str,
    location: str,
    expected: Mapping[str, bytes],
    observed: Mapping[str, bytes],
    issues: IssueCollector,
) -> None:
    """双向集合相等：expected 缺席=漏报（删产物），observed 多出=幻影，同键内容不符=篡改。"""

    for identifier in sorted(expected.keys() - observed.keys()):
        issues.add(f"{prefix}_MISSING", location, f"独立复算存在但自报缺失：{identifier}")
    for identifier in sorted(observed.keys() - expected.keys()):
        issues.add(f"{prefix}_PHANTOM", location, f"自报存在但独立复算不产出：{identifier}")
    for identifier in sorted(expected.keys() & observed.keys()):
        if expected[identifier] != observed[identifier]:
            issues.add(f"{prefix}_MISMATCH", location, f"自报内容与独立复算不一致：{identifier}")


# --- 读入原语 ----------------------------------------------------------------


def iter_canonical_jsonl(
    root: Path,
    relative: str,
    *,
    schema: str,
    contract: type[Any],
    issues: IssueCollector,
) -> Iterable[dict[str, Any]]:
    """逐行读入一张 JSONL 表：LF 结尾、严格 JSON、canonical、隐私兜底、契约闭合后才产出记录。"""

    path = root / relative
    if not path.is_file():
        return
    with path.open("rb") as file:
        for line_number, raw in enumerate(file, 1):
            location = f"{relative}:{line_number}"
            if not raw.endswith(b"\n"):
                issues.add("JSONL_FINAL_LF_MISSING", location, "物理行未以 LF 结束")
            try:
                value = strict_json_loads(raw)
            except StrictJsonError:
                issues.add("JSONL_STRICT_JSON_INVALID", location, "不是严格 UTF-8 JSON")
                continue
            if canonical_json_line(value) != raw:
                issues.add("JSONL_NOT_CANONICAL", location, "不是 canonical JSON + LF")
            check_privacy(value, location, issues)
            if check_contract(value, contract, schema, location, issues):
                yield value


def read_json(path: Path, issues: IssueCollector) -> dict[str, Any] | None:
    location = path.name if path.parent.name != "reports" else f"reports/{path.name}"
    if not path.is_file():
        issues.add("JSON_FILE_MISSING", location, "文件不存在")
        return None
    raw = path.read_bytes()
    try:
        value = strict_json_loads(raw)
    except StrictJsonError:
        issues.add("JSON_FILE_INVALID", location, "不是严格 UTF-8 JSON")
        return None
    if canonical_json_line(value) != raw:
        issues.add("JSON_FILE_NOT_CANONICAL", location, "不是 canonical JSON + LF")
    if not isinstance(value, dict):
        issues.add("JSON_FILE_NOT_OBJECT", location, "顶层必须是对象")
        return None
    check_privacy(value, location, issues)
    return value


def check_privacy(value: Any, location: str, issues: IssueCollector) -> None:
    """兜底扫描 Data URL / reasoning；普通 URL/绝对路径由控制文件路径扫描把关。"""

    for violation in find_privacy_violations(value):
        code = (
            "BASE64_DATA_URL_OBSERVED"
            if violation.code == "RAW_DATA_URL"
            else "RAW_REASONING_OBSERVED"
        )
        issues.add(code, f"{location}{violation.pointer}", "派生产物违反隐私摘要契约")


def check_contract(
    value: Any,
    contract: type[Any],
    schema: str | None,
    location: str,
    issues: IssueCollector,
) -> bool:
    if not isinstance(value, dict):
        issues.add("SCHEMA_NOT_OBJECT", location, "记录顶层必须是对象")
        return False
    expected_fields = {contract_field.name for contract_field in fields(contract)}
    if set(value) != expected_fields:
        issues.add("SCHEMA_FIELDS_MISMATCH", location, "字段集合与当前 contract 不一致")
        return False
    if schema is not None and value.get("schema_version") != schema:
        issues.add("SCHEMA_VERSION_MISMATCH", location, "schema_version 不匹配")
        return False
    return True


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_git_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
