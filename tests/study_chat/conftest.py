import hashlib

import pytest

from oms_hub.anki.sources import LectureSourceExtractor
from oms_hub.db import Database
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import LectureModel, StudyRevisionModel, UploadBatchModel, UploadItemModel
from oms_hub.study_chat.repository import ChatRepository
from oms_hub.study_chat.sources import ChatSources


@pytest.fixture
def setup(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'chat.db'}") as database:
        database.migrate()
        source = tmp_path / "source.txt"
        source.write_text("Iron stores are low in iron deficiency.")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        with database.session() as session:
            session.add(
                LectureModel(id=1, subject="Heme", exam_number=3, lecture_number=1, topic="Iron")
            )
            session.add(UploadBatchModel(id="batch", kind="transcripts"))
            session.flush()
            session.add(
                UploadItemModel(
                    id="upload",
                    batch_id="batch",
                    kind="transcripts",
                    original_filename="source.txt",
                    staged_path=str(source),
                    sha256=sha,
                    size_bytes=source.stat().st_size,
                )
            )
            session.flush()
            session.add(
                StudyRevisionModel(
                    id=1,
                    upload_item_id="upload",
                    lecture_id=1,
                    kind="transcripts",
                    source_sha256=sha,
                    immutable_source_path=str(source),
                    derived_sha256=sha,
                    immutable_derived_path=str(source),
                    state="current",
                    current=True,
                )
            )

        def authorize(owner_id, revision_id):
            if owner_id != "owner" or revision_id != 1:
                raise PermissionError("source unavailable")

        sources = ChatSources(
            database.session,
            LectureSourceExtractor(IngestionRepository(database)),
            authorize_revision=authorize,
        )
        repo = ChatRepository(database.session, sources=sources)
        yield repo, database, source
