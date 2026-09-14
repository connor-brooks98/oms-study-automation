import json
import re
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from oms_hub.db import Database
from oms_hub.models import LectureModel
from oms_hub.web.quarantine_routes import router


def test_quarantine_shares_one_catalog_and_keeps_single_lecture_assignment(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'picker.db'}") as database:
        database.migrate()
        with database.session() as session:
            session.add(
                LectureModel(
                    id=42,
                    subject="Neuro",
                    exam_number=2,
                    lecture_number=7,
                    topic="Reflexes </script>",
                )
            )
        app = FastAPI()
        app.state.database = database
        items = [
            SimpleNamespace(
                id=str(index),
                kind=SimpleNamespace(value="slides"),
                original_filename=f"deck{index}.pdf",
                size_bytes=12,
                evidence=[],
            )
            for index in range(2)
        ]
        app.state.ingestion_repository = SimpleNamespace(list_quarantined=lambda: items)
        assignments = []
        app.state.ingestion_service = SimpleNamespace(
            assign=lambda item, lecture: assignments.append((item, lecture))
        )
        app.include_router(router)
        with TestClient(app) as client:
            page = client.get("/quarantine")
            assert page.status_code == 200
            assert page.text.count("data-lecture-picker") == 2
            assert page.text.count('<script id="quarantine-lecture-catalog"') == 1
            assert page.text.count("Reflexes") == 1
            assert "Reflexes </script>" not in page.text
            catalog = json.loads(
                re.search(
                    r'<script id="quarantine-lecture-catalog" '
                    r'type="application/json">(.*?)</script>',
                    page.text,
                    re.S,
                ).group(1)
            )
            assert catalog[0]["id"] == 42 and catalog[0]["course"] == "Neuro"
            assert catalog[0]["exam"] == 2
            result = client.post(
                "/quarantine/0/assign", data={"lecture_id": "42"}, follow_redirects=False
            )
            assert result.status_code == 303
            assert assignments == [("0", 42)]
