# Anki Curation V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one owned Study Hub package that indexes Anki locally, finds lecture concepts missing from the collection, rescues apparent misses against PowerPoint slides and lecture transcripts, generates only source-grounded cards, lets the user edit first-release note tags, and applies approved changes through AnkiConnect with honest recovery states.

**Architecture:** The NUC-hosted Study Hub owns orchestration, storage, source extraction, Voyage embeddings, exact note-level retrieval, LLM judgment, review, and apply coordination. Anki remains the system of record and is accessed only through loopback AnkiConnect on the NUC. The existing semantic add-on and `SBMatthew/sbm_smart_anki` are research references, not dependencies or code sources; the implementation is clean-room and packaged under `oms_hub.anki`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic, NumPy, Voyage `voyage-4-large` embeddings, existing multi-provider LLM layer, python-pptx, httpx, Jinja/vanilla JavaScript, pytest, Ruff, mypy.

**Implementation status (2026-07-30):** Tasks 1–17 are implemented on
`codex/anki-v4-implementation`. Task 18's evaluator, deterministic regression
gold set, first-index command, and NUC runbook are implemented. Automated gates
pass; copied-profile acceptance and the approval-gated deletion of
`oms_anki_agent` remain intentionally pending.

## Global Constraints

- Do not copy code from the unlicensed local add-on or from `SBMatthew/sbm_smart_anki`; use only documented behavior and independently written interfaces.
- Do not add a runtime, import, subprocess, filesystem, or API dependency on either research project.
- Remove AMBOSS from active API/domain behavior. Existing database columns may remain as inert compatibility columns during the migration, but no new code may read them or present them to users.
- Keep AnkiHub and all collection mutation on the NUC. The Mac remains a study client.
- Require `http://127.0.0.1` or `http://localhost` for AnkiConnect; never expose AnkiConnect to the LAN.
- Use Voyage `voyage-4-large`, 1024 dimensions, `input_type="document"` for indexed content and `input_type="query"` for searches.
- Store semantic matrices as float16 in atomic snapshots; convert selected rows/query vectors to float32 for exact cosine scoring.
- Keep note-level retrieval for the first release. Do not introduce ANN, Rust, or 4-bit quantization without measured evidence from the evaluation task.
- Treat generated cards and tag edits as proposals until the user explicitly approves them.
- Preserve source-managed tags; only pipeline-owned and approved optional tags are editable.
- A failed leading sync performs no writes. A failed trailing sync records that local writes exist and stops all further writes.
- Every task must pass its focused tests, Ruff, and mypy before its commit.
- Do not retire `oms_anki_agent` until the final acceptance gate passes against a copied profile.

---

## Delivery Map

### Wave A — Stable foundation

1. Clean the existing test baseline.
2. Introduce V4 domain, persistence, and configuration.
3. Add the owned local Anki gateway and preflight.
4. Add the Voyage embedding boundary.

### Wave B — Owned indexes

5. Build the atomic semantic snapshot store.
6. Add incremental note refresh, exact search, and query caching.
7. Upgrade the companion Anki index and filtering.
8. Build the lecture source extraction and source index.

### Wave C — Curation intelligence

9. Generate and validate the Lecture Concept Ledger.
10. Implement Pass 1 hybrid retrieval.
11. Judge Pass 1 candidates with cacheable structured outputs.
12. Rescue missed topics against slides/transcripts and run Pass 2.
13. Deduplicate results and generate grounded gap cards.

### Wave D — Review and mutation

14. Add tag policy, review changesets, and staleness checks.
15. Build idempotent envelopes and the local apply coordinator.
16. Orchestrate the resumable worker and artifact trail.
17. Ship the non-technical review and recovery UI.

### Wave E — Prove and consolidate

18. Calibrate retrieval, run copied-profile acceptance, and retire the legacy agent.

## Target File Structure

```text
src/oms_hub/anki/
  ankiconnect.py          # owned loopback client
  apply.py                # sync/apply/recovery state machine
  contracts.py            # API and mutation contracts
  dedupe.py               # note/card overlap and redundancy
  domain.py               # curation domain types and states
  envelope.py             # deterministic mutation plans
  gaps.py                 # source-grounded card generation
  index.py                # companion FTS/tag/deck index
  judgment.py             # structured candidate judgment
  lcl.py                  # lecture concept ledger
  normalize.py            # note text/source-family normalization
  pipeline.py             # stage orchestration
  repository.py           # job/review/evidence persistence
  rescue.py               # slide/transcript localization and Pass 2 queries
  runtime.py              # Anki launch/preflight/runtime composition
  source_index.py         # lecture FTS and semantic retrieval
  sources.py              # PPTX/transcript extraction and stable passages
  tag_policy.py           # editable/protected tag rules
  worker.py               # resumable background execution
  semantic/
    __init__.py
    domain.py
    store.py
    service.py
    voyage.py
src/oms_hub/web/
  anki_routes.py
  static/anki.js
  templates/anki.html
  templates/anki_review.html
scripts/
  evaluate-anki-retrieval.py
tests/anki/
  test_ankiconnect.py
  test_apply.py
  test_companion_index.py
  test_contracts.py
  test_dedupe.py
  test_envelope.py
  test_gaps.py
  test_judgment.py
  test_lcl.py
  test_pipeline.py
  test_repository.py
  test_rescue.py
  test_runtime.py
  test_source_index.py
  test_sources.py
  test_tag_policy.py
  test_web.py
  test_worker.py
  semantic/
    test_service.py
    test_store.py
    test_voyage.py
```

---

### Task 1: Make the Existing Anki Test Baseline Clean

**Files:**

- Modify: `src/oms_hub/anki/index.py`
- Modify: `src/oms_hub/anki/snapshot.py`
- Modify: `src/oms_hub/anki/repository.py`
- Modify: any focused test fixture under `tests/anki/` that owns a connection or SQLAlchemy engine
- Test: `tests/anki/`
- Test: `tests/agent/`
- Test: `tests/v2/test_agent_access.py`

**Interfaces:** No public interface change. This task establishes the zero-warning baseline required by all later tasks.

- [ ] Run the current suite with allocation traces and preserve the failing output in the task notes:

```bash
PYTHONTRACEMALLOC=1 uv run pytest tests/anki tests/agent tests/v2/test_agent_access.py -q
```

Expected: the known unclosed SQLite `ResourceWarning` failures reproduce under the repository's warnings-as-errors policy.

- [ ] Add one regression test for each resource owner proving a database file can be renamed immediately after the object is closed:

```python
def test_index_close_releases_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "anki.sqlite3"
    index = AnkiIndex(path)
    index.close()
    path.rename(tmp_path / "released.sqlite3")
```

- [ ] Run the new regression tests and verify they fail before implementation:

```bash
uv run pytest tests/anki/test_index.py -k "close_releases" -q
```

- [ ] Give each long-lived SQLite owner an idempotent `close()` plus context-manager support, and use `contextlib.closing` for short-lived connections:

```python
def close(self) -> None:
    connection = self._connection
    self._connection = None
    if connection is not None:
        connection.close()

def __enter__(self) -> Self:
    return self

def __exit__(self, *_: object) -> None:
    self.close()
```

- [ ] Dispose SQLAlchemy engines in fixtures and application shutdown hooks that create them.

- [ ] Run the focused and full quality gates:

```bash
uv run pytest tests/anki tests/agent tests/v2/test_agent_access.py -q
uv run ruff check src tests
uv run mypy src
```

Expected: all tests pass with the default warnings policy; no warning filters are weakened.

- [ ] Commit:

```bash
git add src/oms_hub/anki tests/anki tests/agent tests/v2/test_agent_access.py
git commit -m "fix: close Anki persistence resources"
```

---

### Task 2: Introduce the V4 Domain, Persistence, and Configuration

**Files:**

- Modify: `src/oms_hub/anki/domain.py`
- Modify: `src/oms_hub/anki/contracts.py`
- Modify: `src/oms_hub/anki/models.py`
- Modify: `src/oms_hub/anki/repository.py`
- Modify: `src/oms_hub/config.py`
- Modify: `src/oms_hub/migrations.py`
- Test: `tests/anki/test_domains.py`
- Test: `tests/anki/test_contracts.py`
- Test: `tests/anki/test_anki_repository.py`
- Test: `tests/anki/test_migrations.py`

**Interfaces:**

- `CreateCurationJob` consumes lecture/block/deck/tag/source selections and provider settings; it has no AMBOSS fields.
- `CurationJob` produces explicit stage and recovery state.
- `Candidate`, `SourceEvidence`, `GapCardProposal`, and `TagPatch` carry pass labels and provenance.
- Schema version advances from 6 to 7.

- [ ] Write serialization tests that reject AMBOSS input and round-trip the new types:

```python
def test_create_job_rejects_amboss_input() -> None:
    with pytest.raises(ValidationError):
        CreateCurationJobRequest.model_validate(
            {"lecture_id": 7, "amboss_input": "legacy text"}
        )

def test_tag_patch_round_trips_exact_diff() -> None:
    patch = TagPatch(
        note_id=42,
        before=("lecture::03", "source::ankihub"),
        after=("lecture::03", "review::high_yield", "source::ankihub"),
        expected_tag_hash="abc",
    )
    assert TagPatch.model_validate(patch.model_dump()) == patch
```

- [ ] Add the V4 enums and immutable records:

```python
class RetrievalPass(StrEnum):
    PASS_1 = "pass_1"
    PASS_2_RESCUE = "pass_2_rescue"

class ApplyState(StrEnum):
    PENDING = "pending"
    FAILED_BEFORE_APPLY = "failed_before_apply"
    COMPLETE = "complete"
    APPLIED_LOCAL_SYNC_RETRYABLE = "applied_local_sync_retryable"
    APPLIED_LOCAL_SYNC_BLOCKED = "applied_local_sync_blocked"
    APPLY_PARTIAL = "apply_partial"
```

Include stable identifiers for concepts, evidence passages, proposals, review changes, and operations.

- [ ] Remove AMBOSS from request/domain/repository methods. For existing SQLite databases, map the two old non-null columns as private inert compatibility attributes with empty defaults; no service may read them:

```python
_legacy_amboss_input: Mapped[str] = mapped_column(
    "amboss_input", Text, default="", server_default=""
)
_legacy_amboss_sha256: Mapped[str] = mapped_column(
    "amboss_sha256", String(64), default=EMPTY_SHA256, server_default=EMPTY_SHA256
)
```

- [ ] Add schema-7 tables or columns for source evidence, retrieval pass, tag patches, proposal provenance, stage artifacts, and apply state. Make the migration idempotent and verify migration from a schema-6 fixture as well as a clean database.

- [ ] Add configuration with validated defaults:

```python
anki_semantic_model: str = "voyage-4-large"
anki_semantic_dimensions: int = 1024
anki_semantic_min_coverage: float = 0.995
anki_semantic_batch_size: int = 128
anki_semantic_query_cache_size: int = 512
anki_connect_url: str = "http://127.0.0.1:8765"
```

Reject non-loopback AnkiConnect URLs at configuration load time.

- [ ] Run:

```bash
uv run pytest tests/anki/test_domains.py tests/anki/test_contracts.py tests/anki/test_anki_repository.py tests/anki/test_migrations.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki src/oms_hub/config.py src/oms_hub/migrations.py tests
git commit -m "feat: add Anki curation V4 domain"
```

---

### Task 3: Add the Owned Local Anki Gateway and Preflight

**Files:**

- Create: `src/oms_hub/anki/ankiconnect.py`
- Create: `src/oms_hub/anki/runtime.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/anki/test_ankiconnect.py`
- Test: `tests/anki/test_runtime.py`

**Interfaces:**

- `AnkiConnectClient` consumes loopback HTTP calls and produces typed note/card/media/mutation responses.
- `AnkiRuntime.preflight()` produces `AnkiPreflight` without mutating the collection.
- `AnkiRuntime.ensure_running()` may launch Anki only when explicitly invoked by the job/apply workflow.

- [ ] Add HTTP contract tests with a fake transport for `version`, `sync`, `findNotes`, `notesInfo`, `findCards`, `cardsInfo`, `modelFieldNames`, `retrieveMediaFile`, `addTags`, `removeTags`, and `addNotes`. Assert AnkiConnect error payloads become typed exceptions.

- [ ] Add URL-security tests:

```python
@pytest.mark.parametrize("url", [
    "http://192.168.1.20:8765",
    "https://anki.example.com",
])
def test_client_rejects_non_loopback_url(url: str) -> None:
    with pytest.raises(UnsafeAnkiConnectURL):
        AnkiConnectClient(url)
```

- [ ] Implement an owned async client with explicit `aclose()` and one internal request method:

```python
async def _invoke(self, action: str, **params: object) -> object:
    response = await self._http.post(
        self._url, json={"action": action, "version": 6, "params": params}
    )
    response.raise_for_status()
    payload = AnkiConnectResponse.model_validate(response.json())
    if payload.error:
        raise AnkiConnectError(action, payload.error)
    return payload.result
```

- [ ] Implement Windows launch detection behind a `ProcessLauncher` protocol so unit tests never start a real application. Preflight must report: reachable, AnkiConnect version, collection accessible, sync available, profile name when discoverable, and blocking reason.

- [ ] Wire runtime creation and cleanup into FastAPI lifespan without enabling writes.

- [ ] Run:

```bash
uv run pytest tests/anki/test_ankiconnect.py tests/anki/test_runtime.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/ankiconnect.py src/oms_hub/anki/runtime.py src/oms_hub/app.py tests/anki
git commit -m "feat: own local AnkiConnect runtime"
```

---

### Task 4: Add the Voyage Embedding Boundary

**Files:**

- Create: `src/oms_hub/anki/semantic/__init__.py`
- Create: `src/oms_hub/anki/semantic/domain.py`
- Create: `src/oms_hub/anki/semantic/voyage.py`
- Modify: `src/oms_hub/security/secret_store.py`
- Test: `tests/anki/semantic/test_voyage.py`

**Interfaces:**

- `EmbeddingClient.embed(texts, input_type)` produces normalized `float32` arrays of shape `(n, 1024)`.
- The Voyage API key is resolved from Study Hub's secret store under `voyage-api-key`; it is never stored in job artifacts.

- [ ] Write fake-transport tests for document/query input types, batching, order preservation, rate-limit retry, malformed dimensions, empty input, and redacted exceptions.

- [ ] Define a narrow protocol:

```python
class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: Literal["document", "query"],
    ) -> NDArray[np.float32]: ...
```

- [ ] Implement `VoyageEmbeddingClient` against the documented embeddings endpoint. Validate model and dimension from the response, L2-normalize once, retry only 429/5xx with bounded exponential backoff, and include batch index rather than text in errors.

- [ ] Add secret-store read/write/delete tests for `voyage-api-key`.

- [ ] Run:

```bash
uv run pytest tests/anki/semantic/test_voyage.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/semantic src/oms_hub/security/secret_store.py tests/anki/semantic
git commit -m "feat: add Voyage embedding client"
```

---

### Task 5: Build the Atomic Semantic Snapshot Store

**Files:**

- Create: `src/oms_hub/anki/semantic/store.py`
- Modify: `src/oms_hub/anki/semantic/domain.py`
- Test: `tests/anki/semantic/test_store.py`

**Interfaces:**

- `SemanticSnapshotStore.replace(records, vectors)` atomically publishes a complete generation.
- `SemanticSnapshotStore.load()` returns a validated immutable snapshot.
- A manifest binds generation, model, dimensions, note ordering, content hashes, and checksums.

- [ ] Write tests for round-trip ordering, float16 on disk, checksum mismatch, model mismatch, interrupted replacement, and concurrent readers seeing either the old or new generation.

- [ ] Define the manifest:

```python
class SemanticManifest(BaseModel):
    generation: UUID
    model: str
    dimensions: int
    created_at: datetime
    note_ids: tuple[int, ...]
    content_hashes: tuple[str, ...]
    matrix_sha256: str
```

- [ ] Implement replacement in a sibling temporary directory: write manifest and `.npy`, fsync files/directories, validate by reopening, then atomically switch the `CURRENT` pointer. Clean only generations not referenced by `CURRENT`.

- [ ] Load matrices with read-only NumPy memory mapping and reject duplicate note IDs, wrong shapes, non-finite values, or checksum mismatch.

- [ ] Run:

```bash
uv run pytest tests/anki/semantic/test_store.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/semantic tests/anki/semantic/test_store.py
git commit -m "feat: add atomic Anki vector snapshots"
```

---

### Task 6: Add Incremental Refresh, Exact Search, and Query Caching

**Files:**

- Create: `src/oms_hub/anki/semantic/service.py`
- Modify: `src/oms_hub/anki/semantic/domain.py`
- Test: `tests/anki/semantic/test_service.py`

**Interfaces:**

- `refresh(records)` embeds only added/changed content and drops deleted notes.
- `search(queries, eligible_note_ids, limit)` returns per-query exact cosine hits.
- Query cache keys include model, input type, normalized query hash, and dimensions.

- [ ] Write tests proving unchanged notes are not re-embedded, changed/deleted notes are handled, incomplete refresh never replaces the active generation, cache hits avoid API calls, eligibility filtering occurs before top-k, and ties sort by note ID.

- [ ] Implement content identity as SHA-256 over versioned normalized text:

```python
def content_hash(text: str) -> str:
    payload = f"anki-note-v1\0{normalize_semantic_text(text)}"
    return hashlib.sha256(payload.encode()).hexdigest()
```

- [ ] Implement refresh by reusing unchanged rows from the current snapshot, embedding changed rows with `input_type="document"`, enforcing at least 99.5% coverage, and calling `replace()` only after validation.

- [ ] Implement exact search:

```python
scores = matrix[selected_rows].astype(np.float32) @ query.astype(np.float32)
order = np.lexsort((selected_note_ids, -scores))[:limit]
```

Embed searches with `input_type="query"` and bound the in-memory LRU cache to the configured size.

- [ ] Add a 68,000-note synthetic benchmark test marked `performance`; assert peak matrix storage is compatible with float16 and record p50/p95 search time without making wall-time correctness assertions in the unit suite.

- [ ] Run:

```bash
uv run pytest tests/anki/semantic/test_service.py -q
uv run pytest tests/anki/semantic/test_service.py -m performance -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/semantic tests/anki/semantic
git commit -m "feat: add incremental exact semantic search"
```

---

### Task 7: Upgrade the Companion Anki Index and Filtering

**Files:**

- Modify: `src/oms_hub/anki/index.py`
- Modify: `src/oms_hub/anki/normalize.py`
- Create: `tests/anki/test_companion_index.py`

**Interfaces:**

- `CompanionNote` includes note ID, normalized searchable text, model, tags, deck memberships, source families, content hash, and modified timestamp.
- `AnkiIndex.eligible_note_ids(filters)` enforces deck/tag allowlists before semantic ranking.
- `AnkiIndex.search_fts(query, filters, limit)` produces lexical candidates.

- [ ] Write tests for cards from one note in multiple filtered decks, nested tags, excluded tags, empty allowlists, FTS escaping, source-family deduplication, and delta refresh.

- [ ] Compute trusted source families from configured tag roots rather than raw tag count:

```python
def trusted_source_families(tags: Iterable[str]) -> frozenset[str]:
    return frozenset(
        family
        for tag in tags
        if (family := match_configured_source_family(tag)) is not None
    )
```

- [ ] Refresh note/card metadata from `findNotes`, `notesInfo`, `findCards`, and `cardsInfo`. Update FTS/tag/deck rows in one SQLite transaction and publish the semantic refresh only after the companion transaction succeeds.

- [ ] Preserve stable note-level identity when cards move decks. Reject a semantic hit that is absent from the companion generation.

- [ ] Run:

```bash
uv run pytest tests/anki/test_companion_index.py tests/anki/test_index.py tests/anki/test_normalize.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/index.py src/oms_hub/anki/normalize.py tests/anki
git commit -m "feat: add deck-aware Anki companion index"
```

---

### Task 8: Build Lecture Source Extraction and the Source Index

**Files:**

- Create: `src/oms_hub/anki/sources.py`
- Create: `src/oms_hub/anki/source_index.py`
- Modify: `src/oms_hub/anki/models.py`
- Modify: `src/oms_hub/migrations.py`
- Test: `tests/anki/test_sources.py`
- Test: `tests/anki/test_source_index.py`
- Fixture: `tests/fixtures/anki/minimal_lecture.pptx`

**Interfaces:**

- `LectureSourceExtractor.extract(revision_ids)` produces stable `SourcePassage` records.
- `LectureSourceIndex.refresh(passages)` stores FTS plus Voyage document vectors.
- `LectureSourceIndex.search(query, source_scope, limit)` returns evidence with source type, artifact/revision ID, slide number or transcript offsets, text, and scores.

- [ ] Create a tiny PPTX fixture containing slide text, speaker notes, and an image marker, plus a transcript fixture with timestamps. Write expected passage-ID and citation tests.

- [ ] Extract PPTX shape text and speaker notes with `python-pptx`. Send image-only slides through the existing vision boundary when available; otherwise retain an explicit `vision_unavailable` extraction status rather than inventing text.

- [ ] Segment transcripts on sentence/timestamp boundaries into overlapping stable passages. Derive IDs from revision ID, source locator, extraction version, and text hash.

- [ ] Store passage metadata and FTS rows in SQLite; store vectors through a source-specific `SemanticSnapshotStore`. Use `input_type="document"` when refreshing and `"query"` when searching.

- [ ] Fuse source semantic and FTS results with reciprocal-rank fusion and return citations:

```python
score = sum(1.0 / (60 + rank) for rank in contributing_ranks)
```

- [ ] Run:

```bash
uv run pytest tests/anki/test_sources.py tests/anki/test_source_index.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/sources.py src/oms_hub/anki/source_index.py src/oms_hub/anki/models.py src/oms_hub/migrations.py tests
git commit -m "feat: index lecture slides and transcripts"
```

---

### Task 9: Generate and Validate the Lecture Concept Ledger

**Files:**

- Create: `src/oms_hub/anki/lcl.py`
- Modify: `src/oms_hub/llm/provider.py`
- Modify: `src/oms_hub/llm/service.py`
- Modify: `src/oms_hub/llm/openai.py`
- Modify: `src/oms_hub/llm/gemini.py`
- Modify: `src/oms_hub/llm/anthropic.py`
- Test: `tests/anki/test_lcl.py`
- Test: existing provider contract tests

**Interfaces:**

- `StructuredTextService.generate_json(...)` returns validated JSON plus provider/model/request metadata.
- `LCLService.generate(source_bundle)` produces concepts containing source references, canonical statement, hypothetical card, and exactly two paraphrases.

- [ ] Add provider-contract tests for structured JSON success, invalid JSON, schema mismatch, timeout, and redacted provider errors. Extend each supported provider adapter through the same public interface.

- [ ] Define ledger output:

```python
class LectureConcept(BaseModel):
    concept_id: str
    source_refs: tuple[SourceRef, ...]
    statement: str
    hypothetical_card: str
    paraphrases: tuple[str, str]
    importance: Literal["core", "supporting"]
```

- [ ] Require every source reference to resolve to an indexed passage and require each concept's statement to be supported by at least one cited passage. Reject duplicate concept IDs and blank/near-duplicate query variants.

- [ ] Generate four query strings per concept: statement, hypothetical card, and two paraphrases. Persist the validated ledger and raw sanitized model response as immutable stage artifacts.

- [ ] Add deterministic retry: one repair request after invalid structured output, then fail the LCL stage without advancing the job.

- [ ] Run:

```bash
uv run pytest tests/anki/test_lcl.py tests/llm -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/lcl.py src/oms_hub/llm tests/anki/test_lcl.py tests/llm
git commit -m "feat: generate source-grounded lecture ledgers"
```

---

### Task 10: Implement Pass 1 Hybrid Retrieval

**Files:**

- Create: `src/oms_hub/anki/retrieval.py`
- Test: `tests/anki/test_retrieval.py`

**Interfaces:**

- `RetrievalService.retrieve_pass_1(concept, scope)` consumes four query variants and eligible notes.
- It produces ranked `Candidate` records labeled `pass_1` with component scores and reasons.

- [ ] Write ranking tests for semantic-variant subfusion, lexical/semantic RRF, lecture-tag boost, block-tag boost, source-family boost, deterministic ties, and filters applied before ranking.

- [ ] Fuse the four semantic lists into one semantic rank before mixing modalities:

```python
semantic_score[note_id] = sum(
    variant_weight / (60 + rank)
    for variant_weight, rank in appearances[note_id]
)
```

- [ ] Rank the fused semantic list and FTS list with RRF, then apply bounded boosts. Persist both base and boosted scores so the review UI can explain why a note appeared.

- [ ] Cap candidates per concept and global candidate count from configuration. Never return notes outside the deck/tag eligibility set.

- [ ] Run:

```bash
uv run pytest tests/anki/test_retrieval.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/retrieval.py tests/anki/test_retrieval.py
git commit -m "feat: add first-pass hybrid Anki retrieval"
```

---

### Task 11: Judge Pass 1 Candidates with Cacheable Structured Outputs

**Files:**

- Create: `src/oms_hub/anki/judgment.py`
- Modify: `src/oms_hub/anki/repository.py`
- Test: `tests/anki/test_judgment.py`
- Test: `tests/anki/test_anki_repository.py`

**Interfaces:**

- `JudgmentService.judge(concept, candidates)` produces `covered`, `partial`, or `missing`, plus supporting note IDs and a concise explanation.
- Cache identity includes concept content hash, candidate content hashes, prompt version, provider, and model.

- [ ] Write tests for cache hit/miss, changed note invalidation, malformed note IDs, unsupported verdicts, contradictory explanations, and provider failure.

- [ ] Validate structured output:

```python
class CoverageJudgment(BaseModel):
    status: Literal["covered", "partial", "missing"]
    supporting_note_ids: tuple[int, ...]
    missing_facts: tuple[str, ...]
    rationale: str
```

Supporting IDs must be members of the supplied candidates. `covered` requires at least one supporting note; `missing` requires none.

- [ ] Persist prompt version, provider/model, candidate digest, result, and cache timestamps. Do not cache provider failures or invalid responses.

- [ ] Keep `partial` eligible for source rescue; do not generate cards at this stage.

- [ ] Run:

```bash
uv run pytest tests/anki/test_judgment.py tests/anki/test_anki_repository.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/judgment.py src/oms_hub/anki/repository.py tests/anki
git commit -m "feat: add cached Anki coverage judgment"
```

---

### Task 12: Rescue Missed Topics Against Slides and Transcripts

**Files:**

- Create: `src/oms_hub/anki/rescue.py`
- Modify: `src/oms_hub/anki/retrieval.py`
- Modify: `src/oms_hub/anki/judgment.py`
- Test: `tests/anki/test_rescue.py`

**Interfaces:**

- `RescueService.localize(concept)` returns `supported`, `partial`, or `unsupported` plus cited evidence.
- `RescueService.build_queries(evidence)` returns grounded Pass 2 query variants.
- `RetrievalService.retrieve_pass_2(...)` produces candidates labeled `pass_2_rescue`.

- [ ] Write tests for a slide-only rescue, transcript-only rescue, both-source fusion, unsupported concept, partial evidence, stale source revision, and Pass 2 recovery.

- [ ] Localize each `partial` or `missing` Pass 1 concept in the source index. Store exact evidence snippets and locators; no card-generation text may be introduced during localization.

- [ ] Build Pass 2 queries only from validated evidence:

```python
class RescueQuery(BaseModel):
    text: str
    evidence_ids: tuple[str, ...]
    kind: Literal["source_statement", "terminology", "clinical_rephrase"]
```

- [ ] Run the same eligible-note filters and hybrid ranker as Pass 1, but keep a distinct pass label and evidence lineage. Rejudge against Pass 2 candidates.

- [ ] Classify final outcomes:

  - `recovered`: Pass 2 finds adequate coverage.
  - `gap_supported`: still missing and source evidence is sufficient for generation.
  - `unresolved_partial`: evidence or coverage remains ambiguous.
  - `unsupported`: the concept cannot be grounded in selected sources.

- [ ] Run:

```bash
uv run pytest tests/anki/test_rescue.py tests/anki/test_retrieval.py tests/anki/test_judgment.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/rescue.py src/oms_hub/anki/retrieval.py src/oms_hub/anki/judgment.py tests/anki
git commit -m "feat: rescue missed topics from lecture sources"
```

---

### Task 13: Deduplicate Results and Generate Grounded Gap Cards

**Files:**

- Create: `src/oms_hub/anki/dedupe.py`
- Create: `src/oms_hub/anki/gaps.py`
- Test: `tests/anki/test_dedupe.py`
- Test: `tests/anki/test_gaps.py`

**Interfaces:**

- `DeduplicationService.classify(proposal, existing_notes, batch)` returns `unique`, `overlap`, or `duplicate`.
- `GapCardService.generate(gap_supported)` produces a provenance-complete proposal or a validation failure.

- [ ] Write tests for existing-note duplicate, within-batch duplicate, cloze normalization, source contradiction, absent citation, unsupported answer, and valid card provenance.

- [ ] Generate cards only for `gap_supported` outcomes. Require note type, fields, source refs, concept ID, evidence IDs, initial tags, generation provider/model, prompt version, and confidence.

- [ ] Add deterministic validation before semantic deduplication: required fields, cloze numbering, HTML safety, length limits, answer leakage, and resolvable citations.

- [ ] Verify entailment through a separate structured judgment using only cited source passages. Reject `not_supported` or `contradicted`; send `uncertain` to unresolved review rather than auto-proposing it.

- [ ] Compare normalized field text and semantic similarity against eligible existing notes and other proposals. Store the nearest matches and thresholds for UI explanation.

- [ ] Run:

```bash
uv run pytest tests/anki/test_dedupe.py tests/anki/test_gaps.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/dedupe.py src/oms_hub/anki/gaps.py tests/anki
git commit -m "feat: generate and deduplicate grounded gap cards"
```

---

### Task 14: Add Tag Policy, Review Changesets, and Staleness Checks

**Files:**

- Create: `src/oms_hub/anki/tag_policy.py`
- Modify: `src/oms_hub/anki/contracts.py`
- Modify: `src/oms_hub/anki/repository.py`
- Test: `tests/anki/test_tag_policy.py`
- Test: `tests/anki/test_contracts.py`
- Test: `tests/anki/test_anki_repository.py`

**Interfaces:**

- `TagPolicy.classify(tag)` returns `pipeline_owned`, `approved_optional`, `source_managed`, or `unknown`.
- `ReviewChangeSet` includes proposal decisions, field edits, and exact note-level tag patches.
- `validate_tag_patch(current_tags, patch)` returns add/remove operations or a stale/protected error.

- [ ] Write tests for nested tags, case normalization, protected source tags, unknown tag removal, exact before/after diff, unchanged patch, and stale tag hash.

- [ ] Implement configured policy roots. Allow:

  - add/remove pipeline-owned tags;
  - add/remove approved optional tags;
  - initial tags on generated notes under those same policies.

Reject:

  - removal or rewrite of source-managed tags;
  - removal of unknown pre-existing tags;
  - tag strings that Anki cannot safely accept.

- [ ] Hash canonical current tags and require `expected_tag_hash` at review save and envelope creation:

```python
def tag_hash(tags: Iterable[str]) -> str:
    canonical = "\n".join(sorted(normalize_tag(tag) for tag in tags))
    return hashlib.sha256(canonical.encode()).hexdigest()
```

- [ ] Persist changesets append-only with reviewer, timestamp, prior revision, and exact diff. Editing a changeset creates a new revision.

- [ ] Run:

```bash
uv run pytest tests/anki/test_tag_policy.py tests/anki/test_contracts.py tests/anki/test_anki_repository.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/tag_policy.py src/oms_hub/anki/contracts.py src/oms_hub/anki/repository.py tests/anki
git commit -m "feat: add reviewed Anki tag changes"
```

---

### Task 15: Build Idempotent Envelopes and the Local Apply Coordinator

**Files:**

- Create: `src/oms_hub/anki/envelope.py`
- Create: `src/oms_hub/anki/apply.py`
- Modify: `src/oms_hub/anki/contracts.py`
- Modify: `src/oms_hub/anki/repository.py`
- Test: `tests/anki/test_envelope.py`
- Test: `tests/anki/test_apply.py`

**Interfaces:**

- `EnvelopeBuilder.build(changeset, current_collection)` produces ordered, deterministic operations.
- `ApplyCoordinator.apply(envelope_id)` executes leading sync, local mutations, trailing sync, and verification.
- Every operation has a deterministic idempotency key and durable status.

- [ ] Write state-machine tests for:

  - leading sync failure with zero mutation calls;
  - add/remove tags followed by verification;
  - generated-note creation retry without duplicates;
  - trailing sync retryable failure;
  - trailing sync blocked failure;
  - partial mutation failure;
  - process restart between every operation;
  - stale field/tag hash before apply.

- [ ] Define the operation order:

```text
preflight -> leading sync -> stale check -> removeTags -> addTags
-> addNotes -> trailing sync -> read-back verification
```

Generated-note tags are supplied in `addNotes`; separate tag operations apply only to existing notes.

- [ ] Derive each operation key from envelope ID, operation kind, target stable ID, and canonical payload hash. Record intent before the call and result after it.

- [ ] On leading sync failure, set `failed_before_apply` and make no writes. On trailing failure, classify the AnkiConnect error into `applied_local_sync_retryable` or `applied_local_sync_blocked`, record that local changes exist, and stop.

- [ ] Verify note fields and canonical tags after sync. A mismatch becomes `apply_partial` with the exact expected/actual diff; never silently retry a mutation with uncertain outcome.

- [ ] Run:

```bash
uv run pytest tests/anki/test_envelope.py tests/anki/test_apply.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/envelope.py src/oms_hub/anki/apply.py src/oms_hub/anki/contracts.py src/oms_hub/anki/repository.py tests/anki
git commit -m "feat: apply reviewed Anki changes locally"
```

---

### Task 16: Orchestrate the Resumable Worker and Artifact Trail

**Files:**

- Create: `src/oms_hub/anki/pipeline.py`
- Create: `src/oms_hub/anki/worker.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/anki/repository.py`
- Test: `tests/anki/test_pipeline.py`
- Test: `tests/anki/test_worker.py`

**Interfaces:**

- `CurationPipeline.run_stage(job_id)` advances exactly one stage from persisted inputs.
- `AnkiCurationWorker` leases jobs, renews leases, resumes safe stages, and records terminal failures.
- Apply remains a separate explicit user-triggered workflow.

- [ ] Write tests for the complete happy path, restart at every stage, stale lease reclamation, source revision changed mid-job, semantic snapshot changed mid-job, cancellation before review, and two workers racing for one job.

- [ ] Define stage inputs and artifact digests:

```text
preflight -> source_index -> lcl -> pass_1 -> judgment_1
-> rescue/pass_2 -> dedupe -> gap_generation -> ready_for_review
```

Each stage reads only committed artifacts, writes a new immutable artifact, and then advances the job in one database transaction.

- [ ] Pin source revision IDs, companion-index generation, semantic generation, prompt versions, provider/model, and configuration digest at job creation. If a pinned input disappears, fail with a user-actionable reason.

- [ ] Mark LLM/network stages retryable with bounded attempts; mark validation and stale-source failures blocked until the user starts a new job. Never rerun completed stages merely because the process restarted.

- [ ] Start/stop the worker through application lifespan. Keep existing agent routes operational but unused by V4.

- [ ] Run:

```bash
uv run pytest tests/anki/test_pipeline.py tests/anki/test_worker.py -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Commit:

```bash
git add src/oms_hub/anki/pipeline.py src/oms_hub/anki/worker.py src/oms_hub/anki/repository.py src/oms_hub/app.py tests/anki
git commit -m "feat: orchestrate resumable Anki curation"
```

---

### Task 17: Ship the Review and Recovery UI

**Files:**

- Create: `src/oms_hub/web/anki_routes.py`
- Modify: `src/oms_hub/web/templates/anki.html`
- Create: `src/oms_hub/web/templates/anki_review.html`
- Create: `src/oms_hub/web/static/anki.js`
- Modify: existing Study Hub stylesheet
- Modify: `src/oms_hub/app.py`
- Test: `tests/anki/test_web.py`

**Interfaces:**

- Routes create/list/read/cancel jobs, save revisioned review changesets, build/apply envelopes, retry sync, and expose read-only evidence/candidate details.
- The UI groups `Pass 1 matches`, `Recovered in Pass 2`, `Generated cards`, and `Unresolved`.

- [ ] Write route tests for authentication, create-job validation, no AMBOSS fields, optimistic review revision, protected tag rejection, apply confirmation, sync retry, and evidence access.

- [ ] Replace the placeholder page with a guided workflow:

  1. choose lecture, block, decks, tags, source revisions, and LLM;
  2. run preflight and curation;
  3. review grouped outcomes;
  4. inspect why each note matched and cited source evidence;
  5. approve/reject/edit generated cards;
  6. edit allowed tags with a visible before/after diff;
  7. confirm apply and see recovery state.

- [ ] Make protected tags visually locked and explain the policy in plain language. Generated-card edits must show citations and validation state.

- [ ] Require a final confirmation summarizing counts for notes created, existing notes retagged, tags added, and tags removed. Do not enable the apply button for stale or invalid changesets.

- [ ] Present recovery states honestly:

  - no local changes were made;
  - local changes were made but cloud sync should be retried;
  - local changes were made and need manual attention;
  - verification found a partial mismatch.

- [ ] Run:

```bash
uv run pytest tests/anki/test_web.py -q
uv run pytest tests/anki -q
uv run ruff check src tests
uv run mypy src
```

- [ ] Perform a browser smoke test at desktop and narrow widths, using only a disposable database and fake AnkiConnect transport. Record screenshots in the task evidence, not the repository.

- [ ] Commit:

```bash
git add src/oms_hub/web src/oms_hub/app.py tests/anki/test_web.py
git commit -m "feat: add Anki curation review workflow"
```

---

### Task 18: Calibrate, Accept on a Copied Profile, and Retire the Legacy Agent

**Files:**

- Create: `scripts/evaluate-anki-retrieval.py`
- Create: `tests/fixtures/anki/retrieval_gold.json`
- Modify: `README.md`
- Create: `docs/anki-curation-nuc-rollout.md`
- Delete only after acceptance: `src/oms_anki_agent/`
- Delete only after acceptance: `tests/agent/`
- Modify only after acceptance: `src/oms_hub/web/anki_agent_routes.py`
- Modify only after acceptance: `src/oms_hub/app.py`
- Modify only after acceptance: agent-only configuration and packaging entries

**Interfaces:**

- Evaluation script consumes a versioned gold set and emits JSON metrics for semantic variants, FTS, fusion, Pass 1, Pass 2, latency, and coverage.
- Copied-profile acceptance produces an auditable report; it never touches the production profile.

- [ ] Build a gold set from manually labeled lecture concepts and known covering notes. Include easy terminology, paraphrases, slide-only wording, transcript-only wording, multi-source concepts, genuine gaps, and hard negatives.

- [ ] Implement metrics:

```text
Recall@5, Recall@10, MRR, nDCG@10
Pass 1 coverage precision/recall
Pass 2 recovery rate and false-recovery rate
gap proposal precision
semantic coverage percentage
refresh duration and query p50/p95
```

- [ ] Add ablations for statement-only, all four semantic variants, FTS-only, semantic-only, fused retrieval, and boosts. Write results as machine-readable JSON plus a concise Markdown table.

- [ ] Establish release thresholds from the approved V4 acceptance criteria:

  - semantic snapshot coverage at least 99.5%;
  - no eligible-note filter leaks;
  - no generated proposal without resolvable source evidence;
  - no protected-tag mutation;
  - zero duplicate notes in apply retry tests;
  - copied-profile leading/trailing sync and verification scenarios all behave as specified.

- [ ] Copy the Anki profile using Anki's supported backup/export process while Anki is closed. Run full index, one curation job, reviewed tag edits, generated-card apply, forced trailing-sync failure, retry, and read-back verification against only that copy.

- [ ] Record the semantic snapshot size, full/incremental refresh times, query p50/p95, memory peak, and 68k-note extrapolation. Keep exact NumPy search unless measured results fail the agreed interactive target; any ANN/Rust/quantization change requires a separate design review.

- [ ] Run the complete repository gates before removing legacy code:

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv run mypy src
uv run python scripts/evaluate-anki-retrieval.py --gold tests/fixtures/anki/retrieval_gold.json
```

- [ ] Stop at the acceptance gate and obtain the user's approval of the copied-profile report. Do not remove the legacy agent before that approval.

- [ ] After approval, remove `oms_anki_agent`, its routes/tests/configuration/packaging, and all calls to the old agent. Add a test asserting the application exposes no agent registration, heartbeat, command, or snapshot endpoints.

- [ ] Search for forbidden residuals:

```bash
rg -n "amboss|oms_anki_agent|1311966390|sbm_smart_anki" src tests pyproject.toml
```

Expected after retirement: no active code references. Historical migration compatibility names may remain only where explicitly documented.

- [ ] Update setup documentation for one-package NUC operation, Voyage credentials, local AnkiConnect, first index, review/apply recovery, backups, and Mac study sync.

- [ ] Run the complete gates again, then commit:

```bash
git add -A
git commit -m "feat: consolidate Anki curation into Study Hub"
```

---

## Cross-Task Verification Matrix

| Risk | Required automated evidence | Required manual evidence |
|---|---|---|
| Collection mutation without clean sync | Task 15 state-machine tests | Copied-profile forced sync failure |
| Duplicate notes after retry | Deterministic key/restart tests | Reapply same approved envelope |
| Protected tag loss | Task 14 policy tests | Review/apply source-managed tag |
| Unsupported generated card | Source-resolution and entailment tests | Inspect sampled proposals with citations |
| Retrieval filter leak | Companion and retrieval property tests | Deck/tag scoped calibration cases |
| Stale index/source data | Generation and revision pinning tests | Modify copied profile/source mid-job |
| Vector corruption | Snapshot checksum/atomicity tests | Kill refresh and reopen last generation |
| Add-on coupling | Import/path residual scan | Run with add-on folder absent |
| Hidden AMBOSS behavior | API/domain rejection and residual scan | Confirm UI contains no AMBOSS controls |

## Final Release Checklist

- [ ] The approved V4 architecture spec status reads `Approved architecture design`.
- [ ] Default pytest runs with warnings-as-errors and no warning suppressions added for resource leaks.
- [ ] A clean database and a schema-6 database both migrate and run.
- [ ] The application works when both research repositories are unavailable.
- [ ] The semantic index meets 99.5% coverage and publishes atomically.
- [ ] Pass 1, source rescue, Pass 2, judgment, and generation artifacts are reproducible from pinned inputs.
- [ ] Every generated card has resolvable slide/transcript provenance and passes entailment validation.
- [ ] Review permits only approved first-release tag changes and shows exact diffs.
- [ ] Apply/retry behavior matches all five recovery states.
- [ ] The copied-profile report is approved before legacy-agent deletion.
- [ ] Full pytest, Ruff, mypy, and retrieval evaluation pass at the retirement commit.
