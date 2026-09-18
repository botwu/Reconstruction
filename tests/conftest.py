"""测试共用的轻量辅助函数。"""

from __future__ import annotations

import json
from typing import Any


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
