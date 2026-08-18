"""Immutable server-issued selection acknowledgements for card-centric review."""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oms_hub.anki.contracts import canonical_payload_sha256

V3_PHASE_G_SAFETY = {
    "cost_gate_scope": "durable_provider_attempt_lifecycle",
    "crash_durable_reservation": True,
    "v3_job_execution_enabled": True,
    "live_provider_authorized": False,
    "phase_h_precondition": (
        "offline-replay-only execution; live provider authorization remains prohibited"
    ),
}


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


class V3ReviewSnapshot(BaseModel):
    """Frozen R11 projection; deliberately separate from legacy reconciliation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rate_table_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    r0_to_r10_sha256: dict[str, str]
    evidence: dict[str, Any]
    existing_candidates: tuple[dict[str, Any], ...] = ()
    generated_cards: tuple[dict[str, Any], ...] = ()
    selected_existing_note_ids: tuple[int, ...] = ()
    selected_generated_card_ids: tuple[str, ...] = ()
    snapshot_sha256: str = ""

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"snapshot_sha256"})

    @model_validator(mode="after")
    def _validate(self) -> "V3ReviewSnapshot":
        required = {f"R{index}" for index in range(11)}
        if set(self.r0_to_r10_sha256) != required or any(
            len(value) != 64 for value in self.r0_to_r10_sha256.values()
        ):
            raise ValueError("R11 requires the complete R0-R10 hash chain")
        if self.selected_existing_note_ids != tuple(sorted(set(self.selected_existing_note_ids))):
            raise ValueError("selected existing candidates must be sorted and unique")
        if self.selected_generated_card_ids != tuple(sorted(set(self.selected_generated_card_ids))):
            raise ValueError("selected generated cards must be sorted and unique")
        if self.evidence.get("phase_g_safety") != V3_PHASE_G_SAFETY:
            raise ValueError("R11 Phase G safety boundary changed")
        known_existing = {
            int(item["note_id"]) for item in self.existing_candidates if "note_id" in item
        }
        known_generated = {
            str(item["card_id"]) for item in self.generated_cards if "card_id" in item
        }
        projected_existing = {
            int(item["note_id"])
            for item in self.existing_candidates
            if item.get("selected") is True and isinstance(item.get("note_id"), int)
        }
        projected_generated = {
            str(item["card_id"])
            for item in self.generated_cards
            if item.get("selected") is True and item.get("card_id") is not None
        }
        if not (
            set(self.selected_existing_note_ids) <= known_existing
            and set(self.selected_generated_card_ids) <= known_generated
            and set(self.selected_existing_note_ids) == projected_existing
            and set(self.selected_generated_card_ids) == projected_generated
        ):
            raise ValueError("R11 selections escape visible candidates")
        expected = canonical_payload_sha256(self.canonical_payload())
        if self.snapshot_sha256 not in {"", expected}:
            raise ValueError("R11 snapshot hash does not match frozen review evidence")
        if not self.snapshot_sha256:
            object.__setattr__(self, "snapshot_sha256", expected)
        return self


class V3ReviewReconciliation(BaseModel):
    """R11 selection result; no provider, embedding, or Anki behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: V3ReviewSnapshot
    can_render_envelope: bool
    findings: tuple[str, ...] = ()
    reconciliation_sha256: str = ""

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"reconciliation_sha256"})

    @model_validator(mode="after")
    def _validate(self) -> "V3ReviewReconciliation":
        expected = canonical_payload_sha256(self.canonical_payload())
        if self.reconciliation_sha256 not in {"", expected}:
            raise ValueError("R11 reconciliation hash does not match")
        if not self.reconciliation_sha256:
            object.__setattr__(self, "reconciliation_sha256", expected)
        return self


def reconcile_v3(snapshot: V3ReviewSnapshot) -> V3ReviewReconciliation:
    """Prove every scoped fact has one complete, reviewable terminal path."""
    findings: list[str] = []
    facts = _scoped_facts(snapshot.evidence.get("scope"))
    r8 = _rows(snapshot.evidence.get("gap_confirmation"), "records")
    r9 = _rows(snapshot.evidence.get("generation"), "resolutions")
    r10 = _rows(snapshot.evidence.get("dedupe"), "resolutions")
    if not facts or r8 is None or r9 is None or r10 is None:
        findings.append("R11 lacks the scoped-fact R8/R9/R10 closure")
    else:
        r8_by_fact = _partition_by_fact(r8, set(facts), exactly_one=True)
        r9_by_fact = _partition_by_fact(r9, set(facts))
        r10_by_fact = _partition_by_fact(r10, set(facts))
        if r8_by_fact is None:
            findings.append("R8 does not partition scoped facts exactly once")
        elif r9_by_fact is None or r10_by_fact is None:
            findings.append("R9/R10 contain missing, duplicate, or foreign fact rows")
        else:
            if _blocking(snapshot.evidence.get("generation")):
                findings.append("R9 is blocking")
            for fact_id, generation_allowed in facts.items():
                record = r8_by_fact[fact_id][0]
                state = record.get("state")
                if record.get("generation_allowed") is not generation_allowed:
                    findings.append(f"{fact_id}: R8 generation eligibility changed")
                    continue
                if state in {"covered_initial", "covered_residual"}:
                    if r9_by_fact[fact_id] or r10_by_fact[fact_id]:
                        findings.append(f"{fact_id}: covered fact leaked into R9/R10")
                    elif not _selected_existing_keep(snapshot, fact_id):
                        findings.append(f"{fact_id}: covered fact lacks a selected existing keep")
                elif state == "confirmed_missing":
                    if generation_allowed:
                        if not _accepted_generated_path(
                            snapshot, fact_id, r9_by_fact[fact_id], r10_by_fact[fact_id], r10
                        ):
                            findings.append(
                                f"{fact_id}: confirmed gap lacks an accepted R9/R10 path"
                            )
                    elif not _disabled_path(r9_by_fact[fact_id], r10_by_fact[fact_id]):
                        findings.append(
                            f"{fact_id}: generation-disabled gap is not visibly non-applicable"
                        )
                else:
                    findings.append(f"{fact_id}: R8 is unresolved or incomplete")
    return V3ReviewReconciliation(
        snapshot=snapshot,
        can_render_envelope=not findings,
        findings=tuple(findings),
    )


def _scoped_facts(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or not isinstance(value.get("concepts"), list):
        return {}
    facts: dict[str, bool] = {}
    for concept in value["concepts"]:
        if not isinstance(concept, dict) or not isinstance(concept.get("facts"), list):
            return {}
        for fact in concept["facts"]:
            fact_id = fact.get("fact_id") if isinstance(fact, dict) else None
            allowed = fact.get("generation_allowed") if isinstance(fact, dict) else None
            if not isinstance(fact_id, str) or not fact_id or not isinstance(allowed, bool):
                return {}
            if fact_id in facts:
                return {}
            facts[fact_id] = allowed
    return facts


def _rows(product: object, key: str) -> list[dict[str, Any]] | None:
    if not isinstance(product, dict) or not isinstance(product.get(key), list):
        return None
    return (
        [item for item in product[key] if isinstance(item, dict)]
        if all(isinstance(item, dict) for item in product[key])
        else None
    )


def _partition_by_fact(
    rows: list[dict[str, Any]], expected: set[str], *, exactly_one: bool = False
) -> dict[str, list[dict[str, Any]]] | None:
    result: dict[str, list[dict[str, Any]]] = {fact_id: [] for fact_id in expected}
    for row in rows:
        fact_id = row.get("fact_id")
        if not isinstance(fact_id, str) or fact_id not in result:
            return None
        result[fact_id].append(row)
    return None if exactly_one and any(len(value) != 1 for value in result.values()) else result


def _blocking(product: object) -> bool:
    return isinstance(product, dict) and bool(
        product.get("blocking_error") or product.get("blocking")
    )


def _selected_existing_keep(snapshot: V3ReviewSnapshot, fact_id: str) -> bool:
    return any(
        item.get("fact_id") == fact_id
        and item.get("selected") is True
        and item.get("disposition", item.get("decision")) == "keep"
        for item in snapshot.existing_candidates
    )


def _disabled_path(r9: list[dict[str, Any]], r10: list[dict[str, Any]]) -> bool:
    return (
        bool(r9)
        and all(
            row.get("status") == "unresolved"
            and "generation disabled" in str(row.get("reason", "")).casefold()
            for row in r9
        )
        and all(row.get("status") == "unresolved" for row in r10)
    )


def _accepted_generated_path(
    snapshot: V3ReviewSnapshot,
    fact_id: str,
    r9: list[dict[str, Any]],
    r10: list[dict[str, Any]],
    all_r10: list[dict[str, Any]],
) -> bool:
    if not r9 or any(row.get("status") != "generated" for row in r9):
        return False
    expected_cards = {row.get("card_id") for row in r9}
    if None in expected_cards or len(expected_cards) != len(r9):
        return False
    r10_by_card = {row.get("card_id"): row for row in r10}
    if set(r10_by_card) != expected_cards or len(r10_by_card) != len(r10):
        return False
    visible_cards = {str(row.get("card_id")): row for row in snapshot.generated_cards}
    visible_existing = {
        (int(row["note_id"]), str(row.get("fact_id", ""))): row
        for row in snapshot.existing_candidates
        if isinstance(row.get("note_id"), int)
    }
    for card_id in expected_cards:
        row = r10_by_card[card_id]
        status = row.get("status")
        if status == "generated":
            if visible_cards.get(str(card_id), {}).get("selected") is not True:
                return False
        elif status == "duplicate_of_existing":
            target = (
                row.get("dedupe", {}).get("duplicate_of")
                if isinstance(row.get("dedupe"), dict)
                else None
            )
            if not isinstance(target, str) or not target.startswith("note:"):
                return False
            try:
                note_id = int(target.removeprefix("note:"))
            except ValueError:
                return False
            candidate = visible_existing.get((note_id, fact_id))
            if (
                not candidate
                or candidate.get("selected") is not True
                or candidate.get("disposition", candidate.get("decision")) != "keep"
            ):
                return False
        elif status == "duplicate_of_generated":
            target = (
                row.get("dedupe", {}).get("duplicate_of")
                if isinstance(row.get("dedupe"), dict)
                else None
            )
            if not isinstance(target, str) or target == card_id or target not in visible_cards:
                return False
            target_row = next((item for item in all_r10 if item.get("card_id") == target), None)
            if (
                target_row is None
                or target_row.get("status") != "generated"
                or visible_cards[target].get("selected") is not True
            ):
                return False
        else:
            return False
    return True
