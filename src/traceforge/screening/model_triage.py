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
from .observable import ObservableEvidenceError, prepare_model_evidence
from .rubric import admit_after_tasks, coerce_rubric, empty_rubric
from .task_labels import normalize_task_labels


def _prompt(evidence: dict[str, Any]) -> str:
    keys = ", ".join(RUBRIC_KEYS)
    span_ids = list(evidence.get("span_ids") or ())
    return "\n".join(
        [
            "你是重建筛选器。第一性原理：用户 query 是不是一个有效任务，以及这个任务",
            "有没有完成或完成得好。工具调用只辅助看环境和过程，不是入选硬条件。",
            "有效任务但轨迹里缺少本该有的工具，也要重建。不要默认只评最后一问。",
            "不要编造未出现的文件、答案或用户身份。",
            "证据是 shared_context + spans[] 内全部用户要求、可见 assistant、",
            "tool_calls 与 tool 结果。",
            "thinking/reasoning 已省略，不得根据其缺失加分或扣分。",
            "模型输入保留完整可观察字段；不得编造证据中没有的内容。",
            "不要把抓取完成（leaf_response_status=completed）当成任务成功。",
            "闲聊、已成功问答不要标成需要重建。",
            "同一交付物的追问、纠错、继续可合并到一个 task 的多个 span_ids。",
            f"只能使用这些 span_id：{span_ids}",
            "",
            "只返回一个 JSON object：",
            "{",
            '  "tasks": [',
            "    {",
            '      "span_ids": ["span_..."],',
            '      "task_id": "model-task-1",',
            '      "is_actionable": true,',
            '      "outcome": "SUCCESS|FAILURE|INCOMPLETE|UNCERTAIN",',
            '      "needs_reconstruction": true|false|null,',
            '      "domain_route": "terminal|code_file|retrieval|other",',
            f'      "rubric": {{{keys}}},',
            '      "reason": "简短中文依据",',
            '      "evidence_refs": {"message_indices": [1], "span_ids": ["span_..."]}',
            "    }",
            "  ],",
            '  "relations": [{"from_task_id":"model-task-1","to_task_id":"model-task-2",',
            '    "type":"continuation|correction|dependency",',
            '    "evidence_refs":{"message_indices":[1,4],"span_ids":["span_a","span_b"]},',
            '    "reason":"有证据的关系"}]',
            "}",
            "rubric 每个键是 0 到 3 的整数。",
            "R1 意图可拟合：0 无任务或只能猜，2 目标基本明确即可，3 目标与交付物都有用户证据。",
            "R2 没完成或完成不好：只要任务没正常做好，都打 ≥2。包括：",
            "环境问题；模型答错/卡住/没交交付物/用户纠正；该用工具却没用；",
            "轨迹截断或不合理。有回复但对不准任务，也算完成不好，不要标 SUCCESS。",
            "R2=0 只给任务已经做好、过程正常结束。",
            "domain_route 是辅助标签，不决定能不能入选。对着真实工具和参数认：",
            "有终端/工作区工具（read/write/edit/glob/grep，或 exec/bash 读改文件、跑本地命令）→",
            "terminal；",
            "code_file 仅兼容旧记录；",
            "主要是检索（web_search/url_fetch/chat_history_get）→ retrieval；",
            "该域不属于本 terminal 重建管线；",
            "有效任务但没有本该有的工具 → 仍要重建，domain 按用户要的事标，",
            "不要因为没工具就标 other 并丢掉。wait 只是配对。",
            "闲聊、不是任务的寒暄才是 other。同一 capture 里按 task 分开标。",
            "每个 task 必须有 task_id、is_actionable、evidence_refs；每个 span 只能归属一个 task，",
            "所有 span 必须归属某个 task。task_id 只作本次关系引用，服务端会生成稳定 ID。",
            "relations 只在有用户/轨迹证据时填写；from 必须早于 to，证据必须触及两端。",
            "同一目标的澄清/纠错优先合并为一个 task；后续成功不得把此前失败 attempt 当重建样本。",
            "同一交付物按最终完成度合并：中间 SUCCESS 后纠错仍要并进同一个 task，",
            "outcome 取最后状态。不要先标 SUCCESS 再拆一个 correction。",
            "「后续成功不得把此前失败当样本」只适用于后面已经做成；不同交付物仍拆。",
            "relations 不能代替合并：能并的不要拆开再写 relation。",
            "outcome=SUCCESS 时 needs_reconstruction 必须为 false。",
            "outcome=UNCERTAIN 时 actionable task 的 needs_reconstruction 必须为 null。",
            "FAILURE/INCOMPLETE 且 R1≥2 时 needs_reconstruction 为 true，",
            "不论有没有工具、是不是 code_file。",
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
    max_input_chars: int = 120_000,
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
    try:
        model_evidence = prepare_model_evidence(evidence, max_chars=max_input_chars)
        request = ModelRequest(
            request_id,
            model_name,
            "在整条轨迹中找出值得重建的失败任务。证据不足必须 UNCERTAIN。只返回 JSON。",
            _prompt(model_evidence),
            TRIAGE_RESPONSE_SCHEMA,
            max_tokens=2048,
        )
        response = model.complete(request)
        payload = parse_json_object(response.text)
        receipt = receipt_for_response(response)
    except (ModelGatewayError, ObservableEvidenceError) as exc:
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
    tasks, relations, label_errors, label_status = normalize_task_labels(
        payload, evidence=evidence, source_ref=evidence.get("source_ref")
    )
    # The legacy shape checker is retained as a compatibility diagnostic; the
    # v10 label normalizer is the fail-closed admission contract.
    _, legacy_errors = _normalize_tasks(payload.get("tasks"), allowed_span_ids=allowed)
    errors = list(dict.fromkeys([*legacy_errors, *label_errors]))
    parse_errors = tuple(errors)
    admitted = admit_after_tasks(
        rule=rule,
        tasks=tasks,
        relations=relations,
        label_status=label_status,
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
        "relations": relations,
        "label_status": label_status,
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
