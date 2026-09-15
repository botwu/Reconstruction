"""对规则通过的样本做重建价值模型细筛。模型只打分，不决定终态。"""

from __future__ import annotations

from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelGatewayError,
    ModelRequest,
    parse_json_object,
    receipt_for_response,
)
from traceforge.trajectory.json_codec import stable_id

from .contracts import (
    RUBRIC_KEYS,
    TRIAGE_PROMPT_VERSION,
    TRIAGE_RESPONSE_SCHEMA,
    DomainRoute,
)
from .rubric import admit_after_tasks, coerce_rubric, empty_rubric


def _prompt(evidence: dict[str, Any]) -> str:
    keys = ", ".join(RUBRIC_KEYS)
    span_ids = list(evidence.get("span_ids") or ())
    return "\n".join(
        [
            "你是重建筛选器。目标是在整条 capture 的可观察轨迹里，找出失败或未完成、",
            "能拟合用户意图且环境有抓手的任务。不要默认只评最后一问。",
            "不要编造未出现的文件、答案或用户身份。",
            "证据是 shared_context + spans[] 内全部用户要求、可见 assistant、",
            "tool_calls 与 tool 结果。",
            "thinking/reasoning 已省略，不得根据其缺失加分或扣分。",
            "大字段可能被截断（含 omitted_chars/hash）；不得编造被省略的内容。",
            "不要把抓取完成（leaf_response_status=completed）当成任务成功。",
            "不要把末尾未返回的 tool call 单独当成任务失败；需结合用户目标与可见过程判断。",
            "闲聊、已成功问答不要标成需要重建。",
            "同一交付物的追问、纠错、继续可合并到一个 task 的多个 span_ids。",
            f"只能使用这些 span_id：{span_ids}",
            "",
            "只返回一个 JSON object：",
            "{",
            '  "tasks": [',
            "    {",
            '      "span_ids": ["span_..."],',
            '      "outcome": "SUCCESS|FAILURE|INCOMPLETE|UNCERTAIN",',
            '      "needs_reconstruction": true|false|null,',
            '      "domain_route": "code_file|retrieval|other",',
            f'      "rubric": {{{keys}}},',
            '      "reason": "简短中文依据"',
            "    }",
            "  ]",
            "}",
            "rubric 每个键是 0 到 3 的整数。",
            "R1 意图可拟合：0 无任务或只能猜，2 目标基本明确，3 目标与交付物都有用户证据。",
            "R2 没做成：0 无失败/未完成迹象，2 有错误、终止或纠正，3 失败边界可定位。",
            "环境有抓手用 domain_route，不另打分：出现过文件/代码/工作区操作用 code_file；",
            "检索问答用 retrieval；闲聊或纯概念用 other。不要求环境完整。",
            "outcome=SUCCESS 时 needs_reconstruction 必须为 false。",
            "outcome=UNCERTAIN 时 needs_reconstruction 必须为 null。",
            "FAILURE/INCOMPLETE 且意图清楚、环境有抓手时 needs_reconstruction 为 true。",
            "至少返回一个 task；若整条都是闲聊或成功，也要返回对应 task。",
            "",
            "OBSERVABLE_EVIDENCE:",
            repr(evidence),
        ]
    )


def _normalize_tasks(
    raw_tasks: object, *, allowed_span_ids: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    if not isinstance(raw_tasks, list):
        return [], ["TASKS_NOT_ARRAY"]
    if not raw_tasks:
        return [], ["TASKS_EMPTY"]

    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            errors.append(f"TASK_NOT_OBJECT:{index}")
            continue
        raw_ids = item.get("span_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            errors.append(f"TASK_SPAN_IDS_INVALID:{index}")
            continue
        span_ids: list[str] = []
        bad = False
        for span_id in raw_ids:
            if not isinstance(span_id, str) or not span_id:
                errors.append(f"TASK_SPAN_ID_INVALID:{index}")
                bad = True
                break
            if allowed_span_ids and span_id not in allowed_span_ids:
                errors.append(f"TASK_SPAN_ID_UNKNOWN:{span_id}")
                bad = True
                break
            span_ids.append(span_id)
        if bad:
            continue

        outcome = str(item.get("outcome", ""))
        raw_need = item.get("needs_reconstruction")
        if raw_need in {True, False, None}:
            need = raw_need
        else:
            need = None
            errors.append(f"NEEDS_RECONSTRUCTION_INVALID:{index}")
        route = str(item.get("domain_route", ""))
        if route not in {entry.value for entry in DomainRoute}:
            errors.append(f"DOMAIN_ROUTE_INVALID:{index}")
            route = DomainRoute.OTHER.value
        rubric, rubric_errors = coerce_rubric(item.get("rubric"))
        errors.extend(f"{code}:{index}" for code in rubric_errors)
        if outcome == "SUCCESS":
            if need is not False:
                errors.append(f"SUCCESS_NEEDS_RECONSTRUCTION_MUST_BE_FALSE:{index}")
            need = False
        elif outcome == "UNCERTAIN":
            if need is not None:
                errors.append(f"UNCERTAIN_NEEDS_RECONSTRUCTION_MUST_BE_NULL:{index}")
            need = None
        reason = item.get("reason")
        tasks.append(
            {
                "span_ids": span_ids,
                "outcome": outcome,
                "needs_reconstruction": need,
                "domain_route": route,
                "rubric": rubric,
                "reason": reason if isinstance(reason, str) else "",
            }
        )

    if not tasks and not errors:
        errors.append("TASKS_EMPTY")
    return tasks, errors


def judge_reconstructability(
    *,
    evidence: dict[str, Any],
    rule: dict[str, Any],
    model: ChatModel,
    model_name: str,
) -> dict[str, Any]:
    """调用模型并按任务列表关门。"""

    request_id = stable_id(
        "traceforge.reconstruction-screening-triage.v1",
        {
            "source_ref": evidence.get("source_ref"),
            "prompt_version": TRIAGE_PROMPT_VERSION,
            "model": model_name,
        },
    )
    request = ModelRequest(
        request_id,
        model_name,
        "在整条轨迹中找出值得重建的失败任务。证据不足必须 UNCERTAIN。只返回 JSON。",
        _prompt(evidence),
        TRIAGE_RESPONSE_SCHEMA,
        max_tokens=2048,
    )
    try:
        response = model.complete(request)
        payload = parse_json_object(response.text)
        receipt = receipt_for_response(response)
    except ModelGatewayError as exc:
        admitted = admit_after_tasks(
            rule=rule,
            tasks=[],
            parse_errors=(exc.code,),
        )
        return {
            **admitted,
            "tasks": [],
            "outcome": None,
            "needs_reconstruction": None,
            "domain_route": None,
            "rubric": empty_rubric(),
            "reason": "",
            "prompt_version": TRIAGE_PROMPT_VERSION,
            "model": model_name,
            "model_receipt": None,
            "errors": (exc.code,),
        }

    allowed = {
        str(item)
        for item in (evidence.get("span_ids") or ())
        if isinstance(item, str) and item
    }
    tasks, errors = _normalize_tasks(payload.get("tasks"), allowed_span_ids=allowed)
    parse_errors = tuple(
        item
        for item in errors
        if item
        in {
            "TASKS_NOT_ARRAY",
            "TASKS_EMPTY",
        }
        or item.startswith("TASK_SPAN_ID_UNKNOWN")
        or item.startswith("TASK_SPAN_IDS_INVALID")
        or item.startswith("TASK_NOT_OBJECT")
    )
    admitted = admit_after_tasks(
        rule=rule,
        tasks=tasks,
        parse_errors=parse_errors,
    )
    primary = next(
        (
            task
            for task in tasks
            if task.get("outcome") in {"FAILURE", "INCOMPLETE"}
            and task.get("needs_reconstruction") is True
        ),
        tasks[0] if tasks else None,
    )
    return {
        **admitted,
        "tasks": tasks,
        "outcome": None if primary is None else primary.get("outcome"),
        "needs_reconstruction": None if primary is None else primary.get("needs_reconstruction"),
        "domain_route": None if primary is None else primary.get("domain_route"),
        "rubric": empty_rubric() if primary is None else primary.get("rubric"),
        "reason": "" if primary is None else primary.get("reason") or "",
        "prompt_version": TRIAGE_PROMPT_VERSION,
        "model": model_name,
        "model_receipt": {
            "request_id": receipt.request_id,
            "provider": receipt.provider,
            "model": receipt.model,
            "status": receipt.status,
            "attempts": receipt.attempts,
            "latency_seconds": receipt.latency_seconds,
            "response_sha256": receipt.response_sha256,
            "error_code": receipt.error_code,
        },
        "errors": tuple(errors),
    }
