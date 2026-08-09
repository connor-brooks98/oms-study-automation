from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oms_hub.anki.domain import (
    AgentCommandType,
    CreateCurationJob,
    EnvelopeDraft,
)
from oms_hub.anki.models import AnkiEnvelopeModel
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import LectureModel
from oms_hub.security.access import AccessIdentity

PUBLIC_HOST = "study.example.com"
TAILNET_HOST = "study-hub.tailnet-name.ts.net"
AGENT_ID = "connor-mac"
TOKEN = "sentinel-agent-bearer-token"
_OPEN_APPS: list[tuple[TestClient, Any]] = []


@pytest.fixture(autouse=True)
def _close_apps() -> None:
    yield
    while _OPEN_APPS:
        client, app = _OPEN_APPS.pop()
        client.close()
        app.state.database.close()


class MemorySecretStore:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class AcceptingAccessVerifier:
    def verify(self, assertion: str) -> AccessIdentity:
        assert assertion == "valid-cloudflare-jwt"
        now = datetime.now(UTC)
        return AccessIdentity(
            email="connor@example.com",
            subject="connor",
            issued_at=now,
            expires_at=now,
        )


def _prepared_client(
    tmp_path: Path,
    *,
    max_request_bytes: int = 1024 * 1024,
) -> tuple[TestClient, Any]:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        public_hostname=PUBLIC_HOST,
        cloudflare_access_issuer="https://study.cloudflareaccess.com",
        cloudflare_access_audience="audience",
        cloudflare_access_allowed_email="connor@example.com",
        anki_agent_hostname=TAILNET_HOST,
        anki_agent_token_key="anki-agent-token",
        anki_agent_max_request_bytes=max_request_bytes,
    )
    app = create_app(settings)
    app.state.access_verifier = AcceptingAccessVerifier()
    app.state.secrets = MemorySecretStore({"anki-agent-token": TOKEN})
    prepared = (TestClient(app), app)
    _OPEN_APPS.append(prepared)
    return prepared


def _agent_headers(token: str = TOKEN, agent_id: str = AGENT_ID) -> dict[str, str]:
    return {
        "host": TAILNET_HOST,
        "authorization": f"Bearer {token}",
        "x-oms-agent-id": agent_id,
    }


def _heartbeat() -> dict[str, object]:
    return {
        "contract_version": 1,
        "agent_id": AGENT_ID,
        "agent_version": "0.1.0",
        "anki_version": "25.02",
        "ankiconnect_version": 6,
        "active_snapshot_id": None,
        "health": "ok",
        "observed_at": "2026-07-27T15:00:00Z",
    }


def test_dashboard_access_matrix_and_agent_host_isolation(tmp_path) -> None:
    client, _ = _prepared_client(tmp_path)

    assert client.get("/health/live").status_code == 200
    assert client.get("/health").status_code == 503
    assert client.get(
        "/anki",
        headers={
            "host": PUBLIC_HOST,
            "cf-access-jwt-assertion": "valid-cloudflare-jwt",
        },
    ).status_code == 200
    assert client.post(
        "/agent/v1/heartbeat",
        headers={"host": PUBLIC_HOST},
        json=_heartbeat(),
    ).status_code == 404
    assert client.get(
        "/anki",
        headers=_agent_headers(),
    ).status_code == 404
    assert client.get("/health", headers={"host": "unknown.example"}).status_code == 400


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token", "Basic sentinel-agent-bearer-token"],
)
def test_agent_requires_exact_bearer_authentication(
    tmp_path,
    authorization: str | None,
) -> None:
    client, _ = _prepared_client(tmp_path)
    headers = {"host": TAILNET_HOST, "x-oms-agent-id": AGENT_ID}
    if authorization is not None:
        headers["authorization"] = authorization

    response = client.post(
        "/agent/v1/heartbeat",
        headers=headers,
        json=_heartbeat(),
    )

    assert response.status_code == 401
    assert TOKEN not in response.text
    assert TOKEN not in repr(response)


def test_authenticated_agent_bypasses_browser_csrf_and_updates_heartbeat(
    tmp_path,
) -> None:
    client, app = _prepared_client(tmp_path)

    response = client.post(
        "/agent/v1/heartbeat",
        headers=_agent_headers(),
        json=_heartbeat(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert app.state.anki_repository.agent_state().agent_id == AGENT_ID


def test_authenticated_agent_health_endpoint_is_read_only(tmp_path) -> None:
    client, _ = _prepared_client(tmp_path)

    response = client.get("/agent/v1/health", headers=_agent_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_agent_claims_each_durable_command_once(tmp_path) -> None:
    client, app = _prepared_client(tmp_path)
    queued = app.state.anki_repository.queue_agent_command(
        AgentCommandType.FULL_SNAPSHOT,
        {"reason": "manual full reconciliation"},
    )

    first = client.get("/agent/v1/commands/next", headers=_agent_headers())
    second = client.get("/agent/v1/commands/next", headers=_agent_headers())

    assert first.status_code == 200
    assert first.json()["command_id"] == str(queued.id)
    assert first.json()["payload_sha256"] == queued.payload_sha256
    assert second.status_code == 204


def test_agent_upload_requires_command_ownership(tmp_path) -> None:
    client, app = _prepared_client(tmp_path)
    queued = app.state.anki_repository.queue_agent_command(
        AgentCommandType.FULL_SNAPSHOT,
        {"reason": "manual"},
    )
    claimed = client.get(
        "/agent/v1/commands/next",
        headers=_agent_headers(agent_id="other-mac"),
    )
    assert claimed.status_code == 200

    response = client.post(
        f"/agent/v1/commands/{queued.id}/snapshot",
        headers=_agent_headers(),
        json={},
    )

    assert response.status_code == 409


def test_agent_accepts_owned_snapshot_command_once(tmp_path) -> None:
    client, app = _prepared_client(tmp_path)
    queued = app.state.anki_repository.queue_agent_command(
        AgentCommandType.FULL_SNAPSHOT,
        {"reason": "manual"},
    )
    assert client.get("/agent/v1/commands/next", headers=_agent_headers()).status_code == 200
    manifest = {
        "contract_version": 1,
        "snapshot_id": "snapshot-20260727",
        "source_deck": "Anking Step Deck",
        "note_count": 0,
        "id_set_sha256": "a" * 64,
        "content_sha256": "b" * 64,
        "export_version": "1",
        "agent_version": "0.1.0",
        "ankiconnect_version": 6,
        "exported_at": "2026-07-27T15:00:00Z",
        "payload_sha256": "c" * 64,
    }
    payload = {
        "contract_version": 1,
        "manifest": manifest,
        "upserts": [],
        "deleted_note_ids": [],
        "payload_sha256": "d" * 64,
    }

    accepted = client.post(
        f"/agent/v1/commands/{queued.id}/snapshot",
        headers=_agent_headers(),
        json=payload,
    )
    replay = client.post(
        f"/agent/v1/commands/{queued.id}/snapshot",
        headers=_agent_headers(),
        json=payload,
    )

    assert accepted.status_code == 200
    assert replay.status_code == 409


def test_agent_accepts_owned_media_command_once(tmp_path) -> None:
    client, app = _prepared_client(tmp_path)
    queued = app.state.anki_repository.queue_agent_command(
        AgentCommandType.FETCH_MEDIA,
        {"filenames": ["anemia.png"]},
    )
    assert client.get("/agent/v1/commands/next", headers=_agent_headers()).status_code == 200
    payload = {
        "contract_version": 1,
        "command_id": str(queued.id),
        "filename": "anemia.png",
        "mime_type": "image/png",
        "content_base64": "aGVsbG8=",
        "byte_count": 5,
        "sha256": "a" * 64,
    }

    accepted = client.post(
        f"/agent/v1/commands/{queued.id}/media",
        headers=_agent_headers(),
        json=payload,
    )
    replay = client.post(
        f"/agent/v1/commands/{queued.id}/media",
        headers=_agent_headers(),
        json=payload,
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"status": "accepted"}
    assert replay.status_code == 409


def test_agent_accepts_owned_envelope_receipt_and_updates_envelope(tmp_path) -> None:
    client, app = _prepared_client(tmp_path)
    with app.state.database.session() as session:
        lecture = LectureModel(
            subject="Heme Lymph",
            exam_number=1,
            lecture_number=4,
            topic="Anemia I",
            lecturer="Professor",
        )
        session.add(lecture)
        session.flush()
        lecture_id = lecture.id
    job = app.state.anki_repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id=None,
            source_revision_ids=(),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=(),
            instruction_text="",
            target_deck="OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I",
            target_tag=(
                "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
            ),
            index_snapshot_id="snapshot-1",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet-5",
        )
    )
    envelope = app.state.anki_repository.create_envelope(
        job.id,
        EnvelopeDraft(
            envelope_id="0a0de74a-a60b-41e3-808e-e89974b0f615",
            snapshot_id="snapshot-1",
            payload={"target_tag": "lecture-tag"},
            operations=(),
        ),
    )
    command = app.state.anki_repository.queue_agent_command(
        AgentCommandType.APPLY_ENVELOPE,
        {"envelope_id": str(envelope.id)},
    )
    assert client.get("/agent/v1/commands/next", headers=_agent_headers()).status_code == 200
    receipt = {
        "contract_version": 1,
        "envelope_id": str(envelope.id),
        "agent_id": AGENT_ID,
        "operations": [],
        "sync_status": "complete",
        "verified": True,
        "created_note_ids": [],
        "media_filenames": [],
        "safe_error": None,
        "payload_sha256": "b" * 64,
    }

    response = client.post(
        f"/agent/v1/commands/{command.id}/receipt",
        headers=_agent_headers(),
        json=receipt,
    )

    assert response.status_code == 200
    with app.state.database.session() as session:
        stored = session.get(AnkiEnvelopeModel, str(envelope.id))
        assert stored is not None
        assert stored.state == "complete"


def test_agent_request_size_is_rejected_before_body_parsing(tmp_path) -> None:
    client, _ = _prepared_client(tmp_path, max_request_bytes=512)

    response = client.post(
        "/agent/v1/heartbeat",
        headers={
            **_agent_headers(),
            "content-type": "application/json",
        },
        content=b"x" * 513,
    )

    assert response.status_code == 413


def test_agent_secret_is_absent_from_errors_and_logs(tmp_path, caplog) -> None:
    client, _ = _prepared_client(tmp_path)

    with caplog.at_level("INFO"):
        response = client.post(
            "/agent/v1/heartbeat",
            headers=_agent_headers(token=TOKEN + "-wrong"),
            json=_heartbeat(),
        )

    assert response.status_code == 401
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text
