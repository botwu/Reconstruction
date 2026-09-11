"""TRACE 能力发现、标注与跨 run 聚合适配器。

该模块选择性重写 TRACE（Scaling Intelligence Lab，MIT，commit d2db2308）
``aggregate_capabilities.compute_metrics`` 的双阈值和跨 run 一致性算法，并将
输入约束收紧为 TraceForge 的可信 outcome 契约。参考实现本身不作为运行时依赖。

关键安全约束：只有调用方明确给出的 SUCCESS/FAILURE 才是可信对照组；
UNCERTAIN、INCOMPLETE、缺失或推断标签不会参与指标，也不能出现在六个标注列表中。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    parse_json_object,
)

from .reference_prompts import capability_guidance, provenance, trace_labeling_guidance

REFERENCE_COMMIT = "d2db23085409555b3f13ea426f42d62cf0bbc43d"
REFERENCE_LICENSE = "MIT"
SCHEMA_VERSION = "traceforge.trace-capabilities.v1"
PROMPT_VERSION = "trace-capability-selection-adapter.v1"
LABEL_KEYS = (
    "lacking_failed",
    "present_failed",
    "na_failed",
    "lacking_passed",
    "present_passed",
    "na_passed",
)
TRUSTED_OUTCOMES = frozenset({"SUCCESS", "FAILURE"})
UNAVAILABLE_MISSING_OUTCOME_LABELS = "UNAVAILABLE_MISSING_OUTCOME_LABELS"


class CapabilityValidationError(ValueError):
    """能力标注不满足六列表分区契约。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CapabilityMetric:
    coverage: float
    contrastive_gap: float
    error_rate_failed: float
    error_rate_success: float
    lacking_failed_count: int
    present_failed_count: int
    lacking_success_count: int
    present_success_count: int
    lacking_failed_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"lacking_failed_ids": list(self.lacking_failed_ids)}


@dataclass(frozen=True, slots=True)
class CapabilitySummary:
    capability: str
    coverage: float
    contrastive_gap: float
    pass_count: int
    run_count: int
    consistency_ratio: float
    passes_consistency: bool
    status: str
    example_failed_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"example_failed_ids": list(self.example_failed_ids)}


@dataclass(frozen=True, slots=True)
class CapabilityAggregation:
    schema_version: str
    status: str
    metrics: dict[str, CapabilitySummary] | None
    run_metrics: tuple[dict[str, CapabilityMetric], ...]
    trusted_success_count: int
    trusted_failure_count: int
    run_count: int
    rho: float
    delta: float
    consistency_k: int
    reference: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "metrics": (
                {k: v.to_dict() for k, v in self.metrics.items()} if self.metrics else None
            ),
            "run_metrics": [{k: v.to_dict() for k, v in run.items()} for run in self.run_metrics],
            "trusted_success_count": self.trusted_success_count,
            "trusted_failure_count": self.trusted_failure_count,
            "run_count": self.run_count,
            "rho": self.rho,
            "delta": self.delta,
            "consistency_k": self.consistency_k,
            "reference": dict(self.reference),
        }


def _trusted_ids(outcomes: Mapping[str, str]) -> tuple[set[str], set[str], set[str]]:
    if not isinstance(outcomes, Mapping):
        raise TypeError("trajectory_outcomes 必须是 mapping")
    success: set[str] = set()
    failure: set[str] = set()
    unknown: set[str] = set()
    for raw_id, raw_outcome in outcomes.items():
        trajectory_id = str(raw_id)
        outcome = str(raw_outcome).upper()
        if outcome == "SUCCESS":
            success.add(trajectory_id)
        elif outcome == "FAILURE":
            failure.add(trajectory_id)
        else:
            unknown.add(trajectory_id)
    return success, failure, unknown


def _validate_partition(
    labels: Mapping[str, Any], *, trusted_ids: set[str], unknown_ids: set[str]
) -> None:
    if not isinstance(labels, Mapping):
        raise CapabilityValidationError("labels 必须是 mapping", code="LABELS_NOT_OBJECT")
    for capability, raw in labels.items():
        if not isinstance(raw, Mapping):
            raise CapabilityValidationError(
                f"能力 {capability!r} 的标签必须是 object", code="LABELS_NOT_OBJECT"
            )
        missing = [key for key in LABEL_KEYS if key not in raw]
        if missing:
            raise CapabilityValidationError(
                f"能力 {capability!r} 缺少列表：{missing}", code="LABEL_LIST_MISSING"
            )
        seen: dict[str, str] = {}
        for key in LABEL_KEYS:
            values = raw[key]
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise CapabilityValidationError(
                    f"能力 {capability!r}/{key} 必须是字符串列表", code="LABEL_LIST_INVALID"
                )
            for trajectory_id in values:
                if trajectory_id in seen:
                    raise CapabilityValidationError(
                        f"trajectory {trajectory_id!r} 在 {seen[trajectory_id]} 与 {key} 重叠",
                        code="LABEL_LIST_OVERLAP",
                    )
                seen[trajectory_id] = key
        observed = set(seen)
        unknown_observed = observed & unknown_ids
        if unknown_observed:
            raise CapabilityValidationError(
                f"能力 {capability!r} 包含不可信 outcome trajectory：{sorted(unknown_observed)}",
                code="UNTRUSTED_OUTCOME_LABEL",
            )
        if observed != trusted_ids:
            missing_ids = sorted(trusted_ids - observed)
            extra_ids = sorted(observed - trusted_ids)
            raise CapabilityValidationError(
                f"能力 {capability!r} 未覆盖可信 trajectory 分区；"
                f"missing={missing_ids}, extra={extra_ids}",
                code="LABEL_PARTITION_INCOMPLETE",
            )


def validate_label_partition(
    labels: Mapping[str, Any], trajectory_outcomes: Mapping[str, str]
) -> None:
    """校验每个 capability 的六列表恰好覆盖可信 SUCCESS/FAILURE 集合。"""
    success, failure, unknown = _trusted_ids(trajectory_outcomes)
    _validate_partition(labels, trusted_ids=success | failure, unknown_ids=unknown)


def compute_metrics(
    labels: Mapping[str, Any],
    trajectory_outcomes: Mapping[str, str],
    *,
    validate: bool = True,
) -> dict[str, CapabilityMetric] | None:
    """按 TRACE 公式计算单个独立 run；没有任一对照组时返回 None。"""
    success, failure, unknown = _trusted_ids(trajectory_outcomes)
    if not success or not failure:
        return None
    if validate:
        _validate_partition(labels, trusted_ids=success | failure, unknown_ids=unknown)
    metrics: dict[str, CapabilityMetric] = {}
    for capability, raw in labels.items():
        lf = set(raw["lacking_failed"])
        pf = set(raw["present_failed"])
        lp = set(raw["lacking_passed"])
        pp = set(raw["present_passed"])
        applicable_failed = len(lf | pf)
        applicable_success = len(lp | pp)
        er_failed = len(lf) / applicable_failed if applicable_failed else 0.0
        er_success = len(lp) / applicable_success if applicable_success else 0.0
        metrics[str(capability)] = CapabilityMetric(
            coverage=len(lf) / len(failure),
            contrastive_gap=er_failed - er_success,
            error_rate_failed=er_failed,
            error_rate_success=er_success,
            lacking_failed_count=len(lf),
            present_failed_count=len(pf),
            lacking_success_count=len(lp),
            present_success_count=len(pp),
            lacking_failed_ids=tuple(sorted(lf)),
        )
    return metrics


def aggregate_capability_runs(
    runs: Sequence[Mapping[str, Any]],
    trajectory_outcomes: Mapping[str, str],
    *,
    rho: float = 0.10,
    delta: float = 0.20,
    consistency_k: int | None = None,
) -> CapabilityAggregation:
    """聚合独立标注 runs，使用 TRACE 双阈值和跨 run 一致性筛选。"""
    if not runs:
        raise CapabilityValidationError("runs 不能为空", code="RUNS_EMPTY")
    if not 0 <= rho <= 1 or not -1 <= delta <= 1:
        raise ValueError("rho 必须在 [0,1]，delta 必须在 [-1,1]")
    success, failure, unknown = _trusted_ids(trajectory_outcomes)
    run_count = len(runs)
    k = consistency_k if consistency_k is not None else run_count
    if k < 1 or k > run_count:
        raise ValueError("consistency_k 必须在 1..run_count")
    if not success or not failure:
        return CapabilityAggregation(
            schema_version=SCHEMA_VERSION,
            status=UNAVAILABLE_MISSING_OUTCOME_LABELS,
            metrics=None,
            run_metrics=(),
            trusted_success_count=len(success),
            trusted_failure_count=len(failure),
            run_count=run_count,
            rho=rho,
            delta=delta,
            consistency_k=k,
            reference={
                "repository": "TRACE",
                "commit": REFERENCE_COMMIT,
                "license": REFERENCE_LICENSE,
            },
        )
    run_metrics: list[dict[str, CapabilityMetric]] = []
    for index, run in enumerate(runs):
        labels = run.get("labels") if isinstance(run, Mapping) and "labels" in run else run
        if not isinstance(labels, Mapping):
            raise CapabilityValidationError(
                f"run {index} 缺少 labels object", code="RUN_LABELS_MISSING"
            )
        _validate_partition(labels, trusted_ids=success | failure, unknown_ids=unknown)
        metric = compute_metrics(labels, trajectory_outcomes, validate=False)
        assert metric is not None
        run_metrics.append(metric)
    capabilities = sorted({name for run in run_metrics for name in run})
    summaries: dict[str, CapabilitySummary] = {}
    for capability in capabilities:
        values = [run[capability] for run in run_metrics]
        passes = [item.coverage >= rho and item.contrastive_gap >= delta for item in values]
        pass_count = sum(passes)
        summary = CapabilitySummary(
            capability=capability,
            coverage=sum(item.coverage for item in values) / run_count,
            contrastive_gap=sum(item.contrastive_gap for item in values) / run_count,
            pass_count=pass_count,
            run_count=run_count,
            consistency_ratio=pass_count / run_count,
            passes_consistency=pass_count >= k,
            status="SELECTED" if pass_count >= k else "REJECTED",
            example_failed_ids=tuple(
                sorted({x for item in values for x in item.lacking_failed_ids})[:8]
            ),
        )
        summaries[capability] = summary
    return CapabilityAggregation(
        schema_version=SCHEMA_VERSION,
        status="AVAILABLE",
        metrics=summaries,
        run_metrics=tuple(run_metrics),
        trusted_success_count=len(success),
        trusted_failure_count=len(failure),
        run_count=run_count,
        rho=rho,
        delta=delta,
        consistency_k=k,
        reference={"repository": "TRACE", "commit": REFERENCE_COMMIT, "license": REFERENCE_LICENSE},
    )


def _json_context(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def build_discovery_prompt(*, model_name: str, trajectories: Any, n_candidates: int = 12) -> str:
    """构建 TRACE Phase 1 candidate discovery prompt。"""
    if n_candidates < 1:
        raise ValueError("n_candidates 必须为正数")
    return f"""{trace_labeling_guidance()}

You are a capability discovery agent. Model: {model_name}.
This is TRACE Phase 1 (MIT, commit {REFERENCE_COMMIT}); discover general capabilities,
never task-specific hints. Read the supplied trajectories and propose 4-{n_candidates}
distinct capabilities that explain observed failures. Return ONLY JSON:
{{"candidates":[{{"name":"snake_case","description":"precise 2-3 sentence definition",
"example_trajectories":[{{"task_id":"id","outcome":"SUCCESS|FAILURE","note":"evidence"}}]}}]}}
Do not infer an outcome absent an explicit trusted SUCCESS or FAILURE label.
{capability_guidance()}
TRACE PROVENANCE: {_json_context(provenance())}
TRAJECTORIES:\n{_json_context(trajectories)}"""


def build_label_prompt(
    *, model_name: str, trajectories: Any, candidates: Any, attempt_number: int
) -> str:
    """构建 TRACE Phase 2 独立 labeling prompt；每次调用仅接收本地上下文。"""
    if attempt_number < 1:
        raise ValueError("attempt_number 必须为正数")
    return f"""{trace_labeling_guidance()}

You are an independent TRACE Phase 2 labeling agent (attempt {attempt_number}).
Model: {model_name}. For every trajectory and capability return ONLY this JSON shape:
{{"attempt":{attempt_number},"labels":{{"capability":{{"lacking_failed":[],"present_failed":[],"na_failed":[],"lacking_passed":[],"present_passed":[],"na_passed":[]}}}}}}
The six lists must partition only trajectories with explicit trusted SUCCESS/FAILURE outcomes.
Use NA when capability is irrelevant; PRESENT when demonstrated; LACKING only when relevant
and missing. Never invent IDs or treat UNCERTAIN/INCOMPLETE as trusted outcomes.
{capability_guidance()}
TRACE PROVENANCE: {_json_context(provenance())}
TRAJECTORIES:\n{_json_context(trajectories)}\nCANDIDATES:\n{_json_context(candidates)}"""


def _request_id(kind: str, payload: Any, attempt_number: int = 0) -> str:
    digest = hashlib.sha256(_json_context(payload).encode()).hexdigest()[:24]
    return f"traceforge-capability-{kind}-{attempt_number}-{digest}"


def discover_capabilities(
    *, model: ChatModel, model_name: str, trajectories: Any, n_candidates: int = 12
) -> tuple[dict[str, Any], ModelResponse]:
    request = ModelRequest(
        request_id=_request_id("discover", trajectories),
        model=model_name,
        system="Return strict JSON only.",
        prompt=build_discovery_prompt(
            model_name=model_name, trajectories=trajectories, n_candidates=n_candidates
        ),
        response_schema="traceforge.trace-capability-discovery.v1",
        temperature=0.0,
    )
    response = model.complete(request)
    payload = parse_json_object(response.text)
    if not isinstance(payload.get("candidates"), list):
        raise ModelGatewayError("能力发现缺少 candidates 列表", code="DISCOVERY_SCHEMA_INVALID")
    return payload, response


def label_capabilities(
    *,
    model: ChatModel,
    model_name: str,
    trajectories: Any,
    candidates: Any,
    attempt_number: int,
    trajectory_outcomes: Mapping[str, str],
) -> tuple[dict[str, Any], ModelResponse]:
    request = ModelRequest(
        request_id=_request_id(
            "label", {"trajectories": trajectories, "candidates": candidates}, attempt_number
        ),
        model=model_name,
        system="Return strict JSON only.",
        prompt=build_label_prompt(
            model_name=model_name,
            trajectories=trajectories,
            candidates=candidates,
            attempt_number=attempt_number,
        ),
        response_schema="traceforge.trace-capability-labeling.v1",
        temperature=0.0,
    )
    response = model.complete(request)
    payload = parse_json_object(response.text)
    labels = payload.get("labels")
    if not isinstance(labels, Mapping):
        raise ModelGatewayError("能力标注缺少 labels object", code="LABELING_SCHEMA_INVALID")
    validate_label_partition(labels, trajectory_outcomes)
    return payload, response


__all__ = [
    "LABEL_KEYS",
    "PROMPT_VERSION",
    "REFERENCE_COMMIT",
    "UNAVAILABLE_MISSING_OUTCOME_LABELS",
    "CapabilityAggregation",
    "CapabilityMetric",
    "CapabilitySummary",
    "CapabilityValidationError",
    "aggregate_capability_runs",
    "build_discovery_prompt",
    "build_label_prompt",
    "compute_metrics",
    "discover_capabilities",
    "label_capabilities",
    "validate_label_partition",
]
