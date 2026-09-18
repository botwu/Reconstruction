"""只看 completion 响应形态，不打印正文或密钥。"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    root = Path("/tmp/traceforge-paper-validate-r01-claude")
    for dest in sorted(root.glob("L*")):
        path = dest / "completion/private/model_exchange.json"
        print("=" * 40, dest.name)
        if not path.is_file():
            print("missing")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        text = (payload.get("response") or {}).get("text")
        prompt = (payload.get("request") or {}).get("prompt") or ""
        print("prompt_chars", len(prompt))
        if not isinstance(text, str):
            print("resp", type(text).__name__)
            continue
        stripped = text.strip()
        print("resp_chars", len(text), "starts", stripped[:3], "ends", stripped[-3:])
        print("fence", stripped.startswith("```"), "brace", stripped.startswith("{"))
        try:
            value = json.loads(stripped)
            print("json", type(value).__name__, list(value)[:8] if isinstance(value, dict) else "")
        except json.JSONDecodeError as exc:
            print("json_error", exc.msg, "pos", exc.pos)


if __name__ == "__main__":
    main()
