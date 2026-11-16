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
        from pptx import Presentation
        from reportlab.pdfgen.canvas import Canvas

        Presentation().save(str(source))
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


def test_upload_of_removed_transcript_returns_restore_guidance(tmp_path):
    app, service, lecture, revision = prepared(tmp_path)
    removed = service.remove(lecture, revision)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/uploads/transcripts",
        data={"lecture_id": str(lecture)},
        files=[("files", ("captions.txt", removed.immutable_source_path.read_bytes()))],
    )
    assert response.status_code == 409
    assert "Restore the retained material" in response.json()["detail"]
    assert service.list_removed(lecture) == [removed]
    root = app.state.upload_staging.root
    assert not list((root / "manifests").iterdir())
    assert not list((root / "batches").iterdir())


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_panel_identical_readd_restore_and_active_duplicate(tmp_path, kind):
    app, service, lecture, revision = prepared(tmp_path, kind)
    saved = service.remove(lecture, revision)
    payload = saved.immutable_source_path.read_bytes()
    client = TestClient(app, raise_server_exceptions=False)
    client.get(f"/lectures/{lecture}")
    def upload():
        return client.post(
            f"/uploads/{kind.value}", data={"lecture_id": str(lecture)},
            files={"files": (saved.immutable_source_path.name, payload)},
        )
    assert upload().status_code == 409
    assert service.list_removed(lecture) == [saved]
    assert saved.immutable_source_path.read_bytes() == payload
    response = client.post(
        f"/lectures/{lecture}/materials/{revision}/restore",
        data={"csrf_token": client.cookies["study_hub_csrf"]}, follow_redirects=False,
    )
    assert response.status_code == 303
    duplicate = upload()
    assert duplicate.status_code == 202
    batch = client.get(f"/api/upload-batches/{duplicate.json()['batch_id']}").json()
    item = batch["items"][0]
    assert item["state"] == "complete"
    assert item["lecture_id"] == lecture
    assert app.state.ingestion_repository.count_jobs(item["id"], "process") == 0
    restored = app.state.ingestion_repository.get_study_revision(revision)
    assert restored.source_sha256 == saved.source_sha256


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_removed_hash_does_not_block_other_lecture(tmp_path, kind):
    app, service, lecture, revision = prepared(tmp_path, kind)
    removed = service.remove(lecture, revision)
    other = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 1, 2, "Fixture two", "", None)
    )
    client = TestClient(app)
    response = client.post(
        f"/uploads/{kind.value}", data={"lecture_id": str(other)},
        files={"files": (
            removed.immutable_source_path.name, removed.immutable_source_path.read_bytes()
        )},
    )
    assert response.status_code == 202
    item = client.get(f"/api/upload-batches/{response.json()['batch_id']}").json()["items"][0]
    assert item["lecture_id"] == other and item["kind"] == kind.value
    assert item["state"] == "queued"
    assert service.list_removed(lecture) == [removed]


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_material_panel_actions_are_independent_and_file_backed(tmp_path, kind):
    from selectolax.parser import HTMLParser

    app, service, lecture, revision = prepared(tmp_path, kind)
    client = TestClient(app)
    document = HTMLParser(client.get(f"/lectures/{lecture}").text)
    panels = document.css("[data-material-panel]")
    assert [panel.attributes["data-kind"] for panel in panels] == ["slides", "transcripts"]
    panel = document.css_first(f'[data-material-panel][data-kind="{kind.value}"]')
    assert panel.attributes["data-lecture-id"] == str(lecture)
    links = {a.text(strip=True): a for a in panel.css("[data-material-actions] a")}
    if kind is UploadKind.SLIDES:
        assert set(links) == {"Download PPTX", "Download PDF", "View PDF"}
        view = links["View PDF"]
    else:
        assert set(links) == {"Download TXT", "View TXT"}
        view = links["View TXT"]
    assert view.attributes["target"] == "_blank"
    for name, link in links.items():
        if name.startswith("Download"):
            response = client.get(link.attributes["href"])
            assert response.status_code == 200
            assert response.headers["content-disposition"].startswith("attachment")
    saved = app.state.ingestion_repository.get_study_revision(revision)
    saved.canonical_derived_path.unlink()
    changed = HTMLParser(client.get(f"/lectures/{lecture}").text)
    changed_panel = changed.css_first(f'[data-material-panel][data-kind="{kind.value}"]')
    labels = [a.text(strip=True) for a in changed_panel.css("[data-material-actions] a")]
    assert labels == (["Download PPTX"] if kind is UploadKind.SLIDES else [])


def test_pdf_original_does_not_offer_pptx_and_missing_original_hides_download(tmp_path):
    from selectolax.parser import HTMLParser

    app, _, lecture, revision = prepared(tmp_path, UploadKind.SLIDES)
    saved = app.state.ingestion_repository.get_study_revision(revision)
    with app.state.database.session() as session:
        row = session.get(StudyRevisionModel, revision)
        row.canonical_source_path = str(saved.canonical_derived_path)
        row.source_sha256 = saved.derived_sha256
    client = TestClient(app)
    panel = HTMLParser(client.get(f"/lectures/{lecture}").text).css_first('[data-kind="slides"]')
    assert "Download PPTX" not in panel.text()
    assert "Download PDF" in panel.text()
    with app.state.database.session() as session:
        row = session.get(StudyRevisionModel, revision)
        row.canonical_source_path = str(saved.canonical_source_path)
    panel = HTMLParser(client.get(f"/lectures/{lecture}").text).css_first('[data-kind="slides"]')
    assert "Download PPTX" not in panel.text()


def test_pdf_download_retains_artifact_verification(tmp_path):
    app, _, _, revision = prepared(tmp_path, UploadKind.SLIDES)
    client = TestClient(app)
    response = client.get(f"/artifacts/{revision}/pdf/download")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    saved = app.state.ingestion_repository.get_study_revision(revision)
    assert response.content == saved.canonical_derived_path.read_bytes()
    saved.canonical_derived_path.write_bytes(b"changed")
    assert client.get(f"/artifacts/{revision}/pdf/download").status_code == 409


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


def test_ready_transcript_is_compact_with_recoverable_delete_menu(tmp_path):
    from selectolax.parser import HTMLParser

    app, _, lecture, revision = prepared(tmp_path)
    document = HTMLParser(TestClient(app).get(f"/lectures/{lecture}").text)
    panel = document.css_first('[data-material-panel][data-kind="transcripts"]')
    actions = panel.css_first("[data-material-actions]")
    assert [node.text(strip=True) for node in actions.css("a")] == ["Download TXT", "View TXT"]
    menu = actions.css_first("details.material-options")
    assert menu is not None
    assert menu.css_first("summary").attributes["aria-label"] == "Transcript options"
    delete = menu.css_first("button")
    assert delete.text(strip=True) == "Delete transcript"
    form = menu.css_first("form")
    assert form.attributes["action"] == f"/lectures/{lecture}/materials/{revision}/remove"
    assert form.attributes["data-material-remove"] == "transcript"
    assert panel.css_first("[data-material-upload]") is None
    assert panel.css_first(".sh-pill") is None
    assert "Ready to use" not in panel.text()
    assert document.css_first("[data-clinical-notebook]") is None
    assert "clinical_notebook.js" not in document.html


def test_empty_slides_bottom_upload_button_is_the_only_picker(tmp_path):
    from selectolax.parser import HTMLParser

    app, _, lecture, _ = prepared(tmp_path)
    panel = HTMLParser(TestClient(app).get(f"/lectures/{lecture}").text).css_first(
        '[data-material-panel][data-kind="slides"]'
    )
    dropzone = panel.css_first("[data-material-dropzone]")
    assert dropzone.tag == "div"
    assert dropzone.css("input, button, label") == []
    picker = panel.css_first("[data-material-picker]")
    assert picker.text(strip=True) == "Upload Slides"
    assert picker.attributes["type"] == "button"
    assert "hidden" in panel.css_first("[data-material-file]").attributes
    assert len(panel.css("[data-material-upload] button")) == 1


@pytest.mark.parametrize("kind", [UploadKind.SLIDES, UploadKind.TRANSCRIPTS])
def test_removed_hash_remains_scoped_to_its_material_kind(tmp_path, kind):
    app, service, lecture, revision = prepared(tmp_path, kind)
    removed = service.remove(lecture, revision)
    other_kind = UploadKind.TRANSCRIPTS if kind is UploadKind.SLIDES else UploadKind.SLIDES
    repository = app.state.ingestion_repository
    batch = repository.create_batch(other_kind)
    repository.add_item(
        other_kind,
        StagedUpload(
            batch, "other-kind", removed.immutable_source_path, removed.source_sha256,
            removed.immutable_source_path.stat().st_size, removed.immutable_source_path.name,
        ),
    )
    # Exercise repository admission only: source validation remains owned by staging.
    repository.set_manual_assignment("other-kind", lecture)
    item = repository.require_item("other-kind")
    assert item.kind is other_kind and item.lecture_id == lecture
    assert repository.count_jobs(item.id, "process") == 1
    assert service.list_removed(lecture) == [removed]


def test_ready_slides_has_only_available_actions_and_recoverable_trash(tmp_path):
    from selectolax.parser import HTMLParser

    app, _, lecture, revision = prepared(tmp_path, UploadKind.SLIDES)
    document = HTMLParser(TestClient(app).get(f"/lectures/{lecture}").text)
    panel = document.css_first('[data-material-panel][data-kind="slides"]')
    actions = panel.css_first('[data-material-actions]')
    assert [node.text(strip=True) for node in actions.css('a')] == [
        'Download PPTX', 'Download PDF', 'View PDF',
    ]
    trash = actions.css_first('button[aria-label="Delete slides"]')
    assert trash is not None
    assert trash.css_first('svg[aria-hidden="true"]') is not None
    form = actions.css_first('form')
    assert form.attributes['action'] == f'/lectures/{lecture}/materials/{revision}/remove'
    assert form.attributes['data-material-remove'] == 'slides'
    assert actions.css('a, button')[-1].attributes['aria-label'] == 'Delete slides'
    assert panel.css_first('[data-material-upload]') is None
    assert panel.css_first('[data-material-dropzone]') is None
    assert panel.css_first('.material-remove') is None
    assert 'Upload Slides' not in panel.text()
    assert 'Replace slides' not in panel.text()


def test_tracker_count_has_aligned_group_without_changing_count_hook(tmp_path):
    from selectolax.parser import HTMLParser

    app, _, lecture, _ = prepared(tmp_path)
    document = HTMLParser(TestClient(app).get(f'/lectures/{lecture}').text)
    group = document.css_first('.pass-tracker-summary .pass-tracker-count')
    assert group is not None
    assert group.css_first('[data-pass-count]').text(strip=True) == '0/5'
    assert group.css_first('.pass-count-label').text(strip=True) == 'completed / configured'
