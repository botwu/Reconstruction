"""PDF 按原始文件提取，保留分页与不能读取的部分。"""

import hashlib
import io
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from traceforge.reconstruction.search_tools import SearchTools


def pdf_bytes(texts):
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in texts:
        page = writer.add_blank_page(width=200, height=200)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        if text:
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = stream
    writer.add_metadata({"/Title": "Original source"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def response(raw, url):
    stream = io.BytesIO(raw)
    stream.geturl = lambda: url
    return stream


@pytest.mark.parametrize("raw,success", [
    (pdf_bytes(["Source page one", "Source page two", ""]), True),
    (pdf_bytes([""]), False),
    (b"<html>Login required</html>", False),
], ids=["text-and-blank-pages", "no-text-layer", "html-response"])
def test_pdf_uses_original_bytes_and_preserves_extraction_boundary(tmp_path, monkeypatch, raw, success):
    from traceforge.reconstruction import search_tools

    url = "https://example.org/source.pdf"
    monkeypatch.setattr(search_tools, "_public_url", lambda value: None)
    monkeypatch.setattr(search_tools.urllib.request, "build_opener", lambda *args: SimpleNamespace(
        open=lambda *args, **kwargs: response(raw, url),
    ))
    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_fetch_page", lambda value: pytest.fail("PDF 不能交给网页抽取器"))
    result = tools.open(url)
    assert result["success"] is success
    if not success:
        assert "error" in result
        return
    assert result["content_kind"] == "pdf_text"
    assert result["extraction_scope"] == "text_layer"
    assert result["empty_text_pages"] == [3]
    assert result["page_count"] == 3
    assert "Source page one" in result["text"]
    assert "Source page two" in result["text"]
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / (result["raw_sha256"] + ".pdf")).read_bytes() == raw
    assert result["limitations"]


def test_pdf_redirect_is_checked_before_contacting_destination(monkeypatch):
    from traceforge.reconstruction import search_tools

    checked = []

    def validate(url):
        checked.append(url)
        raise ValueError("private destination")

    monkeypatch.setattr(search_tools, "_public_url", validate)
    with pytest.raises(ValueError, match="private destination"):
        search_tools.PublicSourceRedirect().redirect_request(
            None, None, 302, "Found", {}, "http://127.0.0.1/private.pdf",
        )
    assert checked == ["http://127.0.0.1/private.pdf"]
