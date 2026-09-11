import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.db import Database
from oms_hub.question_bank.anki_links import TagRule
from oms_hub.question_bank.imports import preview_import
from oms_hub.question_bank.repository import BankRepository
from oms_hub.question_bank.routes import create_question_bank_router
from oms_hub.security.csrf import CsrfProtector
from oms_hub.study_generation.studio_repository import StudioRepository


def raw():
    return (Path(__file__).parent / "fixtures/normalized-v1.json").read_bytes()


@pytest.fixture
def setup(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'bank.db'}") as database:
        database.migrate()
        repo = BankRepository(database.session)
        app = FastAPI()
        app.state.csrf = CsrfProtector(b"fixture-only" * 4)
        app.state.fixture_owner = "test"
        token = app.state.csrf.issue()

        @app.middleware("http")
        async def owner(request, call_next):
            request.state.study_owner_id = app.state.fixture_owner
            request.state.csrf_token = token
            return await call_next(request)

        index = AnkiIndex(tmp_path / "synthetic-index")
        index.rebuild_companion(
            [
                NormalizedNote(
                    note_id=101,
                    model_name="Fixture",
                    text="Test",
                    extra="",
                    raw_fields={"Text": "{{c1::Test}}"},
                    tags=("fixture::00123",),
                    card_ids=(201, 202),
                    media=(),
                    token_signature="test",
                    content_sha256="a" * 64,
                )
            ],
            snapshot_id="fixture",
            fingerprint="b" * 64,
        )
        app.state.fixture_index = index
        app.include_router(
            create_question_bank_router(
                repo,
                studio=StudioRepository(database),
                anki_index=index,
                tag_rules=(TagRule("uworld", "step1", "fixture::"),),
            )
        )
        with TestClient(app) as client:
            client.cookies.set(app.state.csrf.cookie_name, token)
            client.headers[app.state.csrf.header_name] = token
            yield client, repo, app


def upload(client, action, value=None, **fields):
    return client.post(
        f"/question-bank/imports/{action}",
        files={"file": ("fixture.json", value or raw(), "application/json")},
        data=fields,
    )


def test_preview_confirm_replay_and_owner_receipt(setup):
    client, repo, app = setup
    assert client.get("/question-bank").status_code == 200
    response = upload(client, "preview")
    assert response.status_code == 200
    assert preview_import(raw()).digest in response.text
    assert repo.iter_attempts(learner_id="test") == ()
    response = upload(client, "confirm", digest=preview_import(raw()).digest)
    assert response.status_code == 200 and "Results imported" in response.text
    assert len(repo.iter_attempts(learner_id="test")) == 1
    assert upload(client, "confirm", digest=preview_import(raw()).digest).status_code == 200
    assert len(repo.iter_attempts(learner_id="test")) == 1
    receipt_url = response.url.path
    app.state.fixture_owner = "other"
    assert client.get(receipt_url).status_code == 404
    assert repo.iter_attempts(learner_id="other") == ()


def test_owner_csrf_tamper_and_upload_limits(setup):
    client, repo, app = setup
    assert upload(client, "confirm", digest="0" * 64).status_code == 409
    assert upload(client, "preview", b"unknown format").status_code == 422
    assert upload(client, "preview", b" " * (10 * 1024 * 1024 + 1)).status_code == 413
    client.headers.pop(app.state.csrf.header_name)
    assert upload(client, "preview").status_code == 403
    app.state.fixture_owner = None
    assert client.get("/question-bank").status_code == 401
    assert repo.iter_attempts(learner_id="test") == ()


def test_notes_are_escaped_and_question_holds_visible(setup):
    client, _, _ = setup
    data = json.loads(raw())
    data["rows"][0]["user_note"] = "<script>alert(1)</script>"
    data["rows"][0]["tags"] = ["<img src=x onerror=alert(1)>"]
    data["provenance"]["kind"] = "authorized_question_export"
    data["rows"][0]["question"] = {
        "stem": "Choose A",
        "choices": ["A", "B"],
        "correct_index": -1,
        "rationale": "",
    }
    value = json.dumps(data).encode()
    response = upload(client, "preview", value)
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text and "Held bodies" in response.text
    response = upload(client, "confirm", value, digest=preview_import(value).digest)
    assert "Review questions" in response.text
    assert "invalid_question" in response.text


def test_candidate_search_export_and_review_batch_owner_checks(setup):
    client, _, app = setup
    response = upload(client, "confirm", digest=preview_import(raw()).digest)
    path = response.url.path
    exported = client.get(path + "/anki-export")
    assert exported.status_code == 200 and exported.text == "nid:101\n"
    assert "201, 202" in response.text
    app.state.fixture_owner = "other"
    assert client.get(path + "/anki-export").status_code == 404
    assert (
        client.post(
            path + "/review",
            data={"subject": "Fixture", "exam_number": 1, "label": "Test", "row_numbers": [1]},
        ).status_code
        == 404
    )
    app.state.fixture_owner = "test"
    data = json.loads(raw())
    data["export_id"] = "body-import"
    data["rows"][0]["attempt_id"] = "body-attempt"
    data["provenance"]["kind"] = "authorized_question_export"
    data["rows"][0]["question"] = {
        "stem": "Choose A",
        "choices": ["A", "B"],
        "correct_index": 0,
        "rationale": "A is specified",
    }
    value = json.dumps(data).encode()
    path = upload(client, "confirm", value, digest=preview_import(value).digest).url.path
    fields = {"subject": "Fixture", "exam_number": 1, "label": "Test", "row_numbers": [1]}
    response = client.post(path + "/review", data=fields, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("/review")
    assert (
        client.post(path + "/review", data=fields, follow_redirects=False).headers["location"]
        == response.headers["location"]
    )
    assert client.post(path + "/review", data=fields | {"row_numbers": [999]}).status_code == 422


def test_conflicting_import_rolls_back_and_empty_export_is_not_all_notes(setup):
    client, repo, _ = setup
    upload(client, "confirm", digest=preview_import(raw()).digest)
    data = json.loads(raw())
    data["rows"][0]["result"] = "correct"
    value = json.dumps(data).encode()
    assert upload(client, "confirm", value, digest=preview_import(value).digest).status_code == 409
    assert len(repo.iter_attempts(learner_id="test")) == 1
    data["export_id"] = "unmatched"
    data["rows"][0]["question_id"] = "999"
    value = json.dumps(data).encode()
    response = upload(client, "confirm", value, digest=preview_import(value).digest)
    assert client.get(response.url.path + "/anki-export").status_code == 409


def test_valid_utf8_upload_can_confirm_and_reload_without_escaped_json_limit(setup):
    client, _, _ = setup
    data = json.loads(raw())
    data["rows"] = [{"question_id": str(i), "user_note": "é" * 20000} for i in range(90)]
    source = json.dumps(data, ensure_ascii=False).encode()
    preview = preview_import(source)
    assert len(source) < 10 * 1024 * 1024
    assert len(json.dumps(preview.envelope.model_dump(mode="json")).encode()) > 10 * 1024 * 1024
    response = client.post("/question-bank/imports/confirm", files={
        "file": ("normalized.json", source, "application/json")}, data={"digest": preview.digest},
        follow_redirects=False)
    assert response.status_code == 303
    assert client.get(response.headers["location"]).status_code == 200


def test_manual_candidates_are_read_only_bounded_and_owner_protected(setup, monkeypatch):
    client, repo, app = setup
    path = "/question-bank/anki-candidates"
    assert client.get(path).status_code == 200
    response = client.post(path, data={"query": "nid:101,999 OR nid:101"})
    assert response.status_code == 200
    assert "2 candidate notes" in response.text and "201, 202" in response.text
    assert "Note 999 · missing from snapshot" in response.text
    assert "user-pasted AMBOSS candidates · unverified" in response.text
    assert "Snapshot: fixture · unverified" in response.text
    assert repo.iter_attempts(learner_id="test") == ()

    def no_read(*args):
        pytest.fail("Invalid query must fail before reading the index")

    monkeypatch.setattr(app.state.fixture_index, "snapshot_id", no_read)
    for query in ("tag:AMBOSS", "nid:101 OR deck:*", "nid:0", "x" * 65537):
        assert client.post(path, data={"query": query}).status_code == 422
    client.headers.pop(app.state.csrf.header_name)
    assert client.post(path, data={"query": "nid:101"}).status_code == 403
    app.state.fixture_owner = None
    assert client.get(path).status_code == 401
    assert client.post(path, data={"query": "nid:101"}).status_code == 401


def test_manual_candidates_unavailable_and_snapshot_failure(setup, monkeypatch):
    client, _, app = setup
    path = "/question-bank/anki-candidates"
    monkeypatch.setattr(app.state.fixture_index, "snapshot_id", lambda: None)
    response = client.post(path, data={"query": "nid:101"})
    assert response.status_code == 503 and "Local note index unavailable" in response.text
    snapshots = iter(("before", "before", "after"))
    monkeypatch.setattr(app.state.fixture_index, "snapshot_id", lambda: next(snapshots))
    response = client.post(path, data={"query": "nid:101"})
    assert response.status_code == 422 and "Index snapshot changed" in response.text
