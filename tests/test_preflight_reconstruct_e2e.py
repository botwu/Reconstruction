"""预检必须报告请求的模型，不把隐式 fallback 当作成功。"""

import json
import sys
from pathlib import Path

from scripts import preflight_reconstruct_e2e as preflight


def test_preflight_probes_requested_channel_and_model(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def tokenhub(config, *, channel, model_name):
        seen.update(channel=channel, model=model_name)
        return {"ok": True, "channel": channel, "model": model_name}

    monkeypatch.setattr(preflight, "_tokenhub_newapi", tokenhub)
    monkeypatch.setattr(preflight, "_ags", lambda config, harbor_root, output: {"ok": True})
    monkeypatch.setattr(preflight, "pin_process_tls", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--config",
            str(tmp_path / "config.yaml"),
            "--output",
            str(tmp_path / "out"),
            "--channel",
            "gpt",
            "--model-name",
            "gpt-5",
        ],
    )
    assert preflight.main() == 0
    assert seen == {"channel": "gpt", "model": "gpt-5"}
    report = json.loads((tmp_path / "out/preflight.json").read_text())
    assert report["tokenhub"]["channel"] == "gpt"
    assert len(report["tokenhub_attempts"]) == 1


def test_preflight_requested_failure_is_not_replaced_by_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        preflight,
        "_tokenhub_newapi",
        lambda config, *, channel, model_name: {"ok": False, "channel": channel},
    )
    monkeypatch.setattr(preflight, "_ags", lambda config, harbor_root, output: {"ok": True})
    monkeypatch.setattr(preflight, "pin_process_tls", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--config",
            str(tmp_path / "config.yaml"),
            "--output",
            str(tmp_path / "out"),
            "--channel",
            "gpt",
            "--model-name",
            "gpt-5",
        ],
    )
    assert preflight.main() == 2
    report = json.loads((tmp_path / "out/preflight.json").read_text())
    assert len(report["tokenhub_attempts"]) == 1
    assert report["ok"] is False

