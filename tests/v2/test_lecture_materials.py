"""Recoverable material removal preserves files and cannot race active ingestion."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from oms_hub.app import create_app
from oms_hub.artifact_writes import ArtifactWriteContended, ArtifactWriteCoordinator
from oms_hub.artifacts import ArtifactRole, ArtifactService
from oms_hub.config import Settings
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.lecture_materials import LectureMaterials, MaterialConflict
from oms_hub.models import IngestionJobModel, LectureStepModel, StudyRevisionModel
from oms_hub.repositories import LectureInput
from oms_hub.web.material_routes import router


def prepared(tmp_path, kind=UploadKind.TRANSCRIPTS):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "icloud",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        anki_enabled=False,
    )
    app = create_app(settings)
    if not any(getattr(route, "name", "") == "remove_material" for route in app.routes):
        app.include_router(router)
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 1, 1, "Cardiac Cycle", "", None)
    )
    root = settings.data_dir / "artifacts" / "v2" / kind.value
    root.mkdir(parents=True)
    source = root / ("original.pptx" if kind is UploadKind.SLIDES else "raw.txt")
    source.write_bytes(b"retained source")
    derived = root / ("converted.pdf" if kind is UploadKind.SLIDES else "cleaned.txt")
    if kind is UploadKind.SLIDES:
        from reportlab.pdfgen.canvas import Canvas

        canvas = Canvas(str(derived))
        canvas.drawString(10, 50, "fixture")
        canvas.save()
    else:
        derived.write_bytes(b"retained cleaned text")
    settings.study_root.mkdir(parents=True)
    settings.icloud_staging_root.mkdir(parents=True)
    canonical = settings.study_root / derived.name
    canonical.write_bytes(derived.read_bytes())
    original = settings.study_root / source.name
    original.write_bytes(source.read_bytes())
    icloud = settings.icloud_staging_root / derived.name
    icloud.write_bytes(derived.read_bytes())
    repo = app.state.ingestion_repository
    batch = repo.create_batch(kind)
    repo.add_item(
        kind,
        StagedUpload(
            batch,
            "old",
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            source.stat().st_size,
            source.name,
        ),
    )
    with app.state.database.session() as session:
        row = StudyRevisionModel(
            upload_item_id="old",
            lecture_id=lecture_id,
            kind=kind.value,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            immutable_source_path=str(source),
            immutable_derived_path=str(derived),
            canonical_source_path=str(original),
            canonical_derived_path=str(canonical),
            icloud_path=str(icloud),
            derived_sha256=hashlib.sha256(derived.read_bytes()).hexdigest(),
            state="current",
            current=True,
        )
        session.add(row)
        session.flush()
        revision_id = row.id
    for step in V2StepName:
        app.state.catalog_repository.set_step_status(lecture_id, step, StepStatus.COMPLETE, "ready")
    return app, LectureMaterials(app.state.database, settings), lecture_id, revision_id


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_remove_restore_retains_files_and_unrelated_study_state(tmp_path, kind):
    app, service, lecture, revision = prepared(tmp_path, kind)
    files = {
        p: p.read_bytes()
        for root in (
            app.state.settings.study_root,
            app.state.settings.data_dir / "artifacts",
            app.state.settings.icloud_staging_root,
        )
        for p in root.rglob("*")
        if p.is_file()
    }
    removed = service.remove(lecture, revision)
    assert removed.state == "removed" and not removed.current
    assert service.list_removed(lecture) == [removed]
    artifact = ArtifactService(app.state.database, app.state.settings).resolve(
        revision, ArtifactRole.ORIGINAL if kind is UploadKind.SLIDES else ArtifactRole.CLEANED
    )
    assert artifact.path.is_relative_to(app.state.settings.data_dir)
    with app.state.database.session() as session:
        steps = {s.name: s.status for s in session.scalars(select(LectureStepModel))}
        assert (
            steps["slides_filed" if kind is UploadKind.SLIDES else "transcript_filed"] == "waiting"
        )
        assert steps["quiz_published"] == steps["anki_synced"] == "complete"
        assert (
            steps["transcript_filed" if kind is UploadKind.SLIDES else "slides_filed"] == "complete"
        )
    restored = service.restore(lecture, revision)
    assert restored.current and restored.state == "current"
    assert service.list_removed(lecture) == []
    assert all(path.read_bytes() == payload for path, payload in files.items())


def test_changed_canonical_restore_fails_without_replacing_it(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    removed = service.remove(lecture, revision)
    removed.canonical_derived_path.write_text("new external contents")
    with pytest.raises(MaterialConflict, match="recovery of the original filed copy"):
        service.restore(lecture, revision)
    assert removed.canonical_derived_path.read_text() == "new external contents"
    assert service.list_removed(lecture)[0].id == revision


@pytest.mark.parametrize("state", ["queued", "processing"])
def test_active_ingestion_blocks_removal(tmp_path, state):
    app, service, lecture, revision = prepared(tmp_path)
    from oms_hub.models import UploadItemModel

    with app.state.database.session() as session:
        session.get(UploadItemModel, "old").lecture_id = lecture
        session.add(IngestionJobModel(upload_item_id="old", action="process", state=state))
    with pytest.raises(MaterialConflict, match="active upload"):
        service.remove(lecture, revision)
    assert app.state.ingestion_repository.get_study_revision(revision).current


def test_file_lock_and_lecture_ownership_are_required(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    with ArtifactWriteCoordinator(app.state.database, app.state.settings).claim(lecture, "test"):
        with pytest.raises(ArtifactWriteContended):
            service.remove(lecture, revision)
    with pytest.raises(KeyError):
        service.remove(lecture + 1, revision)
    client = TestClient(app)
    url = f"/lectures/{lecture}/materials/{revision}/remove"
    assert client.post(url).status_code == 403
    client.get(f"/lectures/{lecture}")
    result = client.post(
        url, data={"csrf_token": client.cookies["study_hub_csrf"]}, follow_redirects=False
    )
    assert result.status_code == 303
    assert result.headers["location"] == f"/lectures/{lecture}"


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_same_source_reupload_preserves_revision_and_requires_explicit_restore(tmp_path, kind):
    app, service, lecture, revision = prepared(tmp_path, kind)
    removed = service.remove(lecture, revision)
    retained_source = removed.immutable_source_path.read_bytes()
    retained_derived = removed.immutable_derived_path.read_bytes()
    repo = app.state.ingestion_repository
    source = tmp_path / removed.immutable_source_path.name
    source.write_bytes(retained_source)
    batch = repo.create_batch(kind)
    repo.add_item(
        kind,
        StagedUpload(
            batch, "new", source, removed.source_sha256, len(retained_source), source.name
        ),
    )
    with pytest.raises(ValueError, match="Restore the retained material"):
        repo.set_manual_assignment("new", lecture)
    assert repo.count_jobs("new", "process") == 0
    assert repo.get_study_revision(revision) == removed
    assert removed.immutable_source_path.read_bytes() == retained_source
    assert removed.immutable_derived_path.read_bytes() == retained_derived
    artifact = ArtifactService(app.state.database, app.state.settings).resolve(
        revision, ArtifactRole.ORIGINAL if kind is UploadKind.SLIDES else ArtifactRole.CLEANED
    )
    assert artifact.path.read_bytes() == (
        retained_source if kind is UploadKind.SLIDES else retained_derived
    )
    assert service.list_removed(lecture) == [removed]


def test_restore_never_displaces_a_new_current_revision(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    removed = service.remove(lecture, revision)
    from oms_hub.models import UploadBatchModel, UploadItemModel

    with app.state.database.session() as session:
        session.add(UploadBatchModel(id="replacement", kind="transcripts", state="complete"))
        session.flush()
        session.add(
            UploadItemModel(
                id="replacement",
                batch_id="replacement",
                kind="transcripts",
                original_filename="new.txt",
                staged_path="new.txt",
                sha256="b" * 64,
                size_bytes=1,
                lecture_id=lecture,
            )
        )
        session.flush()
        row = StudyRevisionModel(
            upload_item_id="replacement",
            lecture_id=lecture,
            kind="transcripts",
            source_sha256="b" * 64,
            immutable_source_path="new.txt",
            state="current",
            current=True,
        )
        session.add(row)
    with pytest.raises(MaterialConflict, match="current material"):
        service.restore(lecture, revision)
    assert removed.immutable_source_path.is_file()


def test_active_gpt_lecture_manifest_blocks_detachment(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    from oms_hub.models import StudioRunArtifactModel, StudioRunModel

    with app.state.database.session() as session:
        session.add(
            StudioRunModel(
                id="active-quiz",
                subject="Cardio",
                subject_key="cardio",
                exam_number=1,
                destination_subject="Cardio",
                destination_subject_key="cardio",
                destination_exam_number=1,
                label="active",
                label_key="active",
                prompt="",
                state="running",
                backend="codex_subscription",
            )
        )
        session.flush()
        session.add(
            StudioRunArtifactModel(
                run_id="active-quiz",
                artifact_key="gpt:manifest",
                signature_sha256="a" * 64,
                payload_json='{"lecture_id":' + str(lecture) + "}",
            )
        )
    with pytest.raises(MaterialConflict, match="quiz generation"):
        service.remove(lecture, revision)
    assert service.repository.get_study_revision(revision).current


def test_only_acknowledged_upload_hold_allows_removal(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    from oms_hub.models import ProcessControlModel, UploadItemModel

    with app.state.database.session() as session:
        session.get(UploadItemModel, "old").lecture_id = lecture
        job = IngestionJobModel(upload_item_id="old", action="process", state="processing")
        session.add(job)
        session.flush()
        job_id = str(job.id)
        session.add(
            ProcessControlModel(
                family="ingestion", job_id=job_id, owner_id="local-owner", requested_action="pause"
            )
        )
    with pytest.raises(MaterialConflict, match="wait for it to stop"):
        service.remove(lecture, revision)
    with app.state.database.session() as session:
        session.get(ProcessControlModel, ("ingestion", job_id)).acknowledged_at = "2026-09-15"
    assert service.remove(lecture, revision).state == "removed"


def test_interrupted_gpt_worker_must_finish_stopping_before_removal(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    from oms_hub.models import StudioRunArtifactModel, StudioRunModel

    with app.state.database.session() as session:
        session.add(
            StudioRunModel(
                id="stopping-quiz",
                subject="Cardio",
                subject_key="cardio",
                exam_number=1,
                destination_subject="Cardio",
                destination_subject_key="cardio",
                destination_exam_number=1,
                label="stopping",
                label_key="stopping",
                prompt="",
                state="interrupted",
                backend="codex_subscription",
            )
        )
        session.flush()
        for key, payload in (
            ("gpt:manifest", '{"lecture_id":' + str(lecture) + "}"),
            ("gpt:control", '{"worker_stopping":true}'),
        ):
            session.add(
                StudioRunArtifactModel(
                    run_id="stopping-quiz",
                    artifact_key=key,
                    signature_sha256="a" * 64,
                    payload_json=payload,
                )
            )
    with pytest.raises(MaterialConflict, match="quiz generation"):
        service.remove(lecture, revision)
    with app.state.database.session() as session:
        control = session.scalar(
            select(StudioRunArtifactModel).where(
                StudioRunArtifactModel.run_id == "stopping-quiz",
                StudioRunArtifactModel.artifact_key == "gpt:control",
            )
        )
        control.payload_json = '{"worker_stopping":false}'
    assert service.remove(lecture, revision).state == "removed"


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_different_source_after_removal_queues_without_replacement_review(tmp_path, kind):
    app, service, lecture, revision = prepared(tmp_path, kind)
    removed = service.remove(lecture, revision)
    source = tmp_path / removed.immutable_source_path.name
    source.write_bytes(b"a changed source")
    repo = app.state.ingestion_repository
    batch = repo.create_batch(kind)
    repo.add_item(
        kind,
        StagedUpload(
            batch,
            "changed",
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            source.stat().st_size,
            source.name,
        ),
    )
    repo.set_manual_assignment("changed", lecture)
    assert repo.require_item("changed").state.value == "queued"
    started = repo.begin_revision(
        "changed", app.state.settings.data_dir / "artifacts" / "v2" / kind.value
    )
    assert started.id != revision and not started.current
    assert not repo.has_other_current_revision(lecture, kind, started.id)
    assert repo.get_study_revision(revision) == removed
