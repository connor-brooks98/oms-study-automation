"""Export deterministic JSON Schema snapshots for grounded-learning contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oms_hub.providers.contracts import (  # noqa: E402
    AnswerEvent,
    EvidenceRef,
    ProviderHealth,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
)

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_RESERVED = {
    "question-v1.json": "Study Hub question contract v1 — reserved",
    "mastery-v1.json": "Study Hub mastery contract v1 — reserved",
    "practice-v1.json": "Study Hub practice contract v1 — reserved",
    "journal-v1.json": "Study Hub journal contract v1 — reserved",
}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _active_schemas() -> dict[str, object]:
    knowledge = TypeAdapter(
        RetrievalScope | EvidenceRef | RetrievalRequest | RetrievalResult | ProviderHealth
    ).json_schema()
    knowledge["$schema"] = _SCHEMA
    knowledge["$id"] = "knowledge-v1.json"
    ask = TypeAdapter(AnswerEvent).json_schema()
    ask["$schema"] = _SCHEMA
    ask["$id"] = "ask-v1.json"
    return {"knowledge-v1.json": knowledge, "ask-v1.json": ask}


def export_schemas(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in _active_schemas().items():
        _write_json(output_dir / name, payload)
    for name, title in _RESERVED.items():
        _write_json(
            output_dir / name,
            {
                "$id": name,
                "$schema": _SCHEMA,
                "description": (
                    "Reserved contract namespace; no wire instances are valid until the owning "
                    "domain contract is implemented."
                ),
                "not": {},
                "title": title,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args()
    export_schemas(arguments.output_dir)


if __name__ == "__main__":
    main()
