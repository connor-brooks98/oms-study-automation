from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event

from fastapi.testclient import TestClient
from sqlalchemy import event, text

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import LectureModel
from oms_hub.repositories import LectureInput


def _app(tmp_path):
    return create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )


def _lecture(
    app,
    subject: str = "Neuro",
    exam: int = 1,
    number: int = 1,
    topic: str = "Brain",
) -> int:
    return app.state.catalog_repository.upsert_lecture(
        LectureInput(subject, exam, number, topic, "", None)
    )


def _client_with_lecture(tmp_path) -> tuple[TestClient, int]:
    app = _app(tmp_path)
    lecture_id = _lecture(app)
    client = TestClient(app)
    client.get(f"/lectures/{lecture_id}")
    return client, lecture_id


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("study_hub_csrf")
    assert token is not None
    return {"X-CSRF-Token": token}


def _stored_pass(client: TestClient, lecture_id: int, position: int):
    with client.app.state.database.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT position, completed_on, resource FROM lecture_passes "
                "WHERE lecture_id = :lecture_id AND position = :position"
            ),
            {"lecture_id": lecture_id, "position": position},
        ).one()


def _run_together(first, second):
    ready = Barrier(3)

    def run(call):
        ready.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(run, first)
        second_result = executor.submit(run, second)
        ready.wait(timeout=5)
        return first_result.result(), second_result.result()


def _synchronize_pass_reads(app):
    ready = Barrier(2)

    def synchronize(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM lecture_passes" in statement and statement.lstrip().startswith("SELECT"):
            ready.wait(timeout=5)

    event.listen(app.state.database.engine, "after_cursor_execute", synchronize)
    return synchronize


def test_new_lecture_seeds_exactly_five_passes(tmp_path) -> None:
    app = _app(tmp_path)
    lecture_id = _lecture(app)
    assert _lecture(app) == lecture_id

    with app.state.database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT position, completed_on, resource FROM lecture_passes "
                "WHERE lecture_id = :lecture_id ORDER BY position"
            ),
            {"lecture_id": lecture_id},
        ).all()

    assert rows == [(position, None, None) for position in range(1, 6)]


def test_tracker_import_seeds_new_and_existing_lectures(tmp_path) -> None:
    app = _app(tmp_path)
    with app.state.database.session() as session:
        session.add(
            LectureModel(
                subject="Neuro",
                exam_number=1,
                lecture_number=1,
                topic="Old topic",
                lecturer="",
            )
        )

    result = app.state.catalog_repository.commit_tracker_import(
        [
            LectureInput("Neuro", 1, 1, "Updated topic", "", None),
            LectureInput("Neuro", 1, 2, "New topic", "", None),
        ],
        [],
        "a" * 64,
        "tracker.xlsx",
    )

    with app.state.database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT lectures.lecture_number, lecture_passes.position "
                "FROM lectures JOIN lecture_passes ON lecture_passes.lecture_id = lectures.id "
                "ORDER BY lectures.lecture_number, lecture_passes.position"
            )
        ).all()
    assert result == (1, 1)
    assert rows == [
        (lecture_number, position)
        for lecture_number in (1, 2)
        for position in range(1, 6)
    ]


def test_pass_patch_records_local_date_preserves_it_for_resource_and_clears_on_uncheck(
    tmp_path,
) -> None:
    client, lecture_id = _client_with_lecture(tmp_path)
    url = f"/api/lectures/{lecture_id}/passes/1"
    headers = _csrf_headers(client)
    dates = {date.today().isoformat()}

    completed = client.patch(url, json={"completed": True}, headers=headers)
    dates.add(date.today().isoformat())

    assert completed.status_code == 200
    assert completed.json() == {
        "position": 1,
        "completed_on": completed.json()["completed_on"],
        "resource": None,
    }
    assert completed.json()["completed_on"] in dates
    completed_on = completed.json()["completed_on"]
    assert _stored_pass(client, lecture_id, 1) == (1, completed_on, None)

    resource = client.patch(url, json={"resource": "Lecture recording"}, headers=headers)

    assert resource.status_code == 200
    assert resource.json() == {
        "position": 1,
        "completed_on": completed_on,
        "resource": "Lecture recording",
    }
    assert _stored_pass(client, lecture_id, 1) == (
        1,
        completed_on,
        "Lecture recording",
    )

    reopened = client.patch(url, json={"completed": False}, headers=headers)

    assert reopened.status_code == 200
    assert reopened.json() == {
        "position": 1,
        "completed_on": None,
        "resource": "Lecture recording",
    }
    assert _stored_pass(client, lecture_id, 1) == (1, None, "Lecture recording")


def test_custom_resource_catalog_reuses_first_spelling_across_lectures(tmp_path) -> None:
    client, lecture_id = _client_with_lecture(tmp_path)
    _lecture(client.app, number=2, topic="Spine")
    headers = _csrf_headers(client)
    url = f"/api/lectures/{lecture_id}/passes/1"

    saved = client.patch(url, json={"resource": "Pathoma"}, headers=headers)
    changed = client.patch(url, json={"resource": "Anki"}, headers=headers)
    variant = client.patch(url, json={"resource": "pathoma"}, headers=headers)

    assert saved.json()["resource"] == "Pathoma"
    assert changed.json()["resource"] == "Anki"
    assert variant.json()["resource"] == "Pathoma"
    assert client.app.state.catalog_repository.list_pass_resources() == [
        "Lecture",
        "Anki",
        "Lecture outline",
        "Practice questions",
        "Pathoma",
    ]
    with client.app.state.database.engine.connect() as connection:
        resources = connection.execute(
            text("SELECT name FROM lecture_pass_resources ORDER BY id")
        ).scalars().all()
    assert resources.count("Pathoma") == 1
    assert "pathoma" not in resources


def test_pass_patch_requires_csrf_and_rejects_oversized_resource(tmp_path) -> None:
    client, lecture_id = _client_with_lecture(tmp_path)
    url = f"/api/lectures/{lecture_id}/passes/1"

    missing_csrf = client.patch(url, json={"completed": True})
    missing_post_csrf = client.post(f"/api/lectures/{lecture_id}/passes")
    oversized = client.patch(
        url,
        json={"resource": "x" * 101},
        headers=_csrf_headers(client),
    )
    empty = client.patch(url, json={}, headers=_csrf_headers(client))

    assert missing_csrf.status_code == 403
    assert missing_post_csrf.status_code == 403
    assert oversized.status_code == 422
    assert empty.status_code == 422


def test_pass_patch_rejects_other_without_replacing_the_saved_resource(tmp_path) -> None:
    client, lecture_id = _client_with_lecture(tmp_path)
    url = f"/api/lectures/{lecture_id}/passes/1"
    headers = _csrf_headers(client)

    saved = client.patch(url, json={"resource": "Pathoma"}, headers=headers)
    rejected = client.patch(url, json={"resource": " other "}, headers=headers)

    assert saved.status_code == 200
    assert rejected.status_code == 422
    assert _stored_pass(client, lecture_id, 1) == (1, None, "Pathoma")


def test_overlapping_completion_and_resource_updates_preserve_both(
    tmp_path,
    monkeypatch,
) -> None:
    app = _app(tmp_path)
    lecture_id = _lecture(app)
    clients = (TestClient(app), TestClient(app))
    for client in clients:
        client.get(f"/lectures/{lecture_id}")
    original_get_lecture = app.state.catalog_repository.get_lecture
    original_update_pass = app.state.catalog_repository.update_pass
    reads_ready = Barrier(2)
    completion_saved = Event()

    def synchronized_get_lecture(self, requested_id):
        lecture = original_get_lecture(requested_id)
        reads_ready.wait(timeout=5)
        return lecture

    monkeypatch.setattr(
        "oms_hub.repositories.CatalogRepository.get_lecture",
        synchronized_get_lecture,
    )

    def ordered_update_pass(self, *args, **kwargs):
        if kwargs["completed_on"] is not None:
            result = original_update_pass(*args, **kwargs)
            completion_saved.set()
            return result
        assert completion_saved.wait(timeout=5)
        return original_update_pass(*args, **kwargs)

    monkeypatch.setattr(
        "oms_hub.repositories.CatalogRepository.update_pass",
        ordered_update_pass,
    )

    completed, resource = _run_together(
        lambda: clients[0].patch(
            f"/api/lectures/{lecture_id}/passes/1",
            json={"completed": True},
            headers=_csrf_headers(clients[0]),
        ),
        lambda: clients[1].patch(
            f"/api/lectures/{lecture_id}/passes/1",
            json={"resource": "Anki"},
            headers=_csrf_headers(clients[1]),
        ),
    )

    assert completed.status_code == 200
    assert resource.status_code == 200
    stored = _stored_pass(clients[0], lecture_id, 1)
    assert stored[1] is not None
    assert stored[2] == "Anki"


def test_extra_pass_requires_all_current_passes_then_appends_position_six(tmp_path) -> None:
    client, lecture_id = _client_with_lecture(tmp_path)
    url = f"/api/lectures/{lecture_id}/passes"
    headers = _csrf_headers(client)

    blocked = client.post(url, headers=headers)
    for position in range(1, 6):
        response = client.patch(
            f"{url}/{position}",
            json={"completed": True},
            headers=headers,
        )
        assert response.status_code == 200
    created = client.post(url, headers=headers)

    assert blocked.status_code == 409
    assert created.status_code == 201
    assert created.json() == {
        "position": 6,
        "completed_on": None,
        "resource": None,
    }
    with client.app.state.database.engine.connect() as connection:
        positions = (
            connection.execute(
                text(
                    "SELECT position FROM lecture_passes "
                    "WHERE lecture_id = :lecture_id ORDER BY position"
                ),
                {"lecture_id": lecture_id},
            )
            .scalars()
            .all()
        )
    assert positions == list(range(1, 7))


def test_overlapping_add_pass_requests_return_created_and_conflict(tmp_path) -> None:
    app = _app(tmp_path)
    lecture_id = _lecture(app)
    with app.state.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE lecture_passes SET completed_on = '2026-08-30' "
                "WHERE lecture_id = :lecture_id"
            ),
            {"lecture_id": lecture_id},
        )
    clients = (TestClient(app), TestClient(app))
    for client in clients:
        client.get(f"/lectures/{lecture_id}")
    synchronize = _synchronize_pass_reads(app)

    try:
        responses = _run_together(
            lambda: clients[0].post(
                f"/api/lectures/{lecture_id}/passes",
                headers=_csrf_headers(clients[0]),
            ),
            lambda: clients[1].post(
                f"/api/lectures/{lecture_id}/passes",
                headers=_csrf_headers(clients[1]),
            ),
        )
    finally:
        event.remove(app.state.database.engine, "after_cursor_execute", synchronize)

    assert sorted(response.status_code for response in responses) == [201, 409]
    with app.state.database.engine.connect() as connection:
        positions = connection.execute(
            text(
                "SELECT position FROM lecture_passes "
                "WHERE lecture_id = :lecture_id ORDER BY position"
            ),
            {"lecture_id": lecture_id},
        ).scalars().all()
    assert positions == list(range(1, 7))


def test_exam_overview_isolates_exact_subject_and_exam_and_shows_counts(tmp_path) -> None:
    app = _app(tmp_path)
    first_id = _lecture(app, "Neuro", 1, 1, "Target one")
    second_id = _lecture(app, "Neuro", 1, 2, "Target two")
    _lecture(app, "Neuro", 2, 1, "Wrong exam")
    _lecture(app, "Neuroscience", 1, 1, "Wrong subject")
    with app.state.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE lecture_passes SET completed_on = '2026-08-30' "
                "WHERE lecture_id = :lecture_id AND position <= :count"
            ),
            [
                {"lecture_id": first_id, "count": 2},
                {"lecture_id": second_id, "count": 1},
            ],
        )

    page = TestClient(app).get(
        "/lectures/exams/1/passes",
        params={"subject": "Neuro"},
    )

    assert page.status_code == 200
    assert "Target one" in page.text
    assert "Target two" in page.text
    assert "Wrong exam" not in page.text
    assert "Wrong subject" not in page.text
    assert "<strong>2</strong> / 5" in page.text
    assert "<strong>1</strong> / 5" in page.text
    assert page.text.index("Target one") < page.text.index("Target two")


def test_missing_lecture_and_pass_return_404(tmp_path) -> None:
    client, lecture_id = _client_with_lecture(tmp_path)
    headers = _csrf_headers(client)

    missing_lecture = client.patch(
        "/api/lectures/999999/passes/1",
        json={"completed": True},
        headers=headers,
    )
    missing_pass = client.patch(
        f"/api/lectures/{lecture_id}/passes/99",
        json={"completed": True},
        headers=headers,
    )
    missing_lecture_post = client.post(
        "/api/lectures/999999/passes",
        headers=headers,
    )
    missing_exam = client.get(
        "/lectures/exams/99/passes",
        params={"subject": "Neuro"},
    )

    assert missing_lecture.status_code == 404
    assert missing_pass.status_code == 404
    assert missing_lecture_post.status_code == 404
    assert missing_exam.status_code == 404
