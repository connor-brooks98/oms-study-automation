"""Offline contract checks; these do not assert a live Google/ExtendLM session."""

import base64
import hashlib
import io
import json
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from oms_hub import extendlm as api
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.extendlm_service import ExtendLMService


class Secrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class Provider:
    def __init__(self):
        self.calls = []
        self.lose_add_response = False
        self.sse = False
        self.put_count = 0
        self.add_calls = []
        self.upload_host = "api.extendlm.com"
        self.connection = "browser_connection_0001"
        self.pdf = False
        self.source_status = "ready"
        self.notebooks = [{"id": "notebook-1", "title": "Exam 3"}]
        self.complete = True
        self.notebook_pages = {}

    def handle(self, request):
        self.calls.append(request)
        if request.url.path.endswith("/register"):
            data = json.loads(request.content)
            assert data["token_endpoint_auth_method"] == "none"
            assert data["redirect_uris"] == ["http://testserver/settings/extendlm/callback"]
            return httpx.Response(201, json={"client_id": "hub-client"})
        if request.url.path.endswith("/token"):
            data = parse_qs(request.content.decode())
            assert data["resource"] == [api.MCP_URL]
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "expires_in": 3600,
                    "refresh_token": "test-refresh-token",
                    "token_type": "Bearer",
                },
            )
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200)
        if request.method == "PUT":
            self.put_count += 1
            assert request.headers["Authorization"] == "Bearer upload-capability"
            assert (
                request.content.startswith(b"%PDF")
                if self.pdf
                else request.content == b"A lecture transcript."
            )
            return httpx.Response(201)
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )
        assert request.headers["Mcp-Session-Id"] == "session"
        assert request.headers["Mcp-Protocol-Version"] == "2025-11-25"
        name, args = body["params"]["name"], body["params"]["arguments"]
        result = {}
        if name == "list_notebook_users":
            result = {
                "connections": [
                    {
                        "extension_connection": self.connection,
                        "emails_shared": False,
                        "users": [{"auth_user_id": "0", "is_selected": True}],
                    }
                ]
            }
        elif name == "get_notebooklm_capabilities":
            assert args["auth_user_id"] == "0"
            result = {"authenticated": True, "capabilities": {}}
        elif name == "list_notebooks":
            result = self.notebook_pages.get(args.get("cursor", "")) or {
                "items": self.notebooks,
                "next_cursor": "",
            }
        elif name.startswith("prepare_"):
            result = {
                "resource_id": "exp_0123456789abcdef",
                "method": "PUT",
                "upload_url": f"https://{self.upload_host}/upload/exp_0123456789abcdef",
                "required_headers": {
                    "Authorization": "Bearer upload-capability",
                    "Content-Type": "application/pdf" if self.pdf else "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="lecture.txt"',
                },
                "max_bytes": api.MAX_BYTES,
                "expected_mime_type": "application/pdf" if self.pdf else "application/octet-stream",
                "upload_expires_at_unix_ms": (time.time() + 600) * 1000,
            }
        elif name in {"add_file_source", "add_pdf_source"}:
            self.add_calls.append(args.copy())
            if self.lose_add_response:
                self.lose_add_response = False
                raise httpx.ReadError("sensitive provider detail", request=request)
            result = {
                "notebook_id": args["notebook_id"],
                "resource_id": args["resource_id"],
                "source_id": "source-1",
                "status": "added",
                "complete": self.complete,
            }
            if self.pdf:
                assert args["split_mode"] == "single"
                result.pop("source_id")
                result.update(source_ids=["source-1"], complete=True)
        elif name == "list_notebook_sources":
            result = {
                "items": [{"id": "source-1", "source_status": self.source_status}],
                "next_cursor": "",
            }
        else:
            raise AssertionError(name)
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"structuredContent": result}}
        if self.sse:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=": heartbeat\n\ndata: " + json.dumps(payload) + "\n\n",
            )
        return httpx.Response(200, json=payload)


@contextmanager
def setup(tmp_path):
    provider = Provider()
    service = ExtendLMService(tmp_path, Secrets())

    @contextmanager
    def factory():
        with httpx.Client(
            transport=httpx.MockTransport(provider.handle), follow_redirects=False
        ) as c:
            try:
                yield c
            except httpx.HTTPError as error:
                raise api.ExtendLMError("Connection interrupted. Resume.") from error

    service.client_factory = factory
    try:
        yield service, provider
    finally:
        service.close()


def sign_in(service):
    cookie, url = service.begin("owner", "http://testserver/settings/extendlm/callback")
    key = service.session_key(cookie, "owner")
    query = parse_qs(urlsplit(url).query)
    with service.state(key) as state:
        verifier = state["pending"]["verifier"]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == {
        "notebooks:read",
        "sources:read",
        "sources:write",
        "exports:create",
        "exports:download",
    }
    service.finish(key, "code", query["state"][0], api.ISSUER)
    return cookie, key, query["state"][0]


def select(service, key):
    choices = service.accounts(key)
    assert len(choices) == 1 and choices[0]["label"] == "Browser 1 · Account 0"
    service.select(key, choices[0]["id"])
    service.notebooks(key)


def wait(service, key, job):
    service.active[(key, job)].result(timeout=10)


def test_oauth_pkce_encrypted_tokens_browser_binding_replay_and_refresh(tmp_path):
    with setup(tmp_path) as (service, provider):
        cookie, key, state = sign_in(service)
        assert "test-access-token" not in (tmp_path / key / "session.enc").read_text()
        with pytest.raises(api.ExtendLMError):
            service.finish(key, "code", state, api.ISSUER)
        with pytest.raises(api.ExtendLMError):
            service.token(service.session_key(cookie, "different-owner"))
        with service.state(key, write=True) as data:
            data["tokens"]["expires_at"] = 0
        assert service.token(key) == "test-access-token"
        assert parse_qs(provider.calls[-1].content.decode())["grant_type"] == ["refresh_token"]
        service.disconnect(key)
        with pytest.raises(api.ExtendLMError):
            service.token(key)


def test_lost_add_response_resumes_same_operation_without_reuploading(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        provider.lose_add_response = True
        job = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))]
        )
        wait(service, key, job)
        assert service.jobs(key)[0]["status"] == "needs_attention"
        assert "sensitive" not in json.dumps(service.jobs(key))
        assert provider.put_count == 1
        service.resume(key, job)
        wait(service, key, job)
        assert provider.add_calls[0] == provider.add_calls[1]
        assert provider.put_count == 1
        assert service.jobs(key)[0]["status"] == "accepted"
        assert service.jobs(key)[0]["files"][0]["source_status"] == "unknown"
        service.check_sources(key, job)
        assert service.jobs(key)[0]["status"] == "ready"
        duplicate = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))]
        )
        assert duplicate == job and provider.put_count == 1
        provider.source_status = "failed"
        service.check_sources(key, job)
        assert service.jobs(key)[0]["status"] == "indexing_failed"
        assert not list((tmp_path / key / "files").iterdir())


def test_json_and_sse_and_explicit_selection(tmp_path):
    with setup(tmp_path) as (service, provider):
        provider.sse = True
        _, key, _ = sign_in(service)
        with pytest.raises(api.ExtendLMError, match="Choose"):
            service.notebooks(key)
        select(service, key)
        with pytest.raises(api.ExtendLMError):
            service.select(key, "invented-account")
        with pytest.raises(api.ExtendLMError):
            service.enqueue(key, "invented-notebook", [("a.txt", io.BytesIO(b"x"))])


@pytest.mark.parametrize("filename", ["../lecture.txt", "a\\b.txt", "a\r.txt", "a.pptx", "a.exe"])
def test_upload_filename_boundary(filename):
    with pytest.raises(api.ExtendLMError):
        api.validate_filename(filename)


def test_upload_capability_cannot_send_data_to_other_hosts(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        provider.upload_host = "attacker.example"
        job = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))]
        )
        wait(service, key, job)
        assert service.jobs(key)[0]["status"] == "needs_attention"
        assert provider.put_count == 0
        assert all(r.url.host != "attacker.example" for r in provider.calls)


def test_restarted_service_retains_receipt_original_selection(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        provider.lose_add_response = True
        job = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))]
        )
        wait(service, key, job)
        original = provider.add_calls[0].copy()
        provider.connection = "different_browser_0001"
        select(service, key)
        recovered = ExtendLMService(tmp_path, service.secrets)
        recovered.client_factory = service.client_factory
        try:
            recovered.resume(key, job)
            wait(recovered, key, job)
            assert provider.add_calls[-1] == original
        finally:
            recovered.close()


def test_routes_private_csrf_cookie_and_local_page(tmp_path, monkeypatch):
    monkeypatch.setattr("oms_hub.app.KeyringSecretStore", Secrets)
    with setup(tmp_path / "bridge") as (service, provider):
        app = create_app(
            Settings(
                _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'hub.db'}"
            )
        )
        app.state.extendlm = service
        client = TestClient(app)
        page = client.get("/settings/extendlm")
        assert page.status_code == 200
        assert "ExtendLM settings" in page.text
        assert client.get("/settings/extendlm/status").json() == {"signed_in": False}
        assert (
            client.post("/settings/extendlm/connect", data={"csrf_token": "wrong"}).status_code
            == 403
        )
        token = client.cookies.get(app.state.csrf.cookie_name)
        login = client.post(
            "/settings/extendlm/connect", data={"csrf_token": token}, follow_redirects=False
        )
        assert login.status_code == 200
        assert "location" not in login.headers
        assert "form-action 'self'" in page.headers["content-security-policy"]
        assert (
            "HttpOnly" in login.headers["set-cookie"]
            and "SameSite=lax" in login.headers["set-cookie"]
        )
        state = parse_qs(urlsplit(login.json()["authorization_url"]).query)["state"][0]
        assert (
            client.get(
                "/settings/extendlm/callback",
                params={"code": "c", "state": "wrong", "iss": api.ISSUER},
            ).status_code
            == 409
        )
        complete = client.get(
            "/settings/extendlm/callback",
            params={"code": "c", "state": state, "iss": api.ISSUER},
            follow_redirects=False,
        )
        assert complete.status_code == 303
        assert client.get("/settings/extendlm/status").json()["signed_in"]
        choices = client.post("/settings/extendlm/accounts", data={"csrf_token": token}).json()[
            "accounts"
        ]
        assert client.post(
            "/settings/extendlm/select", data={"csrf_token": token, "choice": choices[0]["id"]}
        ).json() == {"selected": True}
        assert (
            client.post("/settings/extendlm/notebooks", data={"csrf_token": token}).json()["items"][
                0
            ]["id"]
            == "notebook-1"
        )
        outsider = TestClient(app)
        assert outsider.get("/settings/extendlm/status").json() == {"signed_in": False}
        assert (
            outsider.post("/settings/extendlm/accounts", data={"csrf_token": token}).status_code
            == 403
        )
        assert (
            client.post(
                "/settings/extendlm/accounts",
                data={"csrf_token": token},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )


def test_pdf_source_upload_preserves_pdf_and_waits_for_indexing(tmp_path):
    from pypdf import PdfWriter

    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.write(pdf)
    pdf.seek(0)
    with setup(tmp_path) as (service, provider):
        provider.pdf = True
        _, key, _ = sign_in(service)
        select(service, key)
        job = service.enqueue(key, "notebook-1", [("slides.pdf", pdf)])
        wait(service, key, job)
        assert service.jobs(key)[0]["status"] == "accepted"
        assert provider.add_calls[-1]["split_mode"] == "single"
        service.check_sources(key, job)
        assert service.jobs(key)[0]["status"] == "ready"


def test_stale_account_choice_cannot_select_a_different_browser(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        original = service.accounts(key)[0]["id"]
        assert service.accounts(key)[0]["id"] == original
        provider.connection = "different_browser_0001"
        replacement = service.accounts(key)[0]["id"]
        assert replacement != original
        with pytest.raises(api.ExtendLMError, match="Refresh connections"):
            service.select(key, original)
        service.select(key, replacement)
        assert service.selection(key)["extension_connection"] == provider.connection


def test_saved_exam_destination_survives_restart_and_same_account_but_not_account_change(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        service.save_destination(key, "cardio:3", "notebook-1")
        assert service.destination(key, "cardio:4") is None
        select(service, key)
        assert service.destination(key, "cardio:3")["notebook_title"] == "Exam 3"
        recovered = ExtendLMService(tmp_path, service.secrets)
        try:
            assert (
                recovered.destination(key, "cardio:3")["account_label"] == "Browser 1 · Account 0"
            )
        finally:
            recovered.close()
        provider.connection = "different_browser_0001"
        select(service, key)
        assert service.destination(key, "cardio:3") is None
        provider.connection = "browser_connection_0001"
        select(service, key)
        assert service.destination(key, "cardio:3") is None


def test_deleted_destination_never_matches_replacement_by_title(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        service.save_destination(key, "cardio:3", "notebook-1")
        provider.notebooks = [{"id": "replacement", "title": "Exam 3"}]
        with pytest.raises(api.ExtendLMError, match="destination"):
            service.destination(key, "cardio:3", validate=True)
        assert service.destination(key, "cardio:3") is None
        assert provider.put_count == 0


def test_validating_saved_destination_preserves_another_tabs_paginated_choices(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        provider.notebook_pages = {
            "": {"items": [{"id": "notebook-1", "title": "First exam"}], "next_cursor": "page2"},
            "page2": {"items": [{"id": "notebook-2", "title": "Second exam"}], "next_cursor": ""},
        }
        service.notebooks(key)
        service.notebooks(key, "page2")
        service.save_destination(key, "cardio:3", "notebook-1")
        assert service.destination(key, "cardio:3", validate=True)["notebook_id"] == "notebook-1"
        service.save_destination(key, "cardio:4", "notebook-2")
        assert service.destination(key, "cardio:4")["notebook_id"] == "notebook-2"


def test_expired_grant_and_new_authorization_cannot_restore_destination(tmp_path):
    with setup(tmp_path) as (service, _):
        _, key, _ = sign_in(service)
        select(service, key)
        service.save_destination(key, "cardio:3", "notebook-1")
        with service.state(key, write=True) as data:
            data["expires"] = 0
        assert service.destination(key, "cardio:3") is None
        _, new_key, _ = sign_in(service)
        select(service, new_key)
        assert service.destination(new_key, "cardio:3") is None


def test_expired_access_token_without_refresh_hides_saved_destination(tmp_path):
    with setup(tmp_path) as (service, _):
        _, key, _ = sign_in(service)
        select(service, key)
        service.save_destination(key, "cardio:3", "notebook-1")
        with service.state(key, write=True) as data:
            data["tokens"] = {"access_token": "expired", "expires_at": 0}
        assert service.destination(key, "cardio:3") is None


def test_destination_changes_affect_future_jobs_and_preserve_original_bytes(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        provider.notebooks.append({"id": "notebook-2", "title": "Exam 3 replacement"})
        select(service, key)
        original = service.save_destination(key, "cardio:3", "notebook-1")
        provider.lose_add_response = True
        job = service.enqueue(
            key,
            original["notebook_id"],
            [("lecture.txt", io.BytesIO(b"A lecture transcript."))],
            lecture_id=1,
            expected_selection=original["selection"],
            source_revisions=[9],
        )
        wait(service, key, job)
        service.save_destination(key, "cardio:3", "notebook-2")
        service.resume(key, job)
        wait(service, key, job)
        with service.state(key) as state:
            assert state["jobs"][job]["source_revisions"] == [9]
            assert (
                state["jobs"][job]["files"][0]["sha256"]
                == hashlib.sha256(b"A lecture transcript.").hexdigest()
            )
        assert provider.add_calls[-1]["notebook_id"] == "notebook-1"
        assert service.destination(key, "cardio:3")["notebook_id"] == "notebook-2"
        provider.connection = "different_browser_0001"
        select(service, key)
        with pytest.raises(api.ExtendLMError, match="Account changed"):
            service.enqueue(
                key,
                original["notebook_id"],
                [("lecture.txt", io.BytesIO(b"A lecture transcript."))],
                expected_selection=original["selection"],
            )


def test_lecture_receipts_are_isolated_and_pending_or_failed_indexing_never_resends(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        for lecture_id in (1, 2):
            job = service.enqueue(
                key,
                "notebook-1",
                [("lecture.txt", io.BytesIO(b"A lecture transcript."))],
                lecture_id=lecture_id,
            )
            wait(service, key, job)
        receipts = service.jobs(key, lecture_id=1)
        assert len(receipts) == 1
        assert receipts[0]["lecture_id"] == 1
        assert receipts[0]["account_label"] == "Browser 1 · Account 0"
        assert len(service.jobs(key, lecture_id=2)) == 1
        assert len(service.jobs(key)) == 2
        for status in ("processing", "failed"):
            provider.source_status = status
            service.check_sources(key, job)
            duplicate = service.enqueue(
                key,
                "notebook-1",
                [("lecture.txt", io.BytesIO(b"A lecture transcript."))],
                lecture_id=2,
            )
            assert duplicate == job
            assert provider.put_count == 2
        assert service.jobs(key, lecture_id=2)[0]["status"] == "indexing_failed"


def test_existing_receipts_from_before_lecture_scoped_keys_are_reused(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        job = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))], lecture_id=7
        )
        wait(service, key, job)
        digest = hashlib.sha256(b"A lecture transcript.").hexdigest()
        legacy_id = hashlib.sha256(
            json.dumps(
                [service.selection(key), "notebook-1", [("lecture.txt", digest)]], sort_keys=True
            ).encode()
        ).hexdigest()
        with service.state(key, write=True) as state:
            receipt = state["jobs"].pop(job)
            receipt["id"] = legacy_id
            state["jobs"][legacy_id] = receipt
        duplicate = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))], lecture_id=7
        )
        assert duplicate == legacy_id
        assert provider.put_count == 1


def test_partial_import_stays_partial_after_accepted_sources_are_ready(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        provider.complete = False
        job = service.enqueue(
            key, "notebook-1", [("lecture.txt", io.BytesIO(b"A lecture transcript."))]
        )
        wait(service, key, job)
        service.check_sources(key, job)
        assert service.jobs(key)[0]["status"] == "partial"
        service.resume(key, job)
        assert provider.put_count == 1


def test_changed_material_snapshot_is_not_sent_as_approved_revision(tmp_path):
    with setup(tmp_path) as (service, provider):
        _, key, _ = sign_in(service)
        select(service, key)
        with pytest.raises(api.ExtendLMError, match="changed while"):
            service.enqueue(
                key,
                "notebook-1",
                [("lecture.txt", io.BytesIO(b"Changed transcript."))],
                source_hashes=[hashlib.sha256(b"Approved transcript.").hexdigest()],
            )
        assert provider.put_count == 0
        assert list((tmp_path / key / "files").iterdir()) == []


def test_destination_routes_share_only_the_catalog_exam_and_filter_lecture_receipts(
    tmp_path, monkeypatch
):
    from oms_hub.repositories import LectureInput
    from oms_hub.web.extendlm_routes import COOKIE

    monkeypatch.setattr("oms_hub.app.KeyringSecretStore", Secrets)
    with setup(tmp_path / "bridge") as (service, _):
        app = create_app(
            Settings(
                _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'hub.db'}"
            )
        )
        app.state.extendlm = service
        ids = [
            app.state.catalog_repository.upsert_lecture(
                LectureInput(subject, exam, number, "Same lecture title", "", None)
            )
            for subject, exam, number in (
                ("Cardio", 3, 1),
                ("Cardio", 3, 2),
                ("Cardio", 4, 1),
                ("Renal", 3, 1),
            )
        ]
        client = TestClient(app)
        client.get("/settings/extendlm")
        csrf = client.cookies.get(app.state.csrf.cookie_name)
        login = client.post("/settings/extendlm/connect", data={"csrf_token": csrf})
        state = parse_qs(urlsplit(login.json()["authorization_url"]).query)["state"][0]
        client.get(
            "/settings/extendlm/callback", params={"code": "c", "state": state, "iss": api.ISSUER}
        )
        choices = client.post("/settings/extendlm/accounts", data={"csrf_token": csrf}).json()[
            "accounts"
        ]
        client.post(
            "/settings/extendlm/select", data={"csrf_token": csrf, "choice": choices[0]["id"]}
        )
        client.post("/settings/extendlm/notebooks", data={"csrf_token": csrf})
        path = f"/settings/extendlm/lectures/{ids[0]}/destination"
        assert (
            client.post(path, data={"notebook_id": "notebook-1", "csrf_token": "wrong"}).status_code
            == 403
        )
        saved = client.post(path, data={"notebook_id": "notebook-1", "csrf_token": csrf})
        assert saved.status_code == 200
        assert saved.json()["destination"]["notebook_id"] == "notebook-1"
        for lecture_id, expected in zip(ids, [True, True, False, False], strict=True):
            status = client.get(f"/settings/extendlm/status?lecture_id={lecture_id}").json()
            assert bool(status["destination"]) is expected
            assert status["account_label"] == "Browser 1 · Account 0"
        cookie = client.cookies.get(COOKIE)
        # The app's trusted private-owner identity binds the encrypted browser grant.
        assert cookie
        session_key = service.session_key(cookie, "local-owner")
        job = service.enqueue(
            session_key,
            "notebook-1",
            [("lecture.txt", io.BytesIO(b"A lecture transcript."))],
            lecture_id=ids[0],
        )
        wait(service, session_key, job)
        assert len(client.get(f"/settings/extendlm/status?lecture_id={ids[0]}").json()["jobs"]) == 1
        assert client.get(f"/settings/extendlm/status?lecture_id={ids[1]}").json()["jobs"] == []
        assert client.get("/settings/extendlm/status?lecture_id=999999").status_code == 404


def test_lecture_send_page_only_needs_exam_notebook_selection(tmp_path, monkeypatch):
    from selectolax.parser import HTMLParser

    from oms_hub.repositories import LectureInput

    monkeypatch.setattr("oms_hub.app.KeyringSecretStore", Secrets)
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'hub.db'}")
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 1, 1, "Cardiac physiology", "", None)
    )
    client = TestClient(app)
    page = client.get(f"/settings/extendlm?lecture_id={lecture_id}")
    assert page.status_code == 200
    document = HTMLParser(page.text)
    assert len(document.css("select")) == 1
    assert document.css_first("[data-notebook]") is not None
    assert document.css_first("[data-lecture-upload]") is not None
    assert document.css_first("[data-account]") is None
    assert document.css_first("[data-connect-form]") is None
    assert document.css_first("input[type=file]") is None
    assert document.css_first("[data-setup-needed] a").attributes["href"] == "/settings/extendlm"
    settings = HTMLParser(client.get("/settings/extendlm").text)
    assert settings.css_first("[data-account]") is not None
    assert settings.css_first("[data-connect-form]") is not None
