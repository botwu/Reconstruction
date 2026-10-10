"""图片作为原始证据交付，不能只剩链接或文字读取成功记录。"""

import base64
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from traceforge.harbor_task import export_search_task
from traceforge.reconstruction import search_tools


def image_environment(root, monkeypatch):
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lqkAAAAASUVORK5CYII="
    )
    digest = hashlib.sha256(raw).hexdigest()
    url = "https://example.org/rates.png"

    def response(*args, **kwargs):
        stream = io.BytesIO(raw)
        stream.geturl = lambda: url
        stream.headers = {"Content-Type": "image/png"}
        return stream

    monkeypatch.setattr(search_tools.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=response))
    tools = search_tools.SearchTools(root)
    monkeypatch.setattr(tools, "_check_public_url", lambda value: None)
    assert tools.open(url)["success"]
    page = tools.pages[url]
    image = root / (digest + ".png")
    environment = {
        "schema_version": "traceforge.search-environment.v5", "status": "READY",
        "errors": [], "missing_inputs": [], "requires_live_web": False,
        "task": {"task_id": "image-input", "task_instruction": "比较报价。",
                 "source_task": {"user_texts": ["比较报价。"]}},
        "captures": [], "live_references": [page],
        "context_messages": [], "limitations": [],
    }
    return environment, image


def test_harbor_delivers_original_image_and_locatable_source(tmp_path, monkeypatch):
    environment, image = image_environment(tmp_path / "web", monkeypatch)
    task = export_search_task(environment, tmp_path / "tasks", evidence_root=image.parent)
    workspace = task / "workspace"
    index = json.loads((workspace / "evidence-index.json").read_text())
    entry = index["sources"][0]
    assert entry["image_path"] == "source-assets/" + image.name
    assert entry["image_sha256"] == environment["live_references"][0]["raw_sha256"]
    assert entry["mime_type"] == "image/png"
    assert (workspace / entry["image_path"]).read_bytes() == image.read_bytes()
    assert "vision_analyze" in (task / "instruction.md").read_text()


@pytest.mark.parametrize("damage", ["missing", "changed", "wrong_mime"])
def test_harbor_rejects_image_with_missing_or_conflicting_source(tmp_path, monkeypatch, damage):
    environment, image = image_environment(tmp_path / "web", monkeypatch)
    if damage == "missing":
        image.unlink()
    elif damage == "changed":
        image.write_bytes(image.read_bytes() + b"changed")
    else:
        environment["live_references"][0]["mime_type"] = "image/jpeg"
    with pytest.raises(ValueError):
        export_search_task(environment, tmp_path / "tasks", evidence_root=image.parent)
