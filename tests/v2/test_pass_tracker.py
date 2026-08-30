from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import text

from oms_hub.app import create_app
from oms_hub.config import Settings
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

    assert missing_csrf.status_code == 403
    assert missing_post_csrf.status_code == 403
    assert oversized.status_code == 422


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

    assert missing_lecture.status_code == 404
    assert missing_pass.status_code == 404
    assert missing_lecture_post.status_code == 404
