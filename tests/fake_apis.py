"""In-memory stand-ins for the OCR client and split client held by the run context – no network.

FakeOcr reads the test PDFs' own text (like a perfect OCR engine), records every call, and can be scripted to
fail for chosen pages. FakeSplitter splits one page per piece with pypdf and records every call.
"""
from __future__ import annotations

import io
import threading

from pypdf import PdfReader

from estamp.ocr_split import OcrResult, _split_local


class FakeOcr:
    def __init__(self, fail: dict[str, Exception] | None = None):
        self.fail = dict(fail or {})        # name substring → exception raised for matching pages
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def read(self, data: bytes, name: str) -> OcrResult:
        with self.lock:
            self.calls.append(name)
        for key, exc in self.fail.items():
            if key in name:
                raise exc
        text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
        return OcrResult(text, "", "fake")


class FakeSplitter:
    def __init__(self):
        self.calls: list[str] = []

    def split(self, data: bytes, name: str):
        self.calls.append(name)
        return _split_local(data, 1)
