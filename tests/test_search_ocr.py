"""显式逐页 OCR 必须保留原始材料、实际坐标和可恢复来源。"""

import hashlib
import json
from types import SimpleNamespace

import pytest
from test_search_pdf import pdf_bytes, response

from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.search_tools import SearchTools


def ocr_network(tmp_path, monkeypatch):
    from traceforge.reconstruction import search_tools

    url = "https://example.org/paper.pdf"
    raw = pdf_bytes(["Original text layer"])
    monkeypatch.setattr(search_tools, "_public_url", lambda value: None)
    monkeypatch.setattr(search_tools.urllib.request, "build_opener", lambda *args: SimpleNamespace(
        open=lambda *args, **kwargs: response(raw, url),
    ))
    tools = SearchTools(tmp_path)
    tools._ocr_python = "/configured/python"
    image = b"actual rendered page fixture"
    item = {
        "schema_version": "traceforge.pdf-ocr-page.v1", "page_number": 1, "page_count": 1,
        "source_pdf_sha256": hashlib.sha256(raw).hexdigest(),
        "image_sha256": hashlib.sha256(image).hexdigest(), "image_size": [360, 360],
        "versions": {"rapidocr-onnxruntime": "1.4.4"},
        "model_sha256": {"fixture.onnx": "1" * 64},
        "blocks": [{"bbox": [[1, 2], [3, 2], [3, 4], [1, 4]], "text": "真实识别片段",
                    "confidence": 0.91}],
        "limitations": ["OCR 检测顺序不是双栏阅读顺序；公式未核实。"],
    }

    def run(args, **kwargs):
        assert args[0] == "/configured/python"
        assert "PYTHONPATH" not in kwargs["env"]
        (tmp_path / (item["image_sha256"] + ".png")).write_bytes(image)
        return SimpleNamespace(returncode=0, stdout=json.dumps(item, ensure_ascii=False), stderr="")

    monkeypatch.setattr(search_tools.subprocess, "run", run)
    return tools, url, item


def test_explicit_ocr_preserves_text_layer_and_restores_paginated_blocks(tmp_path, monkeypatch):
    tools, url, item = ocr_network(tmp_path / "old", monkeypatch)
    original = tools.open(url)
    session = AgentSession(web_open_handler=tools.open)
    raw_result = execute_tool("web_open", {"url": url, "ocr_page": 1, "offset": 3, "limit": 40}, session)
    result = json.loads(raw_result)
    assert result["success"] and result["content_kind"] == "pdf_ocr"
    assert result["ocr_page"] == 1 and result["image_sha256"] == item["image_sha256"]
    assert tools.pages[url]["text"] == original["text"]
    assert "ocr_pages" not in result
    assert result["text"] == json.dumps(item["blocks"], ensure_ascii=False)[3:43]
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tools.root.iterdir()}
    resumed = SearchTools(tmp_path / "new")
    resumed.restore(tools.root, origin="author-checkpoint", checkpoint_files=files)
    resumed._ocr_python = None
    assert resumed.open(url, ocr_page=1)["success"]
    assert resumed.open(url)["text"] == original["text"]
    assert (resumed.root / (item["image_sha256"] + ".png")).is_file()


def test_ocr_unavailable_is_explicit_and_does_not_replace_pdf(tmp_path, monkeypatch):
    tools, url, _ = ocr_network(tmp_path, monkeypatch)
    original = tools.open(url)
    tools._ocr_python = None
    result = tools.open(url, ocr_page=1)
    assert not result["success"] and "OCR" in result["error"]
    assert result["ocr_page"] == 1 and result["offset"] == 0
    assert tools.pages[url]["text"] == original["text"]
    assert "ocr_pages" not in tools.pages[url]


@pytest.mark.parametrize("damage", ["image", "blocks", "page_number"])
def test_ocr_restore_rejects_unbound_derivatives(tmp_path, monkeypatch, damage):
    tools, url, item = ocr_network(tmp_path / "old", monkeypatch)
    assert tools.open(url, ocr_page=1)["success"]
    if damage == "image":
        (tools.root / (item["image_sha256"] + ".png")).write_bytes(b"other image")
    else:
        path = tools.root / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        page = json.loads(path.read_text())
        page["ocr_pages"]["1"][damage] = [] if damage == "blocks" else 2
        path.write_text(json.dumps(page))
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tools.root.iterdir()}
    with pytest.raises(ValueError):
        SearchTools(tmp_path / "new").restore(tools.root, origin="tampered", checkpoint_files=files)


def test_ocr_checkpoint_and_harbor_export_bind_actual_assets(tmp_path, monkeypatch):
    from test_harbor_task_export import search_environment as environment_fixture

    from traceforge.harbor_task import export_search_task
    from traceforge.reconstruction.agents.session import AgentConversation
    from traceforge.reconstruction.search_environment import save_search_checkpoint

    tools, url, item = ocr_network(tmp_path / "web", monkeypatch)
    assert tools.open(url, ocr_page=1)["success"]
    checkpoint = save_search_checkpoint(
        source={"line_sha256": "source"}, task={"task_id": "task"},
        session=AgentSession(conversation=AgentConversation(messages=[])),
        network=tools, output_root=tmp_path / "checkpoint",
    )
    manifest = json.loads(checkpoint.read_text())
    assert "web/" + item["image_sha256"] + ".png" in manifest["files"]
    environment = environment_fixture()
    environment.update(requires_live_web=False, live_references=[tools.pages[url]])
    with pytest.raises(ValueError, match="OCR"):
        export_search_task(environment, tmp_path / "missing-assets")
    task = export_search_task(environment, tmp_path / "export", evidence_root=tools.root)
    for asset in tools.root.iterdir():
        if asset.suffix in {".raw", ".png", ".pdf"}:
            assert (task / "workspace/source-assets" / asset.name).read_bytes() == asset.read_bytes()
    delivered = json.loads((task / "workspace/evidence.json").read_text())
    assert delivered["live_references"] == environment["live_references"]
    assert "OCR" in (task / "instruction.md").read_text()
    index = json.loads((task / "workspace/evidence-index.json").read_text())
    entry = next(item for item in index["sources"] if item.get("url") == url)
    assert entry["ocr_pages"][0]["page"] == 1
    assert (task / "workspace" / entry["ocr_pages"][0]["raw_path"]).is_file()
    assert (task / "workspace" / entry["body_path"]).read_text() == tools.pages[url]["text"]
    assert not (task / "environment/pdf_ocr.py").exists()
    (tools.root / (item["image_sha256"] + ".png")).write_bytes(b"modified image")
    with pytest.raises(ValueError, match="OCR"):
        export_search_task(environment, tmp_path / "damaged-assets", evidence_root=tools.root)


def test_restore_cannot_merge_different_pdf_with_same_text_layer(tmp_path, monkeypatch):
    import shutil

    tools, url, _ = ocr_network(tmp_path / "old", monkeypatch)
    tools.open(url, ocr_page=1)
    other = tmp_path / "other"
    shutil.copytree(tools.root, other)
    path = other / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    page = json.loads(path.read_text())
    page.pop("ocr_pages")
    raw = (other / (page["raw_sha256"] + ".pdf")).read_bytes() + b"\\n% different source"
    page["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    (other / (page["raw_sha256"] + ".pdf")).write_bytes(raw)
    path.write_text(json.dumps(page))
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in other.iterdir()}
    with pytest.raises(ValueError, match="同一来源快照内容冲突"):
        tools.restore(other, origin="other-pdf", checkpoint_files=files)


def test_ocr_worker_version_mismatch_is_explicit(tmp_path, monkeypatch):
    from traceforge.reconstruction import pdf_ocr

    monkeypatch.setattr(pdf_ocr.sys, "version_info", (0, 0))
    with pytest.raises(ValueError, match="解释器版本"):
        pdf_ocr.run(tmp_path / "unused.pdf", 1, tmp_path)
