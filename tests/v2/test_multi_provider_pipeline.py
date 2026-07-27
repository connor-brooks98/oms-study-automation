import hashlib

import pytest
from sqlalchemy import select

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import CleanResult, ProviderName
from oms_hub.models import StudyUsageModel
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.transcripts.pipeline import TranscriptPipeline
from oms_hub.transcripts.prompt import ApprovedPrompt


class FixedPrompt:
    def current(self):
        return ApprovedPrompt("Remove filler.", "a" * 64)


class FixedCleaner:
    def __init__(self, provider):
        self.provider = provider

    def clean(self, raw_text, prompt):
        return CleanResult(
            text=raw_text,
            provider=self.provider,
            model=f"{self.provider.value}-model",
            request_id=f"{self.provider.value}-request",
            input_tokens=10,
            output_tokens=10,
            cost_microusd=0,
        )


@pytest.mark.parametrize("provider", list(ProviderName))
def test_pipeline_records_the_provider_used_for_cleaning(tmp_path, provider):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Introduction", "Dr Test", None)
    )
    raw = b"A complete medical lecture transcript."
    staged_path = tmp_path / f"{provider.value}.txt"
    staged_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    repository = IngestionRepository(database)
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    item_id = f"{provider.value}-item"
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id=batch_id,
            item_id=item_id,
            path=staged_path,
            sha256=digest,
            size_bytes=len(raw),
            original_filename=staged_path.name,
        ),
    )
    repository.set_manual_assignment(item_id, lecture_id)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
        transcript_min_clean_ratio=0.1,
        transcript_max_clean_ratio=2.0,
    )
    pipeline = TranscriptPipeline(
        database,
        settings,
        FixedPrompt(),
        FixedCleaner(provider),
    )

    revision = pipeline.process(item_id)

    with database.session() as session:
        usage = session.scalar(
            select(StudyUsageModel).where(
                StudyUsageModel.revision_id == revision.id
            )
        )
    assert usage is not None
    assert usage.provider == provider.value
    assert usage.model == f"{provider.value}-model"
