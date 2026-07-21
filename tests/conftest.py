import pytest

from oms_hub.db import Database


@pytest.fixture
def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_schema()
    return db

