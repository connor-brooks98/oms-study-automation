from __future__ import annotations

import hashlib
import json

from oms_hub.question_bank.contracts import ImportEnvelope, ImportPreview, RowIssue
from oms_hub.study_generation.native_quiz import QuizContractError, parse_native_quiz


def parse_import(raw: bytes) -> ImportEnvelope:
    """Validate a normalized v1 document without accessing external state."""
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("Import exceeds 10 MiB")
    return ImportEnvelope.model_validate_json(raw)


def preview_import(raw: bytes) -> ImportPreview:
    envelope = parse_import(raw)
    canonical = json.dumps(
        envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    issues: list[RowIssue] = []
    ready: list[int] = []
    seen: dict[tuple[str, str | None], tuple[int, str]] = {}
    for number, row in enumerate(envelope.rows, start=1):
        identity = (row.question_id, row.attempt_id)
        row_json = json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        previous = seen.get(identity)
        if previous is not None:
            code = "duplicate_row" if previous[1] == row_json else "duplicate_conflict"
            issues.append(RowIssue(number, code, f"Same row identity as row {previous[0]}"))
            continue
        seen[identity] = (number, row_json)
        if row.question is None:
            continue
        try:
            quiz = parse_native_quiz(
                json.dumps({"title": "Imported question", "questions": [row.question]})
            )
        except QuizContractError:
            issues.append(
                RowIssue(number, "invalid_question", "Body fails the native quiz contract")
            )
            continue
        if quiz.questions[0].image_ref is not None:
            issues.append(
                RowIssue(number, "missing_image", "Image requires existing media review/upload")
            )
            continue
        ready.append(number)
    return ImportPreview(
        hashlib.sha256(canonical.encode()).hexdigest(), envelope, tuple(issues), tuple(ready)
    )
