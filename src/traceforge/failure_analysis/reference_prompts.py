"""复用 AgentRx / TRACE 的原始方法提示资产。

这里仅读取随包发布的静态资产，不 import 或 exec 上游运行时。所有来源 commit、
文件摘要和适配说明都会进入 prompt context，便于审计复现。
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

_ASSET_DIR = Path(__file__).with_name("reference_assets")
_AGENTRX_COMMIT = "f228165bfec60a801fd5fedd9d8ffe0f9de0c69d"
_TRACE_COMMIT = "d2db23085409555b3f13ea426f42d62cf0bbc43d"
_AGENTRX_SOURCE_SHA256 = "9629515bc49fa83c686a2aa03343b4a5ee9ddc23215f4756f7767ee6c9a59323"
_TRACE_SOURCE_SHA256 = "5f32d35b633f2fee9f07c5449503be30bb5b96ac55bdcc2024199dda89448600"
_AGENTRX_INVARIANTS_COMMIT = _AGENTRX_COMMIT
_AGENTRX_STATIC_SOURCE_SHA256 = "a113e9ad7dffc08d2de06142bd60609e365435e550721b41db430e0f4927b874"
_AGENTRX_DYNAMIC_SOURCE_SHA256 = "6874f860c423394706308ebf71c23304a2a52cdd5a4a4909c940542a9a14a2d9"
PROMPT_ADAPTER_VERSION = "agentrx-trace-evidence-adapter.v2"


def _read_asset(name: str) -> str:
    path = _ASSET_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"reference prompt asset missing: {path}")
    return path.read_text(encoding="utf-8")


def _asset_sha(name: str) -> str:
    path = _ASSET_DIR / name
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else "MISSING"


def static_invariant_guidance() -> str:
    """返回 AgentRx STATIC_INVARIANT_PROMPT 原文（含上游占位符）。"""
    return _read_asset("agentrx_static_prompt.txt").strip()


def dynamic_invariant_guidance() -> str:
    """返回 AgentRx DYNAMIC_INVARIANT_PROMPT 原文（含上游占位符）。"""
    return _read_asset("agentrx_dynamic_prompt.txt").strip()


def taxonomy_data() -> dict[str, Any]:
    """返回从 AgentRx judge.py AST literal 提取的原始十类 taxonomy。"""
    value = json.loads(_read_asset("agentrx_taxonomy.json"))
    return value


def root_cause_algorithm() -> str:
    """返回 AgentRx 原始 root-cause 算法，已删除不存在的 ground-truth 输入声明。"""
    return _read_asset("agentrx_root_cause.txt").strip()


def trace_labeling_guidance() -> str:
    """返回 TRACE 原始三分类 labeling 段（NA/PRESENT/LACKING）。"""
    return _read_asset("trace_labeling.md").strip()


def root_cause_guidance() -> str:
    """保留 AgentRx 原始算法，同时声明 TraceForge 的证据边界。"""
    return (
        root_cause_algorithm()
        + "\n\nTraceForge adaptation constraints:\n"
        "The source trajectory has no ground-truth action sequence or expected response. "
        "Do not invent either. Use only event/evidence references; an UNCLEAR check means "
        "unproven and must not be converted into success or failure."
    )


def capability_guidance() -> str:
    """复用 TRACE 三分类语义，并禁止无标签时伪造 contrastive gap。"""
    return (
        trace_labeling_guidance()
        + "\n\nTraceForge adaptation constraints:\n"
        "Outcome labels are unavailable for this input unless explicitly supplied and verified. "
        "Emit NA/PRESENT/LACKING only with direct evidence. Cov/Delta and any pass/fail gap "
        "are UNAVAILABLE when trusted outcome labels are missing; never use a zero denominator "
        "as evidence. These labels prioritize reconstruction and are not success labels."
    )


def environment_guidance() -> str:
    """复用 TRACE environment-generation 的接口隔离原则，但限定为重建。"""
    return (
        "Reuse TRACE's environment-generation principle: preserve the target interface and "
        "tool schemas, isolate the capability under study, and validate transfer. TraceForge "
        "must materialize only an evidence-backed initial state; unknown state remains explicit, "
        "and hidden answer material or solution files are forbidden."
    )


def provenance() -> dict[str, str]:
    """返回来源 commit、资产 sha256 和适配版本。"""
    return {
        "adapter_version": PROMPT_ADAPTER_VERSION,
        "agentrx_commit": _AGENTRX_COMMIT,
        "agentrx_source_path": "agentrx/agentrx/judge/judge.py",
        "agentrx_source_sha256": _AGENTRX_SOURCE_SHA256,
        "extraction_method": (
            "ast.literal_eval(TAXONOMY_DATA) + literal extraction of _TMPL_NO_CONTEXT"
        ),
        "agentrx_taxonomy_sha256": _asset_sha("agentrx_taxonomy.json"),
        "agentrx_root_cause_sha256": _asset_sha("agentrx_root_cause.txt"),
        "trace_commit": _TRACE_COMMIT,
        "trace_source_path": "prompts/general/capability_selection.md",
        "trace_source_sha256": _TRACE_SOURCE_SHA256,
        "trace_labeling_sha256": _asset_sha("trace_labeling.md"),
        "agentrx_invariants_commit": _AGENTRX_INVARIANTS_COMMIT,
        "agentrx_static_source_path": "agentrx/invariants/static_invariant_generator.py",
        "agentrx_static_source_sha256": _AGENTRX_STATIC_SOURCE_SHA256,
        "agentrx_dynamic_source_path": "agentrx/invariants/dynamic_invariant_generator.py",
        "agentrx_dynamic_source_sha256": _AGENTRX_DYNAMIC_SOURCE_SHA256,
        "agentrx_static_prompt_sha256": _asset_sha("agentrx_static_prompt.txt"),
        "agentrx_dynamic_prompt_sha256": _asset_sha("agentrx_dynamic_prompt.txt"),
    }


def prompt_context() -> str:
    """组装可注入模型的来源上下文，末尾明确 TraceForge 输出契约。"""
    p = provenance()
    taxonomy = json.dumps(taxonomy_data(), ensure_ascii=False, indent=2)
    return "\n".join(
        (
            "REFERENCE METHOD CONTEXT (source provenance is part of the audit record):",
            f"adapter_version={p['adapter_version']}",
            f"AgentRx commit={p['agentrx_commit']} source_sha256={p['agentrx_source_sha256']} "
            f"taxonomy_sha256={p['agentrx_taxonomy_sha256']} "
            f"root_cause_sha256={p['agentrx_root_cause_sha256']}",
            f"TRACE commit={p['trace_commit']} source_sha256={p['trace_source_sha256']} "
            f"labeling_sha256={p['trace_labeling_sha256']}",
            "AGENTRX_TAXONOMY_JSON:",
            taxonomy,
            "AGENTRX_STATIC_INVARIANT_PROMPT:",
            static_invariant_guidance(),
            "AGENTRX_DYNAMIC_INVARIANT_PROMPT:",
            dynamic_invariant_guidance(),
            "AGENTRX_ROOT_CAUSE_ALGORITHM:",
            root_cause_guidance(),
            "TRACE_THREE_WAY_LABELING:",
            capability_guidance(),
            "TRACE_ENVIRONMENT_PRINCIPLE:",
            environment_guidance(),
            "TRACEFORGE OUTPUT CONTRACT:",
            "Return TraceForge's schema only: outcome, decision, failure_step, failure_type, "
            "failure_reason, evidence_refs, recoverability, reconstruction_value, and "
            "uncertainties. The reused material is diagnostic guidance, not an alternate schema.",
        )
    )


__all__ = [
    "PROMPT_ADAPTER_VERSION",
    "capability_guidance",
    "dynamic_invariant_guidance",
    "environment_guidance",
    "prompt_context",
    "provenance",
    "root_cause_algorithm",
    "root_cause_guidance",
    "static_invariant_guidance",
    "taxonomy_data",
    "trace_labeling_guidance",
]
