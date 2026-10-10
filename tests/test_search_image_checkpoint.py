"""图片检查点必须携带原件，恢复后仍能读取相同像素。"""

import base64
import hashlib
import json
import shutil

import pytest
from test_search_images import URL, image_tools

from traceforge.reconstruction.agents.session import AgentConversation, AgentSession
from traceforge.reconstruction.search_environment import (
    _restore_search_checkpoint,
    save_search_checkpoint,
)
from traceforge.reconstruction.search_tools import SearchTools
from traceforge.reconstruction.session_source import indexed_session


def image_checkpoint(tmp_path, monkeypatch, mime):
    network, _, raw = image_tools(tmp_path / "original-web", monkeypatch, mime)
    assert network.open(URL)["success"]
    source = {"line_sha256": "source", "raw_session": {"messages": []}}
    task = {"task_id": "image-checkpoint"}
    messages = [{"role": "user", "content": json.dumps({
        "SOURCE_SESSION": indexed_session(source["raw_session"]),
    })}]
    checkpoint = save_search_checkpoint(
        source=source, task=task,
        session=AgentSession(conversation=AgentConversation(messages=messages)),
        network=network, output_root=tmp_path / "saved",
    )
    shutil.copytree(checkpoint.parent, tmp_path / "moved")
    shutil.rmtree(network.root)
    return tmp_path / "moved" / checkpoint.name, source, task, raw, messages


@pytest.mark.parametrize("mime,suffix", [("image/jpeg", ".jpeg"), ("image/png", ".png")])
def test_image_checkpoint_preserves_portable_original_pixels(tmp_path, monkeypatch, mime, suffix):
    checkpoint, source, task, raw, messages = image_checkpoint(tmp_path, monkeypatch, mime)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(checkpoint.read_text())
    assert manifest["files"]["web/" + digest + suffix] == digest
    resumed = SearchTools(tmp_path / "resumed")
    conversation = _restore_search_checkpoint(
        checkpoint, source=source, task=task, network=resumed,
    )
    assert conversation.messages == messages
    result = resumed.view_image(URL)
    assert result["_multimodal"] is True
    data_url = result["content"][1]["image_url"]["url"]
    assert base64.b64decode(data_url.split(",", 1)[1]) == raw
    assert result["meta"]["raw_sha256"] == digest
    assert resumed.ready() is False


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_image_checkpoint_rejects_missing_or_modified_jpeg(tmp_path, monkeypatch, damage):
    checkpoint, source, task, raw, _ = image_checkpoint(tmp_path, monkeypatch, "image/jpeg")
    image = checkpoint.parent / "web" / (hashlib.sha256(raw).hexdigest() + ".jpeg")
    if damage == "missing":
        image.unlink(missing_ok=True)
    else:
        image.write_bytes(b"changed original")
    with pytest.raises(ValueError):
        _restore_search_checkpoint(
            checkpoint, source=source, task=task, network=SearchTools(tmp_path / "resumed"),
        )
