from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from oms_hub import cli
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.migrations import LATEST_SCHEMA_VERSION
from oms_hub.models import SchemaVersionModel
from oms_hub.public_boundary import classify_public_path
from oms_hub.runtime import WorkerSupervisor
from oms_hub.security.rate_limit import (
    PublicQuizRateLimiter,
    RatePolicy,
    public_client_identifier,
)


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        **overrides,
    )


class _Worker:
    def __init__(self) -> None:
        self.recoveries = 0

    def recover_interrupted_jobs(self) -> int:
        self.recoveries += 1
        return 0

    def run_once(self) -> bool:
        return False


def test_supervisor_owns_one_worker_of_each_kind_and_reports_metadata() -> None:
    names = ("ingestion_worker", "generation_worker", "studio_worker")
    workers = {name: _Worker() for name in names}
    supervisor = WorkerSupervisor(workers)

    supervisor.start()
    snapshot = supervisor.snapshot()
    ready, reason = supervisor.ready()
    supervisor.stop()

    assert ready is True and reason is None
    assert {item["name"] for item in snapshot.values()} == set(workers)
    assert all(item["thread_id"] is not None for item in snapshot.values())
    assert all(item["start_count"] == 1 for item in snapshot.values())
    assert all(worker.recoveries == 1 for worker in workers.values())


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Thread:
    def __init__(self, alive: bool = True, clock: _Clock | None = None) -> None:
        self.ident = 1
        self._alive = alive
        self._clock = clock
        self.join_timeouts: list[float] = []

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float) -> None:
        self.join_timeouts.append(timeout)
        if self._clock is not None:
            self._clock.advance(timeout)


def _supervisor_with_real_state(clock: _Clock) -> WorkerSupervisor:
    workers = {
        name: _Worker()
        for name in ("ingestion_worker", "generation_worker", "studio_worker")
    }
    supervisor = WorkerSupervisor(workers, heartbeat_timeout_seconds=10, clock=clock)
    for state in supervisor._workers.values():
        state.start_count = 1
        state.thread = _Thread()
        state.heartbeat_at = clock()
    return supervisor


@pytest.mark.parametrize(
    ("scenario", "expected_reason"),
    [
        ("not_started", "worker_not_started"),
        ("dead", "worker_dead"),
        ("duplicate", "worker_duplicate"),
        ("stale", "worker_stale"),
        ("error", "worker_error"),
    ],
)
def test_supervisor_classifies_unhealthy_real_worker_state(
    scenario: str,
    expected_reason: str,
) -> None:
    clock = _Clock()
    supervisor = _supervisor_with_real_state(clock)
    state = next(iter(supervisor._workers.values()))
    if scenario == "not_started":
        state.start_count = 0
    elif scenario == "dead":
        state.thread = _Thread(False)
    elif scenario == "duplicate":
        state.start_count = 2
    elif scenario == "stale":
        state.heartbeat_at = clock() - 11
    else:
        assert scenario == "error"
        state.last_error = "RuntimeError"

    ready, reason = supervisor.ready()

    assert ready is False
    assert reason == expected_reason


def test_readiness_endpoint_maps_real_supervisor_state_without_raw_errors(tmp_path) -> None:
    clock = _Clock()
    app = create_app(_settings(tmp_path))
    app.state.worker_supervisor = _supervisor_with_real_state(clock)
    first_state = next(iter(app.state.worker_supervisor._workers.values()))
    first_state.last_error = "RuntimeError"

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["reason"] == "worker_error"
    assert "traceback" not in response.text.casefold()


def test_stop_uses_one_total_deadline_across_worker_joins() -> None:
    clock = _Clock()
    supervisor = _supervisor_with_real_state(clock)
    threads = [_Thread(clock=clock) for _ in range(3)]
    for state, thread in zip(supervisor._workers.values(), threads, strict=True):
        state.thread = thread

    supervisor.stop(timeout_seconds=3)

    assert threads[0].join_timeouts == [3]
    assert threads[1].join_timeouts == []
    assert threads[2].join_timeouts == []


def test_readiness_rejects_database_failure_but_liveness_preserves_provenance(
    tmp_path, monkeypatch
) -> None:
    app = create_app(_settings(tmp_path, build_revision="revision-sentinel"))
    app.state.worker_supervisor = SimpleNamespace(
        ready=lambda: (True, None), snapshot=lambda: {}
    )

    def unavailable():
        raise RuntimeError("database credential sentinel")

    monkeypatch.setattr(app.state.database.engine, "connect", unavailable)
    client = TestClient(app)

    assert client.get("/health/live").json()["build_revision"] == "revision-sentinel"
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_unavailable"
    assert "credential" not in response.text


def test_readiness_rejects_outdated_schema_but_liveness_remains_available(tmp_path) -> None:
    app = create_app(_settings(tmp_path, build_revision="revision-sentinel"))
    app.state.worker_supervisor = SimpleNamespace(
        ready=lambda: (True, None), snapshot=lambda: {}
    )
    with app.state.database.session() as session:
        version = session.get(SchemaVersionModel, 1)
        assert version is not None
        version.version = LATEST_SCHEMA_VERSION - 1

    client = TestClient(app)
    readiness = client.get("/health/ready")
    legacy = client.get("/health")
    liveness = client.get("/health/live")

    assert readiness.status_code == 503
    assert legacy.status_code == 503
    assert readiness.json()["reason"] == legacy.json()["reason"] == "schema_outdated"
    assert liveness.status_code == 200
    assert liveness.json()["build_revision"] == "revision-sentinel"


def test_cli_serve_only_constructs_app_and_delegates_to_uvicorn(monkeypatch) -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            ingestion_worker=object(), generation_worker=object(), studio_worker=object()
        )
    )
    settings = SimpleNamespace(dashboard_host="127.0.0.1", dashboard_port=8787)
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "create_app", lambda actual: app)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda actual, host, port: called.update(app=actual, host=host, port=port),
    )

    assert cli.serve(SimpleNamespace()) == 0
    assert called == {"app": app, "host": "127.0.0.1", "port": 8787}


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("/public/quizzes", "general"),
        ("/public/practice-questions", "general"),
        ("/public/quizzes/token", "general"),
        ("/public/quizzes/token/content", "general"),
        ("/public/quizzes/token/answer", "general"),
        ("/public/quizzes/token/media/image-key", "general"),
        ("/public/quizzes/token/outline", "outline"),
        ("/public/quizzes/assets/player.js", "general"),
        ("/public/quizzes/assets/player.css", "general"),
        ("/public/quizzes/assets/library.js", "general"),
        ("/public/quizzes/assets/library.css", "general"),
        ("/public/quizzes/assets/tokens.css", "general"),
        ("/public/quizzes/assets/reset.css", "general"),
        ("/public/quizzes/assets/study-hub.css", "general"),
    ],
)
def test_public_classifier_covers_every_canonical_and_one_slash_surface(
    path: str,
    category: str,
) -> None:
    canonical = classify_public_path(path)
    trailing = classify_public_path(f"{path}/")

    assert canonical.is_public and canonical.is_canonical
    assert canonical.category == category
    assert trailing.is_public and not trailing.is_canonical
    assert trailing.category == category


@pytest.mark.parametrize(
    "path",
    [
        "/public/quizzes//",
        "/public/practice-questions//",
        "/public/quizzes/token//content",
        "/public/quizzes/token/unrecognized",
    ],
)
def test_public_classifier_rejects_repeated_slashes_and_unrecognized_paths(path: str) -> None:
    assert not classify_public_path(path).is_public


def test_public_trailing_slash_bypasses_access_and_docs_are_disabled_under_csp(tmp_path) -> None:
    app = create_app(_settings(tmp_path, public_hostname="study.example.com"))
    with TestClient(
        app,
        base_url="https://study.example.com",
        follow_redirects=False,
    ) as client:
        quiz_response = client.get("/public/quizzes/")
        practice_response = client.get("/public/practice-questions/")
    local = TestClient(app).get("/docs")

    assert quiz_response.status_code == 307
    assert practice_response.status_code == 307
    assert local.status_code == 404
    assert "script-src 'self'" in local.headers["content-security-policy"]


def test_limiter_rejects_before_public_repository_or_hashing_work(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    app.state.public_quiz_rate_limiter = PublicQuizRateLimiter(
        general_client=RatePolicy(0, 0),
        general_global=RatePolicy(0, 0),
        outline_client=RatePolicy(0, 0),
        outline_global=RatePolicy(0, 0),
    )
    app.state.generation_repository = object()

    response = TestClient(app).get("/public/quizzes/token/media/image-key")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"


def test_public_client_identity_rejects_forwarded_chains() -> None:
    assert public_client_identifier("203.0.113.9, 198.51.100.2", "127.0.0.1") == "127.0.0.1"


def test_application_logging_is_bounded_and_not_duplicated(tmp_path) -> None:
    first = create_app(_settings(tmp_path))
    second = create_app(_settings(tmp_path))
    handlers = [
        handler
        for handler in __import__("logging").getLogger("oms_hub").handlers
        if handler.get_name().startswith("oms-hub-")
    ]

    assert first.state.application_log_path == tmp_path / "logs" / "oms-study-hub.log"
    assert second.state.application_log_path.is_file()
    assert sorted(handler.get_name() for handler in handlers) == [
        "oms-hub-console",
        "oms-hub-file",
    ]
    file_handler = next(handler for handler in handlers if handler.get_name() == "oms-hub-file")
    assert file_handler.maxBytes > 0 and file_handler.backupCount > 0
