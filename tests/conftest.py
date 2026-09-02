"""M1A、M1B 测试夹具。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA

DEFAULT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "查询虚构资料",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


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
def capture_factory() -> Callable[..., dict[str, Any]]:
    """生成不包含任何真实回流 literal 的 capture。"""

    def make_capture(
        *,
        messages: list[dict[str, Any]],
        terminal_prefix_depths: list[int],
        request_ids: list[str] | None = None,
        capture_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        definition_conflict: bool = False,
        inferred_tool_definitions: list[str] | None = None,
        has_compaction: bool = False,
        thread_id: str = "fixture-thread",
        account_id: str = "fixture-account",
    ) -> dict[str, Any]:
        ids = request_ids or [
            f"fixture-request-{index + 1}" for index in range(len(terminal_prefix_depths))
        ]
        final_capture_id = capture_id or ids[-1]
        compaction_hashes = ["a" * 64] if has_compaction else []
        return {
            "domain_meta": {
                "domain": {"code": "FIXTURE", "name": "虚构业务"},
                "input_audit": {"input_truncated": False},
                "rubric": {"primary": {"code": "FIXTURE", "name": "虚构检索"}},
                "task": {"normalized": "完成一项完全虚构的测试任务。"},
                "version": "fixture-v1",
            },
            "messages": messages,
            "meta": {
                "account_id": account_id,
                "adapter": "fixture-adapter",
                "capture_id": final_capture_id,
                "compaction_count": len(compaction_hashes),
                "compaction_hashes": compaction_hashes,
                "has_compaction": has_compaction,
                "inferred_tool_definitions": inferred_tool_definitions or [],
                "leaf_response_status": "completed",
                "model": "fixture-model",
                "normalization_version": "fixture-normalizer-v1",
                "protocol_adapters": ["fixture-protocol"],
                "raw_request_hash": "b" * 64,
                "representation": "restored_long",
                "request_time_end": "2026-01-01T00:00:01+00:00",
                "request_time_start": "2026-01-01T00:00:00+00:00",
                "response_time_end": "2026-01-01T00:00:02+00:00",
                "sequence_repairs": [],
                "source_models": ["fixture-model"],
                "source_request_count": len(ids),
                "source_request_id": ids[-1],
                "source_request_ids": ids,
                "target_hash": "c" * 64,
                "terminal_prefix_depths": terminal_prefix_depths,
                "thread_id": thread_id,
                "tools_definition_conflict": definition_conflict,
                "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            },
            "tools": DEFAULT_TOOLS if tools is None else tools,
        }

    return make_capture


@pytest.fixture
def two_boundary_capture(capture_factory: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """覆盖边界、工具事件、reasoning 和 data URL 的最小 capture。"""

    return capture_factory(
        terminal_prefix_depths=[3, 7],
        messages=[
            {"role": "system", "content": "仅用于虚构测试的系统说明。"},
            {"role": "user", "content": "先整理背景。"},
            {
                "role": "assistant",
                "content": "背景已整理。",
                "reasoning_content": "旧推理哨兵-禁止外泄",
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "再查询图片中的虚构编号。"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,U0VDUkVUX0JBU0U2NA==",
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "第二个旧推理哨兵-禁止外泄",
                "tool_calls": [
                    {
                        "id": "call-matched",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query":"alpha"}'},
                    },
                    {
                        "id": "call-missing",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query":"beta"}'},
                    },
                ],
            },
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "call-matched",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "虚构结果 alpha，内嵌值为 data:image/png;base64, U0VDUkVUX0JBU0U2NA=="
                        ),
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "看似已经完成，但仍发起工具调用。",
                "tool_calls": [
                    {
                        "id": "call-terminal",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query":"gamma"}'},
                    }
                ],
            },
        ],
    )


@pytest.fixture
def compile_dataset(tmp_path: Path) -> Callable[..., Path]:
    """将虚构 records 编译到独立目录并返回内容寻址运行目录。"""

    counter = 0

    def compile_records(
        records: Iterable[dict[str, Any]],
        *,
        label: str = "fixture",
        dataset_id: str = "fixture-dataset-v1",
    ) -> Path:
        nonlocal counter
        counter += 1
        raw = b"".join(json_line(record) for record in records)
        input_path = tmp_path / f"{label}-{counter}.jsonl"
        input_path.write_bytes(raw)
        output_root = tmp_path / f"artifacts-{label}-{counter}"

        from traceforge.trajectory import compile_trajectory

        result = compile_trajectory(
            input_path=input_path,
            dataset_id=dataset_id,
            source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            output_root=output_root,
        )
        return Path(result)

    return compile_records


@pytest.fixture
def stable_git_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 M1B 编译与 M1C 建图两处的 collect_git_provenance 固定为「已核验」来源。

    provenance 采集不可用时（本仓库虽是 git 仓库，但慢速网络盘上 `git status` 耗时超过
    provenance 的 5s 超时 → available=False）完成时无法核验一致，会使
    validate_compiled_run / validate_lineage_run 报 RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED，
    掩盖测试真正要验的行为。固定为可核验值以隔离环境差异（三个 pipeline 各持一份 import 引用，
    须分别 patch）。
    """

    from traceforge.trajectory.provenance import GitProvenance

    provenance = GitProvenance(True, "a" * 40, "b" * 40, False)
    monkeypatch.setattr("traceforge.trajectory.pipeline.collect_git_provenance", lambda: provenance)
    monkeypatch.setattr("traceforge.lineage.pipeline.collect_git_provenance", lambda: provenance)
    monkeypatch.setattr(
        "traceforge.query_turns.pipeline.collect_git_provenance", lambda: provenance
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def read_private(run_dir: Path, name: str) -> list[dict[str, Any]]:
    return read_jsonl(run_dir / "private" / f"{name}.jsonl")
