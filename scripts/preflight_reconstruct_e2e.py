"""TokenHub 短请求 + AGS 起沙盒预检。密钥不落盘，正文不写报告。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.runtime import (
    anthropic_sdk_base_url,
    load_channel_connection,
)
from traceforge.reconstruction.agents.sandbox import run_coro
from traceforge.reconstruction.container_verification import (
    SandboxUnavailableError,
    build_ags_runtime_factory,
)
from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    ModelRequest,
    NewAPIClient,
)
from traceforge.reconstruction.tls import pin_process_tls


def _tokenhub_claude(config: Path, *, model_name: str) -> dict[str, Any]:
    url, key = load_channel_connection(config, "claude")
    try:
        import anthropic
    except ImportError as exc:
        return {"ok": False, "channel": "claude", "error": f"ImportError:{exc}"}
    client = anthropic.Anthropic(
        base_url=anthropic_sdk_base_url(url),
        api_key=key,
        timeout=30.0,
    )
    started = time.monotonic()
    try:
        message = client.messages.create(
            model=model_name,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the single word pong."}],
        )
    except Exception as exc:
        return {
            "ok": False,
            "channel": "claude",
            "model": model_name,
            "error": f"{type(exc).__name__}",
            "latency_seconds": round(time.monotonic() - started, 3),
        }
    text = ""
    for block in getattr(message, "content", []) or []:
        text += str(getattr(block, "text", "") or "")
    return {
        "ok": bool(text.strip()),
        "channel": "claude",
        "model": model_name,
        "latency_seconds": round(time.monotonic() - started, 3),
        "has_text": bool(text.strip()),
    }


def _tokenhub_newapi(config: Path, *, channel: str, model_name: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        client = NewAPIClient.from_config(config, channel=channel, max_retries=1)
        response = client.complete(
            ModelRequest(
                request_id="e2e-preflight",
                model=model_name,
                system="Reply with JSON only.",
                prompt='Return {"pong":true}',
                response_schema="traceforge.e2e-preflight.v1",
                max_tokens=32,
                timeout_seconds=30,
            )
        )
    except ModelGatewayError as exc:
        return {
            "ok": False,
            "channel": channel,
            "model": model_name,
            "error": exc.code or type(exc).__name__,
            "latency_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "channel": channel,
            "model": model_name,
            "error": type(exc).__name__,
            "latency_seconds": round(time.monotonic() - started, 3),
        }
    return {
        "ok": bool(response.text.strip()),
        "channel": channel,
        "model": model_name,
        "latency_seconds": round(time.monotonic() - started, 3),
        "has_text": bool(response.text.strip()),
    }


def _ags(config: Path, harbor_root: Path, output_root: Path) -> dict[str, Any]:
    dest = output_root / "preflight-ags"
    dest.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    runtime = None
    try:
        factory = build_ags_runtime_factory(
            harbor_root=harbor_root,
            output_root=dest,
            config_path=config,
        )
        runtime = factory()
        run_coro(runtime.start())
        result = run_coro(runtime.exec("pwd", cwd="/", timeout_sec=20, user="user"))
        stdout = str(getattr(result, "stdout", "") or "")
        code = getattr(result, "return_code", None)
        return {
            "ok": True,
            "latency_seconds": round(time.monotonic() - started, 3),
            "exec_ok": code in {0, None} or bool(stdout.strip()),
            "has_stdout": bool(stdout.strip()),
        }
    except SandboxUnavailableError as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "latency_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "latency_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if runtime is not None:
            try:
                run_coro(runtime.stop(delete=True))
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", default="claude")
    parser.add_argument("--model-name", default="claude-opus-4-6")
    parser.add_argument(
        "--harbor-root",
        type=Path,
        default=Path("/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags"),
    )
    arguments = parser.parse_args()
    pin_process_tls()
    arguments.output.mkdir(parents=True, exist_ok=True)
    tokenhub_attempts: list[dict[str, Any]] = []
    if arguments.channel == "claude":
        primary = _tokenhub_claude(arguments.config, model_name=arguments.model_name)
    else:
        primary = _tokenhub_newapi(
            arguments.config, channel=arguments.channel, model_name=arguments.model_name
        )
    tokenhub_attempts.append(primary)
    chosen = dict(primary)
    ags = _ags(arguments.config, arguments.harbor_root, arguments.output)
    report = {
        "tokenhub": chosen,
        "tokenhub_attempts": tokenhub_attempts,
        "ags": {k: v for k, v in ags.items() if k != "stdout"},
        "ok": bool(chosen.get("ok") and ags.get("ok")),
        "timeout_seconds_for_live": 600,
    }
    dest = arguments.output / "preflight.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(dest)
    print(json.dumps({"ok": report["ok"], "tokenhub": chosen.get("channel"), "ags": ags.get("ok")}, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
