"""在 AGS 内执行的无第三方依赖 Anthropic Messages 探针。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def _messages_url(base: str) -> str:
    value = base.rstrip("/")
    if value.endswith("/v1/messages"):
        return value
    if value.endswith("/v1"):
        return value + "/messages"
    return value + "/v1/messages"


def _request(payload: dict[str, object]) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(
        _messages_url(os.environ["ANTHROPIC_BASE_URL"]),
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def main() -> int:
    model = os.environ["ANTHROPIC_MODEL"]
    common: dict[str, object] = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Reply with exactly AGS_OK"}],
    }
    status, body, headers = _request(common)
    parsed = json.loads(body)
    usage = parsed.get("usage") if isinstance(parsed, dict) else None
    content = parsed.get("content") if isinstance(parsed, dict) else None
    text = "".join(
        str(block.get("text", ""))
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )

    streaming = dict(common)
    streaming["stream"] = True
    stream_status, stream_body, _ = _request(streaming)
    stream_text = stream_body.decode("utf-8", "replace")

    tool_payload: dict[str, object] = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Call echo with value AGS_TOOL_OK"}],
        "tools": [
            {
                "name": "echo",
                "description": "Return a value",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }
        ],
        "tool_choice": {"type": "tool", "name": "echo"},
    }
    tool_status, tool_body, _ = _request(tool_payload)
    tool_parsed = json.loads(tool_body)
    tool_blocks = tool_parsed.get("content", []) if isinstance(tool_parsed, dict) else []
    tool_use = next(
        (
            block
            for block in tool_blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ),
        None,
    )

    report = {
        "schema_version": "harbor-ags-preflight/v1",
        "model": parsed.get("model") if isinstance(parsed, dict) else None,
        "non_stream": {
            "status": status,
            "ok": status == 200 and "AGS_OK" in text,
            "request_id_present": any(
                key.lower() in {"request-id", "x-request-id", "anthropic-request-id"}
                for key in headers
            ),
        },
        "stream": {
            "status": stream_status,
            "ok": stream_status == 200 and "message_stop" in stream_text,
        },
        "tool_use": {
            "status": tool_status,
            "ok": tool_status == 200 and tool_use is not None,
            "name": tool_use.get("name") if tool_use else None,
        },
        "usage": {
            key: (usage.get(key) if isinstance(usage, dict) else None)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    passed = all(report[name]["ok"] for name in ("non_stream", "stream", "tool_use"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
