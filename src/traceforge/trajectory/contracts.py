"""活跑仍在用的共享契约：可序列化记录与 artifact 清单项。"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class SerializableContract:
    def to_dict(self) -> dict[str, Any]:
        """只展开当前契约字段。"""

        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class ArtifactEntryV1(SerializableContract):
    relative_path: str
    sha256: str
    byte_length: int
    record_count: int | None
