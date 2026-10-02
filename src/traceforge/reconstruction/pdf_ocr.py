"""可选的单页本地 OCR 工作进程；依赖只装在显式配置的独立解释器中。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path


def run(pdf: Path, page_number: int, output_root: Path) -> dict:
    lock_path = Path(__file__).with_name("pdf_ocr_lock.json")
    lock = json.loads(lock_path.read_text())
    if list(sys.version_info[:2]) != lock["python"]:
        raise ValueError("OCR 解释器版本未通过当前锁定配置验证")
    versions = {name: importlib.metadata.version(name) for name in lock["versions"]}
    if versions != lock["versions"]:
        raise ValueError(f"OCR 包版本不匹配：实际 {versions}")
    import pymupdf
    import rapidocr_onnxruntime
    from rapidocr_onnxruntime import RapidOCR

    package = Path(rapidocr_onnxruntime.__file__).parent
    models = {name: hashlib.sha256((package / name).read_bytes()).hexdigest()
              for name in lock["model_sha256"]}
    if models != lock["model_sha256"]:
        raise ValueError("OCR 模型或配置哈希与锁定记录不一致")
    raw = pdf.read_bytes()
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        if not 1 <= page_number <= len(document):
            raise ValueError("OCR 页号超出原 PDF 范围")
        pixmap = document[page_number - 1].get_pixmap(matrix=pymupdf.Matrix(1.8, 1.8))
        image = pixmap.tobytes("png")
        image_sha = hashlib.sha256(image).hexdigest()
        image_path = output_root / f"{image_sha}.png"
        image_path.write_bytes(image)
        engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
        rows, _ = engine(str(image_path))
        return {
            "schema_version": "traceforge.pdf-ocr-page.v1",
            "source_pdf_sha256": hashlib.sha256(raw).hexdigest(),
            "page_number": page_number, "page_count": len(document),
            "image_sha256": image_sha, "image_size": [pixmap.width, pixmap.height],
            "versions": versions, "model_sha256": models,
            "runtime_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
            "blocks": [{"bbox": box, "text": text, "confidence": float(confidence)}
                       for box, text, confidence in rows or []],
            "limitations": [
                "这是页图 OCR 的带坐标文本块，保留检测顺序；双栏阅读顺序未经重排或人工核对。",
                "公式、上下标、表格和署名可能误识别或漏检；置信度不是正确性保证，不能宣称精确恢复。",
                "原 PDF 和页图为来源；OCR 不替代原文本层。像素坐标原点在页图左上角。",
            ],
        }


if __name__ == "__main__":
    print(json.dumps(run(Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])),
                     ensure_ascii=False))
