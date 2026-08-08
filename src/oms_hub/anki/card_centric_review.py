"""Immutable server-issued selection acknowledgements for card-centric review."""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID


def selection_digest(
    selected_note_ids: tuple[int, ...],
    selected_generated_ids: tuple[str, ...],
    *,
    selection_order: tuple[str, ...] = (),
) -> str:
    if selection_order:
        expected = {
            *(f"existing:{note_id}" for note_id in selected_note_ids),
            *(f"generated:{card_id}" for card_id in selected_generated_ids),
        }
        if len(selection_order) != len(expected) or set(selection_order) != expected:
            raise ValueError("selection order must exactly bind the selected identities")
        value: dict[str, object] = {"selection_order": list(selection_order)}
    else:
        value = {
            "existing": sorted(set(selected_note_ids)),
            "generated": sorted(set(selected_generated_ids)),
        }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class OverflowAcknowledgement:
    token: str
    job_id: UUID
    review_revision: int
    selection_digest: str
    mandatory_count: int
    cap: int
    pipeline_contract_version: str
    model_config_sha256: str
    signature: str

    def document(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "job_id": str(self.job_id),
            "review_revision": self.review_revision,
            "selection_digest": self.selection_digest,
            "mandatory_count": self.mandatory_count,
            "cap": self.cap,
            "pipeline_contract_version": self.pipeline_contract_version,
            "model_config_sha256": self.model_config_sha256,
            "signature": self.signature,
        }


def issue_acknowledgement(
    secret: str,
    *,
    job_id: UUID,
    review_revision: int,
    selected_note_ids: tuple[int, ...],
    selected_generated_ids: tuple[str, ...],
    mandatory_count: int,
    cap: int,
    pipeline_contract_version: str,
    model_config_sha256: str,
    selection_order: tuple[str, ...] = (),
) -> OverflowAcknowledgement:
    selected_count = len(selected_note_ids) + len(selected_generated_ids)
    overflow_count = selected_count - cap
    if overflow_count <= 0:
        raise ValueError("an overflow acknowledgement is not needed")
    if selection_order and mandatory_count != overflow_count:
        raise ValueError("overflow acknowledgement count must equal the overflow slice")
    token = secrets.token_urlsafe(24)
    digest = selection_digest(
        selected_note_ids,
        selected_generated_ids,
        selection_order=selection_order,
    )
    payload = _acknowledgement_payload(
        token,
        job_id,
        review_revision,
        digest,
        mandatory_count,
        cap,
        pipeline_contract_version,
        model_config_sha256,
    )
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return OverflowAcknowledgement(
        token,
        job_id,
        review_revision,
        digest,
        mandatory_count,
        cap,
        pipeline_contract_version,
        model_config_sha256,
        signature,
    )


def verify_acknowledgement(
    secret: str,
    acknowledgement: OverflowAcknowledgement,
) -> bool:
    payload = _acknowledgement_payload(
        acknowledgement.token,
        acknowledgement.job_id,
        acknowledgement.review_revision,
        acknowledgement.selection_digest,
        acknowledgement.mandatory_count,
        acknowledgement.cap,
        acknowledgement.pipeline_contract_version,
        acknowledgement.model_config_sha256,
    )
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, acknowledgement.signature)


def _acknowledgement_payload(*values: object) -> str:
    return "\0".join(str(value) for value in values)
