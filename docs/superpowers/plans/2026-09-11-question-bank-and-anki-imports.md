# Question Bank and Anki Imports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import supported UWorld/TrueLearn results, QIDs, and user notes into a source-neutral study bank, reuse existing AnKing tag mappings, and expose trustworthy attempt facts for analytics.

**Architecture:** A normalized JSON contract is the first working import format; vendor-specific formats become supported only after inspecting a user-supplied export. Retain external identity, provenance, topic mappings, and attempts independently of question text. Resolve local Anki note/card candidates from existing tags without changing the collection or invoking full curation.

**Tech Stack:** Existing Python, Pydantic, SQLAlchemy/SQLite, FastAPI, Jinja, pytest, AnkiIndex; standard-library JSON/hashlib. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-11-gpt-study-platform-design.md`

## Global Constraints

- The present authoring task delivers this plan only. After implementation kickoff, O authorizes bounded implementation, tests, and local commits for these tasks; workers do not repeatedly ask for local commit approval. Push, deployment, account access, and live Anki/provider actions remain outside that kickoff unless explicitly included.
- GPT is the approved core; import parsing, deduplication, and exact tag resolution are deterministic and require no model call.
- User has UWorld and TrueLearn accounts. This is not authorization to read credentials, automate a logged-in account, scrape questions, or ingest an unverified dump.
- Full Anki curation is deferred. This phase includes manual AMBOSS candidate-NID/query intake and read-only candidate review for C2; it does not automate AMBOSS or select/mutate Anki cards.
- Worker Q owns new `src/oms_hub/question_bank/` files and its tests/template. Root orchestrator owns all shared `models.py`, `migrations.py`, and `app.py` changes. Do not have parallel workers edit those shared files.
- Preserve the running Study Hub on port 8765 and all unrelated working-tree changes. At implementation start recheck the spec, Git state, and repository instructions.
- Imported full question bodies need a supplied supported export and verified authorized provenance; a result/QID export must never be advertised as a full question-bank export.
- The separately inspected GitHub dataset remains held: 3,671 records, 149 `correctIndex == -1`, 94 missing referenced images, and no verified UWorld IDs. These are supplied audit findings, not a fresh audit by this plan. Neither its row labels nor array offsets establish vendor identity.

## Verified reuse and source evidence

Repository inspection on 2026-09-11:

- `src/oms_hub/study_generation/native_quiz.py`: `parse_native_quiz(raw: str) -> NativeQuiz`, existing choice/rationale/image contract and public-content separation; tests include the 500-question import ceiling. Do not weaken it to admit unknown answers or missing images.
- `src/oms_hub/study_generation/repository.py`: existing publication and media readiness paths; do not create a second publishing endpoint that bypasses these checks.
- `src/oms_hub/anki/index.py`: `AnkiIndex.list_notes() -> tuple[NormalizedNote, ...]`, `get_note(note_id: int)`, `search_tag(prefix: str)`, and `refresh_from_anki(...)`. `search_tag` deliberately matches tag hierarchy prefixes; QID identity must use an exact complete observed tag, not that prefix search alone.
- `src/oms_hub/anki/normalize.py`: `NormalizedNote` already holds raw fields, exact tags, note IDs, all associated card IDs, and media. `normalize_html` is suitable for display/search text, never for reconstituting cloze cards.
- `src/oms_hub/anki/ankiconnect.py`: existing read-only note/card methods; do not call sync, tag changes, note creation, or media writes in this phase. `refresh_from_anki` is unnecessary for offline import tests; use an existing index/supplied fixture.
- No dedicated UWorld QID adapter was found in `src/` or `tests/`. Reuse the existing external add-on workflow and observed tags instead of claiming this repository already implements that adapter.

Official sources checked on 2026-09-11:

- AnkiHub describes the UWorld QID-to-Anki search add-on on its [Step Deck page](https://www.ankihub.net/step-deck?lang=en). It supplies an existing user workflow; it does not establish a vendor question-content export API.
- AnkiHub documents an [AMBOSS-to-Anki question-ID feature](https://community.ankihub.net/t/how-to-use-the-amboss-to-anki-question-id-feature/352829). Reuse rather than replace that pathway when the deferred phase is authorized.
- [Official AnKing Update #31, August 20, 2026](https://community.ankihub.net/t/anking-step-deck-update-31-41k-updates-new-textbook-tags-new-cards-abim-more/607685) reports TrueLearn Step 2 tags. The [official update log](https://community.ankihub.net/t/anking-step-deck-update-log/166499) also reports TrueLearn COMAT surgery tags. Therefore TrueLearn mapping is supported where tags actually exist; comprehensive coverage and a particular local tag syntax are unverified.

## File map and frozen interfaces

Create:

- `src/oms_hub/question_bank/__init__.py`: package marker only.
- `src/oms_hub/question_bank/contracts.py`: strict external inputs and immutable output records.
- `src/oms_hub/question_bank/imports.py`: normalized parsing, preview digest, full-content readiness.
- `src/oms_hub/question_bank/repository.py`: atomic import and source-neutral queries.
- `src/oms_hub/question_bank/anki_links.py`: exact observed-tag candidate links, offline only.
- `src/oms_hub/question_bank/routes.py`: authenticated local preview/confirm/list views, following existing route conventions.
- `src/oms_hub/web/templates/question_bank_import.html`: native upload, review, confirmation, import report.
- `tests/question_bank/test_imports.py`, `test_repository.py`, `test_anki_links.py`, `test_routes.py`: focused test cycles below.
- `docs/question-bank-import-format.md`: normalized contract and truthful source support matrix.

Root modifies shared `src/oms_hub/models.py`, `src/oms_hub/migrations.py`, `src/oms_hub/app.py` only after agreeing the contract below. Root chooses the next migration version from current checkout, not a guessed number in this plan.

External wire format, version 1 (this is a Study Hub format, not a claim about vendor export fields):

```json
{
  "schema_version": 1,
  "source": "uworld",
  "product": "step1",
  "export_id": "user-block-2026-09-11-a",
  "provenance": {"kind": "user_results", "description": "User-transcribed result and own note"},
  "rows": [{
    "question_id": "00123",
    "attempt_id": "block-a-attempt-1",
    "result": "incorrect",
    "occurred_at": "2026-09-11T15:00:00Z",
    "elapsed_ms": 90000,
    "user_note": "Review coagulation pathway",
    "tags": ["Heme"],
    "topics": [{"axis": "system", "label": "Heme", "canonical_id": null,
                "method": "unmapped", "confidence": null, "review_state": "pending"}],
    "question": null
  }]
}
```

`contracts.py` defines the following Pydantic models with `extra='forbid'`, strict strings (no numeric coercion), and immutable domain outputs. Names below are the required interface, not existing APIs:

```python
Source = Literal['uworld', 'truelearn', 'amboss', 'study_hub', 'user']
Result = Literal['correct', 'incorrect', 'omitted', 'unknown']
Axis = Literal['system', 'discipline', 'topic', 'exam', 'course', 'lecture', 'objective']

class QuestionKey(BaseModel):
    source: Source
    product: str
    question_id: str

class TopicLabel(BaseModel):
    axis: Axis
    label: str
    canonical_id: str | None = None
    method: Literal['unmapped', 'exact', 'user', 'model'] = 'unmapped'
    confidence: float | None = None
    review_state: Literal['pending', 'accepted', 'rejected'] = 'pending'

class ImportRow(BaseModel):
    question_id: str
    attempt_id: str | None = None
    result: Result = 'unknown'
    occurred_at: datetime | None = None
    elapsed_ms: int | None = None
    user_note: str = ''
    tags: tuple[str, ...] = ()
    topics: tuple[TopicLabel, ...] = ()
    question: dict[str, object] | None = None

class Provenance(BaseModel):
    kind: Literal['user_results', 'user_notes', 'authorized_question_export']
    description: str

class ImportEnvelope(BaseModel):
    schema_version: Literal[1]
    source: Source
    product: str
    export_id: str
    provenance: Provenance
    rows: tuple[ImportRow, ...]

@dataclass(frozen=True)
class RowIssue:
    row: int  # one-based original row number
    code: str
    detail: str

@dataclass(frozen=True)
class ImportPreview:
    digest: str
    envelope: ImportEnvelope
    issues: tuple[RowIssue, ...]
    ready_question_rows: tuple[int, ...]

@dataclass(frozen=True)
class ImportReceipt:
    import_id: str
    inserted_questions: int
    inserted_attempts: int
    duplicate_rows: int
    conflicts: tuple[RowIssue, ...]

@dataclass(frozen=True)
class AttemptFact:
    id: int
    learner_id: str
    key: QuestionKey
    attempt_id: str
    result: Result
    occurred_at: datetime | None
    elapsed_ms: int | None
    imported_at: datetime
    import_id: str
    topics: tuple[TopicLabel, ...]
```

Limits: 10 MiB upload; 1–10,000 rows; external IDs 1–200 characters; product 1–100; user note 0–20,000; tag/label 1–300; nonnegative integer elapsed time; timezone-aware timestamps only; finite confidence 0–1 when present. Reject blank IDs, surrounding whitespace in identity fields, unknown keys, non-object rows, NaN/Infinity, and malformed timestamps. Preserve case and leading zeros. `attempt_id=None` permits QID/note imports only: require result `unknown`, timestamp and elapsed time absent. Do not synthesize attempts from QIDs or infer correctness from missing values. Identity duplicates within a file collapse only if the complete canonical row matches; contradictory duplicate content produces a blocking conflict.

A `question` contains exactly one existing native-quiz question object. Validate using `parse_native_quiz(json.dumps({'title': 'Imported question', 'questions': [row.question]}))`; retain the external key separately from generated native IDs. `authorized_question_export` provenance is required for non-null question bodies. Parseable image-dependent content is held until the existing media readiness path succeeds; `correct_index=-1` is held, never guessed. The metadata/results import can succeed while full question content remains held, and the UI must show both statuses separately.

## Task Q1: Build normalized preview and validation

**Files:** Create contracts/imports module, format documentation, and `tests/question_bank/test_imports.py`.

**Interfaces:** `parse_import(raw: bytes) -> ImportEnvelope`; `preview_import(raw: bytes) -> ImportPreview`. Validation failures raise `ValueError` before any write; row-level question readiness problems become `RowIssue`, while contradictory duplicate identity is blocking at commit.

- [ ] Write the following test using the envelope above as `tests/question_bank/fixtures/normalized-v1.json` (synthetic, user-owned text only):

```python
def test_exact_ids_and_unknown_results():
    raw = Path('tests/question_bank/fixtures/normalized-v1.json').read_bytes()
    payload = json.loads(raw)
    payload['rows'][0].update(attempt_id=None, result='unknown', occurred_at=None, elapsed_ms=None)
    parsed = parse_import(json.dumps(payload).encode())
    assert parsed.rows[0].question_id == '00123'
    assert parsed.rows[0].result == 'unknown'
    payload['rows'][0]['question_id'] = 123
    with pytest.raises(ValueError):
        parse_import(json.dumps(payload).encode())
```

- [ ] Run `uv run pytest tests/question_bank/test_imports.py -q`; confirm a missing-module failure, then implement strict models and one parser:

```python
def parse_import(raw: bytes) -> ImportEnvelope:
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError('Import exceeds 10 MiB')
    return ImportEnvelope.model_validate_json(raw)
```

- [ ] Add model validators for the exact limits and cross-field invariants above; canonical preview digest is SHA-256 of sorted-key compact JSON from `model_dump(mode='json')`. Enumerate original rows before deduplication so issues always identify the submitted row. Validate full content through the existing parser; do not publish during preview.
- [ ] Add cases for numeric IDs, ambiguous timestamps, duplicate conflicts, unknown outcome, negative elapsed time, invalid correct index, missing image, and row/upload limits; assert preview never touches repository/provider/Anki.
- [ ] Run `uv run pytest tests/question_bank/test_imports.py tests/study_generation/test_native_quiz.py -q`. Review the diff and create the task-scoped local commit under O kickoff authorization.

## Task Q2: Inspect actual export samples before vendor-specific adapters

**Files:** Update `docs/question-bank-import-format.md`; add only supplied sanitized fixtures under `tests/question_bank/fixtures/`; modify `imports.py` and `test_imports.py` only if a real supported layout is established.

**Interfaces:** Normalized contract from Task 1 remains the sole guaranteed parser. A new adapter, when warranted, must return `ImportEnvelope` and is selected by an explicitly registered `format_id`, never by guessed vendor branding or a file extension alone.

- [ ] Obtain one user-supplied UWorld export and one TrueLearn export or user-made results/QID/notes file. Until samples arrive, ship the normalized format and mark vendor export adapters as unavailable; this is an external dependency, not permission to log in.
- [ ] Record the exact export UI/workflow, product, file extension, column/JSON paths, question vs result content, timezone, repeated-attempt identifiers, tag availability, image packaging, and provenance. Do not copy account identifiers into fixtures.
- [ ] Classify each sample as `results`, `qids`, `user_notes`, or `full_questions`. If it lacks stable attempt identity, import QID/notes only unless the user supplies a stable block/attempt key. If it is an aggregate report, do not invent per-question rows.
- [ ] Write a fixture-specific regression before implementing any adapter:

```python
# Run only after the UWorld sample has supplied the actual format registration.
def assert_sample_contract(adapter, sample_bytes, expected_qids):
    result = adapter(sample_bytes)
    assert isinstance(result, ImportEnvelope)
    assert [row.question_id for row in result.rows] == expected_qids
    assert all(isinstance(row.question_id, str) for row in result.rows)
```

The adapter name, exact sample paths, actual expected QIDs, and field mapping must be added to this task as a reviewed plan amendment after inspection. This task does not authorize an invented proprietary parser. A sample rejection with a clear supported-format explanation is a successful bounded inspection result.
- [ ] Run the amended sample tests and `uv run pytest tests/question_bank/test_imports.py -q`; update support matrix with verified scope and limitations, then request no account access.

## Task Q3: Persist questions and attempts without duplicate inflation

**Files:** Worker Q creates `repository.py`, `tests/question_bank/test_repository.py`; root adds models/migration.

**Interfaces:** `BankRepository(session_factory)` where `session_factory` follows the existing repository convention; `commit_import(preview: ImportPreview, *, learner_id: str, expected_digest: str) -> ImportReceipt`; `iter_attempts(*, learner_id: str, after_id: int = 0, limit: int = 500) -> tuple[AttemptFact, ...]`.

Question enumeration and reviewed-topic persistence, consumed by C4:

```python
@dataclass(frozen=True)
class BankQuestion:
    key: QuestionKey
    content_hash: str
    native_question: dict[str, object]
    topics: tuple[TopicLabel, ...]

# BankRepository methods:
def get_question(self, key: QuestionKey) -> BankQuestion | None: ...
def list_ready_questions(
    self, *, learner_id: str, course: str | None = None,
    exam: str | None = None, topic_ids: tuple[str, ...] = (),
) -> tuple[BankQuestion, ...]: ...
def set_topics(
    self, key: QuestionKey, topics: tuple[TopicLabel, ...],
    expected_content_hash: str,
) -> None: ...
```

`get_question` returns `None` for absent or held/missing full content. `list_ready_questions` includes only content available through this learner's imports with complete validated content/media readiness; filters match accepted canonical mappings for the course/exam/topic axes, never raw unreviewed labels. An empty result is `()`. Question identity is the original source/product/string QID, and `content_hash` is its immutable content version. Do not substitute the review run's `qN` identifier. C supplies native published-quiz candidates separately.

`set_topics` is a trusted repository operation behind C's authorized topic-review workflow, not a generic external import update endpoint. Persist the complete reviewed topic set separately from immutable raw import labels, with previous/new topic JSON, reviewer context supplied by the request layer, and review time in audit metadata. Require the current non-held question hash to equal `expected_content_hash` atomically; otherwise raise `ValueError` and change nothing. New imports may propose mappings but cannot overwrite accepted mappings. Only accepted mappings feed analytics; rejected and pending mapping history remains visible. This isolated metadata operation does not rewrite question content or attempts.

- [ ] Add focused Q3 enumeration and stale-edit regression using the existing repository fixture:

```python
def test_held_questions_and_stale_topic_edits(bank_repo, held_key, ready_key):
    assert bank_repo.get_question(held_key) is None
    assert held_key not in {q.key for q in bank_repo.list_ready_questions(learner_id='test')}
    question = bank_repo.get_question(ready_key)
    assert question is not None
    accepted = (TopicLabel(axis='topic', label='Coagulation', canonical_id='topic:coagulation',
                           method='user', confidence=None, review_state='accepted'),)
    with pytest.raises(ValueError):
        bank_repo.set_topics(ready_key, accepted, expected_content_hash='0' * 64)
    assert bank_repo.get_question(ready_key).topics == question.topics
    bank_repo.set_topics(ready_key, accepted, expected_content_hash=question.content_hash)
    assert bank_repo.get_question(ready_key).topics == accepted
    assert bank_repo.list_ready_questions(learner_id='other') == ()
```

Native authenticated player contract, also owned by Q and consumed by root C:

```python
def record_attempt(
    self, *, learner_id: str, key: QuestionKey, attempt_id: str,
    result: Result, occurred_at: datetime | None,
    elapsed_ms: int | None, topics: tuple[TopicLabel, ...] = (),
) -> AttemptFact: ...
```

Use `source='study_hub'`, `product='lecture_quiz'`, and an immutable revision-qualified question ID supplied by the existing quiz owner; never a mutable array position or naked `q1`. `record_attempt` creates a metadata-only question identity if needed, an internal provenance record (`kind='study_hub_attempt'`, separate from the external `Provenance` input model), and the attempt atomically. No full question import is required. Reusing the same learner/question/attempt ID with identical content returns the original fact; a different result/time/content raises `ValueError`. Root C supplies server-graded outcomes, server-associated learner identity, and a stable per-answer-event ID. Root C never trusts a browser-supplied correctness boolean. Anonymous public playback remains separate until the parent spec defines authentication.

Shared-table contract for root:

| Table | Required identity and content |
|---|---|
| `bank_imports` | UUID string PK, learner ID, source/product, export ID, normalized digest, provenance JSON, imported-at; unique learner/source/product/export ID. Same identity/different digest is a conflict. |
| `bank_questions` | Integer PK; source/product/external question ID unique as exact strings; optional validated native question JSON and content hash; body readiness separate from existence. |
| `bank_import_rows` | Import FK, original row number, question FK, canonical row hash, user note, raw tags, topics JSON, row readiness/issues; unique import/row. User notes belong to learner/import provenance, not shared vendor question content. |
| `bank_attempts` | Monotonic integer PK, learner/question FK, external attempt ID, result, occurred-at nullable, elapsed-ms nullable, imported-at, import-row FK; unique learner/question/external attempt ID. |

No physical row deletion/update through this import API; the separately reviewed `set_topics` operation above changes only canonical mapping metadata. Reimporting an identical export returns the original receipt; unchanged attempts from another export count as duplicates, new attempt IDs append. Changed outcome/time for the same attempt ID or differing non-null question bodies under the same source key produce a conflict requiring a future explicit correction workflow. A whole transaction rolls back on conflicts; no partial hidden success. Identical question bodies across different source IDs stay separate; content hash is not vendor identity.

- [ ] Use the existing SQLite/session setup pattern from `tests/study_generation/test_migration.py` to define a temporary migrated database fixture named `bank_repo`. Write:

```python
def test_reimport_does_not_invent_attempts(bank_repo):
    raw = Path('tests/question_bank/fixtures/normalized-v1.json').read_bytes()
    preview = preview_import(raw)
    first = bank_repo.commit_import(preview, learner_id='test', expected_digest=preview.digest)
    second = bank_repo.commit_import(preview, learner_id='test', expected_digest=preview.digest)
    assert second.import_id == first.import_id
    facts = bank_repo.iter_attempts(learner_id='test')
    assert len(facts) == 1
    assert facts[0].key.question_id == '00123'
    assert facts[0].result == 'incorrect'
```

- [ ] Run `uv run pytest tests/question_bank/test_repository.py -q`; confirm failure. Have root add only agreed tables using current migration style; verify upgrading a database containing published quizzes preserves those rows.
- [ ] Implement one transaction with unique constraints as the final race guard; recompute digest before commit, verify `expected_digest`, validate learner ownership, compare canonical immutable payloads before treating collisions as duplicates. Return a conflict receipt only after rollback, never insert a subset.
- [ ] Add a native write regression:

```python
def test_native_attempt_without_question_body(bank_repo):
    key = QuestionKey(source='study_hub', product='lecture_quiz', question_id='revision-7:q1')
    kwargs = dict(learner_id='test', key=key, attempt_id='answer-event-1',
                  result='correct', occurred_at=None, elapsed_ms=1500)
    first = bank_repo.record_attempt(**kwargs)
    assert bank_repo.record_attempt(**kwargs).id == first.id
    assert bank_repo.iter_attempts(learner_id='test') == (first,)
    with pytest.raises(ValueError):
        bank_repo.record_attempt(**(kwargs | {'result': 'incorrect'}))
```

- [ ] Add tests for two distinct attempts on one question, equal IDs across UWorld/TrueLearn and products, omitted/unknown distinct from incorrect, changed-result collision, duplicate file rows, concurrent duplicate commit, rollback after an insert failure, and cross-learner isolation.
- [ ] Implement ordered keyset analytics reads using `id > after_id`, ascending ID, limit 1–500. Emit raw topic labels plus their mapping/review metadata; include unknown outcomes and null timestamps, and never replace event time with import time.
- [ ] Run `uv run pytest tests/question_bank/test_repository.py tests/study_generation/test_migration.py -q`; review root integration diff before completion.

## Task Q4: Reuse exact tags and preserve Anki note/card identity

**Files:** Create `anki_links.py`, `tests/question_bank/test_anki_links.py`; no Anki apply or collection mutation.

**Interfaces:**

```python
@dataclass(frozen=True)
class TagRule:
    source: Source
    product: str
    prefix: str  # exact observed complete parent, including final '::'

@dataclass(frozen=True)
class AnkiCandidate:
    key: QuestionKey
    note_id: str
    card_ids: tuple[str, ...]
    evidence_tag: str
    confidence: Literal['exact_tag']
    review_state: Literal['pending'] = 'pending'


def match_qid_tags(
    keys: Sequence[QuestionKey],
    notes: Sequence[NormalizedNote],
    rules: Sequence[TagRule],
) -> tuple[AnkiCandidate, ...]: ...
```

- [ ] Write an offline one-note fixture with QID tag `fixture::uworld::step1::00123`, note ID 101, card IDs 201/202, and raw cloze text containing c1 and c2. Write the exact assertions:

```python
def test_qid_is_exact_and_all_cloze_cards_remain(note):
    key = QuestionKey(source='uworld', product='step1', question_id='00123')
    rules = [TagRule('uworld', 'step1', 'fixture::uworld::step1::')]
    links = match_qid_tags([key], [note], rules)
    assert [(x.note_id, x.card_ids) for x in links] == [('101', ('201', '202'))]
    assert links[0].confidence == 'exact_tag'
    assert links[0].review_state == 'pending'
    assert match_qid_tags([key.model_copy(update={'question_id': '123'})], [note], rules) == ()
```

- [ ] Run `uv run pytest tests/question_bank/test_anki_links.py -q`; confirm failure. Implement equality against `rule.prefix + key.question_id` in the note's stored tag set; no lowercase, integer conversion, substring, FTS, semantic search, or topic inference for exact identity.
- [ ] Resolve one QID to multiple notes and all cards per note, deduplicate only identical key/note/tag evidence, sort stable output, and retain unmatched QIDs in the UI. Require source/product-qualified rules; conflicting rules are rejected. Observe actual local tag prefixes in a separately authorized existing-index review before enabling real UWorld/TrueLearn rules. Do not hardcode a claimed universal `#AK_*` version path.
- [ ] Derive ordinary topic labels from existing metadata as proposals, using independent axis/label values. Only explicit reviewed lookup entries or user confirmation set `canonical_id` and `review_state='accepted'`; exact QID identity confidence does not imply topic certainty or sufficient card coverage.
- [ ] Test tags `00123` vs `001230`, product isolation, duplicate evidence, missing notes/cards, stale snapshot labeling, multi-note matches, and retained raw cloze fields. Call no `refresh_from_anki`, sync, tag writes, suspend/unsuspend, move, reschedule, or note update method. Existing index use guarantees schedule preservation for this phase.
- [ ] Run `uv run pytest tests/question_bank/test_anki_links.py tests/anki/test_index.py tests/anki/test_companion_index.py -q`; record fixture proof separately from real-profile acceptance.

### Q4 candidate-query intake used by C2

The [Anki Manual](https://docs.ankiweb.net/searching) documents `nid:123` and OR composition. The [official parser and its tests](https://github.com/ankitects/anki/blob/main/rslib/src/search/parser.rs) also accept comma-separated note IDs (`check_id_list`, `nid:1237123712,2,3` test), although the manual does not document this list form. Checked 2026-09-11. Support only this bounded subset; never forward arbitrary search text to Anki.

**Additional interfaces in `anki_links.py`:**

```python
@dataclass(frozen=True)
class CandidateNotePreview:
    note_id: str
    card_ids: tuple[str, ...]
    text: str
    missing: bool
    provenance: Literal['user_pasted_amboss_candidates']
    review_state: Literal['pending'] = 'pending'


def parse_candidate_query(raw: str) -> tuple[int, ...]: ...
def preview_candidate_notes(
    raw: str, index: AnkiIndex,
) -> tuple[CandidateNotePreview, ...]: ...
```

- [ ] Add tests before implementing the parser:

```python
def test_candidate_query_is_not_general_anki_search():
    assert parse_candidate_query('nid:123,456 OR nid:123') == (123, 456)
    assert parse_candidate_query('nid:123 or nid:456') == (123, 456)
    for raw in ('tag:AMBOSS', 'nid:123 OR deck:*', '-nid:123',
                'nid:123 AND nid:456', 'nid:123,', 'nid:0', '(nid:123)', ''):
        with pytest.raises(ValueError):
            parse_candidate_query(raw)
```

- [ ] Implement with standard `re.fullmatch` against `nid:[0-9]+(?:,[0-9]+)*(?:[ \t]+(?i:or)[ \t]+nid:[0-9]+(?:,[0-9]+)*)*` after outer whitespace trim. Bound input to 64 KiB, total IDs to 2,000, each ID to 1–9223372036854775807. Split only after the whole expression matches, deduplicate in first-seen order, and never evaluate the string. Arbitrary fields, parentheses, implicit AND, quotes, negation, newlines within input, and other operators fail as a whole.
- [ ] Resolve every parsed ID with `index.get_note(note_id)`. Emit missing IDs explicitly with empty card/text values. Existing notes expose all card IDs and normalized preview text without rewriting raw cloze fields. The returned provenance means the user pasted candidates associated with AMBOSS, not that the app verified an AMBOSS question-to-note match. C2 attaches these previews to its source-bound candidate review; an NID alone never creates a vendor QID or a clinical topic.
- [ ] Run `uv run pytest tests/question_bank/test_anki_links.py -q`; include missing-NID and multi-cloze preview assertions, index snapshot provenance, and no AnkiConnect calls.

## Task Q5: Reviewable import UI and source-neutral analytics boundary

**Files:** Create `routes.py`, template, `tests/question_bank/test_routes.py`; root wires router/dependencies in `app.py`. Avoid modifying shared CSS unless an existing component demonstrably cannot express the form.

**Interfaces:** `create_question_bank_router(repository: BankRepository) -> APIRouter`; `POST /question-bank/imports/preview` accepts bounded multipart `file`; `POST /question-bank/imports/confirm` accepts the same file plus `digest`; `GET /question-bank/imports/{import_id}` shows receipt scoped to current learner. Resolve learner identity using the parent platform identity decision, never a trusted arbitrary form field. No new public analytics API is required: Task 3's repository iterator is the consumer boundary.

- [ ] Add a route test using existing app/client test conventions. Submit the synthetic fixture, verify preview returns review counts with no database writes; confirm the displayed digest once, then repeat and assert one attempt:

```python
assert before_count == after_preview_count == 0
assert confirm_response.status_code in (200, 303)
assert len(bank_repo.iter_attempts(learner_id='test')) == 1
assert len(bank_repo.iter_attempts(learner_id='other')) == 0
```

- [ ] Run `uv run pytest tests/question_bank/test_routes.py -q`; confirm missing-route failure. Implement upload length bound while reading, standard existing request protection, escaped template output, digest-bound confirmation, conflict HTTP 409, malformed input 422, oversized input 413. No raw user text enters HTML without escaping.
- [ ] Show source/product/provenance, original row counts, unique QIDs, attempts, unknown outcomes, held bodies/images, duplicate/conflict counts, unmatched Anki QIDs, and pending topic mappings. Use the actual import outcome: “Results imported” does not say “Questions ready.”
- [ ] Add tampered-digest, foreign-import access, executable HTML in notes/tags, oversized-upload, unknown-format, repeat-submit, and full-question-hold tests. Server validates every action; disabling the button is not an integrity control.
- [ ] Run `uv run pytest tests/question_bank tests/v2/test_public_quiz_routes.py -q`. Open the local development preview only when an isolated test server is authorized; don't restart the main Hub to demonstrate the form.

### Q5 full-question bridge to the existing native review/player

**Files:** Q creates `src/oms_hub/question_bank/native_review.py` and `tests/question_bank/test_native_review.py`. O owns shared changes to `src/oms_hub/study_generation/studio_repository.py`, `practice_review.py` if needed, and shared models/migrations. Q's routes/template show a “Review questions” action only for a confirmed authorized-content import.

**Interfaces:**

```python
@dataclass(frozen=True)
class BankReviewRef:
    run_id: str
    review_url: str
    original_keys: tuple[tuple[str, QuestionKey], ...]  # native qN -> exact provider key

# Q adapter, injected repositories use the existing classes.
def stage_native_review(
    bank: BankRepository, studio: StudioRepository, *, import_id: str,
    learner_id: str, subject: str, exam_number: int, label: str,
) -> BankReviewRef: ...

# O adds this narrow shared repository entry point.
def create_bank_import_review(
    self, *, bank_import_id: str, subject: str, exam_number: int,
    label: str, drafts: tuple[QuestionDraft, ...],
) -> StudioRun: ...
```

- [ ] Add failing adapter regression with a synthetic authorized two-question import (one text-complete and one image-dependent). Assert staging returns `/studio/runs/{run_id}/review`, original provider keys round-trip, the resulting run is `awaiting_review`, and no `PublishedQuizModel` exists. Repeating stage returns the same run; a foreign learner fails. The adapter rejects a QID/results-only import with `ValueError('Import has no authorized question content')`.
- [ ] Implement the adapter for the existing supported single-answer native schema first. Keep the full raw submitted body in bank provenance, including held answer/image records. Convert structurally valid stems/choices to existing `QuestionDraft`: assign local `q1`...`q500`, set `original_identifier` to the source/product/QID display form, preserve the structured key separately, use `PROVIDED_BY_SOURCE` only for a supplied valid answer, set unknown answers to `None`, and retain image requirements. All imports enter with `verification_required=True`, `verified_at=None`; missing answer/rationale/image issues remain explicit blockers. Unparseable bodies stay held in the bank, displayed as excluded rows, never silently dropped. More than 500 reviewable rows requires explicit selection into bounded batches; do not raise the native player limit.
- [ ] O implements `create_bank_import_review` using the existing `queue_import_run` scope/label validation and local-import source bookkeeping, but creates the run directly in `AWAITING_REVIEW`/`REVIEW` with `DIRECT_IMPORT` workflow, persists drafts/source refs and bank question mapping in one transaction, and never exposes a queued/running state. Do not call queue-then-pause: a worker could claim it. The existing `await_import_review`/`save_question_reviews` behavior identifies required stored fields; factor only the necessary in-session write shared by both paths. Enforce a unique bank-import/batch-to-run mapping to make replay safe. Source refs identify the bank import and original row. A protected read-only `BankRepository.review_rows(*, import_id:str, learner_id:str) -> tuple[ImportRow,...]` supplies the adapter and enforces ownership.
- [ ] Add bounded assets via the existing `PracticeReviewService.upload_image(run_id, question_id, original_filename, payload)` workflow. The normalized v1 upload has no implicit remote or archive asset downloader: users attach PNG/JPEG/WebP files through existing review upload controls, with existing byte/type/dimension limits and validated decoder; do not accept SVG/HTML, arbitrary paths, remote URLs, or zip extraction. If an inspected vendor adapter later supplies assets, feed explicitly mapped bytes through this same function and its bounds. Missing assets remain blocked. Original file names are labels only, never destination paths.
- [ ] Preserve full `(source, product, question_id)` mapping in a root-owned `bank_review_questions` join keyed by run ID/local question ID, with bank-question FK and original import-row FK. When an existing reviewed publish action succeeds, its published quiz/run/version mapping lets root C resolve the originating vendor key; do not overwrite it with `q1` or treat the same published answer as both a vendor-import attempt and a native attempt.
- [ ] Link into existing `studio_quiz_review.html` and `PracticeReviewService`; only the existing user-initiated reviewed publish path (`GenerationRepository.publish_reviewed_studio_quiz`) makes questions playable. Never call `NativeQuizPublisher.publish` directly from import. This preserves existing source verification and media readiness before player access.
- [ ] Add the focused acceptance test:

```python
assert run.state.value == 'awaiting_review'
assert run.published_token is None
assert review.question(run.id, 'q2').draft.verification_required
with pytest.raises(ValueError):
    generation_repository.publish_reviewed_studio_quiz(run.id)
assert bank_review_ref.original_keys[0][1].question_id == '00123'
```

Use existing review test helpers to complete source verification and attach the synthetic image, then invoke the existing reviewed publish action and assert its public player loads the quiz. Assert staging itself never publishes and incomplete question bodies cannot publish. Run `uv run pytest tests/question_bank/test_native_review.py tests/study_generation/test_practice_review.py tests/v2/test_public_quiz_routes.py -q`.

## Task Q6: Acceptance and handoff

**Files:** Update `docs/question-bank-import-format.md` with evidence, formats, and limitations. No automatic enabling of account integrations.

- [ ] Run `uv run pytest tests/question_bank tests/study_generation/test_native_quiz.py tests/anki/test_companion_index.py -q` and inspect `git diff --check` plus scoped diff.
- [ ] Reconcile a single synthetic run: submitted rows = accepted original rows + blocked original rows, report duplicates separately, attempt count unchanged after replay, distinct source/product identity preserved, unknown outcomes excluded from correctness denominators by the analytics consumer.
- [ ] Confirm no execution path calls provider APIs, modifies Anki, accesses account credentials, imports the held GitHub dump, or publishes an image-incomplete/answer-incomplete quiz.
- [ ] Record actual vendor-sample acceptance separately. If samples are unavailable, deliver normalized import support with the vendor adapter limitations stated; no fabricated claim of successful UWorld or TrueLearn export import.
- [ ] Hand root the exact new files, shared-model migration contract, test results, and source-neutral `AttemptFact` contract. Full curation and account automation remain separately authorized future work; manual AMBOSS NID intake is covered by Q4.
