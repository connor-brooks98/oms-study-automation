"""Optional local OCR, deliberately unavailable outside the supported NUC runtime."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path


class LocalOcr:
    """Lazy RapidOCR adapter; tests can inject a tiny callable instead."""

    def __init__(self, recognize: Callable[[Path], str] | None = None) -> None:
        self._recognize = recognize
        self._engine: object | None = None

    def text(self, path: Path) -> str | None:
        if self._recognize is not None:
            return self._recognize(path).strip() or None
        if sys.platform != "win32" or sys.version_info >= (3, 13):
            return None
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
            if self._engine is None:
                self._engine = RapidOCR()
            result, _elapsed = self._engine(str(path))
        except (ImportError, OSError, RuntimeError, ValueError):
            return None
        return "\n".join(str(item[1]) for item in result or ()).strip() or None
