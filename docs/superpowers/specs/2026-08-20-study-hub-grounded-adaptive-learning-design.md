# Study Hub Grounded Adaptive Learning — Approved Design Specification

**Status:** Approved architecture captured for implementation planning  
**Date:** 2026-08-20  
**Repository:** `connor-brooks98/oms-study-automation`  
**Primary user:** Connor Brooks  
**Target board examination:** COMLEX-USA Level 1, anticipated May–July 2027  
**Companion implementation plan:** `docs/superpowers/plans/2026-08-20-study-hub-grounded-adaptive-learning.md`

---

## 1. Purpose

Study Hub will preserve its existing lecture outlines, lecture quizzes, and custom quiz generator while adding a trusted knowledge layer, context-aware Ask StudyHub chat, source-grounded board-style question generation, adaptive practice, Anki-linked remediation, and a longitudinal Board Runway dashboard.

The design exists to meet two learning goals simultaneously:

1. Perform well on course examinations by treating lecture materials as the default exam authority.
2. Build durable COMLEX Level 1 application skills throughout the academic year so a long dedicated period is less necessary.

The implementation must remove NotebookLM session continuity from the critical path. NotebookLM may remain an optional comparison and manual companion, but Study Hub must be able to ingest, index, retrieve, generate, and explain through normal backend API credentials without browser-cookie reconnection.

---

## 2. Non-negotiable product invariants

1. Existing lecture outlines, lecture quizzes, and custom quiz generation continue to work during migration.
2. In `course_only` mode, factual medical claims may come only from approved course materials.
3. In literature-enabled modes, factual medical claims may come only from approved course materials and explicitly approved published journal sources.
4. General model knowledge is never an authority source.
5. Generated outlines, answers, rationales, flashcards, and questions are derivative artifacts; they never become independent truth sources.
6. Every trusted answer and every medically meaningful generated-question claim must map to one or more stable evidence identifiers.
7. An unsupported answer must fail closed with an insufficient-evidence response.
8. A source revision invalidates all dependent indexes and derivative artifacts by content hash.
9. Pre-submit quiz assistance must not reveal the correct answer.
10. The hosted Study Hub remains usable from phone or iPad while the Mac and Anki Desktop are off.
11. Anki synchronization and any future Anki mutation remain local. The hosted backend never writes directly to `collection.anki2`.
12. No pass-probability or score-prediction claim is shown until it is supported by a genuinely calibrated dataset.
13. Gemini File Search is a rebuildable provider index, not the canonical source library.
14. API keys remain server-side and are never exposed to browser JavaScript or logs.
15. NotebookLM reconnect state is not required for Ask StudyHub, board questions, adaptive practice, or source-grounded generation.

---

## 3. Architectural style

Extend the existing Study Hub modular monolith rather than introducing a second web service, queue framework, ORM, or deployment unit.

Use ports-and-adapters boundaries:

- **Domain:** source authority, evidence identity, truth policy, question validity, objectives, mastery, and scheduling rules.
- **Application:** ingestion orchestration, retrieval, Ask StudyHub, artifact generation, adaptive queue construction, and Anki synchronization.
- **Infrastructure:** current database/repository conventions, file storage, Gemini API, current model providers, AnkiConnect, NCBI metadata, and logging.
- **Presentation:** existing web shell, quiz page, lecture/exam pages, source preview, and Board Runway dashboard.

The exact repository integration points are frozen by the first implementation gate because the live workstation was unavailable during plan authoring. All new backend package paths in this specification are canonical. Sol-0 maps existing frontend, migration, configuration, and test-runner paths before parallel feature work begins.

---

## 4. Current code to reuse

Known existing code and patterns include:

- `src/oms_hub/app.py` — application composition and route registration.
- `src/oms_hub/files/office.py` and `src/oms_hub/files/office_worker.py` — killable Office conversion process boundary.
- `src/oms_hub/files/promotion.py` — staged-to-canonical promotion behavior.
- `src/oms_hub/slides/pipeline.py` — source processing and immutable artifact flow.
- `src/oms_hub/ingestion/repository.py` — durable repository and revision state.
- `src/oms_hub/ingestion/worker.py` — retry, recovery, and background job patterns.
- `src/oms_hub/study_generation/notebook_storage.py` — legacy NotebookLM session migration only; do not reuse its reconnect dependency as the new provider design.
- Existing artifact/private-preview code used by slide and quiz review.
- Existing provider settings and OpenRouter work.
- Existing Anki v2 source/preflight and lifecycle work.
- Existing Python, JavaScript, and Windows document-processing CI lanes.

Lessons already established in this repository are binding:

- Recover interrupted states before fallible preprocessing.
- Persist destination paths instead of recomputing them during recovery.
- Preserve retryable source state.
- Terminalize exhausted, unusable revisions.
- Run external Office conversion in a process tree that can be killed.
- A migration failure must degrade to a disconnected/disabled feature rather than prevent application startup.
- Cleanup diagnostics do not invalidate a successfully created canonical artifact.
- Promotion and indexing operations must be idempotent and content-hash addressed.

---

## 5. External code and APIs to reuse

### 5.1 Google Gemini

Use the official Apache-2.0 `google-genai` Python SDK rather than the deprecated `google-generativeai` package.

Initial verified pins:

```text
google-genai==2.14.0
GEMINI_FILE_SEARCH_MODEL=gemini-3.7-flash
GEMINI_MULTIMODAL_EMBEDDING_MODEL=models/gemini-embedding-2
```

The SDK adapter must be isolated so pins and model names can be changed after contract tests without altering domain code.

Verified provider capabilities as of 2026-08-20:

- File Search imports, chunks, embeds, and indexes source files for RAG.
- PDF and PPTX are supported.
- `gemini-embedding-2` supports text and image/multimodal retrieval.
- Custom document metadata and metadata filters are supported.
- Citations may include PDF page numbers.
- Gemini 3 models can combine File Search and structured output.
- Embeddings persist until deletion; the temporary raw File object expires after 48 hours.
- File Search is not supported by the Live API.
- File Search cannot be combined with Google Search or URL Context in one request.
- A document is limited to 100 MB.
- Google recommends stores below 20 GB for retrieval latency.

Implementation policy:

- Canonical source bytes remain in Study Hub.
- Use Files API upload followed by `import_file` as the default index path because it provides explicit resumable phases and a durable provider operation name.
- Use the direct upload endpoint only after a compatibility test.
- Do not enable a thinking configuration in the same critical File Search + structured-output call until a live contract test proves the configured SDK/model combination.
- Prefer a two-stage evidence-packet pipeline for board question generation even though combined File Search + structured output is supported.
- All provider calls have fake implementations for default CI and opt-in live smoke tests.

### 5.2 AnkiConnect

Reuse the existing typed AnkiConnect integration and version-6 request envelope. AnkiConnect is a local HTTP API and requires Anki Desktop to be running. The hosted application therefore consumes a synchronized, read-only snapshot uploaded by a trusted local bridge rather than attempting to reach `127.0.0.1:8765` from the server.

### 5.3 Journal metadata

Use NCBI E-utilities for PubMed/PMC metadata lookup. The first release does not automatically search for or download journal full text. The user explicitly uploads a PDF and approves it; DOI/PMID metadata is supplemental validation and provenance.

### 5.4 Dependencies deliberately rejected

Do not add LangChain, LlamaIndex, Celery, a new vector database, a new frontend framework, or a second web API service unless Sol-0 documents a concrete missing capability and Connor approves the change. The current application already has the necessary ingestion, persistence, job, provider, and web boundaries.

---

## 6. Source authority and truth modes

### 6.1 Authority classes

```text
course_material
published_journal
generated_artifact
question_style_reference
```

Only the first two may support medical claims.

### 6.2 Truth modes

```text
course_only
course_and_literature
literature_only
```

Default scope is `course_only`.

| Mode | Allowed authority |
|---|---|
| `course_only` | `course_material` |
| `course_and_literature` | `course_material`, `published_journal` |
| `literature_only` | `published_journal` |

### 6.3 Conflict behavior

Course and literature claims are never silently blended. When both are enabled and the source sets disagree, return separate labeled sections:

```text
Course material states:
...

Published literature states:
...

Potential discrepancy:
...
```

Course-exam experiences default to course truth even when a literature conflict exists.

---

## 7. Canonical source lifecycle

### 7.1 Canonical source group

A lecture source group may include:

```text
original PowerPoint
canonical rendered PDF
speaker notes
transcript
faculty handout
course objectives
normalized Markdown
slide/page images
```

The rendered PDF is the preferred visual citation target. Original PowerPoint and normalized text preserve speaker notes and structural text.

### 7.2 Stable identifiers

Use deterministic identifiers based on source and content hashes.

```text
source_document_id = stable UUID assigned when a logical source is created
source_revision_id = "sr_" + base32(sha256(source_document_id + file_sha256))[:26]
evidence_id        = "ev_" + base32(sha256(source_revision_id + locator + content_sha256))[:26]
```

A content change creates a new source revision and new evidence identifiers. The old revision remains immutable and may be retired only after all references are preserved.

### 7.3 Evidence locators

```text
page
slide
speaker_note
transcript_segment
section
figure
table
article_page
```

Each evidence unit stores:

- stable evidence ID
- source revision ID
- authority class
- course/exam/lecture scope
- locator type and number
- normalized text
- optional image asset ID
- content checksum
- source priority
- creation and retirement timestamps

### 7.4 Provider stores

Use multiple provider stores to reduce accidental cross-scope retrieval:

```text
course:<course_id>:exam:<exam_id>
literature:<course_id>
```

Current-lecture retrieval applies a metadata filter for `lecture_id`. Current-exam retrieval uses the exam store. Entire-course retrieval may search multiple exam stores. Literature remains physically separated from course stores.

---

## 8. Shared provider contracts

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import AsyncIterator, Protocol, Sequence

class AuthorityClass(StrEnum):
    COURSE_MATERIAL = "course_material"
    PUBLISHED_JOURNAL = "published_journal"
    GENERATED_ARTIFACT = "generated_artifact"
    QUESTION_STYLE_REFERENCE = "question_style_reference"

class TruthMode(StrEnum):
    COURSE_ONLY = "course_only"
    COURSE_AND_LITERATURE = "course_and_literature"
    LITERATURE_ONLY = "literature_only"

@dataclass(frozen=True)
class RetrievalScope:
    course_id: str
    exam_id: str | None
    lecture_ids: tuple[str, ...]
    truth_mode: TruthMode
    source_revision_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    source_revision_id: str
    authority_class: AuthorityClass
    locator_kind: str
    locator_value: str
    excerpt: str
    checksum: str

@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    scope: RetrievalScope
    maximum_evidence: int = 12

@dataclass(frozen=True)
class RetrievalResult:
    evidence: tuple[EvidenceRef, ...]
    provider_request_id: str
    insufficient_evidence: bool

class RetrievalProvider(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...
    async def health(self) -> "ProviderHealth": ...

class GenerationProvider(Protocol):
    async def generate_structured(
        self,
        request: "StructuredGenerationRequest",
    ) -> "StructuredGenerationResult": ...

    def stream_answer(
        self,
        request: "GroundedAnswerRequest",
    ) -> AsyncIterator["AnswerEvent"]: ...
```

Provider-specific IDs do not escape the infrastructure adapter. Domain and persistence code use Study Hub IDs.

---

## 9. Ask StudyHub

### 9.1 Context envelope

Every request includes structured context:

```json
{
  "query": "Why is this not TTP?",
  "scope": {
    "course_id": "heme-lymph",
    "exam_id": "exam-2",
    "lecture_ids": ["lecture-13"],
    "truth_mode": "course_only"
  },
  "page_context": {
    "kind": "quiz_question",
    "quiz_id": "quiz-42",
    "question_id": "question-8",
    "objective_ids": ["objective-hit-recognition"],
    "submitted": true,
    "selected_option_id": "B"
  },
  "thread_id": "thread-question-8"
}
```

### 9.2 Main Hub behavior

The Ask bar inherits the current page scope and displays it. The user may change between current lecture, current exam, entire course, and literature-enabled modes.

### 9.3 Quiz behavior

Before submission:

- Correct answer, answer rationale, and correctness state are excluded from the model request.
- Direct requests for the answer receive a submit-first response.
- Supported actions are term definition, concept hint, relevant mechanism, and source excerpt.
- Generated text is checked for exact correct-answer leakage before display.

After submission:

- Why the correct option is correct.
- Why the selected option is wrong.
- Compare two choices.
- Show source.
- Generate another question targeting the same distinction.
- Increase or decrease difficulty.

### 9.4 Citation behavior

A citation resolves to a Study Hub evidence ID, then opens the canonical slide/page/segment in a drawer without leaving the quiz. Provider citations are translated into stable Study Hub evidence records and rejected if translation fails.

### 9.5 Conversation memory

Conversation history improves wording continuity but never becomes evidence. Every user turn performs a fresh retrieval under the current truth policy.

---

## 10. Board-style question engine

### 10.1 Separation of concerns

```text
Objective selector: which skill to test
Evidence retriever: what facts are allowed
Item blueprint: how to frame the test
Generator: produce the item
Validator: prove support and single-best-answer quality
Critic: independently review the item
Repository: version, quarantine, and serve approved items
```

### 10.2 Generation pipeline

1. Select one or more objectives.
2. Retrieve a bounded evidence packet.
3. Refuse generation if the packet is insufficient.
4. Generate a structured item.
5. Validate schema and deterministic invariants.
6. Verify every medical claim against evidence IDs.
7. Run an independent item-quality critic.
8. Accept, quarantine, or regenerate within a bounded retry budget.
9. Persist an immutable question version.

### 10.3 Question schema

```python
class QuestionClaimRole(StrEnum):
    STEM = "stem"
    CORRECT_SUPPORT = "correct_support"
    DISTRACTOR_SUPPORT = "distractor_support"
    RATIONALE = "rationale"
    TEACHING_POINT = "teaching_point"

class QuestionOption(BaseModel):
    option_id: str
    text: str
    rationale: str
    evidence_ids: list[str]

class QuestionClaim(BaseModel):
    claim_id: str
    role: QuestionClaimRole
    text: str
    evidence_ids: list[str]

class BoardQuestionDraft(BaseModel):
    stem: str
    lead_in: str
    options: list[QuestionOption]
    correct_option_id: str
    objective_ids: list[str]
    difficulty: int
    blueprint_tags: list[str]
    claims: list[QuestionClaim]
```

### 10.4 Validation rules

- Four or five homogeneous options.
- Exactly one correct option.
- Correct option ID exists.
- Every medically meaningful claim has at least one allowed evidence ID.
- Every evidence ID was present in the request packet.
- The stem does not depend on an unstated fact.
- No answer-length cue.
- No “all of the above” or “none of the above.”
- No duplicate option.
- Rationale explains the correct answer and each distractor.
- Unsupported numeric thresholds fail validation.
- Image-dependent items reference a specific source image.
- A quarantined item is never served to learners or used for mastery.

### 10.5 Question modes

```text
lecture_recall
lecture_application
board_style
integrated_board_style
comlex_omm
remediation
timed_mixed_block
```

Board-style is the default adaptive mode after foundational exposure.

---

## 11. Objective graph and mastery

### 11.1 Objectives

Objectives are source-derived, reviewed, stable nodes. They may map to:

- evidence units
- prerequisite objectives
- question versions
- Anki note IDs
- board blueprint tags
- course/exam/lecture scope

### 11.2 Separate mastery dimensions

```text
recall_retention
application_mastery
timed_application
assistance_dependence
```

Anki remains the recall scheduler. Study Hub does not replace FSRS or card due dates.

### 11.3 Initial transparent application model

For objective `o`, use a decayed weighted beta model:

```text
alpha = 2 + Σ(correct_event_weight × recency_weight)
beta  = 2 + Σ(incorrect_event_weight × recency_weight)
application_score = 100 × alpha / (alpha + beta)
```

Recency:

```text
recency_weight = 0.5 ** (age_days / 60)
```

Attempt weights:

| Signal | Multiplier |
|---|---:|
| Unaided | 1.00 |
| Concept hint | 0.70 |
| Source excerpt | 0.55 |
| Explanation after miss | 0.35 |
| Answer revealed | 0.10 |
| Confident correct | 1.10 |
| Unsure correct | 0.85 |
| Guessed correct | 0.65 |
| Confident incorrect | 1.15 penalty |
| Unsure incorrect | 1.00 penalty |

Difficulty multiplier:

```text
1 = 0.75
2 = 0.90
3 = 1.00
4 = 1.20
5 = 1.40
```

The score is a local learning estimate, not a pass prediction.

### 11.4 Daily adaptive allocation

Default quota:

```text
40% current weak objectives
25% delayed remediation
20% cumulative prior systems
10% stronger calibration objectives
 5% untested objectives
```

Reallocate empty buckets proportionally. Do not show the same objective more than twice in ten questions except inside an explicit remediation micro-set. Do not repeat the same immutable question version within 30 days except after a miss.

---

## 12. Practice modes, Error Notebook, and blueprint coverage

### 12.1 Versioned practice policies

Study Hub supports:

```text
tutor
timed
timed_mixed
outline_checkpoint
remediation
exam_simulation_preview
```

Tutor mode reveals correctness and rationale after each submitted item. Timed modes withhold correctness and rationale until block submission. During an active timed block, Ask StudyHub may clarify terminology or locate an approved source but cannot reveal or narrow the correct answer. Timers are server-authoritative and policy-versioned.

### 12.2 Custom blocks

Custom blocks may filter by:

```text
course/exam/lecture
objective
question mode
difficulty
new/incorrect/flagged status
date last seen
COMLEX blueprint tags
truth mode
question count
tutor/timed policy
```

Only accepted, nonstale question versions are eligible. Inventory shortage is reported rather than filled with unreviewed generation.

### 12.3 Error Notebook

After an attempt, Study Hub may suggest a reviewable error category:

```text
knowledge_gap
misread_stem
misread_question_task
reasoning_error
changed_from_correct
overthinking
timing_pressure
answer_choice_confusion
unsupported_or_bad_item
unclassified
```

These are hypotheses, not diagnoses of the learner. The user may confirm or replace a suggestion. The system preserves both the original suggestion and user-selected category.

### 12.4 Blueprint coverage

Objectives and accepted question versions may be mapped to a versioned COMLEX exam profile. Mappings are separately approved and are not medical evidence. Coverage reports use approved mappings only and retain the profile version used by each historical session.

### 12.5 Outline checkpoints

A generated outline may offer one to three accepted questions after a section. The questions must share approved evidence/objectives with that section. If inventory is absent, the UI offers generation for later review rather than embedding an unchecked question.

---

## 13. Anki learning loop

### 13.1 Hosted/local boundary

Hosted Study Hub stores the latest read-only Anki synchronization snapshot:

- note ID
- card IDs
- deck and selected tags
- objective mappings
- due/overdue state
- lapse count
- interval or retrievability values exposed by the existing local integration
- synchronization timestamp

A local trusted bridge reads AnkiConnect and uploads this minimized summary. The server never receives the Anki collection file.

### 13.2 Workflows

```text
Missed question → related mapped notes → review suggestion
Anki objective/tag → targeted board-style question set
Coverage gap → proposed card → native approval workflow
```

The first release does not auto-create cards or filtered decks.

---

## 14. Journal evidence

A published journal source requires:

- uploaded source PDF
- title
- authors
- journal
- publication date
- DOI and/or PMID when available
- user approval
- content hash
- source status

PubMed metadata may be fetched through NCBI E-utilities. Metadata lookup does not make a source authoritative; user approval does.

Journal PDFs are indexed into a physically separate literature store. Course-only retrieval cannot search that store.

---

## 15. Board Runway

The dashboard shows longitudinal preparation without an unsupported score prediction:

```text
recall retention
application mastery
timed mixed-block accuracy
blueprint exposure
recently weak objectives
Anki due/overdue load
current-course question target
cumulative question target
external assessment history
```

The target exam window is configurable and initially set to May–July 2027.

---

## 16. Persistence model

The implementation may adapt names to established repository conventions, but the following logical records are required:

```text
knowledge_sources
source_revisions
evidence_units
provider_stores
provider_documents
index_jobs
artifact_recipes
artifact_runs
artifact_evidence
ask_threads
ask_messages
retrieval_runs
retrieval_evidence
learning_objectives
objective_edges
objective_evidence
question_items
question_versions
question_options
question_claims
question_evidence
quiz_attempts
attempt_events
mastery_snapshots
practice_sessions
practice_session_items
error_notebook_entries
exam_profiles
blueprint_mappings
anki_sync_runs
anki_note_snapshots
anki_objective_mappings
journal_records
study_plan_days
external_assessments
```

All derivative records include source revision IDs, prompt/schema/model versions, and input/output hashes.

---

## 17. Feature flags

```text
source_trust_v1
gemini_file_search_v1
ask_studyhub_v1
ask_quiz_context_v1
board_question_v1
adaptive_practice_v1
practice_modes_v1
error_notebook_v1
timed_blocks_v1
anki_learning_loop_v1
board_runway_v1
journal_evidence_v1
legacy_notebooklm_generation
```

New flags default off outside explicit development/test configuration. Existing outline and quiz features remain routed through their current implementation until their regression fixtures pass under the recipe wrapper.

---

## 18. Reliability and security

### 18.1 Fail-closed states

- Missing evidence → insufficient evidence.
- Invalid provider citation → answer withheld.
- Out-of-scope source → request rejected.
- Stale source revision → dependent result stale.
- Invalid structured output → retry or quarantine.
- Provider quota/auth failure → actionable provider health state.
- Failed migration → feature disabled; application still starts.
- Exhausted index retries → terminal failed index record with rebuild action.
- Quiz pre-submit answer leakage → response discarded and safe response shown.

### 18.2 Prompt-injection defense

Uploaded documents are untrusted data even when medically authoritative. Source text cannot change the system policy, tool choice, truth mode, or response schema. Evidence is delimited and labeled; instructions found inside source text are ignored unless they are part of the lecture content being discussed.

### 18.3 Logging

Never log:

- API keys
- raw uploaded files
- full prompts containing private lecture text
- full model responses by default
- Anki collection contents

Log:

- Study Hub IDs
- provider request IDs
- hashes
- durations
- token/cost metadata
- state transitions
- validation outcomes
- error categories

---

## 19. Evaluation and acceptance

### 19.1 Golden benchmark lectures

Use at least:

1. Text-heavy mechanism lecture.
2. Image-heavy pathology/radiology lecture.
3. Integrated clinical or OMM lecture.
4. Lecture 13 — Coagulopathy, because representative PowerPoint/PDF fixtures already exist.

### 19.2 Question comparison

Blindly compare:

- current NotebookLM quiz
- current Study Hub lecture quiz
- Gemini source-locked lecture quiz
- Gemini board-style quiz

Rubric:

```text
source fidelity
single-best-answer quality
clinical application
distractor plausibility
cueing
rationale completeness
difficulty
usefulness for Level 1
```

### 19.3 Release thresholds

- Existing outline/quiz golden outputs: no unapproved schema or routing regression.
- Supported medical claims: 100% linked to allowed evidence.
- Citation-to-preview resolution: 100% on accepted answers.
- Unsupported-fact fixture set: zero trusted answers.
- Pre-submit answer-leak fixture set: zero leaks.
- Accepted question set: exactly one best answer.
- Quarantined questions: never served.
- Index rebuild: idempotent for the same source revision.
- Provider outage: cached artifacts remain readable.
- Anki server offline: hosted Study Hub remains operational with stale-sync labeling.
- Main Python and JavaScript CI: green.
- Windows document-processing lane: no regression relative to the accepted baseline and green before final release.

---

## 20. Rollout strategy

1. Freeze current behavior and collect golden fixtures.
2. Add source registry and provider contracts with flags off.
3. Backfill one lecture and build a Gemini store in shadow mode.
4. Enable Ask StudyHub for one lecture.
5. Run NotebookLM/Gemini board-question comparison.
6. Enable board question generation only after quality acceptance.
7. Add objective and mastery event collection before adaptive selection.
8. Enable adaptive queues for one course/exam.
9. Add tutor/custom practice, then timed blocks and Error Notebook.
10. Add read-only Anki snapshot integration.
11. Add Board Runway.
12. Add manually approved journal evidence.
13. Remove NotebookLM reconnect from the critical path only after one full accepted course/exam cycle and rollback verification.

NotebookLM may remain available as a manual companion after cutover.

---

## 21. Explicitly deferred

- Automatic internet research during student questions.
- Automatic journal discovery/download.
- Automatic Anki mutation from the hosted server.
- Social leaderboards.
- Fake peer benchmarking.
- Calibrated board-score or pass prediction.
- Full high-stakes exam simulation until the current official exam profile and timing are verified; `exam_simulation_preview` remains feature-gated.
- A separate microservice architecture.
- Model fine-tuning.
- Audio/video generation.
- Replacing Anki scheduling.
- Full institutional LMS integration.
