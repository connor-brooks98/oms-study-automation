"""Synthetic source API client checks; no authenticated external calls."""

import hashlib
import io
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.clinical_notebook import ClinicalNotebookError, SourceAPI, normalize_endpoint
from oms_hub.clinical_notebook_service import ClinicalNotebookService, Material
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput


class Secrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


TOKEN = "cn_int_" + "testonly_" * 6
BASE = "https://clinical.example.test/api/external/v1"


class Provider:
    def __init__(self):
        self.requests = []
        self.offline = False
        self.fail_second = False
        self.receipts = {}
        self.posts = []
        self.status = "accepted"
        self.notebooks = [{"id": "notebook:exam3", "title": "Cardio Exam 3"}]
        self.redirect = False

    def handle(self, request):
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if self.redirect:
            return httpx.Response(307, headers={"Location": "https://other.example.test/steal"})
        if self.offline:
            raise httpx.ConnectError("private endpoint/token detail", request=request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(
                200,
                json={
                    "api_version": "1",
                    "input_types": ["file", "text"],
                    "max_file_bytes": 100 * 1024 * 1024,
                    "extensions": [".pdf", ".txt", ".md"],
                    "processing": "local_only",
                },
            )
        if request.url.path.endswith("/notebooks"):
            return httpx.Response(200, json={"items": self.notebooks, "next_cursor": None})
        if request.method == "POST":
            from email.parser import BytesParser
            from email.policy import default

            multipart = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["Content-Type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            fields = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in multipart.iter_parts()
            }
            self.posts.append(fields)
            if self.fail_second and len(self.posts) == 2:
                raise httpx.ReadError("private lost response", request=request)
            key = fields["idempotency_key"].decode()
            digest = hashlib.sha256(fields["file"]).hexdigest()
            assert fields["sha256"].decode() == digest
            if key not in self.receipts:
                self.receipts[key] = {
                    "id": f"import-{len(self.receipts)}",
                    "source_id": f"source:{len(self.receipts)}",
                    "notebook_id": "notebook:exam3",
                    "sha256": digest,
                    "status": self.status,
                    "warnings": [],
                }
            return httpx.Response(202, json=self.receipts[key])
        receipt = next(
            v for v in self.receipts.values() if request.url.path.endswith("/" + v["id"])
        )
        return httpx.Response(200, json={**receipt, "status": self.status})


@contextmanager
def setup(tmp_path):
    provider = Provider()
    service = ClinicalNotebookService(tmp_path, Secrets())
    service.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(provider.handle), follow_redirects=False
    )
    try:
        service.configure("owner", BASE, TOKEN, "My Clinical Notebook")
        service.notebooks("owner")
        service.save_destination("owner", '["Cardio",3]', "notebook:exam3")
        yield service, provider
    finally:
        service.close()


def material(content=b"A lecture transcript.", role="transcript", revision=1):
    return Material("lecture.txt", io.BytesIO(content), role, revision)


def wait(service, job):
    service.active[("owner", job)].result(timeout=10)


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@clinical.test",
        "https://clinical.test?token=secret",
        "https://clinical.test/#secret",
        "http://clinical.test",
        "file:///tmp/a",
        "https://clinical.test/api/admin",
        "https://clinical.test\\@evil.test",
    ],
)
def test_endpoint_rejects_credential_and_unsafe_transport_variants(url):
    with pytest.raises(ClinicalNotebookError):
        normalize_endpoint(url)


def test_endpoint_accepts_explicit_https_and_loopback_without_redirects():
    assert normalize_endpoint("https://clinical.test/") == "https://clinical.test/api/external/v1"
    assert (
        normalize_endpoint("http://127.0.0.1:8767/api/external/v1/")
        == "http://127.0.0.1:8767/api/external/v1"
    )
    provider = Provider()
    provider.redirect = True
    with httpx.Client(
        transport=httpx.MockTransport(provider.handle), follow_redirects=False
    ) as client:
        with pytest.raises(ClinicalNotebookError, match="redirect"):
            SourceAPI(client, BASE, TOKEN).notebooks()
    assert len(provider.requests) == 1


def test_configuration_is_encrypted_owner_bound_and_new_connection_invalidates_mapping(tmp_path):
    with setup(tmp_path) as (service, _):
        assert TOKEN not in "".join(p.read_text() for p in tmp_path.rglob("*.enc"))
        assert (
            service.status("owner", '["Cardio",3]')["destination"]["notebook_id"]
            == "notebook:exam3"
        )
        assert service.status("outsider", '["Cardio",3]')["configured"] is False
        assert TOKEN not in json.dumps(service.status("owner"))
        service.configure("owner", BASE, TOKEN, "Renamed installation")
        assert service.status("owner", '["Cardio",3]')["destination"]
        service.configure("owner", BASE, TOKEN + "new", "Other integration")
        assert service.status("owner", '["Cardio",3]')["destination"] is None


def test_repeated_send_reuses_source_and_changed_content_requires_review(tmp_path):
    with setup(tmp_path) as (service, provider):
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        assert service.jobs("owner", lecture_id=7)[0]["status"] == "accepted"
        assert service.jobs("owner", lecture_id=8) == []
        assert service.enqueue("owner", '["Cardio",3]', 7, [material()]) == job
        assert len(provider.posts) == 1
        metadata = json.loads(provider.posts[0]["metadata"])
        assert metadata["study_hub"]["lecture_id"] == 7
        assert metadata["study_hub"]["revision_id"] == 1
        with pytest.raises(ClinicalNotebookError, match="changed"):
            service.enqueue(
                "owner", '["Cardio",3]', 7, [material(b"Changed transcript.", revision=2)]
            )
        assert len(provider.posts) == 1
        provider.status = "ready"
        service.check("owner", job)
        assert service.jobs("owner")[0]["status"] == "ready"


def test_partial_delivery_retries_only_unacknowledged_file_with_pinned_bytes(tmp_path):
    with setup(tmp_path) as (service, provider):
        provider.fail_second = True
        # The fixture uses distinct logical materials without requiring a remote PDF parser.
        job = service.enqueue(
            "owner",
            '["Cardio",3]',
            7,
            [material(), material(b"Second notes.", role="slides", revision=2)],
        )
        wait(service, job)
        assert service.jobs("owner")[0]["status"] == "partial"
        assert "private" not in json.dumps(service.jobs("owner"))
        service.configure(
            "owner", "https://other.example.test", TOKEN + "new", "Different installation"
        )
        service.resume("owner", job)
        wait(service, job)
        assert len(provider.posts) == 3
        assert provider.posts[1] == provider.posts[2]
        assert service.jobs("owner")[0]["status"] == "accepted"
        assert service.jobs("owner")[0]["installation"] == "My Clinical Notebook"
        assert all(request.url.host == "clinical.example.test" for request in provider.requests)


def test_offline_delivery_survives_restart_without_automatic_replay(tmp_path):
    with setup(tmp_path) as (service, provider):
        provider.offline = True
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        assert service.jobs("owner")[0]["status"] == "pending"
        recovered = ClinicalNotebookService(tmp_path, service.secrets)
        recovered.client_factory = service.client_factory
        try:
            assert recovered.jobs("owner")[0]["status"] == "pending"
            provider.offline = False
            recovered.resume("owner", job)
            wait(recovered, job)
            assert recovered.jobs("owner")[0]["status"] == "accepted"
        finally:
            recovered.close()


def test_accepted_needs_attention_receipt_never_resends(tmp_path):
    with setup(tmp_path) as (service, provider):
        provider.status = "needs_attention"
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        service.resume("owner", job)
        wait(service, job)
        assert len(provider.posts) == 1
        assert service.jobs("owner")[0]["status"] == "needs_attention"


def test_saved_notebook_is_invalidated_by_authorized_list_without_title_guess(tmp_path):
    with setup(tmp_path) as (service, provider):
        provider.notebooks = [{"id": "notebook:replacement", "title": "Cardio Exam 3"}]
        service.notebooks("owner")
        assert service.status("owner", '["Cardio",3]')["destination"] is None


def test_private_routes_csrf_exam_mapping_and_secret_redaction(tmp_path, monkeypatch):
    from selectolax.parser import HTMLParser
    monkeypatch.setattr("oms_hub.app.KeyringSecretStore", Secrets)
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'hub.db'}")
    )
    provider = Provider()
    service = app.state.clinical_notebook
    service.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(provider.handle), follow_redirects=False
    )
    ids = [
        app.state.catalog_repository.upsert_lecture(
            LectureInput(subject, exam, n, "Lecture", "", None)
        )
        for subject, exam, n in [("Cardio", 3, 1), ("Cardio", 3, 2), ("Renal", 3, 1)]
    ]
    client = TestClient(app)
    try:
        page = client.get("/settings/clinical-notebook")
        assert page.status_code == 200
        connection_form = HTMLParser(page.text).css_first("[data-cn-connect]")
        assert connection_form.attributes.get("method") == "post"
        assert connection_form.attributes.get("action") == "/settings/clinical-notebook/connect"
        csrf = client.cookies.get(app.state.csrf.cookie_name)
        fields = {
            "endpoint": BASE,
            "token": TOKEN,
            "label": "My Clinical Notebook",
            "csrf_token": csrf,
        }
        assert (
            client.post(
                "/settings/clinical-notebook/connect", data={**fields, "csrf_token": "wrong"}
            ).status_code
            == 403
        )
        assert client.post("/settings/clinical-notebook/connect", data=fields).status_code == 200
        assert TOKEN not in client.get("/settings/clinical-notebook/status").text
        assert (
            client.post("/settings/clinical-notebook/notebooks", data={"csrf_token": csrf}).json()[
                "items"
            ][0]["id"]
            == "notebook:exam3"
        )
        assert (
            client.post(
                f"/settings/clinical-notebook/lectures/{ids[0]}/destination",
                data={"csrf_token": csrf, "notebook_id": "notebook:exam3"},
            ).status_code
            == 200
        )
        for lecture, saved in zip(ids, [True, True, False], strict=True):
            assert (
                bool(
                    client.get(f"/settings/clinical-notebook/status?lecture_id={lecture}").json()[
                        "destination"
                    ]
                )
                is saved
            )
        assert (
            client.post(
                f"/settings/clinical-notebook/lectures/{ids[0]}/send", data={"csrf_token": csrf}
            ).status_code
            == 409
        )
        assert provider.posts == []
        assert client.get("/settings/clinical-notebook/status?lecture_id=999999").status_code == 404
        assert (
            client.post(
                "/settings/clinical-notebook/connect",
                data={**fields, "token": "native_admin_secret"},
            ).status_code
            == 409
        )
        assert (
            "native_admin_secret"
            not in client.post(
                "/settings/clinical-notebook/connect",
                data={**fields, "token": "native_admin_secret"},
            ).text
        )
    finally:
        service.close()
        app.state.extendlm.close()


def test_mismatched_receipt_does_not_claim_delivery(tmp_path):
    with setup(tmp_path) as (service, provider):
        original = provider.handle

        def wrong_receipt(request):
            response = original(request)
            if request.method == "POST":
                payload = response.json()
                payload["notebook_id"] = "notebook:other"
                return httpx.Response(202, json=payload)
            return response

        service.client_factory = lambda: httpx.Client(
            transport=httpx.MockTransport(wrong_receipt), follow_redirects=False
        )
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        receipt = service.jobs("owner")[0]
        assert receipt["status"] == "pending"
        assert receipt["files"][0]["receipt"] is None
        service.client_factory = lambda: httpx.Client(
            transport=httpx.MockTransport(original), follow_redirects=False
        )
        service.resume("owner", job)
        wait(service, job)
        assert provider.posts[0]["idempotency_key"] == provider.posts[1]["idempotency_key"]
        assert service.jobs("owner")[0]["files"][0]["receipt"]["source_id"] == "source:0"


def test_revoked_notebook_rejects_source_and_clears_future_destination(tmp_path):
    with setup(tmp_path) as (service, provider):
        original = provider.handle

        def revoked(request):
            if request.method == "POST":
                return httpx.Response(403)
            return original(request)

        service.client_factory = lambda: httpx.Client(
            transport=httpx.MockTransport(revoked), follow_redirects=False
        )
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        assert service.jobs("owner")[0]["status"] == "needs_attention"
        assert service.status("owner", '["Cardio",3]')["destination"] is None


def test_lecture_route_transfers_only_selected_approved_artifact(tmp_path, monkeypatch):
    from oms_hub.ingestion.domain import StagedUpload, UploadKind
    from oms_hub.models import StudyRevisionModel

    monkeypatch.setattr("oms_hub.app.KeyringSecretStore", Secrets)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            study_root=tmp_path / "study",
            icloud_staging_root=tmp_path / "icloud",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    service = app.state.clinical_notebook
    provider = Provider()
    service.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(provider.handle), follow_redirects=False
    )
    lecture = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 3, 1, "Lecture", "", None)
    )
    source_dir = app.state.settings.data_dir / "artifacts" / "v2" / "transcripts"
    source_dir.mkdir(parents=True)
    raw, cleaned = source_dir / "raw.txt", source_dir / "cleaned.txt"
    raw.write_bytes(b"Unapproved raw transcript.")
    cleaned.write_bytes(b"Approved cleaned transcript.")
    app.state.settings.study_root.mkdir(parents=True)
    canonical = app.state.settings.study_root / cleaned.name
    canonical.write_bytes(cleaned.read_bytes())
    batch = app.state.ingestion_repository.create_batch(UploadKind.TRANSCRIPTS)
    app.state.ingestion_repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch,
            "clinical-fixture",
            raw,
            hashlib.sha256(raw.read_bytes()).hexdigest(),
            raw.stat().st_size,
            raw.name,
        ),
    )
    with app.state.database.session() as session:
        revision = StudyRevisionModel(
            upload_item_id="clinical-fixture",
            lecture_id=lecture,
            kind="transcripts",
            source_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
            immutable_source_path=str(raw),
            immutable_derived_path=str(cleaned),
            canonical_source_path=str(raw),
            canonical_derived_path=str(canonical),
            derived_sha256=hashlib.sha256(cleaned.read_bytes()).hexdigest(),
            state="current",
            current=True,
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id
    client = TestClient(app)
    try:
        client.get("/settings/clinical-notebook")
        csrf = client.cookies.get(app.state.csrf.cookie_name)
        service.configure("local-owner", BASE, TOKEN, "Test installation")
        service.notebooks("local-owner")
        service.save_destination("local-owner", '["Cardio",3]', "notebook:exam3")
        denied = client.post(
            f"/settings/clinical-notebook/lectures/{lecture}/send",
            data={"csrf_token": csrf, "slides": "true", "transcript": "false"},
        )
        assert denied.status_code == 409
        stale_selection = client.post(
            f"/settings/clinical-notebook/lectures/{lecture}/send",
            data={"csrf_token": csrf, "slides": "true", "transcript": "true"},
        )
        assert stale_selection.status_code == 409
        assert provider.posts == []
        sent = client.post(
            f"/settings/clinical-notebook/lectures/{lecture}/send",
            data={"csrf_token": csrf, "slides": "false", "transcript": "true"},
        )
        assert sent.status_code == 202, sent.text
        job = sent.json()["job_id"]
        service.active[("local-owner", job)].result(timeout=10)
        assert provider.posts[0]["file"] == b"Approved cleaned transcript."
        assert json.loads(provider.posts[0]["metadata"])["study_hub"]["revision_id"] == revision_id
        assert (
            client.get(f"/settings/clinical-notebook/status?lecture_id={lecture}").json()["jobs"][
                0
            ]["files"][0]["receipt"]["source_id"]
            == "source:0"
        )
    finally:
        service.close()
        app.state.extendlm.close()


def test_disconnect_waits_for_active_delivery_and_then_removes_credentials(tmp_path):
    from concurrent.futures import Future

    with setup(tmp_path) as (service, _):
        active = Future()
        service.active[("owner", "pending")] = active
        with pytest.raises(ClinicalNotebookError, match="Wait"):
            service.disconnect("owner")
        active.set_result(None)
        service.disconnect("owner")
        assert service.status("owner")["configured"] is False
        assert service.status("owner", '["Cardio",3]')["destination"] is None
        with service.state("owner") as state:
            assert all("token" not in connection for connection in state["connections"].values())


def test_other_owner_cannot_retry_or_inspect_receipt(tmp_path):
    with setup(tmp_path) as (service, _):
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        assert service.jobs("outsider") == []
        with pytest.raises(ClinicalNotebookError, match="not found"):
            service.resume("outsider", job)
        with pytest.raises(ClinicalNotebookError, match="not found"):
            service.check("outsider", job)


@pytest.mark.parametrize("source_id", [None, ""])
def test_ready_receipt_requires_a_stable_source_identity(source_id):
    with pytest.raises(ClinicalNotebookError, match="Receipt"):
        SourceAPI.receipt(
            {
                "id": "import-1",
                "source_id": source_id,
                "notebook_id": "notebook:exam3",
                "sha256": "a" * 64,
                "status": "ready",
                "warnings": [],
            },
            "notebook:exam3",
            "a" * 64,
        )


def test_readiness_cannot_switch_an_acknowledged_source_identity(tmp_path):
    with setup(tmp_path) as (service, provider):
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        old = service.jobs("owner")[0]["files"][0]["receipt"]["source_id"]
        provider.receipts[next(iter(provider.receipts))]["source_id"] = "source:replacement"
        with pytest.raises(ClinicalNotebookError, match="different"):
            service.check("owner", job)
        assert service.jobs("owner")[0]["files"][0]["receipt"]["source_id"] == old


def test_provider_pending_receipt_is_not_reported_accepted_or_resent(tmp_path):
    with setup(tmp_path) as (service, provider):
        provider.status = "pending"
        job = service.enqueue("owner", '["Cardio",3]', 7, [material()])
        wait(service, job)
        assert service.jobs("owner")[0]["status"] == "pending"
        assert service.jobs("owner")[0]["can_retry"] is False
        service.resume("owner", job)
        assert len(provider.posts) == 1
        provider.status = "ready"
        service.check("owner", job)
        assert service.jobs("owner")[0]["status"] == "ready"


def test_material_changed_during_snapshot_is_not_sent_as_approved_revision(tmp_path):
    with setup(tmp_path) as (service, provider):
        selected = material(b"Changed after artifact resolution.")
        selected.expected_sha256 = hashlib.sha256(b"Approved transcript.").hexdigest()
        with pytest.raises(ClinicalNotebookError, match="changed while"):
            service.enqueue("owner", '["Cardio",3]', 7, [selected])
        assert provider.posts == []
        assert list((service.directory("owner") / "files").iterdir()) == []
