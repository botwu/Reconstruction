"""Job 级 AGS sandbox ledger 的只读审计。

该模块不依赖 Harbor / E2B，供环境适配层和 TraceForge 结果门禁共用同一套规则。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class SandboxCleanupError(RuntimeError):
    """AGS 沙盒未能被可验证地销毁。"""


@dataclass(frozen=True)
class SandboxLedgerAudit:
    """Job 级 sandbox ledger 的闭合性检查结果。"""

    created_ids: tuple[str, ...]
    terminal_ids: tuple[str, ...]
    unverified_ids: tuple[str, ...]
    unresolved_create_operations: tuple[str, ...]
    malformed_lines: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.unverified_ids or self.unresolved_create_operations or self.malformed_lines
        )


def audit_sandbox_ledger(path: Path | str) -> SandboxLedgerAudit:
    """只读审计 ledger；任何未闭合 create 或未终止 sandbox 都是不确定状态。"""

    ledger = Path(path)
    if not ledger.is_file():
        raise SandboxCleanupError(f"AGS sandbox ledger 不存在: {ledger}")
    created: set[str] = set()
    terminal: set[str] = set()
    requested: set[str] = set()
    resolved: set[str] = set()
    malformed: list[int] = []
    for line_number, raw in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get("schema_version") != 1:
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            malformed.append(line_number)
            continue
        event = record.get("event")
        sandbox_id = record.get("sandbox_id")
        operation_id = record.get("create_operation_id")
        if event == "create_requested" and isinstance(operation_id, str):
            requested.add(operation_id)
        if event in {"created", "create_failed", "create_cancelled_without_sandbox"}:
            if isinstance(operation_id, str):
                resolved.add(operation_id)
        if event == "created" and isinstance(sandbox_id, str) and sandbox_id:
            created.add(sandbox_id)
        if event in {"killed", "kill_not_found"} and isinstance(sandbox_id, str):
            terminal.add(sandbox_id)
    return SandboxLedgerAudit(
        created_ids=tuple(sorted(created)),
        terminal_ids=tuple(sorted(terminal)),
        unverified_ids=tuple(sorted(created - terminal)),
        unresolved_create_operations=tuple(sorted(requested - resolved)),
        malformed_lines=tuple(malformed),
    )


def assert_all_sandboxes_deleted(path: Path | str) -> SandboxLedgerAudit:
    """作为认证 gate 使用；不能证明全部删除时抛出 ``SandboxCleanupError``。"""

    audit = audit_sandbox_ledger(path)
    if not audit.ok:
        raise SandboxCleanupError(
            "CLEANUP_UNVERIFIABLE: "
            f"sandboxes={list(audit.unverified_ids)}, "
            f"creates={list(audit.unresolved_create_operations)}, "
            f"malformed_lines={list(audit.malformed_lines)}"
        )
    return audit


__all__ = [
    "SandboxCleanupError",
    "SandboxLedgerAudit",
    "assert_all_sandboxes_deleted",
    "audit_sandbox_ledger",
]
