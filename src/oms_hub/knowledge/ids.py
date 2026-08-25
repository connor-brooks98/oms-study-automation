from __future__ import annotations

import base64
import hashlib

from oms_hub.files.atomic import sha256_file

__all__ = ["evidence_id", "sha256_file", "sha256_text", "source_revision_id"]


def _digest(value: str) -> str:
    raw = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.b32encode(raw).decode("ascii").lower().rstrip("=")[:26]


def sha256_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def source_revision_id(source_document_id: str, file_sha256: str) -> str:
    return f"sr_{_digest(f'{source_document_id}\0{file_sha256}')}"


def evidence_id(source_revision_id_value: str, locator: str, content_sha256: str) -> str:
    return f"ev_{_digest(f'{source_revision_id_value}\0{locator}\0{content_sha256}')}"
