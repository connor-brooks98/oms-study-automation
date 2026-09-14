import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from oms_hub.db import Database
from oms_hub.security.csrf import CsrfProtector
from oms_hub.study_generation.quiz_presets import PresetConflict, QuizPresetRepository
from oms_hub.web.quiz_preset_routes import router


@pytest.fixture
def database(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'presets.db'}") as database:
        database.migrate()
        yield database


def test_crud_restart_names_and_owner_isolation(database):
    repo = QuizPresetRepository(database)
    first = repo.save("teacher-a", "  Dr. Smith  ", "Use clinical vignettes.")
    assert repo.save("teacher-a", "dr. SMITH", "Five choices").id == first.id
    other = repo.save("teacher-b", "Dr. Smith", "Other private instructions")
    assert other.id != first.id
    for owner in ("teacher-b", "Teacher-a"):
        with pytest.raises(KeyError):
            repo.save(owner, "stolen", "changed", preset_id=first.id)
        with pytest.raises(KeyError):
            repo.delete(owner, first.id)
    with Database(str(database.engine.url)) as reopened:
        reopened.migrate()
        persisted = QuizPresetRepository(reopened)
        assert [row.instructions for row in persisted.list("teacher-a")] == ["Five choices"]
        renamed = persisted.save("teacher-a", "Renamed", "Updated", preset_id=first.id)
        assert renamed.id == first.id and renamed.name == "Renamed"
        persisted.delete("teacher-a", first.id)
        assert not persisted.list("teacher-a")
        assert persisted.list("teacher-b") == (other,)


def test_bounds_cap_and_rename_conflict(database):
    repo = QuizPresetRepository(database)
    for index in range(30):
        repo.save("owner", f"Preset {index}", "x" * 4000)
    with pytest.raises(PresetConflict):
        repo.save("owner", "31st", "text")
    first = repo.save("owner", "Preset 0", "Updated while at cap")
    with pytest.raises(PresetConflict):
        repo.save("owner", "Preset 1", "Cannot overwrite another ID", preset_id=first.id)
    assert len(repo.list("owner")) == 30
    assert repo.save("another", "31st", "text")
    for name, instructions in [
        (" ", "text"),
        ("x" * 81, "text"),
        ("name", " "),
        ("name", "x" * 4001),
        ("name", "a\x00b"),
    ]:
        with pytest.raises(ValueError):
            repo.save("owner", name, instructions)


def test_upgrade_40_is_additive_idempotent_and_current_integrity_checked(database):
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE retained_fixture (value TEXT)"))
        connection.execute(text("INSERT INTO retained_fixture VALUES ('keep me')"))
        connection.execute(text("DROP TABLE quiz_instruction_presets"))
        connection.execute(text("UPDATE schema_version SET version=40 WHERE id=1"))
    database.migrate()
    preset = QuizPresetRepository(database).save("owner", "Name", "Instructions")
    database.migrate()
    assert QuizPresetRepository(database).list("owner") == (preset,)
    with database.engine.begin() as connection:
        assert connection.execute(text("SELECT version FROM schema_version")).scalar_one() == 41
        assert (
            connection.execute(text("SELECT value FROM retained_fixture")).scalar_one() == "keep me"
        )
        connection.execute(text("DROP TABLE quiz_instruction_presets"))
    with pytest.raises(RuntimeError, match="schema v41"):
        database.migrate()


def client_for(database):
    app = FastAPI()
    app.state.owner = "owner"
    app.state.quiz_presets = QuizPresetRepository(database)
    app.state.csrf = CsrfProtector(b"x" * 32)

    @app.middleware("http")
    async def owner(request: Request, call_next):
        if app.state.owner:
            request.state.study_owner_id = app.state.owner
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app)
    token = app.state.csrf.issue()
    client.cookies.set("study_hub_csrf", token)
    client.headers["X-CSRF-Token"] = token
    return client, app


def test_api_private_crud_auth_csrf_and_validation(database):
    client, app = client_for(database)
    path = "/study/quiz-presets"
    values = {"name": "Teacher", "instructions": "Vignettes only"}
    with client:
        app.state.owner = None
        response = client.get(path, headers={"X-Owner": "owner"})
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"
        app.state.owner = "owner"
        token = client.headers.pop("X-CSRF-Token")
        assert client.post(path, json=values).status_code == 403
        client.headers["X-CSRF-Token"] = token
        for invalid in [values | {"owner_id": "other"}, values | {"instructions": "x" * 4001}]:
            response = client.post(path, json=invalid)
            assert response.status_code == 422
            assert response.headers["cache-control"] == "private, no-store"
        saved = client.post(path, json=values)
        assert saved.status_code == 200
        assert "owner_id" not in saved.json()
        item = f"{path}/{saved.json()['id']}"
        assert client.get(path).json()["presets"] == [saved.json()]
        assert client.put(item, json=values | {"name": "Renamed"}).json()["name"] == "Renamed"
        app.state.owner = "other"
        assert client.get(path).json() == {"presets": []}
        assert client.put(item, json=values).status_code == 404
        assert client.delete(item).status_code == 404
        app.state.owner = "owner"
        token = client.headers.pop("X-CSRF-Token")
        assert client.put(item, json=values).status_code == 403
        assert client.delete(item).status_code == 403
        client.headers["X-CSRF-Token"] = token
        assert client.delete(item).json() == {"deleted": True}
        assert client.get(path).json() == {"presets": []}
