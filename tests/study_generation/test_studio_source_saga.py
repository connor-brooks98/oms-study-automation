from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.models import StudioSourceModel, StudioSourceOperationModel
from oms_hub.study_generation.notebook_errors import (
    NotebookGatewayError,
    NotebookScopeBusyError,
    NotebookScopeLostError,
    NotebookSourceNotFoundError,
)
from oms_hub.study_generation.studio_domain import StudioSourceState, StudioSourceType
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_worker import StudioWorker


class _Connection:
    def invalidate(self, diagnostic: str) -> None:
        pass


class _SagaGateway:
    def __init__(self, effect: str) -> None:
        self.effect = effect
        self.remote_ids: set[str] = {"baseline"}
        self.add_calls = 0
        self.delete_calls = 0
        self.scope_depth = 0

    @contextmanager
    def mutation_scope(self, subject, exam_number, owner_kind, owner_id):
        assert subject
        assert exam_number == 1
        assert owner_kind == "studio"
        assert owner_id
        self.scope_depth += 1
        try:
            yield
        finally:
            self.scope_depth -= 1

    def prepare_studio_source_add(self, subject: str, exam_number: int):
        assert self.scope_depth == 1
        return "notebook-1", frozenset(self.remote_ids)

    def add_studio_source_to_notebook(self, notebook_id, source_type, title, **kwargs):
        assert self.scope_depth == 1
        self.add_calls += 1
        if self.effect == "zero":
            raise _network_error()
        if self.effect == "one":
            self.remote_ids.add("remote-1")
            raise _network_error()
        if self.effect == "one_after_zero":
            self.remote_ids.add("remote-final")
            raise _network_error()
        if self.effect == "multiple":
            self.remote_ids.update({"remote-1", "remote-2"})
            raise _network_error()
        self.remote_ids.add("remote-success")
        return "remote-success"

    def list_studio_source_ids(self, notebook_id):
        assert self.scope_depth == 1
        return frozenset(self.remote_ids)

    def delete_studio_source(self, notebook_id, source_id):
        assert self.scope_depth == 1
        self.delete_calls += 1
        if self.effect == "delete_crash":
            self.effect = "delete_missing"
            raise _network_error()
        if self.effect == "delete_missing":
            raise NotebookSourceNotFoundError()
        return True


def _network_error() -> NotebookGatewayError:
    return NotebookGatewayError(
        "NotebookLM outcome is unknown.",
        source=DiagnosticSource.NETWORK,
        retryable=True,
    )


def _repository(tmp_path: Path) -> StudioRepository:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    return StudioRepository(database)


def _source(repository: StudioRepository, tmp_path: Path):
    payload = tmp_path / "notes.txt"
    payload.write_text("durable notes", encoding="utf-8")
    return repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Notes",
        payload_path=payload,
    )


def _worker(repository: StudioRepository, gateway: _SagaGateway) -> StudioWorker:
    return StudioWorker(repository, gateway, object(), _Connection())  # type: ignore[arg-type]


def _make_source_retry_due(repository: StudioRepository, source_id: str) -> None:
    with repository.database.session() as session:
        source = session.get(StudioSourceModel, source_id)
        assert source is not None
        source.next_attempt_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()


def test_interrupted_add_with_one_delta_is_adopted_without_duplicate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    gateway = _SagaGateway("one")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    assert repository.get(source.id).state is StudioSourceState.ATTACHING  # type: ignore[union-attr]
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True

    attached = repository.get(source.id)
    assert attached is not None
    assert attached.state is StudioSourceState.ATTACHED
    assert attached.remote_source_id == "remote-1"
    assert gateway.add_calls == 1


def test_lease_loss_after_remote_add_remains_reconcilable_until_adopted(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)

    class LosingGateway(_SagaGateway):
        @contextmanager
        def mutation_scope(self, subject, exam_number, owner_kind, owner_id):
            with super().mutation_scope(subject, exam_number, owner_kind, owner_id):
                yield
            raise NotebookScopeLostError()

    gateway = LosingGateway("success")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    pending = repository.get(source.id)
    assert pending is not None
    assert pending.state is StudioSourceState.ATTACHING
    assert pending.remote_source_id is None
    with repository.database.session() as session:
        operation = session.scalar(select(StudioSourceOperationModel))
        assert operation is not None
        assert operation.state == "reconciling"
        assert operation.remote_source_id is None

    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    attached = repository.get(source.id)
    assert attached is not None
    assert attached.state is StudioSourceState.ATTACHED
    assert attached.remote_source_id == "remote-success"
    assert gateway.add_calls == 1


def test_interrupted_add_with_zero_delta_is_the_only_retry_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    gateway = _SagaGateway("zero")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    assert gateway.add_calls == 1

    gateway.effect = "success"
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    attached = repository.get(source.id)
    assert attached is not None
    assert attached.state is StudioSourceState.ATTACHED
    assert gateway.add_calls == 2


def test_final_zero_delta_retry_reconciles_one_remote_delta_without_a_fourth_add(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    gateway = _SagaGateway("zero")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    assert gateway.add_calls == 1

    gateway.effect = "one_after_zero"
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True

    attached = repository.get(source.id)
    assert attached is not None
    assert attached.state is StudioSourceState.ATTACHED
    assert attached.remote_source_id == "remote-final"
    assert gateway.add_calls == 2


def test_interrupted_add_with_multiple_deltas_stops_for_review(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    gateway = _SagaGateway("multiple")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    reviewed = repository.get(source.id)
    assert reviewed is not None
    assert reviewed.state is StudioSourceState.NEEDS_REVIEW
    assert gateway.add_calls == 1
    assert worker.run_once() is False


def test_delete_remote_not_found_is_terminal_success(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    repository.complete(source.id, "notebook-1", "remote-1")
    queued = repository.queue_source_delete(source.id)
    assert queued.state is StudioSourceState.DELETING

    gateway = _SagaGateway("delete_missing")
    assert _worker(repository, gateway).run_once() is True
    deleted = repository.get(source.id)
    assert deleted is not None
    assert deleted.state is StudioSourceState.DELETED
    assert gateway.delete_calls == 1


def test_interrupted_delete_retries_and_converges_on_remote_not_found(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    repository.complete(source.id, "notebook-1", "remote-1")
    repository.queue_source_delete(source.id)
    gateway = _SagaGateway("delete_crash")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    assert repository.get(source.id).state is StudioSourceState.DELETING  # type: ignore[union-attr]
    _make_source_retry_due(repository, source.id)
    assert worker.run_once() is True
    assert repository.get(source.id).state is StudioSourceState.DELETED  # type: ignore[union-attr]
    assert gateway.delete_calls == 2


def test_only_one_worker_can_claim_a_queued_source_operation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)

    assert repository.claim_next() is not None
    first_claim = repository.claim_next_source_operation()
    assert first_claim is not None and first_claim[1].id == source.id

    competing_repository = StudioRepository(repository.database)
    assert competing_repository.claim_next_source_operation() is None


def test_logical_notebook_scope_is_reserved_before_remote_preparation(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _source(repository, tmp_path)
    second_path = tmp_path / "second.txt"
    second_path.write_text("second", encoding="utf-8")
    second = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Second", payload_path=second_path
    )

    assert repository.claim_next().id == first.id  # type: ignore[union-attr]
    competing_repository = StudioRepository(repository.database)

    assert competing_repository.claim_next() is None
    waiting = competing_repository.get(second.id)
    assert waiting is not None
    assert waiting.state is StudioSourceState.PENDING
    assert waiting.attempts == 0

    other_scope_path = tmp_path / "other-scope.txt"
    other_scope_path.write_text("other", encoding="utf-8")
    other_scope = competing_repository.create_source(
        "Cardio", 1, StudioSourceType.TEXT, "Other scope", payload_path=other_scope_path
    )
    assert competing_repository.claim_next().id == other_scope.id  # type: ignore[union-attr]


def test_busy_generation_scope_defers_studio_without_consuming_remote_attempt(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = _source(repository, tmp_path)
    assert repository.claim_next() is not None
    claimed = repository.claim_next_source_operation()
    assert claimed is not None

    class BusyGateway(_SagaGateway):
        @contextmanager
        def mutation_scope(self, subject, exam_number, owner_kind, owner_id):
            raise NotebookScopeBusyError()
            yield  # pragma: no cover

    gateway = BusyGateway("success")
    _worker(repository, gateway)._run_source_operation(*claimed)

    waiting = repository.get(source.id)
    assert waiting is not None
    assert waiting.state is StudioSourceState.ATTACHING
    assert waiting.next_attempt_at is not None
    assert gateway.add_calls == 0
    with repository.database.session() as session:
        operation = session.get(StudioSourceOperationModel, claimed[0].id)
        assert operation is not None
        assert operation.attempts == 0
        assert operation.lease_owner is None


def test_same_notebook_add_defers_without_remote_effect_then_proceeds(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _source(repository, tmp_path)
    second_path = tmp_path / "second.txt"
    second_path.write_text("second", encoding="utf-8")
    second = repository.create_source(
        "Cardio", 1, StudioSourceType.TEXT, "Second", payload_path=second_path
    )

    assert repository.claim_next() is not None
    first_claim = repository.claim_next_source_operation()
    assert first_claim is not None
    repository.record_attach_baseline(first_claim[0].id, "notebook-1", {"baseline"})
    repository.mark_attach_reconciling(
        first_claim[0].id,
        DiagnosticSource.NETWORK.value,
        "remote outcome is pending reconciliation",
    )
    assert repository.claim_next() is not None
    gateway = _SagaGateway("success")
    worker = _worker(repository, gateway)

    assert worker.run_once() is True
    waiting = repository.get(second.id)
    assert waiting is not None
    assert waiting.state is StudioSourceState.ATTACHING
    assert waiting.next_attempt_at is not None
    assert gateway.add_calls == 0
    assert repository.get(first.id).state is StudioSourceState.ATTACHING  # type: ignore[union-attr]

    repository.complete_attach_operation(first_claim[0].id, "remote-first")
    _make_source_retry_due(repository, second.id)
    assert worker.run_once() is True

    attached = repository.get(second.id)
    assert attached is not None
    assert attached.state is StudioSourceState.ATTACHED
    assert attached.remote_source_id == "remote-success"
    assert gateway.add_calls == 1


def test_delete_waits_while_the_notebook_has_an_active_add(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    active = _source(repository, tmp_path)
    deleting_path = tmp_path / "deleting.txt"
    deleting_path.write_text("delete me", encoding="utf-8")
    deleting = repository.create_source(
        "Neuro", 1, StudioSourceType.TEXT, "Deleting", payload_path=deleting_path
    )
    repository.complete(deleting.id, "notebook-1", "remote-delete")

    assert repository.claim_next() is not None
    claimed = repository.claim_next_source_operation()
    assert claimed is not None and claimed[1].id == active.id
    repository.record_attach_baseline(claimed[0].id, "notebook-1", {"baseline"})

    with pytest.raises(ValueError, match="pending source mutation"):
        repository.queue_source_delete(deleting.id)
    stored = repository.get(deleting.id)
    assert stored is not None and stored.state is StudioSourceState.ATTACHED
