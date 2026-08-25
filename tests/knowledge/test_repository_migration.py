from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.knowledge.repository import KnowledgeRepository


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'migration.db'}")
    yield database
    database.close()


def test_initialize_is_additive_on_old_database_and_preserves_tables_and_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE legacy_records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO legacy_records (id, value) VALUES (7, 'keep-me')")

    database = Database(f"sqlite:///{path}")
    try:
        KnowledgeRepository(database).initialize()

        tables = set(inspect(database.engine).get_table_names())
        assert "legacy_records" in tables
        with database.engine.connect() as connection:
            assert connection.execute(
                text("SELECT id, value FROM legacy_records")
            ).one() == (7, "keep-me")
    finally:
        database.close()


def test_initialize_does_not_require_existing_legacy_schema(database: Database) -> None:
    repository = KnowledgeRepository(database)

    repository.initialize()

    assert inspect(database.engine).has_table("knowledge_sources")
