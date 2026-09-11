# GPT Backend and Lecture Image Quizzes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run source-grounded lecture generation through a locally supervised Codex app-server with ChatGPT subscription login, preserving Hub review, native quizzes, and publication.

**Architecture:** A small Python stdio client owns Codex transport and managed login; application code supplies immutable source text/images and consumes validated output. A lecture generator writes durable artifacts into existing Studio review machinery; it never publishes directly. Orchestrator O owns shared schema and application wiring, while worker B owns this backend slice.

**Tech Stack:** Existing Python, subprocess, JSON, Pydantic, SQLAlchemy, pytest, document processors, image sanitization, and native quiz contracts; pinned Codex executable; no API SDK or Node bridge.

**Spec:** `docs/superpowers/specs/2026-09-11-gpt-study-platform-design.md`

## Global Constraints

- Execute under `docs/superpowers/plans/2026-09-11-gpt-study-platform.md`; do not treat this subplan as deployment authorization.
- GPT subscription-login-first; managed ChatGPT login only. No API-key fallback, cookie scraping, private endpoints, token copying, or externally managed auth tokens.
- Windows NUC hosts the local worker. Prove support for the installed executable and service identity before relying on it.
- Gemini and NotebookLM are no longer prerequisites for the new workflow; existing jobs/artifacts remain readable and explicitly routed by their recorded backend.
- Required lecture inputs: slides, current cleaned transcript, and explicit learning objectives. Outlines are optional; Anki is deferred.
- Cover every requested objective, with verifiable source citations and actual slide-image provenance; never silently truncate inputs or claim unsupported coverage.
- No provider or NUC live calls during this planning task. Subsequent login, smoke generation, restart, release, and production-source actions require their applicable current authorization.
- Preserve all unrelated edits. B submits requested changes to shared `app.py`, `config.py`, `models.py`, migrations, routes, and worker wiring to O rather than editing them concurrently.

---

## Current interfaces and ownership

Read these existing implementations before writing code:

- `src/oms_hub/llm/provider.py`: `LLMProvider` requires `api_key`; do not smuggle session login into this API-key interface.
- `src/oms_hub/study_generation/service.py`: `GenerationService._queue` currently hard-requires Notebook authentication.
- `src/oms_hub/study_generation/studio_worker.py`: `StudioWorker.run_once` selects source operations and then runs; image-free Notebook results currently auto-publish. New GPT runs must branch before this provider-specific path.
- `src/oms_hub/study_generation/studio_repository.py`: reuse `save_run_artifact`, `run_artifact`, and `await_import_review` for durable content and review.
- `src/oms_hub/study_generation/practice_review.py`: authoritative review state and publication checks; extend its checks, not an alternate review store.
- `src/oms_hub/document_processing/domain.py`: immutable `SourceSnapshot`, `ParsedDocument`, `ParsedAsset`, `DocumentLocator`.
- `src/oms_hub/document_processing/pdf_adapter.py`, `presentation_render.py`, `pptx_locator.py`: existing page/slide extraction and rendering.
- `src/oms_hub/study_generation/quiz_images.py`: `sanitize_quiz_image`, `StudioQuizImageService.copy_import_candidate` verify and sanitize media.
- `src/oms_hub/study_generation/native_quiz.py`: `parse_native_quiz`, `serialize_native_quiz`, native question/answer limits and grading remain authoritative.

B owns new `llm/codex_session.py`, `study_generation/gpt_lecture.py`, their tests, and focused changes to `study_generation/studio_worker.py`, `quiz_images.py`, and `document_processing/presentation_render.py` if required. O owns changes to shared domain enums, repositories, `practice_review.py`, database schema, config, constructors and dispatch wiring; B supplies concrete requests below. O serializes any dispatch change touching B's `studio_worker.py` through B before integration. Do not create a general provider framework.

## Task B1: Freeze and prove the installed subscription contract before generation

**Files:**
- Create: `docs/operations/codex-session-contract.md`
- Create: `scripts/probe-codex-session.py`
- Create: `tests/llm/test_codex_session.py`
- Create: `src/oms_hub/llm/codex_session.py`

**Interfaces:** Python-facing names below are proposed application contracts, not assertions about raw JSON protocol fields. Freeze raw fields from the installed version's generated schema before implementation.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

@dataclass(frozen=True)
class SessionRequest:
    request_id: str
    model: str
    instructions: str
    source_text: str
    image_paths: tuple[Path, ...] = ()
    output_schema: dict[str, object] | None = None

@dataclass(frozen=True)
class SessionResult:
    thread_id: str
    turn_id: str
    text: str

@dataclass(frozen=True)
class SessionLifecycle:
    request_id: str
    phase: Literal["dispatching", "thread_created", "turn_started", "completed", "failed", "interrupted"]
    thread_id: str | None = None
    turn_id: str | None = None

@dataclass(frozen=True)
class SessionStatus:
    state: Literal["disconnected", "connecting", "connected", "limited", "unavailable"]
    model_ids: tuple[str, ...]
    image_model_ids: tuple[str, ...]
    reset_at: str | None
    error_code: str | None

@dataclass(frozen=True)
class LoginChallenge:
    login_id: str
    url: str
    user_code: str | None

# CodexSessionClient methods, shared by generation and chat:
# __init__(self, executable: Path, session_home: Path, work_root: Path)
# status(self) -> SessionStatus
# start_login(self, *, device_code: bool = True) -> LoginChallenge
# cancel_login(self, login_id: str) -> None
# generate(self, request: SessionRequest,
#          *, cancelled: Callable[[], bool],
#          on_lifecycle: Callable[[SessionLifecycle], None]) -> SessionResult
# cancel(self, thread_id: str, turn_id: str) -> None
# close(self) -> None
```

- [ ] Inspect the installed executable with `codex --version`, `codex app-server --help`, and its documented schema-generation help, without starting authenticated work. Store version, binary hash, schema hash and exact Windows launch command in the contract document. Pin the proven version in O's deployment configuration; reject incompatible protocol versions rather than guess.
- [ ] Add `probe-codex-session.py` with explicit `--offline` default, `--login`, and `--smoke` modes. `--offline` only validates exported schema and transport fixtures; login and smoke flags are mutually exclusive and never selected implicitly. Importing the module must not launch a process.
- [ ] Document and replay the following methods from the pinned schema: initialization, managed login/cancel, account status, model list pagination, rate-limit state, thread creation, turn start, turn interruption, final output and completion/error notification. [Official app-server documentation](https://learn.chatgpt.com/docs/app-server) documents stdio JSONL, managed browser/device login, model modality metadata, per-turn output schema, and local image input. Availability of a model and its actual image/schema behavior must still be proved on the target account.
- [ ] Write an offline failing test for fail-closed model readiness:

```python
from oms_hub.llm.codex_session import model_ready

def test_missing_image_capability_cannot_enable_image_generation():
    assert model_ready({"model": "chosen", "inputModalities": ["text"]}, images=True) is False
    assert model_ready({"model": "chosen"}, images=True) is False
    assert model_ready({"model": "chosen", "inputModalities": ["text", "image"]}, images=True)
```

- [ ] Run `uv run pytest tests/llm/test_codex_session.py -q`; expect missing import first. Implement `model_ready(model: dict[str, object], *, images: bool) -> bool` using explicit advertised modalities; missing metadata means unverified for this application, even if a client compatibility default exists. Run again and require pass.
- [ ] When authorized for the isolated Windows proof, verify the exact scheduled-task identity can launch the pinned executable, complete device login, survive worker restart with managed session persistence, and generate one synthetic text result and one synthetic image result with schema validation. Record model id, effort, timestamps, turn ids and redacted results. No lecture/private source is used for this proof.
- [ ] Prove that shell/process execution, edits, network tools, MCP tools, skills and subagents cannot execute in the session. Require either a pinned runtime policy that disables all these tools before a turn begins or supported pre-execution approval interception for every tool class with unconditional denial. An event reporting tool execution is already too late: rejection after execution is detection, not prevention. Verify prevention with a malicious synthetic source fixture that requests shell reads, exfiltration and hidden tools; assert no tool starts and no side effect occurs. Enforce a restricted OS identity with only the explicitly staged source roots accessible to model tools, disable default readable platform roots, and keep managed credentials outside that access boundary. A read-only sandbox does not deny shell reads or exfiltration, and prompt instructions cannot enforce this policy. If the supported pinned runtime cannot guarantee prevention of unexpected tool execution, B1 live readiness fails and activation remains blocked; B2 fake transport implementation and B3 onward offline construction can continue. Do not relax the sandbox.

**Acceptance:** Offline compatibility tests pass independently. Windows subscription/text/image/schema/restriction proof is a separate evidence record. Failure blocks enabling live generation, not B3 source construction, later offline fake-based pipeline work, or unrelated local UI work. Track B1 offline-contract status and Windows/live-readiness status independently. Do not run a giant all-feature live gate.

## Task B2: Implement the bounded session client and interruption semantics

**Files:**
- Modify: `src/oms_hub/llm/codex_session.py`
- Modify: `tests/llm/test_codex_session.py`
- Create: `tests/llm/fixtures/codex_session_events.jsonl`

**Interfaces:** B1 client methods plus `SessionError(code: str, *, retryable: bool = False, reset_at: str | None = None)`. Its `code` is exactly `Literal["auth_required", "rate_limited", "model_unavailable", "capability_unverified", "context_limit", "invalid_output", "timeout", "interrupted", "tool_request_denied", "protocol_error"]`. `reset_at` is an ISO-8601 UTC timestamp or `None`, never a guessed reset. Its public message comes from an allowlist, never raw provider stderr. The chat lane catches this same `SessionError` and reads `code`, `retryable`, and `reset_at`; it must not invent a second error contract. `collect_completed_text(events: list[dict[str, object]], *, thread_id: str, turn_id: str) -> str` is the pure event reducer used by the transport, with exact event shapes frozen in B1.

- [ ] Capture synthetic protocol fixtures including interleaved responses, final assistant output, unrelated turn output, failed completion, malformed JSON and an unsolicited approval. Use documentation-generated ids and source-free strings; do not capture account secrets or real prompts.
- [ ] Add tests with the exact pinned event fixture for a final text result and these assertions: unrelated turn data is excluded; EOF before completed status raises `SessionError("interrupted")`; failed completion never returns success; an unsolicited pre-execution tool approval request is denied before execution and raises `SessionError("tool_request_denied")`. A tool-started/executed event is a failed restriction invariant, not a safe denial: invalidate readiness, interrupt the turn and report the boundary failure. Use the same reducer in production and tests.
- [ ] Run `uv run pytest tests/llm/test_codex_session.py -q`, observe the new failures, then implement the reducer and JSONL reader. Launch with an argument list and `shell=False`; correlate request ids; drain bounded stderr separately; cap a JSONL frame at 4 MiB, aggregate output at 8 MiB, startup at 30 seconds and a generation turn at 180 seconds. Limits are explicit application ceilings, not provider context claims. Make timeout values constructor keyword parameters for deterministic tests.
- [ ] Build each generation request in a fresh isolated directory containing only that request's explicitly staged assets; never run from the repository or course-library root. Use a dedicated private session home and restricted worker identity; omit inherited API-key variables. Do not read token files in Hub or expose session home to the model. Codex owns managed credential storage.
- [ ] Validate local image paths against the per-request staging root, resolve symlinks, require recorded hashes and sanitized image formats, and send only those paths as image inputs. Inputs contain trusted instructions and clearly delimited untrusted source data; source text cannot alter tool policy or spawn actions.
- [ ] Enforce one in-flight turn across quiz, cleaning, outlines and chat through the same O-owned client singleton using a lock; acquire with cancellation polling. No generic task scheduler. Poll cancellation at most every 250 ms, interrupt the known turn, await bounded termination, then terminate only the owned subprocess if needed. `close` drains/reaps it on Windows and Unix. Never kill an unrelated Codex process.
- [ ] Normalize failures to `auth_required`, `rate_limited`, `model_unavailable`, `capability_unverified`, `context_limit`, `invalid_output`, `timeout`, `interrupted`, `tool_request_denied`, or `protocol_error`. Unknown errors remain unavailable; missing rate-limit data stays unknown. No automatic provider turn retry after dispatch ambiguity, quota exhaustion, auth failure or restart. `generate` invokes `on_lifecycle` synchronously; its consumer commits each event through O's repository before returning. Commit `dispatching` before remote dispatch, commit `thread_created` before `turn/start`, and commit `turn_started` with the returned id as soon as received. Persist completion before accepting output. A callback failure before dispatch aborts without a provider call; after dispatch it interrupts the known turn where possible and retains an interrupted/unknown attempt with no automatic retry. The turn id cannot be persisted before the remote request returns it: crashes in that interval remain explicit ambiguity, never an assumed unstarted turn.
- [ ] Add a fake transport lifecycle-order test. `scripted_client` supplies B2's synthetic protocol fixture; `fake_wire` records outgoing methods; the callback below stands in for a synchronous committed repository write:

```python
def test_lifecycle_is_durable_before_turn_dispatch(scripted_client, fake_wire):
    committed = []
    def persist(event):
        committed.append(event.phase)
        if event.phase == "thread_created":
            assert "turn/start" not in fake_wire.sent_methods
    scripted_client.generate(
        SessionRequest("req-1", "chosen", "Return JSON", "synthetic"),
        cancelled=lambda: False, on_lifecycle=persist,
    )
    assert committed == ["dispatching", "thread_created", "turn_started", "completed"]
```

Also inject a callback exception before dispatch and after turn dispatch: assert zero remote calls in the first case, and interruption plus no replay in the second.
- [ ] Add subprocess-fake checks for timeout, cancellation, bounded stdout/stderr, thread correlation, process reaping and lock release, and rerun the focused test file. Ensure import/status never initiates login or generation.

**Acceptance:** Fake transport proves lifecycle safety. No “connected” readiness until managed auth, chosen model, synthetic modality/schema proof and restricted execution evidence match the current executable/model. Client status distinguishes account login from generation readiness. `image_model_ids` contains only models with current explicit advertised image support AND a successful synthetic image/schema capability record for the pinned executable/account; enumeration alone cannot populate it.

## Task B3: Freeze lecture sources and objective/image evidence

**Files:**
- Create: `src/oms_hub/study_generation/gpt_lecture.py`
- Create: `tests/study_generation/test_gpt_lecture.py`
- Modify only if necessary: `src/oms_hub/document_processing/presentation_render.py`

**Interfaces:**

```python
from dataclasses import dataclass
from oms_hub.document_processing.domain import ParsedDocument

@dataclass(frozen=True)
class LectureInputs:
    lecture_id: int
    subject: str
    exam_number: int
    slide_revision_id: int
    transcript_revision_id: int
    slide_source_id: str
    transcript_source_id: str
    objectives: tuple[tuple[str, str], ...]  # stable id, verbatim objective
    documents: tuple[ParsedDocument, ...]

# validate_lecture_inputs(inputs: LectureInputs) -> None
# uncovered_objectives(expected: tuple[str, ...],
#                      assigned: tuple[tuple[str, ...], ...]) -> tuple[str, ...]
# source_manifest(inputs: LectureInputs) -> dict[str, object]
```

- [ ] Add the following pure contract check and run `uv run pytest tests/study_generation/test_gpt_lecture.py -q` to see the missing function failure:

```python
from oms_hub.study_generation.gpt_lecture import uncovered_objectives

def test_coverage_is_per_objective_not_question_count():
    assert uncovered_objectives(("LO1", "LO2"), (("LO1",),) * 20) == ("LO2",)
    assert uncovered_objectives(("LO1", "LO2"), (("LO1", "LO2"),)) == ()
```

- [ ] Implement stable-order set difference for `uncovered_objectives`; reject blank/duplicate objective ids in `validate_lecture_inputs`. Also reject missing slide/transcript roles, empty text/objectives, cross-lecture revision ownership, hash mismatch, missing assets and parser `BLOCKER:` warnings. O loads ownership and freezes actual source revisions before calling B; do not infer ownership from file names.
- [ ] Reuse `DocumentProcessorRouter.parse` and immutable snapshots; build manifest entries with source id/revision/hash, parser/version, objective ids/text/hash, segment keys/locators and asset key/hash/page-or-slide number. Record scope and prompt version in manifest digest. Preserve `ParsedSegment.style_metadata` and existing `run_styles.py` extraction so professor emphasis/red text reaches the prompt; retain slide images for visual meaning. Transcript must be the approved cleaned revision, not a raw upload or unrelated cached text.
- [ ] Extract actual embedded images with existing processors. For slides lacking a suitable extracted image, reuse bounded full-slide rendering using installed PyMuPDF and the existing Office converter; preserve page/slide numbering. Do not substitute generated images, remote image URLs, or guessed descriptions. Image-required requests with unavailable render/extraction remain blocked for review.
- [ ] Add fixture tests with two distinct lectures and repeated asset names; lookup uses `(source_id, asset_key)`, rejects a valid asset from the other lecture, and detects changed bytes. Add a PDF fixture with a vector-only slide to prove full-slide fallback retains its numbered provenance.
- [ ] Run `uv run pytest tests/study_generation/test_gpt_lecture.py tests/document_processing -q`; require no parser regressions.

**Acceptance:** Input manifest is immutable and lecture-scoped; every image can be traced to source revision/hash and slide/page. All objective text is retained. No retrieval database is needed for a single lecture.

## Task B4: Generate structured draft questions with audited coverage

**Files:**
- Modify: `src/oms_hub/study_generation/gpt_lecture.py`
- Modify: `tests/study_generation/test_gpt_lecture.py`
- Create: `src/oms_hub/study_generation/prompt_assets/gpt-lecture-quiz.md`

**Interfaces:** `generate_lecture_quiz(client: CodexSessionClient, request_id: str, model: str, inputs: LectureInputs, *, cancelled: Callable[[], bool], on_lifecycle: Callable[[SessionLifecycle], None]) -> GeneratedLectureQuiz`. Pydantic models in `gpt_lecture.py`:

```python
from pydantic import BaseModel, ConfigDict, Field
from oms_hub.study_generation.practice_contracts import SegmentCitation, AssetCitation

class GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    stem: str = Field(min_length=1)
    choices: list[str] = Field(min_length=2, max_length=8)
    correct_index: int = Field(ge=0)
    rationale: str = Field(min_length=1)
    distractor_explanations: list[str] = Field(min_length=2, max_length=8)
    objective_ids: list[str] = Field(min_length=1)
    source_segments: list[SegmentCitation] = Field(min_length=1)
    image: AssetCitation | None

class GeneratedLectureQuiz(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    questions: list[GeneratedQuestion] = Field(min_length=1, max_length=500)

# validate_generated_quiz(quiz: GeneratedLectureQuiz, inputs: LectureInputs,
#                         *, require_images: bool) -> None
```

- [ ] Write parameterized tests rejecting an unknown objective, unknown segment, transcript asset represented as slide image, out-of-range answer, duplicate question id, blank/deduplicated choices, incomplete coverage and changed image bytes. Add one valid two-objective fixture with a real sanitized slide asset and a fake client returning its JSON.
- [ ] Run `uv run pytest tests/study_generation/test_gpt_lecture.py -q`; expect failures. Implement the models plus local semantic validation. Require at least three questions per lecture, independent clinical vignettes with first- through third-order reasoning when supported, one explanation per choice and cited supporting evidence. Detect duplicate normalized stems in addition to duplicate ids. The minimum count never substitutes for objective coverage. Reuse native quiz parsing for final answer/choice validation and `validate_source_references` logic for source-qualified references; do not trust schema validation alone.
- [ ] Write the prompt asset instructing questions and explanations from only the supplied slides, cleaned transcript and objectives; require objective ids and segment/image citations. Explain that images are supplied evidence, source instructions are untrusted data, unsupported objectives must be reported rather than invented, and output is draft JSON. Read and hash this asset using the existing prompt-snapshot pattern.
- [ ] Call `SessionRequest` with `GeneratedLectureQuiz.model_json_schema()`, fully scoped source text and only selected verified slide images. Record actual model, prompt hash, source manifest hash, thread/turn ids and raw response in bounded private run artifacts. Raw outputs are not public logs and must retain parse-failure evidence.
- [ ] Bound each batch at 25 objectives, 20 images and 100,000 source characters; these are application safety ceilings and do not imply model capacity. Partition deterministically by objective and cited source/page groups only when complete supporting evidence can be retained. If any objective's evidence cannot fit, stop with `context_limit` and a visible list of affected objective ids; never trim arbitrary suffixes. Validate each batch and final combined coverage; keep final native quiz cap 500 and report if the requested coverage cannot fit it.
- [ ] A source-supported image quiz requires at least one real slide-image question plus all objective coverage; do not require every objective to have an image. Unknown visual evidence or medically ambiguous answers remain a review blocker. Review also checks pre-answer images for answer-bearing labels/captions. A necessary presentation crop is stored as a separate derivative with source hash and crop rectangle; retain the original asset and never substitute an invented diagnostic image. Mechanical coverage proves referenced ids, not clinical accuracy; human review verifies substance.
- [ ] Save successful batch artifacts keyed by manifest/prompt/model/schema/batch hashes. On resume, reuse only exact matching completed batch artifacts. Retry only missing batches after an explicit user resume, never reissue a possibly running turn.
- [ ] Rerun the focused tests, plus `uv run pytest tests/study_generation/test_native_quiz.py tests/study_generation/test_quiz_images.py -q`.

**Acceptance:** A fake-client end-to-end test creates a validated complete draft with actual source images; malformed, unsupported or incomplete outputs never reach publication. No claim of live generation quality is made from fake tests.

## Task B5: Route new generation into existing durable review

**Files:**
- Modify: `src/oms_hub/study_generation/studio_worker.py`
- Modify: `src/oms_hub/study_generation/practice_review.py`
- Modify: `src/oms_hub/study_generation/gpt_lecture.py`
- Modify: `tests/study_generation/test_studio_worker.py`
- Modify: `tests/study_generation/test_practice_review.py`
- Test: `tests/study_generation/test_gpt_lecture.py`

**Interfaces:** `to_review_drafts(quiz: GeneratedLectureQuiz, inputs: LectureInputs) -> tuple[QuestionDraftValue, ...]`, using existing `practice_domain` draft constructors. Add a concrete `GptLectureWorker.run(run: StudioRun) -> None` in `gpt_lecture.py`; it consumes O-frozen manifest and session dependencies and ends with existing `StudioRepository.await_import_review(run.id, drafts)`. O injects the worker; B does not create a second queue. `GptLectureWorker` passes an `on_lifecycle` callback that synchronously commits `SessionLifecycle` to O's run-attempt repository; `generate_lecture_quiz` forwards that callback unchanged to the shared client for each batch request id. Completion and publication cannot outrun persistence failure.

- [ ] Send O this exact integration request before changes: persist backend `codex_subscription` and workflow `lecture_generation` on newly queued Studio runs; retain legacy defaults for old rows; freeze lecture/revision/objective ids and source hashes in `gpt:manifest` artifact; persist `gpt:attempt` with thread/turn ids and state; add one dispatch branch before Notebook remote work. Do not place a Codex thread id into a `notebook_id` column. Shared migration design and constructor changes belong to O.
- [ ] Extend the existing Studio worker test fixture with a fake GPT worker and assert a `lecture_generation` run delegates once without calling notebook source attachment/auth/chat. Existing direct-import and Notebook tests continue to pass.
- [ ] Write a restart test that seeds completed `gpt:response` and parsed draft artifacts, runs recovery and confirms the fake client's call count stays zero. Seed an attempt dispatched without final completion and require state `interrupted` with explicit resume, not an automatic provider retry.
- [ ] Convert validated generated questions to existing review drafts with segment and candidate-image provenance, unresolved review status and explanations. Use existing artifact storage for `gpt:coverage` mapping every objective to question ids and citation keys. Store the image-selected binding through `copy_import_candidate` and its SHA check. Do not duplicate native player or media-serving routes.
- [ ] Add publication-time validation in the shared review path: after edits/deletions, recompute objective coverage and verify selected image hashes/source ownership; block publication if required coverage/image evidence is lost. Citation ids cannot be changed to invented values by the UI. Preserve original provider output alongside reviewed changes.
- [ ] Require all new lecture generation, including an image-free valid draft, to enter review; no `publish_and_complete_studio_run` call from `GptLectureWorker`. Existing reviewed publication remains atomic and idempotent.
- [ ] Run `uv run pytest tests/study_generation/test_studio_worker.py tests/study_generation/test_practice_review.py tests/study_generation/test_gpt_lecture.py tests/study_generation/test_source_isolation.py -q` and report results to O.

**Acceptance:** Local fake generation → source/image validation → review edits → explicit publication → native preview/grade passes. Cancellation/restart cannot duplicate provider work or publication. Gemini/Notebook authentication is never consulted for new GPT runs.

## Task B6: Finish the GPT-only prerequisite slice and staged acceptance

**Files:**
- Modify: `docs/operations/codex-session-contract.md`
- Create: `tests/v2/test_gpt_lecture_acceptance.py`
- O-owned integration targets: `src/oms_hub/study_generation/service.py`, `src/oms_hub/ingestion/worker.py`, `src/oms_hub/workers.py`, `src/oms_hub/app.py`, config and persistence modules identified in the overarching plan.

**Interfaces:** Use B1 `generate` for transcript cleaning and optional outline generation at O's integration seam; keep existing cleaned-transcript artifact/revision conventions and optional outline records. Chat uses the same client rather than constructing an API-key provider.

- [ ] Give O a no-Gemini test scenario: upload slides and transcript, clean the transcript with fake session output, provide explicit objectives, queue lecture generation, review and publish without any Google credentials or notebook connection. A fixture trap raises on every Gemini/Notebook invocation. The test must pass through actual service/worker constructors after O wiring.
- [ ] Preserve current transcript-cleaning validation, source provenance and prompt snapshot metadata. Optional outlines do not gate quizzes. If transcript cleaning is not yet switched, new GPT-only workflow is not complete; report that dependency instead of claiming quiz generation removed all Google requirements.
- [ ] Add native public-route acceptance checking question count, one resolved image URL/bytes, answer grading and absence of answers in the initial public payload. Verify all persisted objective ids survive the review/publication boundary.
- [ ] Run `uv run pytest tests/v2/test_gpt_lecture_acceptance.py tests/v2/test_public_quiz_routes.py tests/study_generation/test_native_quiz.py -q`. Run project-required lint/type checks on the touched modules after O integration, using the repository's configured commands.
- [ ] Record acceptance separately: (1) offline client contract; (2) authorized Windows subscription/restriction proof; (3) local full pipeline with fakes; (4) authorized one-lecture live generation with manual coverage/citation/image audit; (5) authorized guarded production release and rendered/public media checks. A later failure does not erase earlier evidence or justify automatically repeating a consumed live attempt.
- [ ] For each authorized live attempt, record scope, source/target hashes, model/version, start/end, success/error code, artifact paths and exact retry status. Credentials and private prompt bodies remain out of public reports. O handles release backups, existing listener ownership, rollback and service restart.

**Acceptance:** The actual new user path works without a required Google provider. Subscription exhaustion becomes a clear resumable blocked state with available reset time; no spending fallback. Anki, a new vector store, automatic batch repair loops, and broad provider rewrites remain out of scope.

## Task B7: Export the accepted payload as JSON, image bundle and readable PDF

**Files:**
- Create: `src/oms_hub/study_generation/quiz_export.py`
- Create: `tests/study_generation/test_quiz_export.py`
- Reuse: `src/oms_hub/study_generation/outline.py` ReportLab styling patterns and `native_quiz.py` serialization.
- O-owned: authenticated export route and authorization wiring in `src/oms_hub/web/generation_routes.py`.

**Interfaces:** `export_reviewed_quiz(quiz: NativeQuiz, image_paths: dict[str, Path], provenance: dict[str, object], output_dir: Path) -> tuple[Path, Path, Path]` returns JSON, ZIP and PDF paths. It accepts only the final reviewed quiz and verified image mapping provided by the authoritative review service, not raw model output.

- [ ] Write a test using a two-question native quiz fixture with one sanitized PNG: call the export function, parse returned JSON with `parse_native_quiz`, assert identical question ids/count, open the ZIP with `zipfile.ZipFile` and assert `manifest.json` plus the referenced image are present, and extract PDF text with installed `pypdf.PdfReader` to verify both stems and explanations. Run `uv run pytest tests/study_generation/test_quiz_export.py -q`; expect missing implementation.
- [ ] Serialize native JSON with `serialize_native_quiz`; write a ZIP with safe application-generated relative media names and a provenance manifest containing question/objective/source-image bindings and file hashes. Use `verified_atomic_write`; reject absent/mismatched media. All three artifacts use the same accepted-payload hash so review edits invalidate prior exports.
- [ ] Render PDF with installed ReportLab: numbered vignettes and scaled actual images first, a separated answer/explanation section, then readable source/objective citations. Escape text passed to ReportLab markup. Keep answers out of the question section and prevent clipped images and headings stranded at page bottoms.
- [ ] Rerun the export test and render every PDF page to images using installed PyMuPDF for visual inspection; inspect one long vignette, multi-page answer section, portrait figure and wide slide. Fix overflow before reporting completion. No new PDF package is needed.
- [ ] Send O the exact export method signature and authenticated route requirements. Add the export assertions to the local end-to-end acceptance in B6 and include downloadable artifacts in the authorized one-lecture quality audit.

**Acceptance:** Native JSON, image bundle and readable PDF all match the accepted reviewed content, coverage and source provenance. A stale or incomplete export is rejected, not silently served.

## Checkpoint and handoff

Each task ends with its focused test evidence and a reviewed local diff; commit only within the execution session's authorization. Do not commit during this planning task. B sends O interface changes before other workers consume them and reports separately which results are offline, Windows-contract, live-source and deployed. This plan adds no approval request for routine reversible implementation.
