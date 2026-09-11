from datetime import UTC, datetime
from types import SimpleNamespace

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
