"""把通过验证的 rollout 导出为可审计 JSONL SFT 数据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SFTExportError(ValueError):
    pass


def export_sft_jsonl(rows: list[dict[str, Any]], output: str | Path) -> dict[str, Any]:
    accepted = []
    for index, row in enumerate(rows):
        if row.get("eligibility") != "ELIGIBLE":
            continue
        required = ("candidate_id", "task", "trajectory")
        missing = [x for x in required if x not in row]
        if missing:
            raise SFTExportError(f"第 {index} 条缺少：{','.join(missing)}")
        if row.get("solution_leakage") is not False or row.get("reproducible") is not True:
            raise SFTExportError(f"第 {index} 条缺少泄漏/可复现硬证据")
        accepted.append(
            {
                "id": row["candidate_id"],
                "task": row["task"],
                "trajectory": row["trajectory"],
                "metadata": {
                    k: row[k] for k in ("bundle_id", "rollout_id", "trial_id") if k in row
                },
            }
        )
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        for row in accepted:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "schema_version": "traceforge.sft-jsonl.v1",
        "input_count": len(rows),
        "exported_count": len(accepted),
        "output": str(target),
    }
