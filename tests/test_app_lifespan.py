import threading

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


class RecordingWorker:
    def __init__(self):
        self.recovered = 0
        self.ran = threading.Event()

    def recover_interrupted_jobs(self):
        self.recovered += 1
        return 0

    def run_once(self):
        self.ran.set()
        return False


def test_application_lifespan_recovers_starts_and_stops_workers(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    ingestion = RecordingWorker()
    generation = RecordingWorker()
    app.state.ingestion_worker = ingestion
    app.state.generation_worker = generation

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert ingestion.ran.wait(1)
        assert generation.ran.wait(1)
        assert ingestion.recovered == 1
        assert generation.recovered == 1

    assert all(not thread.is_alive() for thread in app.state.worker_threads)
