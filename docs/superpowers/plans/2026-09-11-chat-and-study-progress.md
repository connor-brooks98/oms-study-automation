# Chat and Study Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add source-scoped GPT chat, an optional AMBOSS connection, and trustworthy personal practice tracking without blocking lecture generation.

**Architecture:** Worker C reuses B's single Codex client and Q's attempt records. Local source lookup supplies bounded evidence to chat; AMBOSS is a separate explicit mode. Deterministic progress and block selection run without a model or external service.

**Tech Stack:** Existing Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic, Jinja/vanilla JavaScript, pytest; standard-library collections and dataclasses.

**Spec:** `docs/superpowers/specs/2026-09-11-gpt-study-platform-design.md`

## Global Constraints

- Follow the main orchestration plan. C owns new `src/oms_hub/study_chat/`, `src/oms_hub/study_progress/`, and focused tests/templates/static files below. O owns shared schema/app/config/navigation; C submits exact shared patches.
- Subscription login first; no silent paid API fallback. Use B's singleton `CodexSessionClient` and its mutex rather than start another app-server.
- External AMBOSS access is optional and pending documented entitlement. Neither an AMBOSS GPT link nor a mocked MCP response is provider acceptance.
- Existing public quiz behavior remains usable. Only authenticated owner submissions enter personal progress. Full Anki curation and live collection mutation remain deferred.
- No guessed endpoint, copied browser session token, public access to private sources, or model-controlled mutation tools.

## C1: Source-scoped chat and durable conversation state

**Dependencies:** B1/B2 contract can be consumed from fakes; B1's actual Windows/provider capability proof is necessary only for live activation. O supplies schema and authenticated routing.

**Files:** Create `src/oms_hub/study_chat/{__init__,contracts,sources,service,repository,routes}.py`, `src/oms_hub/web/templates/study_chat.html`, `src/oms_hub/web/static/study_chat.js`, `tests/study_chat/test_sources.py`, `test_service.py`, `test_routes.py`, `tests/js/study_chat.test.js`.

**Interfaces:** Import `SessionRequest`, `SessionResult`, `SessionLifecycle`, `SessionError`, `CodexSessionClient` from B's `llm/codex_session.py`. Pass its required `on_lifecycle` callback to synchronously persist request dispatch/thread/turn state through the chat repository before accepting output. Reuse `SourcePassage` and `LectureSourceExtractor.extract(revision_ids, summary_outline_id=None)` from `anki/sources.py`; do not instantiate the embedding-dependent `LectureSourceIndex` just to perform lexical lookup.

Define the following local contracts; Pydantic models forbid extras and validate IDs/mode membership:

```python
from dataclasses import dataclass
from typing import Literal
from oms_hub.anki.sources import SourcePassage

ChatMode = Literal['lecture', 'medical_reference', 'general']

@dataclass(frozen=True)
class ChatRequest:
    request_id: str
    owner_id: str
    conversation_id: str
    mode: ChatMode
    question: str
    revision_ids: tuple[int, ...]

@dataclass(frozen=True)
class ChatAnswer:
    status: Literal['answered', 'no_support', 'unavailable']
    text: str
    citation_ids: tuple[str, ...]

# sources.select_passages(question: str, passages: tuple[SourcePassage, ...],
#                         *, max_chars: int = 24000) -> tuple[SourcePassage, ...]
# service.validate_answer(answer: ChatAnswer, allowed_ids: set[str]) -> None
# ChatService.answer(request: ChatRequest) -> ChatAnswer
# ChatRepository.create(owner_id: str, mode: ChatMode, revision_ids: tuple[int,...]) -> str
# ChatRepository.append(request: ChatRequest, answer: ChatAnswer) -> None
# ChatRepository.record_lifecycle(request_id: str, event: SessionLifecycle) -> None
```

- [ ] Add a failing citation-membership test before implementing the validator:

```python
import pytest
from oms_hub.study_chat.contracts import ChatAnswer
from oms_hub.study_chat.service import validate_answer

def test_invented_citation_is_rejected():
    answer = ChatAnswer('answered', 'Supported claim', ('other-lecture',))
    with pytest.raises(ValueError, match='citation'):
        validate_answer(answer, {'selected-slide'})
```

- [ ] Run `python -m pytest tests/study_chat/test_service.py -q`, verify the failure names the absent/incorrect citation behavior.
- [ ] Resolve revision ownership, current hashes and lecture/course/exam server-side before extraction. Select lexical matches with case-folded word tokens, overlap score and stable passage-ID tie break; include no partial passage beyond the character bound. Empty overlap returns `no_support`, not unrestricted sources. This initial heuristic is explicitly limited; evaluate known paraphrases before deciding whether semantic search is necessary.
- [ ] Implement validation: `answered` lecture results require at least one allowed passage ID, unknown IDs fail, and `no_support` results cannot assert supporting citations. Persist the exact evidence snapshot and map IDs to server-generated source links. Reject stale source hashes before accepting a result. Check claim support in human evaluation as well as membership.
- [ ] Store request idempotency, owner, conversation mode, source IDs/hashes, question, validated result, remote thread/turn and terminal/error state. Do not reuse a provider thread when mode/source scope changes; fresh scoped requests contain only that conversation's approved context. Interrupted requests preserve state and require explicit retry, while limit/auth failures show B's documented status.
- [ ] Call B's client with schema-constrained output and the supplied passages. Revalidate decoded output. Deny model tool calls; all lookups are performed by application code. History is bounded and never contains messages from another owner.
- [ ] Add authenticated `/study/chat` UI and POST `/study/chat/answer`, explicit mode and source selection, streaming/progress if supported, clear unavailable-source states and cancel control. Use existing owner/CSRF middleware; do not invent a second authentication system. Render text safely with existing sanitized Markdown support, or plain text if none exists. Reject script/link injection and do not display raw exception bodies.
- [ ] Add tests for mismatched owner/revision, empty evidence, source hash changed, unsupported citation, source prompt injection, limited/auth/interrupted status, duplicate request and history isolation. Test keyboard submission/cancel and safe rendering in JS.
- [ ] Run `python -m pytest tests/study_chat -q`, `node --test tests/js/study_chat.test.js`, Ruff/mypy for changed modules. Commit only owned files after O reviews shared patches.

## C2: AMBOSS reference capability and manual AnKing candidate import

**Dependencies:** C1; actual MCP activation additionally needs the access evidence in the research note. Q4 exact local card resolution can be consumed by the candidate UI; no dependency on full Anki curation.

**Files:** Create `src/oms_hub/study_chat/amboss.py`, `tests/study_chat/test_amboss.py`; modify C1's service/template. Q owns candidate-ID parsing/resolution; C adds the AMBOSS provenance label and entry point to that result view.

**Interfaces:** Define a deliberately small boundary for the two concrete retrieval states, not a plugin framework:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ReferencePassage:
    source_id: str
    title: str
    url: str
    text: str

class AmbossUnavailable(RuntimeError):
    pass

class AmbossReference:
    def search(self, question: str) -> tuple[ReferencePassage, ...]:
        raise AmbossUnavailable('AMBOSS access has not been configured')
```

- [ ] Add the default unavailable test and run `python -m pytest tests/study_chat/test_amboss.py -q` before adding the boundary:

```python
import pytest
from oms_hub.study_chat.amboss import AmbossReference, AmbossUnavailable

def test_reference_access_is_not_assumed():
    with pytest.raises(AmbossUnavailable):
        AmbossReference().search('Explain this mechanism')
```

- [ ] Implement the unavailable state, official AMBOSS link and a clear message that this answer has not used AMBOSS. Never silently answer with general GPT under a reference badge.
- [ ] Read `docs/implementation/2026-09-11-amboss-access-research.md` and the recorded Gmail inquiry. O may inspect a reply in the same email thread when continuing this integration; no automatic resend, follow-up email, subscription purchase or credential search.
- [ ] If access documentation arrives, record endpoint/transport, registration, auth lifecycle, pricing/limits, permitted external-model processing and retention, and actual tool schemas. Add an access-specific amendment to this task from that evidence before writing the real wire adapter. If it does not arrive, mark **C2-live external pending** and continue all other tasks; unavailable UI is a completed deliverable, live integration is not.
- [ ] For a documented implementation, test supplied schemas with fixture responses before one separately authorized live query. Validate returned article links and enforce documented content/cache limits. Keep credentials in existing secret storage or documented OAuth storage, not chat/DB logs. Keep reference citations and lecture citations distinguishable.
- [ ] Route manual AMBOSS query/NID exports to Q's exact observed-identifier validation. A pasted query is parsed as data with an allowlist; never execute arbitrary Anki search commands or model-provided code. Report unmatched NIDs and cloze sibling counts. Add no real MCP card-matching call unless its tool exists in supplied documentation.
- [ ] Run C2 fixture/unavailable tests and C1 regressions; commit the local capability. O records optional live state separately.

## C3: Authenticated personal attempts and versioned progress

**Dependencies:** Q3 repository/`AttemptFact`; O shared schema approval. Can develop using Q contract fixtures before bank export access.

**Files:** Create `src/oms_hub/study_progress/{__init__,service,routes}.py`, `tests/study_progress/test_service.py`, `test_routes.py`, `src/oms_hub/web/templates/study_progress.html`; O integrates `web/public_quiz_routes.py`, `web/static/public_quiz.js` and associated JS tests through serialized patches.

**Interfaces:** Import Q's `QuestionKey`, `TopicLabel`, `AttemptFact`, `Result` and `BankRepository`. Use `BankRepository.record_attempt(...)` and `iter_attempts(learner_id=..., after_id=..., limit=...)` exactly as defined in Q3. For Hub events use `source='study_hub'`, `product='lecture_quiz'`, and immutable version-qualified question identity. Importer cursor order is not chronological test order.

```python
from dataclasses import dataclass
from oms_hub.question_bank.contracts import AttemptFact

@dataclass(frozen=True)
class ProgressSummary:
    graded: int
    correct: int
    incorrect: int
    omitted: int
    unknown: int
    repeated: int

# summarize(attempts: tuple[AttemptFact,...], *, first_only: bool) -> ProgressSummary
# tests use actual Q models and database fixture, no duplicate DTO definitions
```

- [ ] Add a failing route regression: two submissions with the same owner/session/question version/attempt ID produce one record; same key with a conflicting result produces HTTP 409. An anonymous submission remains graded through the existing endpoint and creates no personal record.
- [ ] Create an authenticated study-session identity and server-generated attempt identifier; grade on the server using existing native grading, ignoring client-provided `correct`. Preserve immutable quiz revision/content hash and selected answer. Model output cannot write performance. Existing browser-only records are never silently migrated into trusted history.
- [ ] Use Q3's atomic idempotent record method after grading; unknown or omitted provider results remain distinct. Reject negative durations and invalid dates. Missing attempt time stays missing. For first-attempt ordering use known occurrence time, then source-stable attempt identity; ambiguous ordering is reported, never interpreted as known first performance.
- [ ] Implement `summarize` with counts only; percentage denominator is graded correct+incorrect, omitted/unknown reported separately. Separate repeat view and first-attempt view. Deduplicate the overall total by event identity before multi-topic aggregation. Pending/model-only tags are not treated as accepted categories.
- [ ] Add tests using Q's fixture constructors covering known incorrect+correct repeats, unknown dates, omitted/unknown results, accepted versus pending topics, and the same event matching two topics. Expected global count remains one for that event.
- [ ] Add versioned taxonomy metadata with separate `nbome_comlex_level1` and `usmle_step1` namespaces and official source URLs. Import the official blueprint version at implementation time; preserve category identifiers and review the mapping. Never use the backlog's mistaken 'NBME Level 1' wording. Do not claim a validated pass prediction from raw accuracy.
- [ ] Show sample count, source, date coverage, repeated attempts and last activity on the authenticated progress page. Use existing pass tracker dates/resources; do not replace it with model-generated progress.
- [ ] Run `python -m pytest tests/study_progress tests/question_bank tests/study_generation -q`, relevant public-player JS tests, Ruff/mypy. Commit owned changes after O's route/schema integration.

## C4: Custom/cumulative blocks and objective-tag review

**Dependencies:** C3 and Q1/Q3; B5 native publication validation. Bank content unavailable does not block blocks made from accepted lecture questions.

**Files:** Create `src/oms_hub/study_progress/blocks.py`, `tests/study_progress/test_blocks.py`; modify C3 routes/template and Q's topic-review surface through Q-owned patches.

**Interfaces:** A block selects references to existing versioned questions; it does not manufacture replacement questions. Use Q3's `BankRepository.list_ready_questions(...)`, `get_question(key)` and separate `set_topics(key, topics, expected_content_hash=...)` contract; do not invent another bank store. C resolves accepted published-native questions through the existing `GenerationRepository` and projects the same immutable question/version keys. Q results-only rows never become playable candidates. Taxonomy review changes metadata via the hash-checked method, not by mutating imported attempts or source text.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class BlockCandidate:
    key: str
    graded_attempts: int
    accuracy: float | None

def select_block(candidates: tuple[BlockCandidate, ...], *, count: int) -> tuple[str, ...]:
    if not 1 <= count <= 500:
        raise ValueError('count must be between 1 and 500')
    unique = {c.key: c for c in candidates}
    ordered = sorted(unique.values(), key=lambda c: (
        c.graded_attempts > 0,
        c.accuracy if c.accuracy is not None else -1.0,
        c.graded_attempts,
        c.key,
    ))
    return tuple(c.key for c in ordered[:count])
```

- [ ] Add the following test and run `python -m pytest tests/study_progress/test_blocks.py -q` to observe failure before implementing the small selector:

```python
from oms_hub.study_progress.blocks import BlockCandidate, select_block

def test_underpracticed_then_weak_and_no_duplicates():
    unseen = BlockCandidate('unseen', 0, None)
    weak = BlockCandidate('weak', 4, 0.25)
    strong = BlockCandidate('strong', 4, 1.0)
    assert select_block((strong, weak, unseen, weak), count=3) == ('unseen', 'weak', 'strong')
```

- [ ] Resolve candidate references server-side from accepted, accessible, complete source records and review-approved topic filters. Exclude current-block duplicates, unresolved answers/media, deleted revisions and records without licensed full content. Results-only entries can suggest a vendor QID block, not appear as playable questions.
- [ ] Implement the selector above with constructor validation for nonnegative attempts and accuracy in [0,1] or None; conflicting duplicate keys must be rejected upstream instead of silently choosing contradictory metadata. Include sample counts in UI; deterministic selection is a simple study heuristic, not psychometric calibration.
- [ ] Support course/exam/topic filters and cumulative selection across explicitly chosen exams; use existing player/native schema and preserve answer-hidden metadata behavior. Store selected versioned identities so resuming a block cannot change its questions.
- [ ] Add optional model tag suggestions using B's client only when actual text/objective evidence exists. Freeze taxonomy choices in the request, reject invented IDs, record evidence/method/confidence and require user review before analytics uses them. Bare QIDs use observed exact mappings or remain unclassified.
- [ ] Test candidate filtering, too-large/zero counts, conflicting duplicates, version changes, missing media, and source labeling. Run C/Q focused suites and public-player regressions. Commit owned files and hand O the acceptance receipt.

## C acceptance and deferrals

Local release requires lecture/general chat with scoped evidence, unavailable AMBOSS behavior, owner-only progress, correct duplicate/repeat handling, and accepted-question blocks. Actual AMBOSS API, actual vendor export parsing, and live Anki mutation have separate evidence/status. O does not delay the first lecture-quiz release for C3/C4. Preserve prior UX changes: home resume, lecture search, navigation context, answer metadata, pass counts, source selection, mobile layout and authentication.
