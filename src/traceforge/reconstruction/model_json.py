"""模型 JSON 产物的校验反馈与逐次回执，不筛选原始 session。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelGatewayError,
    ModelRequest,
    parse_json_object,
    receipt_for_response,
)


class ModelOutputError(ValueError):
    """模型产物不满足当前阶段的数据契约。"""


def complete_checked_json(
    *, model: ChatModel, request: ModelRequest, output_root: Path,
    validate: Callable[[dict[str, Any]], object],
) -> tuple[dict[str, Any], Path]:
    """保存每次调用，按具体校验错误修正；传输失败与无进展不伪装成质量拒收。"""
    output_root.mkdir(parents=True, exist_ok=True)
    original = json.loads(request.prompt)
    seen: set[str] = set()
    seen_errors: dict[str, int] = {}
    number = max((int(p.name.removeprefix("attempt-"))
                  for p in output_root.glob("attempt-[0-9]*") if p.is_dir()), default=0)
    previous = sorted(output_root.glob("attempt-*/response.txt"),
                      key=lambda path: int(path.parent.name.removeprefix("attempt-")))
    if previous:
        path = previous[-1]
        saved = json.loads((path.parent / "request.json").read_text())
        context = json.loads(saved["prompt"])
        context.pop("validation_feedback", None)
        if (context != original or any(saved[key] != getattr(request, key)
                                       for key in ("model", "system", "response_schema"))):
            raise ModelOutputError("解析检查点的原始输入、模型或策略与当前请求不一致")
        receipt = json.loads((path.parent / "receipt.json").read_text())
        if receipt.get("finish_reason") in {"length", "max_tokens"}:
            raise ModelGatewayError("检查点输出被截断，不能据此恢复解析", code="RESPONSE_TRUNCATED")
        text = path.read_bytes().decode("utf-8")
        try:
            value = parse_json_object(text)
            validate(value)
        except (ModelOutputError, ModelGatewayError) as exc:
            seen.add(hashlib.sha256(text.encode()).hexdigest())
            request = replace(request, prompt=json.dumps({**original, "validation_feedback": {
                "error": str(exc), "previous_output": text,
                "instruction": "沿用已保存的真实输出，修正全部引用错误并返回完整对象，不修改原始数据。",
            }}, ensure_ascii=False))
        else:
            (path.parent / "revalidation.json").write_text(json.dumps({
                "status": "VALID", "model_called": False, "previous_status": receipt.get("status"),
                "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }) + "\n", encoding="utf-8")
            return value, path.parent
    while True:
        number += 1
        attempt = output_root / f"attempt-{number:04d}"
        attempt.mkdir()
        identity = asdict(request)
        identity.pop("request_id")
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        request = replace(request, request_id=f"session-{digest[:20]}")
        (attempt / "request.json").write_text(
            json.dumps(asdict(request), ensure_ascii=False, indent=2), encoding="utf-8")
        receipt: dict[str, Any] = {"status": "ERROR", "request_id": request.request_id,
                                   "model": request.model, "request_sha256": digest}
        try:
            response = model.complete(request)
            (attempt / "response.txt").write_text(response.text, encoding="utf-8")
            receipt.update(asdict(receipt_for_response(response)), status="ERROR",
                           input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                           finish_reason=response.finish_reason)
            if response.finish_reason in {"length", "max_tokens"}:
                raise ModelGatewayError("模型输出被截断，保留原文但不作质量判定",
                                        code="RESPONSE_TRUNCATED")
            try:
                value = parse_json_object(response.text)
                validate(value)
            except (ModelOutputError, ModelGatewayError) as exc:
                error = str(exc)
                receipt.update(status="INVALID", error_code="OUTPUT_INVALID", error=error)
                seen_errors[error] = seen_errors.get(error, 0) + 1
                if response.content_sha256 in seen or seen_errors[error] >= 3:
                    raise ModelOutputError(f"模型修正无进展：{error}") from exc
                seen.add(response.content_sha256)
                request = replace(request, prompt=json.dumps({**original, "validation_feedback": {
                    "error": error, "previous_output": response.text,
                    "instruction": "只修正派生 JSON 的错误，返回完整对象，不修改原始数据。",
                }}, ensure_ascii=False))
                continue
            (attempt / "result.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            receipt["status"] = "VALID"
            return value, attempt
        except (ModelGatewayError, ModelOutputError) as exc:
            receipt.update(status="ERROR", error_code=getattr(exc, "code", "NO_PROGRESS"),
                           error=str(exc))
            raise
        finally:
            (attempt / "receipt.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
