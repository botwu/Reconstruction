"""测试共用的轻量辅助函数。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest


def json_line(value: Any) -> bytes:
    """构造稳定的虚构 JSONL 物理行。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


@pytest.fixture
def harbor_cleanup(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """桥接单元测试使用外部审计回执，不加载部署机的 Harbor。"""
    result = SimpleNamespace(ok=True)
    monkeypatch.setattr(
        "traceforge.harbor_ags.results._import_audit_sandbox_ledger",
        lambda: lambda ledger: result,
    )
    return result
