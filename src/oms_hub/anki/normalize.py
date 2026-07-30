import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from oms_hub.anki.contracts import SnapshotNote

_CLOZE = re.compile(r"\{\{c\d+::(.*?)(?:::[^{}]*?)?\}\}", re.IGNORECASE)
_MEDIA = re.compile(
    r"""(?:
        <(?:img|source)\b[^>]*?\bsrc\s*=\s*["'](?P<html>[^"']+)["'][^>]*>
        |
        \[sound:(?P<sound>[^\]\r\n]+)\]
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")
_KEPT_TAG_ROOTS = (
    "ankihub_optional::lmu_oms_ii",
    "#ak_step",
    "#pathoma",
    "#sketchy",
    "#firstaid",
    "#boardsandbeyond",
    "#ome",
    "#uworld",
)


@dataclass(frozen=True, slots=True)
class MediaReference:
    field_name: str
    filename: str
    media_type: Literal["image", "audio"]
    source_order: int


@dataclass(frozen=True, slots=True)
class NormalizedNote:
    note_id: int
    model_name: str
    text: str
    extra: str
    raw_fields: dict[str, str]
    tags: tuple[str, ...]
    card_ids: tuple[int, ...]
    media: tuple[MediaReference, ...]
    token_signature: str
    content_sha256: str


def normalize_html(value: str) -> str:
    """Produce compact searchable text while preserving visible cloze answers."""
    unclozed = _CLOZE.sub(lambda match: match.group(1), value)
    tree = HTMLParser(_MEDIA.sub("", unclozed))
    for node in tree.css("script,style,img,source,audio,video"):
        node.decompose()
    return _SPACE.sub(" ", html.unescape(tree.text(separator=" ", strip=True))).strip()


def kept_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                tag.strip()
                for tag in tags
                if tag.strip().casefold().startswith(_KEPT_TAG_ROOTS)
            },
            key=str.casefold,
        )
    )


def extract_media_references(fields: dict[str, str]) -> tuple[MediaReference, ...]:
    references: list[MediaReference] = []
    for field_name, value in fields.items():
        for order, match in enumerate(_MEDIA.finditer(value)):
            image = match.group("html")
            filename = html.unescape(image or match.group("sound")).strip()
            if _is_external_media_url(filename):
                continue
            references.append(
                MediaReference(
                    field_name=field_name,
                    filename=filename,
                    media_type="image" if image is not None else "audio",
                    source_order=order,
                )
            )
    return tuple(references)


def _is_external_media_url(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme or parsed.netloc)


def normalize_snapshot_note(note: SnapshotNote) -> NormalizedNote:
    tags = kept_tags(note.tags)
    raw_fields = dict(note.fields)
    text = normalize_html(raw_fields.get("Text", ""))
    extra = normalize_html(raw_fields.get("Extra", ""))
    media = extract_media_references(raw_fields)
    signature = " ".join(sorted(set(_TOKEN.findall(f"{text} {extra}".casefold()))))
    canonical = {
        "note_id": note.note_id,
        "model_name": note.model_name,
        "text": text,
        "extra": extra,
        "raw_fields": raw_fields,
        "tags": tags,
        "card_ids": sorted(note.card_ids),
        "media": [
            {
                "field_name": item.field_name,
                "filename": item.filename,
                "media_type": item.media_type,
                "source_order": item.source_order,
            }
            for item in media
        ],
        "token_signature": signature,
    }
    content_sha256 = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedNote(
        note_id=note.note_id,
        model_name=note.model_name,
        text=text,
        extra=extra,
        raw_fields=raw_fields,
        tags=tags,
        card_ids=tuple(sorted(note.card_ids)),
        media=media,
        token_signature=signature,
        content_sha256=content_sha256,
    )
