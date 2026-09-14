"""Queue newly filed lecture artifacts locally; the Studio worker owns uploads."""

from oms_hub.ingestion.domain import StudyRevision
from oms_hub.study_generation.notebook_connection import NotebookConnectionService
from oms_hub.study_generation.studio_repository import StudioRepository


class LectureNotebookSync:
    def __init__(self, repository: StudioRepository, connection: NotebookConnectionService):
        self.repository = repository
        self.connection = connection

    def __call__(self, result: object) -> None:
        if not isinstance(result, StudyRevision) or not result.current or result.state != "current":
            return
        # status() reads saved local state; it does not authenticate or contact Google.
        try:
            connected = self.connection.status().state == "connected"
        except Exception:  # noqa: BLE001 - an optional integration stays separate from ingestion
            connected = False
        self.repository.enqueue_lecture_upload(result.id, connected=connected)
