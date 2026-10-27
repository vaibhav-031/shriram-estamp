"""In-memory stand-ins for the OCR client and split client held by the run context – no network.

FakeOcr reads the test PDFs like a perfect OCR engine (the hidden text of scanned test pages, the text layer of
digital ones), records every call, and can be scripted to fail for chosen pages. FakeSplitter splits with pypdf
into parts of the requested size and records every call.
"""
from __future__ import annotations

import threading

from estamp.ocr_split import OcrResult, _split_local
from samples import ocr_text_of


class FakeOcr:
    def __init__(self, fail: dict[str, Exception] | None = None, certs: dict[str, str] | None = None):
        self.fail = dict(fail or {})        # exact page name → exception raised for that page
        self.certs = dict(certs or {})      # exact page name → certificate the "API" returns directly
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def read(self, data: bytes, name: str) -> OcrResult:
        with self.lock:
            self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]
        return OcrResult(ocr_text_of(data), self.certs.get(name, ""), "fake")


class FakeSplitter:
    def __init__(self, force_size: int | None = None):
        self.calls: list[tuple[str, int]] = []
        self.force_size = force_size        # simulate an API that ignores the requested splitSize

    def split(self, data: bytes, name: str, split_size: int = 1):
        self.calls.append((name, split_size))
        return _split_local(data, self.force_size or split_size)
