from datetime import UTC, datetime
from types import SimpleNamespace

from oms_hub.ingestion.domain import IngestionJob, UploadKind
from oms_hub.ingestion.worker import IngestionWorker


def test_ingestion_uses_recorded_backend_without_paid_fallback():
    calls = []
    job = IngestionJob(1, 'item', UploadKind.TRANSCRIPTS, 'process', 1, datetime.now(UTC),
        backend='codex_subscription')
    repository = SimpleNamespace(claim_next_job=lambda now: job)
    legacy = SimpleNamespace(process=lambda item: calls.append('legacy'))
    gpt = SimpleNamespace(process=lambda item: calls.append('gpt'))
    worker = IngestionWorker(repository, legacy, legacy, gpt_transcript_pipeline=gpt)
    assert worker.run_once()
    assert calls == ['gpt']


def test_gpt_lecture_backend_keeps_transcript_task_routing(tmp_path):
    from oms_hub.app import create_app
    from oms_hub.config import Settings
    from oms_hub.llm.domain import CleanResult, GeneratedText, LLMTask, ProviderName
    from oms_hub.transcripts.prompt import ApprovedPrompt

    app = create_app(Settings(
        _env_file=None, data_dir=tmp_path, study_root=tmp_path / "lectures",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", anki_enabled=False,
        study_backend="codex_subscription",
    ))
    assert app.state.ingestion_repository.transcript_backend == "legacy_api"
    assert app.state.generation_service.backend == "codex_subscription"
    cleaner = app.state.ingestion_worker.transcript_pipeline.cleaner
    assert cleaner is app.state.llm_service
    app.state.llm_settings.set_assignment(
        LLMTask.TRANSCRIPTS, ProviderName.GEMINI, "chosen-cleaner-model"
    )
    calls = []
    cleaner.secrets = SimpleNamespace(get=lambda key: "fixture-key")

    def clean(text, prompt, *, api_key, model):
        calls.append((api_key, model))
        return CleanResult(text, ProviderName.GEMINI, model, "fixture", 0, 0, 0)

    cleaner.providers[ProviderName.GEMINI] = SimpleNamespace(clean=clean)
    result = cleaner.clean("fixture transcript", ApprovedPrompt("Clean", "a" * 64))
    assert result.model == "chosen-cleaner-model"
    assert calls == [("fixture-key", "chosen-cleaner-model")]

    # Imported question extraction has its own assignment under the same GPT app.
    extractor = app.state.quiz_import_worker.extractor
    assert extractor.generator is cleaner
    app.state.llm_settings.set_assignment(
        LLMTask.QUIZ_EXTRACTION, ProviderName.ANTHROPIC, "chosen-import-model"
    )

    def generate(instruction, text, *, api_key, model, output_schema):
        calls.append((api_key, model))
        return GeneratedText(
            '{"questions":[],"answers":[]}', ProviderName.ANTHROPIC, model,
            "fixture-extraction", 0, 0, 0,
        )

    cleaner.providers[ProviderName.ANTHROPIC] = SimpleNamespace(generate_text=generate)
    _, metadata = extractor._extract_chunk("fixture import", ())
    assert metadata[0].provider is ProviderName.ANTHROPIC
    assert metadata[0].model == "chosen-import-model"
    assert calls[-1] == ("fixture-key", "chosen-import-model")
