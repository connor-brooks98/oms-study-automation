"""Private activity projection and cooperative holds over the existing workers."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, cast, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from oms_hub.anki.models import AnkiCurationJobModel
from oms_hub.db import Database
from oms_hub.models import (
    BankImportModel,
    BankReviewRunModel,
    GenerationJobModel,
    IngestionJobModel,
    LectureModel,
    ProcessControlModel,
    StudioRunArtifactModel,
    StudioRunModel,
    StudioSourceModel,
    StudioSourceOperationModel,
    StudyRevisionModel,
    UploadItemModel,
)

_MODELS = {
    "ingestion": IngestionJobModel,
    "generation": GenerationJobModel,
    "studio": StudioRunModel,
    "source": StudioSourceModel,
    "anki": AnkiCurationJobModel,
    "bank": BankImportModel,
}
_FINISHED = {
    "complete",
    "completed",
    "ready",
    "attached",
    "awaiting_review",
    "awaiting_images",
    "ready_for_review",
    "failed",
    "needs_review",
    "quarantined",
    "awaiting_confirmation",
    "discarded",
    "deleted",
    "canceled",
    "removed",
}
_OPERATION: ContextVar[tuple[Database, str, str] | None] = ContextVar(
    "process_operation", default=None
)


class ProcessHeld(Exception):
    """The worker reached a safe boundary with a durable user hold."""


def claim_allowed(family: str, job_id: Any) -> ColumnElement[bool]:
    return (
        ~select(ProcessControlModel.job_id)
        .where(
            ProcessControlModel.family == family,
            ProcessControlModel.job_id == cast(job_id, String),
            ProcessControlModel.requested_action.in_(("pause", "remove")),
        )
        .exists()
    )


def acknowledge_boundary(database: Database, family: str, job_id: str) -> bool:
    with database.session() as session:
        control = session.get(ProcessControlModel, (family, str(job_id)))
        held = control is not None and control.requested_action in {"pause", "remove"}
        if held and control is not None:
            control.acknowledged_at = control.acknowledged_at or datetime.now(UTC).isoformat()
            if control.requested_action == "remove":
                control.hidden_at = control.acknowledged_at
        return held


def recover_hold(session: Session, family: str, job_id: str) -> bool:
    """Acknowledge an old worker only inside existing pre-worker startup recovery."""
    control = session.get(ProcessControlModel, (family, str(job_id)))
    if control is None or control.requested_action not in {"pause", "remove"}:
        return False
    if not control.acknowledged_at:
        control.events_json = json.dumps(
            json.loads(control.events_json)
            + [
                {
                    "action": "recovered_unacknowledged",
                    "at": datetime.now(UTC).isoformat(),
                }
            ]
        )
    control.acknowledged_at = control.acknowledged_at or datetime.now(UTC).isoformat()
    if control.requested_action == "remove":
        control.hidden_at = control.acknowledged_at
    return True


def checkpoint() -> None:
    operation = _OPERATION.get()
    if operation is not None and acknowledge_boundary(*operation):
        raise ProcessHeld()


@contextmanager
def process_operation(database: Database, family: str, job_id: str) -> Iterator[None]:
    token = _OPERATION.set((database, family, str(job_id)))
    try:
        checkpoint()
        yield
    except BaseException:
        raise
    else:
        acknowledge_boundary(database, family, str(job_id))
    finally:
        _OPERATION.reset(token)


def _object(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class ProcessService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self, owner_id: str) -> dict[str, object]:
        if not owner_id:
            raise ValueError("private owner is required")
        items = []
        with self.database.session() as session:
            for family, model in _MODELS.items():
                for row in session.scalars(select(model)):
                    try:
                        self._authorize(session, family, row, owner_id)
                    except KeyError:
                        continue
                    card = self._card(session, family, row)
                    if not card["hidden"]:
                        items.append(card)
        # Retain every unfinished/review item; bound only completed history.
        ongoing = [
            item
            for item in items
            if item["state"]
            not in {
                "complete",
                "completed",
                "ready",
                "attached",
                "discarded",
                "deleted",
                "canceled",
                "removed",
            }
        ]
        completed = [item for item in items if item not in ongoing]
        completed.sort(key=lambda item: str(item["updated_at"]), reverse=True)
        visible = ongoing + completed[:100]
        visible.sort(key=lambda item: str(item["updated_at"]), reverse=True)
        return {
            "items": visible,
            "completed_limit": 100,
            "completed_history_truncated": len(completed) > 100,
        }

    def _row(self, session: Session, family: str, job_id: str, owner: str) -> Any:
        model = _MODELS.get(family)
        if model is None:
            raise KeyError(job_id)
        identity: str | int = job_id
        if family == "ingestion":
            try:
                identity = int(job_id)
            except ValueError as error:
                raise KeyError(job_id) from error
        row = session.get(model, identity)
        if row is None:
            raise KeyError(job_id)
        self._authorize(session, family, row, owner)
        return row

    @staticmethod
    def _authorize(session: Session, family: str, row: Any, owner: str) -> None:
        if not owner:
            raise KeyError(row.id)
        if family == "bank" and row.learner_id != owner:
            raise KeyError(row.id)
        if family == "source":
            bank = session.get(BankImportModel, row.id)
            if bank is not None and bank.learner_id != owner:
                raise KeyError(row.id)
        if family == "studio":
            if row.backend == "codex_subscription":
                settings = session.scalar(
                    select(StudioRunArtifactModel).where(
                        StudioRunArtifactModel.run_id == row.id,
                        StudioRunArtifactModel.artifact_key == "gpt:settings",
                    )
                )
                if settings is None or _object(settings.payload_json).get("owner_id") != owner:
                    raise KeyError(row.id)
            bank = session.scalar(
                select(BankImportModel)
                .join(BankReviewRunModel, BankReviewRunModel.import_id == BankImportModel.id)
                .where(BankReviewRunModel.run_id == row.id)
            )
            if bank is not None and bank.learner_id != owner:
                raise KeyError(row.id)
        # Historical catalog jobs share the Hub's existing single private owner.

    def _card(self, session: Session, family: str, row: Any) -> dict[str, Any]:
        control = session.get(ProcessControlModel, (family, str(row.id)))
        state = getattr(row, "state", "complete")
        lecture_id = getattr(row, "lecture_id", None)
        stage = getattr(row, "stage", "")
        title = getattr(row, "label", None) or getattr(row, "title", None) or family.title()
        backend = getattr(row, "backend", "")
        detail_url = f"/lectures/{lecture_id}" if lecture_id else "/lectures"
        source_busy = False
        unsafe_paid_recovery = False
        if family == "ingestion":
            upload = session.get(UploadItemModel, row.upload_item_id)
            if upload is not None:
                title, lecture_id = upload.original_filename, upload.lecture_id
                detail_url = f"/lectures/{lecture_id}" if lecture_id else "/quarantine"
            stage = row.action
            unsafe_paid_recovery = bool(
                upload and upload.kind == "transcripts" and backend != "codex_subscription"
            )
        elif family == "generation":
            title = f"Lecture {row.kind}"
        elif family == "studio":
            unsafe_paid_recovery = backend != "codex_subscription"
            if backend == "codex_subscription":
                manifest = session.scalar(
                    select(StudioRunArtifactModel).where(
                        StudioRunArtifactModel.run_id == row.id,
                        StudioRunArtifactModel.artifact_key == "gpt:manifest",
                    )
                )
                bound_lecture = (
                    _object(manifest.payload_json).get("lecture_id") if manifest else None
                )
                lecture_id = bound_lecture if type(bound_lecture) is int else None
            detail_url = (
                f"/lectures/gpt-runs/{row.id}"
                if backend == "codex_subscription"
                else f"/studio/runs/{row.id}/review"
            )
        elif family == "source":
            detail_url = "/studio"
            operations = session.scalars(
                select(StudioSourceOperationModel).where(
                    StudioSourceOperationModel.source_id == row.id,
                    or_(
                        StudioSourceOperationModel.state.in_(
                            ("executing", "reconciling", "deleting")
                        ),
                        StudioSourceOperationModel.lease_owner.is_not(None),
                    ),
                )
            ).all()
            source_busy = bool(operations)
            if operations:
                stage = operations[0].state
        elif family == "anki":
            title, stage = "Anki curation", state
            detail_url = f"/anki/jobs/{row.id}"
        elif family == "bank":
            title = f"{row.source} · {row.product} import"
            detail_url = f"/question-bank/imports/{row.id}"
        lecture = session.get(LectureModel, lecture_id) if lecture_id else None
        if lecture is not None and family != "ingestion":
            title += f" · {lecture.topic}"
        gpt_stopping = False
        if family == "studio" and backend == "codex_subscription":
            artifact = session.scalar(
                select(StudioRunArtifactModel).where(
                    StudioRunArtifactModel.run_id == row.id,
                    StudioRunArtifactModel.artifact_key == "gpt:control",
                )
            )
            gpt_stopping = bool(
                artifact and _object(artifact.payload_json).get("worker_stopping", True)
            )
        active = state in {"processing", "running", "attaching"} or source_busy
        if family == "source":
            active = source_busy
        if family == "anki":
            active = bool(row.lease_owner)
        held = control is not None and control.requested_action in {"pause", "remove"}
        acknowledged = (
            held
            and control is not None
            and bool(control.acknowledged_at)
            and not gpt_stopping
            and not source_busy
        )
        active = active and not acknowledged
        terminal = state in _FINISHED
        control_state = (
            "terminal"
            if terminal
            else "paused"
            if acknowledged
            else "pause_requested"
            if held or gpt_stopping
            else "paused"
            if state in {"paused", "interrupted"}
            else "running"
        )
        labels = {
            "terminal": state.replace("_", " ").capitalize(),
            "paused": "Paused",
            "pause_requested": "Pausing after current operation",
            "running": state.replace("_", " ").capitalize(),
        }
        blocked = ""
        if source_busy:
            blocked = "Remote source mutation must finish reconciliation first."
        elif family == "anki" and (
            state in {"envelope_pending", "applying_local", "syncing", "verifying", "v3_r12_apply"}
            or row.apply_state not in {"pending", "not_started"}
        ):
            blocked = "Use the Anki review/recovery flow; live changes cannot be interrupted here."
        elif family == "generation" and backend != "codex_subscription":
            blocked = "Legacy NotebookLM generation is disabled; retained work requires review."
        pause = not terminal and not held and not blocked and state not in {"paused", "interrupted"}
        restart = (
            not blocked
            and not active
            and (not terminal or state == "failed")
            and (acknowledged or state in {"paused", "interrupted", "failed"})
        )
        if family == "source" and state not in {"pending", "attaching"}:
            restart = False
        if family == "bank":
            pause = restart = False
        if family == "generation" and state == "failed":
            restart = False
        if (
            family == "studio"
            and backend != "codex_subscription"
            and row.workflow_kind != "direct_import"
        ):
            pause = restart = False
        if family == "anki" and state == "failed":
            restart = False
        if family == "ingestion" and state == "failed":
            restart = False
        if family == "studio" and backend != "codex_subscription":
            restart = restart and acknowledged and state != "failed"
        uncertain_recovery = bool(
            unsafe_paid_recovery
            and control
            and any(
                event.get("action") == "recovered_unacknowledged"
                for event in json.loads(control.events_json)
            )
        )
        if uncertain_recovery:
            restart = False
        if gpt_stopping:
            restart = False
        if held and not acknowledged:
            restart = False
        remove = (not blocked or (terminal and not source_busy and family != "anki")) and not (
            control and control.hidden_at
        )
        reason = blocked or "This action is unavailable in the current state."
        restart_reason = (
            "Retry this failed stage from its Anki review page."
            if family == "anki" and state == "failed"
            else reason
        )
        if uncertain_recovery or (state == "failed" and family in {"ingestion", "studio"}):
            restart_reason = (
                "Retained provider work requires its existing review flow before retry."
            )
        if (
            held
            and not acknowledged
            and control is not None
            and control.requested_action == "remove"
        ):
            labels[control_state] = "Removing after current operation"
        return {
            "id": f"{family}:{row.id}",
            "family": family,
            "job_id": str(row.id),
            "title": title,
            "lecture_id": lecture_id,
            "detail_url": detail_url,
            "state": state,
            "stage": stage,
            "backend": backend,
            "attempts": getattr(row, "attempts", 0),
            "created_at": getattr(row, "created_at", getattr(row, "imported_at", "")),
            "updated_at": getattr(row, "updated_at", getattr(row, "imported_at", "")),
            "error": getattr(row, "error", None),
            "next_attempt_at": getattr(row, "next_attempt_at", None),
            "control_state": control_state,
            "control_label": labels[control_state],
            "hidden": bool(control and control.hidden_at),
            "actions": {
                name: {
                    "enabled": enabled,
                    "reason": "" if enabled else restart_reason if name == "restart" else reason,
                }
                for name, enabled in (("pause", pause), ("restart", restart), ("remove", remove))
            },
            "active": active,
        }

    def action(self, family: str, job_id: str, action: str, *, owner_id: str) -> dict[str, Any]:
        if action not in {"pause", "restart", "remove"}:
            raise ValueError("unsupported process action")
        # GPT's existing authority validates and releases its hold in one transaction.
        # Never clear a hold here before its journal/worker checks have succeeded.
        if family == "studio" and action == "restart":
            with self.database.session() as session:
                row = self._row(session, family, job_id, owner_id)
                gpt = row.backend == "codex_subscription"
                card = self._card(session, family, row)
                if gpt and not card["actions"]["restart"]["enabled"]:
                    raise ValueError(card["actions"]["restart"]["reason"])
            if gpt:
                from oms_hub.study_generation.studio_repository import StudioRepository

                StudioRepository(self.database).control_gpt_run(
                    job_id, owner_id=owner_id, action="resume"
                )
                with self.database.session() as session:
                    return self._card(session, family, self._row(session, family, job_id, owner_id))
        with self.database.session() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            row = self._row(session, family, job_id, owner_id)
            card = self._card(session, family, row)
            if not card["actions"][action]["enabled"]:
                raise ValueError(card["actions"][action]["reason"])
            gpt = family == "studio" and row.backend == "codex_subscription"
            if action == "restart":
                self._restart(session, family, row)
            control = session.get(ProcessControlModel, (family, str(row.id)))
            if control is None:
                control = ProcessControlModel(
                    family=family, job_id=str(row.id), owner_id=owner_id, events_json="[]"
                )
                session.add(control)
            control.events_json = json.dumps(
                json.loads(control.events_json)
                + [{"action": action, "owner_id": owner_id, "at": datetime.now(UTC).isoformat()}]
            )
            control.requested_action = None if action == "restart" else action
            control.acknowledged_at = None
            control.hidden_at = None
            if action != "restart" and not card["active"]:
                control.acknowledged_at = datetime.now(UTC).isoformat()
                if action == "remove":
                    control.hidden_at = control.acknowledged_at
            session.flush()
        if gpt and (action == "restart" or card["state"] not in _FINISHED):
            from oms_hub.study_generation.studio_repository import StudioRepository

            StudioRepository(self.database).control_gpt_run(
                job_id, owner_id=owner_id, action="cancel"
            )
        with self.database.session() as session:
            return self._card(session, family, self._row(session, family, job_id, owner_id))

    def _restart(self, session: Session, family: str, row: Any) -> None:
        if family == "ingestion":
            revision = session.scalar(
                select(StudyRevisionModel).where(
                    StudyRevisionModel.upload_item_id == row.upload_item_id
                )
            )
            if revision is not None and revision.state == "removed":
                raise ValueError("Restore or reupload removed material before restarting.")
            upload = session.get(UploadItemModel, row.upload_item_id)
            if upload is None:
                raise ValueError("upload is missing")
            exact = session.scalar(
                select(StudyRevisionModel).where(
                    StudyRevisionModel.lecture_id == upload.lecture_id,
                    StudyRevisionModel.kind == upload.kind,
                    StudyRevisionModel.source_sha256 == upload.sha256,
                )
            )
            if exact is not None and exact.upload_item_id != upload.id:
                raise ValueError("This retained upload was superseded; use its current upload.")
            row.state = upload.state = "queued"
            row.next_attempt_at = None
            from oms_hub.ingestion.repository import IngestionRepository

            IngestionRepository(self.database)._sync_batch_state(session, upload.batch_id)
        elif family == "generation":
            if row.state == "failed":
                raise ValueError("Retained failed outline requires its existing review flow.")
            row.state, row.next_attempt_at = "queued", None
        elif family == "studio":
            if row.backend != "codex_subscription":
                if row.workflow_kind != "direct_import":
                    raise ValueError("Legacy generation requires its existing review flow.")
                row.state, row.next_attempt_at = "queued", None
        elif family == "anki":
            if row.state == "failed":
                # Keep retry in its existing transaction and stage eligibility checks.
                raise ValueError("Retry the failed stage from its Anki review page.")
        elif family == "source":
            if row.state not in {"pending", "attaching"}:
                raise ValueError("Resume source work from its existing review flow.")
