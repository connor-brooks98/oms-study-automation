"""Unmatched deletion must never consume a processing or shared source."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from oms_hub.db import Database
from oms_hub.ingestion.domain import MatchDecision, StagedUpload, UploadKind, UploadState
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService, UploadRejected
from oms_hub.models import IngestionJobModel, StudyRevisionModel, UploadItemModel
from oms_hub.repositories import CatalogRepository, LectureInput


@pytest.fixture
def upload(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'delete.db'}")
    database.create_schema()
    repository = IngestionRepository(database)
    catalog = CatalogRepository(database)
    lecture = catalog.upsert_lecture(LectureInput("Cardio", 1, 1, "Heart", "Test", None))
    root = tmp_path / "staging"
    root.mkdir()
    staging = StagingService(root, 1_000_000, 2_000_000)
    service = IngestionService(repository, catalog, UploadMatcher(), staging)
    batch = repository.create_batch(UploadKind.SLIDES)
    (root / batch).mkdir()

    def add(item="mistake", path=None):
        path = path or root / batch / f"{item}.ready"
        path.write_bytes(b"slides")
        repository.add_item(
            UploadKind.SLIDES,
            StagedUpload(
                batch,
                item,
                path,
                hashlib.sha256(b"slides").hexdigest(),
                6,
                f"{item}.pdf",
            ),
        )
        repository.apply_match(item, MatchDecision("quarantined", None, 0, ("unmatched",)))
        return path

    path = add()
    yield database, repository, service, lecture, batch, path, add
    database.close()


def test_delete_commits_tombstone_before_single_file_cleanup(upload, monkeypatch):
    database, repo, service, _, batch, path, add = upload
    sibling = add("sibling")
    original = service.staging.discard_file

    def cleanup(candidate):
        # A separate connection sees the durable tombstone before bytes disappear.
        assert IngestionRepository(database).require_item("mistake").state is UploadState.DISCARDED
        original(candidate)

    monkeypatch.setattr(service.staging, "discard_file", cleanup)
    deleted = service.discard_item("mistake")
    assert deleted.state is UploadState.DISCARDED
    assert not path.exists()
    assert sibling.read_bytes() == b"slides"
    assert sibling.parent.is_dir()
    assert repo.get_batch(batch).state is UploadState.QUARANTINED
    assert "unmatched" in deleted.evidence
    assert any("Discarded" in entry and "quarantined" in entry for entry in deleted.evidence)
    assert service.discard_item("mistake").error is None


def test_missing_file_is_success_and_cleanup_error_is_durable_retry(upload, monkeypatch):
    _, repo, service, _, _, path, _ = upload
    original = service.staging.discard_file

    def denied(candidate):
        raise PermissionError("fixture failure")

    monkeypatch.setattr(service.staging, "discard_file", denied)
    result = service.discard_item("mistake")
    assert result.state is UploadState.DISCARDED
    assert result.error == "Removed from uploads; file cleanup pending"
    assert path.exists()
    assert repo.list_quarantined()[0].id == "mistake"
    monkeypatch.setattr(service.staging, "discard_file", original)
    path.unlink()
    assert service.discard_item("mistake").error is None
    assert repo.list_quarantined() == []


@pytest.mark.parametrize(
    "reference", ["lecture", "job", "revision", "shared_path", "revision_path"]
)
def test_downstream_or_shared_source_cannot_be_deleted(upload, reference):
    database, repo, service, lecture, _, path, add = upload
    if reference in {"shared_path", "revision_path"}:
        add("sibling", path if reference == "shared_path" else None)
    with database.session() as session:
        item = session.get(UploadItemModel, "mistake")
        if reference == "lecture":
            item.lecture_id = lecture
        elif reference == "job":
            session.add(IngestionJobModel(upload_item_id=item.id, action="process"))
        elif reference in {"revision", "revision_path"}:
            session.add(
                StudyRevisionModel(
                    upload_item_id=item.id if reference == "revision" else "sibling",
                    lecture_id=lecture,
                    kind="slides",
                    source_sha256="a" * 64,
                    immutable_source_path=str(path),
                    state="failed",
                    current=False,
                )
            )
    with pytest.raises(ValueError):
        service.discard_item("mistake")
    assert path.exists()
    assert repo.require_item("mistake").state is UploadState.QUARANTINED


@pytest.mark.parametrize(
    "unsafe", ["outside", "traversal", "symlink", "parent_symlink", "directory"]
)
def test_unsafe_staged_paths_cannot_be_deleted(upload, tmp_path, unsafe):
    database, repo, service, _, _, path, _ = upload
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"preserve")
    candidate = outside
    if unsafe == "traversal":
        candidate = path.parent / ".." / path.parent.name / path.name
    elif unsafe == "symlink":
        path.unlink()
        path.symlink_to(outside)
        candidate = path
    elif unsafe == "parent_symlink":
        link = service.staging.root / "alias"
        link.symlink_to(path.parent, target_is_directory=True)
        candidate = link / path.name
    elif unsafe == "directory":
        candidate = path.parent
    with database.session() as session:
        session.get(UploadItemModel, "mistake").staged_path = str(candidate)
    with pytest.raises(UploadRejected):
        service.discard_item("mistake")
    assert outside.read_bytes() == b"preserve"
    assert repo.require_item("mistake").state is UploadState.QUARANTINED


def test_stale_assignment_and_match_cannot_resurrect_deleted_upload(upload):
    _, repo, service, lecture, _, _, _ = upload
    service.discard_item("mistake")
    with pytest.raises(ValueError):
        repo.set_manual_assignment("mistake", lecture)
    with pytest.raises(ValueError):
        repo.apply_match("mistake", MatchDecision("matched", lecture, 1, ()))
    with pytest.raises(ValueError):
        repo.confirm_processing("mistake")
    assert repo.require_item("mistake").state is UploadState.DISCARDED
    assert repo.count_jobs("mistake", "process") == 0


def test_assignment_and_delete_serialize_between_repository_instances(upload):
    database, repo, service, lecture, _, path, _ = upload
    barrier = Barrier(2)
    other = IngestionRepository(database)

    def assign():
        barrier.wait()
        try:
            other.set_manual_assignment("mistake", lecture)
            return "assigned"
        except ValueError:
            return "rejected"

    def delete():
        barrier.wait()
        try:
            service.discard_item("mistake")
            return "deleted"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assignments = pool.submit(assign)
        deletions = pool.submit(delete)
        outcomes = {assignments.result(), deletions.result()}
    item = repo.require_item("mistake")
    if "deleted" in outcomes:
        assert item.state is UploadState.DISCARDED and not path.exists()
        assert repo.count_jobs("mistake", "process") == 0
    else:
        assert outcomes == {"assigned", "rejected"}
        assert item.state is UploadState.QUEUED and path.exists()
        assert repo.count_jobs("mistake", "process") == 1


def test_confirm_and_delete_serialize_across_services(upload):
    database, repo, service, lecture, _, path, _ = upload
    with database.session() as session:
        item = session.get(UploadItemModel, "mistake")
        item.state = "awaiting_confirmation"
        item.lecture_id = lecture
    barrier = Barrier(2)
    other = IngestionRepository(database)

    def confirm():
        barrier.wait()
        try:
            other.confirm_processing("mistake")
        except ValueError:
            pass

    def delete():
        barrier.wait()
        try:
            service.discard_item("mistake")
        except ValueError:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm), pool.submit(delete)]
        for future in futures:
            future.result()
    item = repo.require_item("mistake")
    assert (item.state, path.exists(), repo.count_jobs("mistake", "process")) in {
        (UploadState.DISCARDED, False, 0),
        (UploadState.QUEUED, True, 1),
    }


def test_awaiting_confirmation_with_recovery_references_keeps_source(upload):
    database, repo, service, lecture, _, path, _ = upload
    with database.session() as session:
        item = session.get(UploadItemModel, "mistake")
        item.state = "awaiting_confirmation"
        item.lecture_id = lecture
        session.add(
            StudyRevisionModel(
                upload_item_id=item.id,
                lecture_id=lecture,
                kind="slides",
                source_sha256="a" * 64,
                immutable_source_path=str(path),
                state="proposed",
                current=False,
            )
        )
    with pytest.raises(ValueError, match="processing history"):
        service.discard_item("mistake")
    assert path.exists()
    assert repo.require_item("mistake").state is UploadState.AWAITING_CONFIRMATION


def test_delete_route_owner_csrf_detail_and_linked_recovery(tmp_path):
    from fastapi.testclient import TestClient

    from oms_hub.app import create_app
    from oms_hub.config import Settings

    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'routes.db'}",
            allow_local_access=True,
        )
    )
    repo = app.state.ingestion_repository
    staging = app.state.upload_staging.root
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / "mistake.ready"
    path.write_bytes(b"unmatched")
    batch = repo.create_batch(UploadKind.SLIDES)
    repo.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch,
            "mistake",
            path,
            "b" * 64,
            9,
            "Mistaken <file>.pdf",
        ),
    )
    repo.apply_match("mistake", MatchDecision("quarantined", None, 0, ()))
    client = TestClient(app)
    # Even if this router is mounted without the Hub's host middleware, a
    # missing private owner must not authorize a filesystem mutation.
    from fastapi import FastAPI

    from oms_hub.web.upload_routes import router

    no_owner = FastAPI()
    no_owner.include_router(router)
    assert TestClient(no_owner).post("/api/upload-items/mistake/delete").status_code == 403
    page = client.get("/quarantine")
    assert 'data-delete-upload="mistake"' in page.text
    assert "Mistaken &lt;file&gt;.pdf" in page.text
    detail = client.get(f"/api/upload-batches/{batch}").json()["items"][0]
    assert detail["can_delete_upload"] is True
    endpoint = "/api/upload-items/mistake/delete"
    assert client.post(endpoint).status_code == 403
    token = client.cookies["study_hub_csrf"]
    assert (
        client.post(
            endpoint,
            headers={"X-CSRF-Token": token, "Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    with TestClient(app, base_url="http://outsider.example") as outsider:
        assert outsider.post(endpoint).status_code == 400
    assert path.exists()
    response = client.post(endpoint, headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.json()["cleanup_pending"] is False
    assert not path.exists()
    assert client.post("/quarantine/mistake/assign", data={"lecture_id": 999}).status_code == 404
    app.state.database.close()


def test_delete_after_assignment_cannot_discard_new_confirmation(upload):
    _, repo, service, lecture, _, path, _ = upload
    service.assign("mistake", lecture)
    with pytest.raises(ValueError):
        service.discard_item("mistake", expected_state=UploadState.QUARANTINED)
    assert repo.require_item("mistake").state is UploadState.QUEUED
    assert path.exists()
