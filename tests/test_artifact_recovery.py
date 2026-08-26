from types import SimpleNamespace
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import oms_hub.artifacts as artifacts_module
import oms_hub.web.artifact_routes as artifact_routes
from oms_hub.artifacts import (
    ArtifactCleanupError,
    ArtifactPromotionError,
    ArtifactRecoveryError,
    ArtifactRecoveryState,
    ArtifactService,
    artifact_operator_diagnostic,
)


@pytest.fixture(autouse=True)
def unfenced_recovery_unit_service(monkeypatch):
    class Claim:
        def assert_owned(self):
            return None

    class Coordinator:
        def __init__(self, *_args):
            pass

        @contextmanager
        def claim(self, *_args):
            yield Claim()

    monkeypatch.setattr(artifacts_module, "ArtifactWriteCoordinator", Coordinator)
    monkeypatch.setattr(ArtifactService, "database", object(), raising=False)
    monkeypatch.setattr(ArtifactService, "settings", object(), raising=False)


@pytest.mark.parametrize("failed_artifact", ["backup", "journal"])
def test_committed_cleanup_failure_is_typed_and_does_not_reset_promotion(
    tmp_path,
    monkeypatch,
    failed_artifact,
):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    revision = SimpleNamespace(id=52, lecture_id=1, state="proposed")
    backup = ArtifactService._backup_path(destination, revision.id)
    journal = destination.with_name(".oms-promotion-52.recovery.json")
    reset_calls: list[int] = []
    commit_calls: list[int] = []

    def commit(revision_id: int):
        commit_calls.append(revision_id)
        revision.state = "accepted"
        return revision

    service = ArtifactService.__new__(ArtifactService)
    service.repository = SimpleNamespace(
        get_study_revision=lambda _revision_id: revision,
        begin_study_promotion=lambda _revision_id: revision,
        promote_study_revision=commit,
        reset_study_promotion=reset_calls.append,
    )
    monkeypatch.setattr(service, "_approval_pairs", lambda _revision: [(source, destination)])
    original_unlink = artifacts_module.Path.unlink
    failed_path = backup if failed_artifact == "backup" else journal

    def fail_cleanup(path, missing_ok=False):
        if path == failed_path:
            raise OSError(f"{failed_artifact} cleanup is blocked")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(artifacts_module.Path, "unlink", fail_cleanup)

    with pytest.raises(ArtifactCleanupError) as raised:
        service.approve(revision.id)

    assert isinstance(raised.value.original_error, OSError)
    assert (
        raised.value.recovery_state
        is ArtifactRecoveryState.COMMITTED_CLEANUP_REQUIRED
    )
    assert raised.value.recovery_journal_path == journal
    assert commit_calls == [revision.id]
    assert reset_calls == []
    assert revision.state == "accepted"
    assert destination.read_bytes() == b"new"
    assert journal.is_file()
    if failed_artifact == "backup":
        assert raised.value.backup_paths == (backup,)
        assert backup.read_bytes() == b"old"
    else:
        assert raised.value.backup_paths == ()


def test_rollback_cleanup_failure_is_typed_and_retains_recovery_files(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    backup = ArtifactService._backup_path(destination, 53)
    journal = destination.with_name(".oms-promotion-53.recovery.json")
    original_unlink = artifacts_module.Path.unlink

    def fail_backup_cleanup(path, missing_ok=False):
        if path == backup:
            raise OSError("backup cleanup is blocked")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(artifacts_module.Path, "unlink", fail_backup_cleanup)

    with pytest.raises(ArtifactRecoveryError) as raised:
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            53,
            lambda: (_ for _ in ()).throw(RuntimeError("database commit failed")),
        )

    assert isinstance(raised.value.original_error, RuntimeError)
    assert isinstance(raised.value.restore_error, ArtifactCleanupError)
    assert (
        raised.value.recovery_state
        is ArtifactRecoveryState.ROLLED_BACK_CLEANUP_REQUIRED
    )
    assert raised.value.backup_paths == (backup,)
    assert raised.value.recovery_journal_path == journal
    assert destination.read_bytes() == b"old"
    assert backup.read_bytes() == b"old"
    assert journal.is_file()


def test_cleanup_failure_is_translated_to_operator_visible_409(monkeypatch, tmp_path) -> None:
    backup = tmp_path / "current-backup.pdf"
    journal = tmp_path / "recovery.json"

    class FailingService:
        def approve(self, revision_id: int) -> None:
            assert revision_id == 52
            raise ArtifactCleanupError(
                "artifact promotion committed, but recovery-file cleanup failed",
                backup_paths=(backup,),
                recovery_journal_path=journal,
                original_error=OSError("cleanup blocked"),
                recovery_state=ArtifactRecoveryState.COMMITTED_CLEANUP_REQUIRED,
            )

    monkeypatch.setattr(artifact_routes, "_service", lambda _request: FailingService())

    with pytest.raises(HTTPException) as raised:
        artifact_routes.approve_replacement(SimpleNamespace(), 52)

    assert raised.value.status_code == 409
    assert "cleanup failed" in raised.value.detail["message"]


@pytest.mark.parametrize(
    (
        "scenario",
        "expected_code",
        "expected_message",
        "expected_statement",
        "expected_destination",
    ),
    [
        (
            "committed_cleanup",
            "artifact_cleanup_required",
            "artifact promotion committed or recovered, but recovery-file cleanup "
            "failed; retain the reported recovery paths",
            "Promotion committed; retained recovery files require operator cleanup.",
            b"new",
        ),
        (
            "rolled_back_cleanup",
            "artifact_cleanup_required",
            "artifact promotion failed and original destinations were restored, but "
            "recovery-file cleanup failed; retain the recovery paths",
            "Promotion did not commit; original destinations were restored, and "
            "retained recovery files require operator cleanup.",
            b"old",
        ),
        (
            "rollback_incomplete",
            "artifact_recovery_required",
            "artifact promotion failed and verified rollback could not complete; "
            "retain the recovery journal and backup paths",
            "Promotion did not commit and rollback remains incomplete; retained "
            "recovery files require operator recovery.",
            b"new",
        ),
    ],
)
def test_approval_route_preserves_typed_recovery_locations_without_internals(
    monkeypatch,
    tmp_path,
    caplog,
    scenario,
    expected_code,
    expected_message,
    expected_statement,
    expected_destination,
) -> None:
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    backup = ArtifactService._backup_path(destination, 91)
    journal = destination.with_name(".oms-promotion-91.recovery.json")
    original_copy = artifacts_module.verified_atomic_copy
    original_unlink = artifacts_module.Path.unlink

    class FailingService:
        def approve(self, revision_id: int) -> None:
            assert revision_id == 91
            if scenario in {"committed_cleanup", "rolled_back_cleanup"}:
                monkeypatch.setattr(
                    artifacts_module.Path,
                    "unlink",
                    lambda path, missing_ok=False: (
                        (_ for _ in ()).throw(OSError("PRIVATE_CLEANUP_ERROR"))
                        if path == backup
                        else original_unlink(path, missing_ok=missing_ok)
                    ),
                )
            if scenario == "rollback_incomplete":
                monkeypatch.setattr(
                    artifacts_module,
                    "verified_atomic_copy",
                    lambda copy_source, copy_destination: (
                        (_ for _ in ()).throw(OSError("PRIVATE_RESTORE_ERROR"))
                        if copy_source == backup and copy_destination == destination
                        else original_copy(copy_source, copy_destination)
                    ),
                )

            def commit() -> None:
                if scenario != "committed_cleanup":
                    raise RuntimeError("PRIVATE_ORIGINAL_ERROR")

            ArtifactService._promote_with_rollback(
                [(source, destination)],
                revision_id,
                commit,
            )

    monkeypatch.setattr(artifact_routes, "_service", lambda _request: FailingService())
    app = FastAPI()
    app.include_router(artifact_routes.router)

    with caplog.at_level("ERROR", logger="oms_hub.web.artifact_routes"):
        response = TestClient(app).post("/review/replacements/91/approve")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail == {
        "code": expected_code,
        "message": expected_message,
        "recovery_state": expected_statement,
        "backup_paths": [str(backup)],
        "recovery_journal_path": str(journal),
    }
    assert destination.read_bytes() == expected_destination
    assert backup.is_file()
    assert journal.is_file()
    serialized = response.text
    assert "PRIVATE_ORIGINAL_ERROR" not in serialized
    assert "PRIVATE_RESTORE_ERROR" not in serialized
    assert "PRIVATE_JOURNAL_ERROR" not in serialized
    assert "PRIVATE_CLEANUP_ERROR" not in serialized
    assert str(backup) in caplog.text
    assert str(journal) in caplog.text


def test_interrupted_recovery_cleanup_failure_is_typed(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    backup = ArtifactService._backup_path(destination, 54)
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    journal = ArtifactService._write_recovery_journal(
        [(source, destination)],
        {destination: backup},
        54,
    )
    backup.write_bytes(b"old")
    destination.write_bytes(b"interrupted")
    original_unlink = artifacts_module.Path.unlink

    def fail_backup_cleanup(path, missing_ok=False):
        if path == backup:
            raise OSError("recovery cleanup is blocked")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(artifacts_module.Path, "unlink", fail_backup_cleanup)
    reset_calls: list[int] = []
    service = ArtifactService.__new__(ArtifactService)
    service.repository = SimpleNamespace(
        reset_study_promotion=reset_calls.append,
        promote_study_revision=lambda _revision_id: pytest.fail(
            "interrupted rollback must not commit"
        ),
    )

    with pytest.raises(ArtifactCleanupError) as raised:
        service._recover_promotion(
            SimpleNamespace(id=54),
            [(source, destination)],
        )

    assert isinstance(raised.value.original_error, OSError)
    assert (
        raised.value.recovery_state
        is ArtifactRecoveryState.ROLLED_BACK_CLEANUP_REQUIRED
    )
    assert reset_calls == [54]
    assert destination.read_bytes() == b"old"
    assert raised.value.backup_paths == (backup,)
    assert raised.value.recovery_journal_path == journal
    assert artifact_operator_diagnostic(raised.value).recovery_state == (
        "Promotion did not commit; original destinations were restored, and "
        "retained recovery files require operator cleanup."
    )


def test_rollback_restore_failure_keeps_verified_backup_and_journal(tmp_path, monkeypatch):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    backup = ArtifactService._backup_path(destination, 42)
    original_copy = artifacts_module.verified_atomic_copy

    def fail_restore(copy_source, copy_destination):
        if copy_source == backup and copy_destination == destination:
            raise OSError("destination is locked during rollback")
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", fail_restore)

    def fail_commit():
        raise RuntimeError("database commit failed")

    with pytest.raises(ArtifactRecoveryError) as raised:
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            42,
            fail_commit,
        )

    error = raised.value
    assert isinstance(error.__cause__, RuntimeError)
    assert isinstance(error.restore_error, OSError)
    assert error.backup_paths == (backup,)
    assert backup.read_bytes() == b"old"
    assert error.recovery_journal_path.is_file()
    assert "current.pdf" in error.recovery_journal_path.read_text(encoding="utf-8")


def test_successful_promotion_removes_verified_backup_after_commit(tmp_path):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    ArtifactService._promote_with_rollback(
        [(source, destination)],
        43,
        lambda: None,
    )

    assert destination.read_bytes() == b"new"
    assert not ArtifactService._backup_path(destination, 43).exists()


def test_partial_backup_failure_does_not_mask_original_error_with_journal_keyerror(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first-immutable.pdf"
    first_destination = tmp_path / "first-current.pdf"
    second_source = tmp_path / "second-immutable.pdf"
    second_destination = tmp_path / "second-current.pdf"
    first_source.write_bytes(b"first-new")
    first_destination.write_bytes(b"first-old")
    second_source.write_bytes(b"second-new")
    second_destination.write_bytes(b"second-old")
    original_copy = artifacts_module.verified_atomic_copy

    def fail_second_backup(copy_source, copy_destination):
        if copy_source == second_destination:
            raise OSError("second destination backup is locked")
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", fail_second_backup)

    with pytest.raises(
        ArtifactPromotionError,
        match="original destinations were restored",
    ) as raised:
        ArtifactService._promote_with_rollback(
            [
                (first_source, first_destination),
                (second_source, second_destination),
            ],
            44,
            lambda: None,
        )

    assert isinstance(raised.value.original_error, OSError)
    assert "second destination backup is locked" in str(
        raised.value.original_error
    )
    assert first_destination.read_bytes() == b"first-old"
    assert second_destination.read_bytes() == b"second-old"


def test_journal_failure_blocks_before_the_first_filesystem_effect(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    def fail_journal(*_args, **_kwargs):
        raise OSError("recovery journal disk is unavailable")

    monkeypatch.setattr(
        ArtifactService,
        "_write_recovery_journal",
        staticmethod(fail_journal),
    )

    with pytest.raises(
        ArtifactPromotionError,
        match="could not start",
    ) as raised:
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            45,
            lambda: None,
        )

    assert isinstance(raised.value.original_error, OSError)
    assert destination.read_bytes() == b"old"
    assert not ArtifactService._backup_path(destination, 45).exists()


def test_partial_backup_failure_is_translated_to_operator_visible_409(
    monkeypatch,
) -> None:
    class FailingService:
        def approve(self, revision_id: int) -> None:
            assert revision_id == 44
            raise ArtifactPromotionError(
                "artifact promotion failed; original destinations were restored",
                original_error=OSError("backup locked"),
            )

    monkeypatch.setattr(artifact_routes, "_service", lambda _request: FailingService())

    with pytest.raises(HTTPException) as raised:
        artifact_routes.approve_replacement(SimpleNamespace(), 44)

    assert raised.value.status_code == 409
    assert "original destinations were restored" in raised.value.detail


def test_process_death_before_first_backup_recovers_untouched_destination(
    tmp_path,
    monkeypatch,
):
    class ProcessDeath(BaseException):
        pass

    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    original_copy = artifacts_module.verified_atomic_copy
    monkeypatch.setattr(
        artifacts_module,
        "verified_atomic_copy",
        lambda *_args: (_ for _ in ()).throw(ProcessDeath()),
    )

    with pytest.raises(ProcessDeath):
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            46,
            lambda: None,
        )

    journal = destination.with_name(".oms-promotion-46.recovery.json")
    assert journal.is_file()
    assert destination.read_bytes() == b"old"

    service = ArtifactService.__new__(ArtifactService)
    reset_calls: list[int] = []
    service.repository = SimpleNamespace(reset_study_promotion=reset_calls.append)
    monkeypatch.setattr(
        artifacts_module,
        "verified_atomic_copy",
        original_copy,
    )
    assert service._recover_promotion(SimpleNamespace(id=46), [(source, destination)]) is False
    assert reset_calls == [46]
    assert destination.read_bytes() == b"old"
    assert not journal.exists()


def test_process_death_before_recovery_journal_resets_untouched_promotion(
    tmp_path,
) -> None:
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    service = ArtifactService.__new__(ArtifactService)
    reset_calls: list[int] = []
    service.repository = SimpleNamespace(reset_study_promotion=reset_calls.append)

    assert service._recover_promotion(
        SimpleNamespace(id=48),
        [(source, destination)],
    ) is False
    assert reset_calls == [48]
    assert destination.read_bytes() == b"old"
    assert not ArtifactService._backup_path(destination, 48).exists()


def test_process_death_mid_backup_restores_completed_backup_and_untouched_peer(
    tmp_path,
    monkeypatch,
):
    class ProcessDeath(BaseException):
        pass

    pairs = [
        (tmp_path / "first-immutable.pdf", tmp_path / "first-current.pdf"),
        (tmp_path / "second-immutable.pdf", tmp_path / "second-current.pdf"),
    ]
    pairs[0][0].write_bytes(b"first-new")
    pairs[0][1].write_bytes(b"first-old")
    pairs[1][0].write_bytes(b"second-new")
    pairs[1][1].write_bytes(b"second-old")
    original_copy = artifacts_module.verified_atomic_copy

    def die_on_second_backup(copy_source, copy_destination):
        if copy_source == pairs[1][1]:
            raise ProcessDeath()
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(
        artifacts_module,
        "verified_atomic_copy",
        die_on_second_backup,
    )
    with pytest.raises(ProcessDeath):
        ArtifactService._promote_with_rollback(pairs, 50, lambda: None)

    service = ArtifactService.__new__(ArtifactService)
    reset_calls: list[int] = []
    service.repository = SimpleNamespace(reset_study_promotion=reset_calls.append)
    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", original_copy)

    assert service._recover_promotion(SimpleNamespace(id=50), pairs) is False
    assert reset_calls == [50]
    assert pairs[0][1].read_bytes() == b"first-old"
    assert pairs[1][1].read_bytes() == b"second-old"


def test_process_death_mid_copy_restores_every_original_destination(
    tmp_path,
    monkeypatch,
):
    class ProcessDeath(BaseException):
        pass

    pairs = [
        (tmp_path / "first-immutable.pdf", tmp_path / "first-current.pdf"),
        (tmp_path / "second-immutable.pdf", tmp_path / "second-current.pdf"),
    ]
    pairs[0][0].write_bytes(b"first-new")
    pairs[0][1].write_bytes(b"first-old")
    pairs[1][0].write_bytes(b"second-new")
    pairs[1][1].write_bytes(b"second-old")
    original_copy = artifacts_module.verified_atomic_copy

    def die_on_second_canonical_copy(copy_source, copy_destination):
        if copy_source == pairs[1][0] and copy_destination == pairs[1][1]:
            raise ProcessDeath()
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(
        artifacts_module,
        "verified_atomic_copy",
        die_on_second_canonical_copy,
    )
    with pytest.raises(ProcessDeath):
        ArtifactService._promote_with_rollback(pairs, 51, lambda: None)

    assert pairs[0][1].read_bytes() == b"first-new"
    service = ArtifactService.__new__(ArtifactService)
    reset_calls: list[int] = []
    service.repository = SimpleNamespace(reset_study_promotion=reset_calls.append)
    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", original_copy)

    assert service._recover_promotion(SimpleNamespace(id=51), pairs) is False
    assert reset_calls == [51]
    assert pairs[0][1].read_bytes() == b"first-old"
    assert pairs[1][1].read_bytes() == b"second-old"


def test_approve_does_not_reset_recovery_required_promotion(monkeypatch, tmp_path):
    revision = SimpleNamespace(id=47, lecture_id=1, state="proposed")
    reset_calls: list[int] = []
    service = ArtifactService.__new__(ArtifactService)
    service.repository = SimpleNamespace(
        get_study_revision=lambda _revision_id: revision,
        begin_study_promotion=lambda _revision_id: revision,
        reset_study_promotion=reset_calls.append,
    )
    monkeypatch.setattr(service, "_approval_pairs", lambda _revision: [])
    recovery_error = ArtifactRecoveryError(
        "manual recovery required",
        backup_paths=(tmp_path / "current-backup.pdf",),
        recovery_journal_path=tmp_path / "recovery.json",
        original_error=RuntimeError("promotion failed"),
        restore_error=OSError("restore failed"),
        recovery_state=ArtifactRecoveryState.ROLLBACK_INCOMPLETE,
    )

    def fail_promotion(*_args):
        raise recovery_error

    monkeypatch.setattr(service, "_promote_with_rollback", fail_promotion)

    with pytest.raises(ArtifactRecoveryError) as raised:
        service.approve(47)

    assert raised.value is recovery_error
    assert reset_calls == []


def test_crash_recovery_restores_every_destination_before_backup_cleanup(tmp_path):
    revision = SimpleNamespace(id=48)
    service = ArtifactService.__new__(ArtifactService)
    reset_calls: list[int] = []
    service.repository = SimpleNamespace(reset_study_promotion=reset_calls.append)
    first_source = tmp_path / "first-immutable.pdf"
    second_source = tmp_path / "second-immutable.pdf"
    first_destination = tmp_path / "first-current.pdf"
    second_destination = tmp_path / "second-current.pdf"
    first_source.write_bytes(b"first-new")
    second_source.write_bytes(b"second-new")
    first_destination.write_bytes(b"first-new")
    second_destination.write_bytes(b"partial")
    first_backup = ArtifactService._backup_path(first_destination, revision.id)
    second_backup = ArtifactService._backup_path(second_destination, revision.id)
    first_backup.write_bytes(b"first-old")
    second_backup.write_bytes(b"second-old")

    recovered = service._recover_promotion(
        revision,
        [
            (first_source, first_destination),
            (second_source, second_destination),
        ],
    )

    assert recovered is False
    assert reset_calls == [revision.id]
    assert first_destination.read_bytes() == b"first-old"
    assert second_destination.read_bytes() == b"second-old"
    assert not first_backup.exists()
    assert not second_backup.exists()


def test_crash_restore_failure_keeps_all_backups_and_does_not_reset_state(
    tmp_path,
    monkeypatch,
):
    revision = SimpleNamespace(id=49)
    service = ArtifactService.__new__(ArtifactService)
    reset_calls: list[int] = []
    service.repository = SimpleNamespace(reset_study_promotion=reset_calls.append)
    first_source = tmp_path / "first-immutable.pdf"
    second_source = tmp_path / "second-immutable.pdf"
    first_destination = tmp_path / "first-current.pdf"
    second_destination = tmp_path / "second-current.pdf"
    first_source.write_bytes(b"first-new")
    second_source.write_bytes(b"second-new")
    first_destination.write_bytes(b"first-new")
    second_destination.write_bytes(b"partial")
    first_backup = ArtifactService._backup_path(first_destination, revision.id)
    second_backup = ArtifactService._backup_path(second_destination, revision.id)
    first_backup.write_bytes(b"first-old")
    second_backup.write_bytes(b"second-old")
    original_copy = artifacts_module.verified_atomic_copy

    def fail_second_restore(copy_source, copy_destination):
        if copy_source == second_backup and copy_destination == second_destination:
            raise OSError("second destination is locked")
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(
        artifacts_module,
        "verified_atomic_copy",
        fail_second_restore,
    )

    with pytest.raises(ArtifactRecoveryError) as raised:
        service._recover_promotion(
            revision,
            [
                (first_source, first_destination),
                (second_source, second_destination),
            ],
        )

    assert isinstance(raised.value.restore_error, OSError)
    assert raised.value.backup_paths == (first_backup, second_backup)
    assert first_destination.read_bytes() == b"first-old"
    assert first_backup.read_bytes() == b"first-old"
    assert second_backup.read_bytes() == b"second-old"
    assert reset_calls == []
