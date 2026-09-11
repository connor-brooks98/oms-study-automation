from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.codex_session import SessionError, SessionLifecycle
from oms_hub.repositories import CatalogRepository
from oms_hub.study_generation.domain import GenerationKind, NotebookAnswer, PromptSnapshot
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.service import GenerationService
from oms_hub.study_generation.worker import GenerationWorker
from tests.study_generation.test_gpt_lecture import gpt_review_run as gpt_review_run


def test_outline_uses_frozen_backend_model_and_pauses_without_google(gpt_review_run, tmp_path):
    studio, _, _, _, _ = gpt_review_run
    database = studio.database
    repository = GenerationRepository(database)
    catalog, ingestion = CatalogRepository(database), IngestionRepository(database)
    prompts = SimpleNamespace(inspect=lambda kind: PromptSnapshot(tmp_path/'prompt', 'text',
                                                                 'a'*64, 'now'))
    service = GenerationService(catalog, ingestion, repository, prompts, object(),
                                backend='codex_subscription', model='frozen')
    job = service.queue_outline(1)
    assert job.backend == 'codex_subscription' and job.codex_model == 'frozen'
    filed = []

    class Generator:
        limited = True

        def generate(self, active, prompt, pdf, transcript, *, on_lifecycle):
            assert active.codex_model == 'frozen'
            if self.limited:
                raise SessionError('rate_limited')
            on_lifecycle(SessionLifecycle(active.id, 'dispatching'))
            return NotebookAnswer('A locally tested outline')

    generator = Generator()
    worker = GenerationWorker(repository, catalog, ingestion, prompts, object(),
        SimpleNamespace(file=lambda *a, **k: filed.append(a)), object(), gpt_outline=generator)
    assert worker.run_once()
    assert repository.get(job.id).state.value == 'paused' and not filed
    assert not worker.run_once()
    service.model = 'changed'
    resumed = service.queue_outline(1)
    assert resumed.id == job.id and resumed.codex_model == 'frozen'
    generator.limited = False
    assert worker.run_once() and len(filed) == 1
    assert repository.get(job.id).state.value == 'complete'

    # Forward upgrade keeps historical jobs on their recorded/default backend.
    with database.engine.begin() as connection:
        connection.execute(text('ALTER TABLE generation_jobs DROP COLUMN backend'))
        connection.execute(text('ALTER TABLE generation_jobs DROP COLUMN codex_model'))
        connection.execute(text('UPDATE schema_version SET version=38'))
    database.migrate()
    assert repository.get(job.id).backend == 'notebooklm'
    next_job = repository.queue(1, GenerationKind.OUTLINE, backend='codex_subscription',
                                codex_model='frozen')
    repository.claim_next(datetime.now(UTC))
    repository.recover_interrupted()
    assert repository.get(next_job.id).state.value == 'paused'
    assert repository.claim_next(datetime.now(UTC)) is None
    from oms_hub.study_generation.repository import ImportedOutlineReplacementRequired
    from oms_hub.study_generation.service import GenerationPrerequisiteError

    def blocked_file(*args, **kwargs):
        raise ImportedOutlineReplacementRequired("durable replacement review required")

    worker.outline.file = blocked_file
    service.queue_outline(1)
    assert worker.run_once()
    stopped = repository.get(next_job.id)
    assert stopped.state.value == "failed" and stopped.stage.value == "pdf"
    with pytest.raises(GenerationPrerequisiteError, match="retained GPT outline replacement"):
        service.queue_outline(1)


@pytest.mark.parametrize("change_at", ["render", "commit"])
def test_outline_filing_rechecks_sources_and_preserves_prior_bytes(
    gpt_review_run, tmp_path, monkeypatch, change_at
):
    from oms_hub.config import Settings
    from oms_hub.domain import LectureKey
    from oms_hub.models import StudyRevisionModel
    from oms_hub.routing import build_outline_destination
    from oms_hub.study_generation.domain import GenerationStage
    from oms_hub.study_generation.outline import OutlinePdfRenderer, OutlineService

    studio, _, _, _, inputs = gpt_review_run
    repository = GenerationRepository(studio.database)
    job = repository.queue(1, GenerationKind.OUTLINE, backend="codex_subscription", codex_model="x")
    repository.advance(job.id, GenerationStage.PDF,
                       pdf_revision_id=inputs.slide_revision_id,
                       transcript_revision_id=inputs.transcript_revision_id)
    job = repository.claim_next(datetime.now(UTC))
    settings = Settings(_env_file=None, data_dir=tmp_path, study_root=tmp_path / "study")
    key = LectureKey("Heme", 3, 1, "Fixture")
    destination = build_outline_destination(settings, key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"retained prior outline")

    def promote():
        with studio.database.session() as session:
            session.get(StudyRevisionModel, inputs.transcript_revision_id).current = False

    class Renderer(OutlinePdfRenderer):
        def render(self, title, content):
            payload = super().render(title, content)
            if change_at == "render":
                promote()
            return payload

    original = repository.record_outline
    if change_at == "commit":
        def record(*args, **kwargs):
            promote()
            return original(*args, **kwargs)
        monkeypatch.setattr(repository, "record_outline", record)
    with pytest.raises(ValueError, match="source is no longer current"):
        OutlineService(settings, repository, Renderer()).file(job, key, NotebookAnswer("Outline"))
    assert destination.read_bytes() == b"retained prior outline"
    assert repository.current_outline(1) is None
