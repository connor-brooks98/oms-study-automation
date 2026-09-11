import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from oms_hub.document_processing.pdf_adapter import PdfProcessor
from oms_hub.llm.codex_session import SessionError
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
    GenerationState,
    PromptSnapshot,
    RevisionSource,
    SourceIsolationError,
    SourceKind,
)
from oms_hub.study_generation.gpt_outline import GptOutlineGenerator
from tests.study_generation.test_gpt_lecture import _isolated_native_check
from tests.v2.test_codex_transcript_cleaner import SessionFixture


def inputs(tmp_path):
    pdf_path = tmp_path / "immutable.pdf"
    transcript_path = tmp_path / "immutable.txt"
    if not pdf_path.exists():
        pdf_path.write_bytes(b"synthetic PDF fixture")
    transcript_path.write_text("Complete transcript evidence.")
    prompt = PromptSnapshot(tmp_path / "prompt.md", "Create an outline.",
                            hashlib.sha256(b"Create an outline.").hexdigest(), "today")
    pdf = RevisionSource(1, 2, pdf_path, hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                         SourceKind.LECTURE_PDF)
    transcript = RevisionSource(1, 3, transcript_path,
                                hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
                                SourceKind.CLEANED_TRANSCRIPT)
    job = GenerationJob("outline-1", 1, GenerationKind.OUTLINE, GenerationState.RUNNING,
                        GenerationStage.VALIDATE, 1, prompt_sha256=prompt.sha256,
                        pdf_revision_id=2, transcript_revision_id=3,
                        backend="codex_subscription", codex_model="fixture")
    return job, prompt, pdf, transcript


@pytest.fixture
def parsed_pdf(monkeypatch):
    parsed = SimpleNamespace(warnings=(), segments=[SimpleNamespace(
        text="Complete PDF evidence.", locator=SimpleNamespace(label="page 1"),
    )])
    monkeypatch.setattr(PdfProcessor, "parse", lambda *args: parsed)
    return parsed


def test_outline_cache_binds_model_prompt_and_immutable_sources(tmp_path, parsed_pdf):
    args = inputs(tmp_path)
    client = SessionFixture(output='{"text":"# Outline\\nComplete facts."}')
    generator = GptOutlineGenerator(client, tmp_path / "work")
    events = []
    first = generator.generate(*args, on_lifecycle=events.append)
    assert first.text == "# Outline\nComplete facts."
    assert generator.generate(*args, on_lifecycle=events.append) == first
    assert client.calls == 1
    assert [event.phase for event in events] == [
        "dispatching", "thread_created", "turn_started", "completed",
    ]
    job, prompt, pdf, transcript = args
    with pytest.raises(SessionError, match="interrupted"):
        generator.generate(replace(job, codex_model="different"), prompt, pdf, transcript,
                           on_lifecycle=events.append)
    transcript.path.write_text("Changed source")
    with pytest.raises(SourceIsolationError, match="hash changed"):
        generator.generate(*args, on_lifecycle=events.append)
    assert client.calls == 1
    receipt = json.loads((tmp_path / "work/outline-1/outline-attempt.json").read_text())
    assert receipt["identity"]["sources"][0]["path"] == str(pdf.path.resolve())


@pytest.mark.parametrize("failure", ["preflight", "interrupted"])
def test_outline_preflight_retry_and_ambiguous_no_retry(tmp_path, parsed_pdf, failure):
    args = inputs(tmp_path)
    client = SessionFixture(failure=failure)
    generator = GptOutlineGenerator(client, tmp_path / "work")
    with pytest.raises(SessionError):
        generator.generate(*args, on_lifecycle=lambda event: None)
    client.failure = None
    if failure == "preflight":
        assert generator.generate(*args, on_lifecycle=lambda event: None).text
        assert client.calls == 2
    else:
        with pytest.raises(SessionError, match="interrupted"):
            generator.generate(*args, on_lifecycle=lambda event: None)
        assert client.calls == 1


def test_outline_refuses_incomplete_or_oversized_sources(tmp_path, parsed_pdf):
    args = inputs(tmp_path)
    client = SessionFixture()
    generator = GptOutlineGenerator(client, tmp_path / "work")
    parsed_pdf.warnings = ("BLOCKER: OCR missing",)
    with pytest.raises(SourceIsolationError, match="incomplete"):
        generator.generate(*args, on_lifecycle=lambda event: None)
    parsed_pdf.warnings = ()
    parsed_pdf.segments[0].text = "x" * 100_001
    with pytest.raises(SessionError, match="input limit"):
        generator.generate(*args, on_lifecycle=lambda event: None)
    assert client.calls == 0


def test_source_change_during_dispatch_retains_ambiguous_claim(tmp_path, parsed_pdf):
    args = inputs(tmp_path)
    client = SessionFixture()
    generator = GptOutlineGenerator(client, tmp_path / "work")

    def change_source(event):
        if event.phase == "dispatching":
            args[3].path.write_text("Changed during dispatch")

    with pytest.raises(SourceIsolationError, match="hash changed"):
        generator.generate(*args, on_lifecycle=change_source)
    args[3].path.write_text("Complete transcript evidence.")
    with pytest.raises(SessionError, match="interrupted"):
        generator.generate(*args, on_lifecycle=lambda event: None)
    assert client.calls == 1


@pytest.mark.parametrize("text", ["   ", "x" * 100_001])
def test_outline_rejects_empty_or_oversized_output_including_cache(tmp_path, parsed_pdf, text):
    args = inputs(tmp_path)
    client = SessionFixture(output=json.dumps({"text": text}))
    generator = GptOutlineGenerator(client, tmp_path / "work")
    for _ in range(2):
        with pytest.raises(SessionError, match="invalid output"):
            generator.generate(*args, on_lifecycle=lambda event: None)
    assert client.calls == 1


def test_real_pdf_outline_extracts_full_text_locally(tmp_path, request):
    if _isolated_native_check(request):
        return
    import fitz

    with fitz.open() as document:
        page = document.new_page()
        page.insert_text(
            (50, 50), "Synthetic lecture evidence explains alpha beta gamma delta.\n" * 8,
        )
        document.save(tmp_path / "immutable.pdf")
    args = inputs(tmp_path)

    class InspectSession(SessionFixture):
        def generate(self, request, **kwargs):
            assert "[page 1]" in request.source_text
            assert request.source_text.count("Synthetic lecture evidence") == 8
            assert "Complete transcript evidence." in request.source_text
            assert "Do not use tools" in request.instructions
            return super().generate(request, **kwargs)

    answer = GptOutlineGenerator(InspectSession(), tmp_path / "work").generate(
        *args, on_lifecycle=lambda event: None,
    )
    assert answer.text == "Preserved facts."
