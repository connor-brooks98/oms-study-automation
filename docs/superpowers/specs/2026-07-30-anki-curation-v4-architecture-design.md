# Anki Curation Pipeline — Architecture V4

**Status:** Revised architecture design — pending final user review
**Date:** 2026-07-30
**Supersedes:** `anki_curation_architecture_v3.md`, `anki_curation_one_click_design_v2.md`, and topology-specific portions of `anki_curation_implementation_plan.md`

## 1. Decision

Adopt the V3 single-NUC topology and build one Hub-owned semantic subsystem inside the OMS Study Hub package. It performs document indexing, query expansion, semantic retrieval, lexical retrieval, source localization, and index maintenance without requiring a separately installed semantic-search add-on.

The semantic subsystem is a clean-room implementation of observed behavior and public techniques: Voyage document/query embeddings, content-hash incremental indexing, float16 vector storage, note-ID mapping, hierarchical tag allowlists, batched NumPy retrieval, HyDE, multi-query fusion, and retrieval provenance. No unlicensed add-on source is copied or shipped.

Retain the strongest implemented V2 components:

- Curation domain contracts and job state machine.
- Durable repository, migrations, candidates, gaps, reviews, envelopes, and receipts.
- HTML/cloze normalization, tag classification, domain assignment, FTS5, and media extraction.
- Strict loopback AnkiConnect client.
- Atomic artifacts, hashes, validation, idempotency, and test patterns.

Replace:

- Mac agent, launchd service, Tailscale transport, bearer authentication, and cross-machine snapshot protocol.
- FastEmbed as the production embedding source.
- The Semantic Search add-on as a runtime dependency.
- One-pass gap detection that immediately creates a card.

The defining V4 change is a two-pass retrieval policy:

1. Search the existing card corpus using the Lecture Concept Ledger.
2. For every uncovered concept, return to the PowerPoint and transcript, locate the best supporting passages, generate source-grounded search variants, and search the existing corpus again.
3. Generate a new card only when the second search still finds no suitable card and the lecture sources contain sufficient evidence.

## 2. Goals

1. Produce a reviewable set of existing and generated Anki notes for a lecture with one final approval action.
2. Prefer existing AnKing notes over generated notes.
3. Recover missed existing notes by re-querying with the lecturer's actual terminology before declaring a gap.
4. Ground every generated note in identifiable PowerPoint and/or transcript evidence.
5. Allow reviewed first-release tag additions and removals without modifying existing note fields or scheduling.
6. Make no destructive content changes to existing notes.
7. Keep every mutation auditable, idempotent, locally verified, and synchronized safely.
8. Keep the embedding provider replaceable without changing retrieval, judgment, review, or apply behavior.
9. Measure retrieval recall before enabling deterministic auto-include or auto-drop decisions.

## 3. Non-goals for the first V4 release

- Editing, deleting, moving, or suspending existing AnKing notes.
- Automatically resolving a forced full-sync direction.
- Treating generated cards as medical evidence independent of lecture sources.
- Automatically generating images.
- Multi-job concurrent curation.
- Depending on, modifying, forking, or distributing either inspected semantic-search add-on.
- Four-bit quantization or native Rust acceleration before profiling proves it necessary.
- Automatically removing protected AnKing, AnkiHub, or source-maintained tags.

## 4. Deployment topology

```text
NUC — Windows 11 Pro, logged-in interactive session
├── Study Hub
│   ├── Single curation worker
│   ├── Source artifact store
│   ├── Integrated semantic indexer and search engine
│   ├── Companion Index
│   ├── Curation pipeline
│   ├── Review UI
│   └── Audit artifacts and receipts
├── Anki desktop
│   ├── AnkiConnect on 127.0.0.1:8765
│   └── AnkiHub add-on
├── Voyage AI
│   └── Document and query embeddings using voyage-4-large
└── Configured LLM providers
    ├── Gemini for the Lecture Concept Ledger, evidence assessment, and source-grounded query generation
    └── Claude Sonnet for judgment, close-call dedupe, and gap-card authoring

AnkiWeb
└── Synchronizes the NUC writer profile with the Mac study profile

MacBook
└── Study and review device; no automated curation or indexing
```

Anki remains a GUI application. The NUC must maintain a logged-in session, start Anki at login, and disconnect rather than log off from RDP.

## 5. Trust boundaries and component ownership

### 5.1 Integrated semantic subsystem

The Hub owns a narrow interface:

```text
SemanticIndex.refresh(notes) -> SemanticSnapshot
SemanticIndex.embed_queries(queries) -> QueryMatrix
SemanticIndex.search(query_matrix, eligible_note_ids) -> RankedNoteLists

SemanticSnapshot
├── snapshot_id
├── embedding_model
├── dimensions
├── built_at
├── note_ids
├── normalized_vectors
├── note_decks
├── note_content_hashes
└── coverage_report
```

The implementation lives under `oms_hub.anki.semantic`. Provider calls remain behind the existing LLM/provider boundary, while vector persistence and retrieval remain provider-neutral.

### 5.2 Reference implementations

The locally inspected add-on and [SBMatthew/sbm_smart_anki](https://github.com/SBMatthew/sbm_smart_anki) are research inputs only. Useful behaviors may be independently reimplemented, but their unlicensed source files, comments, structure, and native binaries are not copied into the project.

V4 adopts these clean-room ideas:

- Note-level ID mapping and eligible-note allowlists applied before final ranking.
- Hierarchical deck and tag filtering.
- Content-hash incremental refresh.
- Contiguous, compact vector storage.
- Stress tests for large collections, concurrency, stale queries, and memory use.

V4 does not adopt:

- Hash-based pseudo-embeddings.
- Direct immutable reads of a live Anki collection database.
- Automatic dependency installation inside Anki.
- Unverified zero-RAM or SIMD performance claims.
- Four-bit quantization before medical retrieval recall is measured.

### 5.3 Hub-owned data

The Hub owns normalized note text, document embeddings, FTS5, tags, deck membership, block parsing, trusted-source evidence, lecture source passages, retrieval provenance, judgments, gaps, generated notes, proposed tag changes, envelopes, and receipts.

## 6. Integrated semantic indexing

### 6.1 Source records

The indexer retrieves notes from local AnkiConnect in bounded chunks. The initial eligible universe is the configured AnKing deck subtree. Each source record contains:

```text
note_id
guid
model_name
fields
tags
card_ids
deck_memberships
modified_at
```

The indexer normalizes note-level text using the existing V2 normalizer. One note produces one document embedding regardless of cloze-card count.

### 6.2 Document text and model contract

For approved AnKing note types, document text combines normalized `Text`, compact `Extra`, and other explicitly configured searchable fields. Media markup is removed while visible cloze answers are retained. The normalized document is capped by tokens, not an arbitrary preview-character limit.

The production embedding contract is:

```text
model: voyage-4-large
document input_type: document
query input_type: query
dimensions: 1024
stored dtype: float16
retrieval dtype: float32
similarity: cosine over normalized vectors
```

Every model, normalization, field-selection, or dimension change creates a new full index version.

### 6.3 Incremental refresh

For each note, compute a document content hash from normalized searchable text plus the embedding contract version.

- Unchanged hash: reuse the existing vector.
- New or changed hash: batch a new Voyage document embedding.
- Deleted or newly ineligible note: remove it from the active note-ID map.
- Failed note: retain the prior vector only if its prior hash still matches; otherwise quarantine it.

Refresh writes a complete replacement snapshot atomically. A failed refresh leaves the previous snapshot intact.

### 6.4 Owned storage

```text
semantic/
├── index.sqlite3
├── note_vectors.f16.npy
├── note_ids.npy
├── manifest.json
└── query_cache.sqlite3
```

`index.sqlite3` stores note IDs, content hashes, model/version metadata, deck membership, and refresh status. Vectors and parallel note IDs are atomically replaced, read-only between refreshes, and optionally memory-mapped when profiling shows a benefit.

The initial release uses float16 storage and float32 retrieval. Four-bit quantization is deferred because the corpus is small enough for exact matrix retrieval and medical recall matters more than saving approximately 100–200 MB.

### 6.5 Search execution

All lecture query vectors are embedded in provider batches. Search executes matrix operations against eligible row allowlists rather than scoring unrelated notes and filtering afterward.

For the current approximately 68,000-note corpus, exact NumPy retrieval is the default. Approximate nearest-neighbor indexes, Rust extensions, and quantization require a demonstrated latency or memory problem plus a retrieval-regression evaluation.

### 6.6 Validation

Every built snapshot must pass:

- Positive, unique note IDs.
- Exact alignment of vector rows and note-ID rows.
- Declared dimension of 1,024.
- Finite, non-zero vectors.
- Expected model and normalization versions.
- Reconciliation with the live eligible note-ID set.
- Coverage of at least 99.5%.
- Atomic load from a fresh process.

The Voyage credential lives only in the Hub's operating-system secret store.

## 7. Companion Index

The companion index joins Hub-owned metadata to the collapsed embedding corpus by note ID.

```text
notes_meta(
    nid PRIMARY KEY,
    guid,
    model_name,
    text_norm,
    extra_norm,
    raw_fields_json,
    tags_kept_json,
    trusted_source_count,
    norm_hash,
    block_course,
    block_exam,
    block_lecture,
    modified_at,
    content_sha256
)

note_decks(nid, deck_name, PRIMARY KEY(nid, deck_name))
note_cards(nid, card_id, PRIMARY KEY(nid, card_id))
note_tags(nid, tag, tag_prefix)
note_domains(nid, domain)
note_media(nid, field_name, filename, media_type, source_order)
notes_fts(nid UNINDEXED, text_norm, extra_norm)

embedding_meta(
    snapshot_id,
    model,
    dimensions,
    normalizer_version,
    document_contract_version,
    built_at,
    live_note_count,
    embedded_note_count,
    coverage_ratio
)

note_vectors.npy
note_ids.npy
```

Vectors are normalized once when loaded into a contiguous NumPy matrix. Retrieval batches all lecture query vectors into matrix operations.

### 7.1 Deck scoping

Every semantic, lexical, and tag retrieval path uses the same eligible-note universe. The initial production universe is notes with at least one card in the configured AnKing deck subtree.

Notes outside the configured eligible deck subtree are not included in the initial production semantic snapshot. Expanding the eligible universe requires an explicit configuration and a new snapshot.

### 7.2 Trusted source count

`trusted_source_count` counts distinct source families, not tags. Multiple tags below one Sketchy or First Aid hierarchy count once.

The initial source-family taxonomy includes Sketchy, First Aid, Pathoma, Boards and Beyond, Bootcamp, Pixorize, Physeo, Ninja Nerd, configured yield flags, and approved local course sources. Question-bank, OME, and AnkiHub identifier tags are not counted as independent trusted sources.

### 7.3 Staleness and coverage

Before a job starts, the Hub compares:

- Live eligible note-ID set.
- Companion Index note-ID set.
- Embedded note-ID set.
- Live note modifications since companion build.
- Stored document hashes against changed live notes.
- Semantic snapshot model and normalizer versions.

The job is blocked if required AnKing coverage is below 99.5%, any selected candidate lacks live metadata, or the semantic model/normalizer contract is invalid. A changed note count alone is never considered a sufficient staleness check.

## 8. Lecture source model

The pipeline ingests the canonical PowerPoint and cleaned transcript already associated with the lecture.

### 8.1 PowerPoint extraction

Extract:

- Slide number and stable slide ID.
- Shape text in reading order.
- Grouped-shape text.
- Tables.
- Speaker notes.
- Image references.
- Vision descriptions for slides with insufficient extractable text.

### 8.2 Transcript segmentation

Split the cleaned transcript into overlapping, stable passages with:

- Passage ID.
- Start and end timestamps when available.
- Character offsets.
- Text.
- Content hash.
- Neighboring passage IDs.

Segments are large enough to preserve explanation context but small enough to localize evidence. Passage IDs and hashes remain stable for unchanged text.

### 8.3 Source index

Create a lecture-local source index:

- FTS5 over slide text, speaker notes, vision descriptions, and transcript passages.
- Voyage `voyage-4-large` document embeddings for slide and transcript passages, generated and stored by the Hub.
- Query embeddings produced with the same model and `input_type="query"`.
- Links from passages to neighboring transcript segments and nearby slides where timing/order can be inferred.

This source index searches lecture evidence. It is separate from the Anki card index.
Lexical and semantic source results are fused before the evidence bundle is assembled.

## 9. Lecture Concept Ledger

Gemini produces a versioned Lecture Concept Ledger from the PowerPoint, transcript, saved lecture instructions, and vision fallbacks.

Each concept contains:

```text
concept_id
statement
objective_ids
depth
emphasis
keywords
synonyms
abbreviations
eponyms
mechanisms
context_traps
excluded_facts
soft_domains
source_refs[]
initial_query
hypothetical_card
paraphrases[2]
```

`source_refs` identify the slides and transcript passages that caused the concept to be included. The LCL does not merely summarize the lecture; it establishes the coverage checklist used by retrieval, gap detection, and generated-card validation.

The hypothetical card is embedded with Voyage `input_type="document"`. The statement and two paraphrases are embedded with `input_type="query"`. This yields four semantic query vectors per concept.

## 10. Curation pipeline

```text
QUEUED
→ PREFLIGHT
→ SNAPSHOTTING_EMBEDDINGS
→ BUILDING_COMPANION_INDEX
→ BUILDING_SOURCE_INDEX
→ BUILDING_LCL
→ RETRIEVING_PASS_1
→ JUDGING_PASS_1
→ LOCALIZING_MISSED_CONCEPTS
→ RETRIEVING_PASS_2
→ JUDGING_PASS_2
→ DEDUPING
→ GENERATING_GAPS
→ READY_FOR_REVIEW
→ APPLYING_LOCAL
→ SYNCING
→ VERIFYING
→ COMPLETE
```

Jobs may skip index-building states when compatible, fresh artifacts already exist. Every stage has a content-addressed input manifest and an immutable output artifact.

## 11. Retrieval Pass 1

Pass 1 searches every LCL concept against the complete eligible AnKing note universe.

### 11.1 Retrieval signals

1. Lecture-tag matches.
2. Block-tag matches.
3. Semantic search using:
   - Concept statement as query.
   - Two paraphrases as queries.
   - Hypothetical card as a document.
4. FTS5 lexical search using names, mechanisms, abbreviations, organisms, drugs, and exact phrases.

### 11.2 Semantic multi-query fusion

The four semantic result lists are fused into one semantic ranking before cross-retriever fusion. This prevents semantic retrieval from receiving four independent votes merely because four variants were generated.

### 11.3 Cross-retriever fusion

Use Reciprocal Rank Fusion for genuinely ranked semantic and lexical lists.

Do not assign arbitrary RRF rank to unranked evidence:

- Exact lecture-tag hits are high-priority evidence.
- Block-tag membership is a bounded boost whose strength decreases with subtree size.
- Trusted source count is a tiebreaker, not a primary relevance signal.

Every candidate records:

- Concept ID.
- Semantic ranks and similarities for each query variant.
- Combined semantic rank.
- Lexical rank and matched terms.
- Lecture/block tag evidence.
- Trusted source families.
- Final fused rank.
- Index and source fingerprints.

### 11.4 Pass 1 judgment

Claude Sonnet judges compacted candidates against the complete LCL and rubric. Each result contains:

- Note ID.
- Verdict.
- Best concept ID.
- Coverage direction.
- Reason.
- Context trap.
- Confidence.
- Recall direction.
- Mnemonic style.
- Retrieval pass: `pass_1`.

During shadow mode, deterministic bands are recorded but do not bypass model judgment.

## 12. Concept coverage and missed-topic trigger

A concept is covered only when at least one candidate:

- Receives a keep verdict.
- Maps to that concept.
- Matches the required recall direction.
- Is not rejected as a context trap.
- Survives later deduplication.

A concept enters missed-topic rescue when Pass 1 has:

- No kept candidate.
- Only low-confidence candidates.
- Candidates that mention the topic but test the wrong fact or recall direction.
- Candidates rejected as context traps.

The system does not create a gap card at this point.

## 13. Missed-topic rescue

### 13.1 Source localization

For each missed concept, search the lecture-local source index using:

- Original concept statement.
- LCL keywords and exact phrases.
- Synonyms, abbreviations, eponyms, and mechanisms.
- Terms found in near-miss cards.
- Objective text.

Retrieve a bounded evidence bundle:

- Up to five primary slides.
- Speaker notes attached to those slides.
- Up to five transcript passages.
- One neighboring passage on each side when needed for context.
- Vision descriptions when the source slide is image-driven.

The evidence bundle records retrieval scores and content hashes.

### 13.2 Evidence assessment

Before generating new searches, assess whether the evidence bundle actually supports the concept.

Outcomes:

- `supported`: the lecture explicitly teaches the concept.
- `partially_supported`: the lecture mentions it but lacks enough detail for a standalone card.
- `unsupported`: the concept was inferred incorrectly or is absent from the sources.

Unsupported concepts are removed from gap generation and shown as LCL corrections. Partially supported concepts remain reviewable but do not automatically produce cards.

### 13.3 Source-grounded query generation

For supported concepts, generate:

- Lecturer-language query using exact terminology from the evidence.
- Mechanism/relationship query.
- Alternate-name query.
- Source-grounded hypothetical Anki card.
- Exact lexical phrases and entities.

Each query includes its supporting source references. Query generation may paraphrase but may not introduce facts absent from the evidence bundle.

### 13.4 Retrieval Pass 2

Pass 2 searches the same eligible AnKing universe, but uses:

- The source-grounded query variants.
- Exact phrases from slides and transcript.
- Near-miss card vocabulary.
- Expanded synonyms and eponyms supported by the sources.
- Focused semantic and lexical retrieval with larger candidate limits.

Pass 2 does not weaken deck scoping or admit arbitrary cards from unrelated decks.

### 13.5 Pass 2 judgment

Claude judges Pass 2 candidates using:

- Original LCL concept.
- Source evidence bundle.
- Pass 1 near misses and rejection reasons.
- Pass 2 retrieval provenance.

Every result is labeled `pass_2`. A recovered existing card is preferred over generating a new card when it adequately tests the concept.

### 13.6 Rescue stop conditions

The rescue loop runs once. It does not recursively generate searches.

After Pass 2:

- Kept candidate found: mark `recovered_existing`.
- No candidate and evidence supported: mark `generation_eligible`.
- Evidence partial: mark `manual_source_review`.
- Evidence unsupported: mark `lcl_correction`.
- Provider or retrieval failure: mark `rescue_failed`, preserve artifacts, and allow retry.

## 14. Deduplication

Combine kept candidates from both retrieval passes and deduplicate only within concept clusters.

- Token-set similarity at or above 0.85 is a deterministic duplicate candidate.
- Similarity from 0.70 through 0.85 receives close-call model adjudication.
- Forward and reverse recall directions remain distinct unless the LCL says only one is required.
- A Pass 2 recovery receives no automatic preference over Pass 1; the survivor is selected by educational quality.

Existing-note losers are simply not tagged. No existing note is edited or suspended.

## 15. Gap-card generation

Only `generation_eligible` concepts enter card generation.

### 15.1 Inputs

Claude Sonnet receives:

- Concept and objectives.
- Source evidence bundle.
- Exact slide and transcript references.
- Pass 1 and Pass 2 search summaries.
- Reasons near-miss cards were rejected.
- Accepted, versioned house-style prompt.
- Runtime note-type field names.

### 15.2 Output rules

Generated notes:

- Use `AnKingOverhaul (AnKing Step Deck / AnKingMed)` unless runtime configuration selects another approved model.
- Query `modelFieldNames` at runtime.
- Populate exact `Text` and `Extra` field names.
- Use one atomic cloze unless the accepted house style explicitly allows another form.
- Test only the identified concept and recall direction.
- Include concise explanation in `Extra`.
- Do not claim facts absent from the source evidence.
- Do not include unsupported treatment recommendations or numerical values.
- Carry an invisible deterministic generation marker for idempotency.

### 15.3 Provenance record

The Hub stores, outside the visible card:

- Concept ID.
- Slide IDs and numbers.
- Transcript passage IDs and timestamps.
- Source content hashes.
- LCL version.
- Search Pass 1 and Pass 2 fingerprints.
- Gap prompt version.
- Model/provider.
- Generated note content hash.

Visible source citations inside the Anki note are optional and controlled by the accepted house-style prompt.

### 15.4 Validation

A generated note must pass:

- Strict schema validation.
- Required field validation.
- Cloze syntax validation.
- Source-entailment judgment.
- Duplicate check against existing notes and other generated notes.
- Medical-number and medication-name consistency check against source text.
- Note-type rendering test in a disposable profile during acceptance.

Failure routes the proposal to manual review; it is never silently discarded or applied.

## 16. Review and tag editing

### 16.1 First-release tag editing

Anki tags belong to notes, even when the UI presents them alongside cards. The first V4 release allows the user to propose tag additions and removals for selected existing notes and to edit the initial tags on generated notes.

Each existing-note proposal stores:

```text
note_id
current_tags
add_tags
remove_tags
expected_pre_apply_tag_hash
tag_policy_version
```

Tag roots are classified:

- `pipeline_owned`: lecture, block, and approved local curation namespaces; add and remove are allowed.
- `approved_optional`: specifically configured optional-tag roots that passed the AnkiHub namespace test; add and remove are allowed.
- `source_managed`: AnKing, AnkiHub identifiers, and other protected source roots; removal is blocked.
- `unknown`: addition is allowed only after explicit review; removal is blocked in the first release.

The review UI shows an exact before/after tag diff. The same tag cannot appear in both add and remove sets. Tags must pass length, character, hierarchy, and configured-root validation.

Apply uses separate idempotent AnkiConnect `addTags` and `removeTags` operations rather than replacing the complete tag list. This avoids erasing tags added concurrently by AnkiHub or the user. Every operation is re-queried and verified after local application and again after synchronization.

Generated notes expose an editable initial tag list. Their required deterministic generation marker is not a user-editable tag.

### 16.2 Review experience

The review page has four clearly labeled groups:

1. Existing cards found in Pass 1 — selected by default.
2. Existing cards recovered from lecture sources in Pass 2 — selected by default and visibly marked as recovered.
3. Generated gap cards — selected by default, editable, with source evidence available.
4. Unresolved concepts — unselected, with status `manual_source_review`, `lcl_correction`, or `rescue_failed`.

Each result shows:

- Card text.
- Concept and objective.
- Retrieval pass.
- Short judgment reason.
- Confidence.
- Retrieval provenance.
- Source evidence for Pass 2 and generated notes.
- Dedupe relationship.
- Current tags and proposed additions/removals.

Warnings require acknowledgment before Apply:

- Fewer than 10 selected existing notes.
- More than 40% of concepts still unresolved after Pass 2.
- Low embedding coverage.
- Stale index.
- Source-evidence conflicts.
- Provider failures or manually routed judgments.

## 17. Local apply and synchronization

### 17.1 Preflight

Before any mutation:

1. Verify AnkiConnect on exactly `127.0.0.1:8765`.
2. Verify the active profile and target note type.
3. Verify selected existing-note hashes.
4. Verify no unresolved prior sync-blocked job exists.
5. Run a leading sync.
6. Abort before writes if the leading sync fails or requests a full sync.

### 17.2 Apply

Build an immutable envelope containing:

- Selected tag-add and tag-remove operations.
- Selected generated notes.
- Deterministic note markers and content hashes.
- Expected existing-note hashes.
- Index and source fingerprints.

Apply locally in bounded, idempotent operations:

1. Add approved tags to existing notes.
2. Remove approved mutable tags from existing notes.
3. Add selected generated notes with their reviewed initial tags.
4. Verify local tag diffs and generated-note markers.
5. Run the trailing sync.
6. Verify local state again and record the final receipt.

### 17.3 Honest sync states

A trailing sync happens after local writes, so it cannot guarantee "apply nothing" on failure.

Required states:

- `complete`: writes verified locally and trailing sync completed.
- `applied_local_sync_retryable`: local writes verified; transient sync failure; bounded retry allowed.
- `applied_local_sync_blocked`: local writes verified; full sync or non-retryable sync decision required.
- `apply_partial`: one or more operations failed; idempotent reconciliation required.
- `failed_before_apply`: no writes occurred.

When `applied_local_sync_blocked` occurs:

- Stop all subsequent curation writes.
- Preserve the envelope, operation ledger, created note IDs, and receipt.
- Surface a blocking alert.
- Never choose upload or download direction automatically.
- Require collection backups and deliberate operator recovery.

## 18. AnkiWeb and AnkiHub operating policy

- AnkiHub is authenticated on the NUC only.
- The Mac has sync on profile open and close enabled.
- The NUC performs leading and trailing sync around each approved apply.
- Initial migration is performed with backups of both profiles and a disposable-profile rehearsal.
- Routine reviews on the Mac are allowed.
- Automated curation, optional-tag mutation, note creation, and AnkiHub operations occur only on the NUC.

## 19. Artifacts and audit trail

Each job stores:

```text
jobs/<job_uuid>/
├── input-manifest.json
├── semantic-snapshot-manifest.json
├── companion-index-manifest.json
├── slides-extracted.json
├── transcript-passages.jsonl
├── source-index-manifest.json
├── lcl.json
├── retrieval-pass-1.jsonl
├── judgments-pass-1.jsonl
├── missed-concepts.json
├── source-evidence.jsonl
├── retrieval-pass-2.jsonl
├── judgments-pass-2.jsonl
├── dedupe.json
├── gaps.json
├── tag-changes.json
├── envelope.json
└── receipt.json
```

Artifacts are content-addressed. A changed lecture source invalidates the LCL and downstream stages. A changed semantic snapshot invalidates retrieval and downstream stages but does not require re-extracting lecture sources.

## 20. Failure handling

| Failure | Required behavior |
|---|---|
| Anki unavailable | Attempt one configured launch, wait, retry, then fail with an actionable message |
| Voyage indexing outage | Retain the last compatible snapshot; quarantine changed notes and block jobs if coverage falls below threshold |
| Semantic snapshot invalid | Reject the replacement and retain the last known-good atomic snapshot |
| Embedding dimension/model mismatch | Block all semantic retrieval |
| Missing embedded AnKing notes | Block below 99.5% coverage and report exact IDs |
| Duplicate or conflicting note records | Quarantine the note and report its card IDs |
| Voyage query outage | Retry with backoff; reuse content-addressed query cache |
| LCL concept unsupported by sources | Mark `lcl_correction`; do not generate a card |
| Pass 2 provider failure | Mark `rescue_failed`; retain Pass 1 results and allow retry |
| Insufficient source evidence | Mark `manual_source_review`; do not generate |
| Generated note not entailed | Route to manual review; never apply by default |
| Protected tag removal requested | Reject the operation before envelope creation and show the governing tag policy |
| Note tags changed after review | Refuse that note's tag patch, refresh its before/after diff, and require approval again |
| Open Anki Add/Browse dialog | Retry write twice with backoff, then request that dialogs be closed |
| Leading sync requires full sync | Fail before apply |
| Trailing sync requires full sync | Mark `applied_local_sync_blocked` and stop writes |
| Duplicate apply request | Reconcile operation hashes and deterministic note markers |
| Thin result set | Warn and require acknowledgment; block if configured minimum safety threshold is violated |

## 21. Security

- AnkiConnect remains loopback-only and is never exposed through Cloudflare or the tailnet.
- Apply routes require the existing authenticated Hub session, CSRF protection, and explicit review approval.
- Provider secrets remain in operating-system secret storage.
- No API keys appear in artifacts, logs, debug views, envelopes, or receipts.
- Uploaded lecture sources and generated artifacts use bounded paths and content validation.
- Semantic snapshots are Hub-owned and read-only outside atomic refresh.
- Cloudflare access does not grant direct access to AnkiConnect.

## 22. Testing strategy

### 22.1 Unit tests

- Semantic manifest validation and atomic replacement.
- Float16 persistence, normalization, and dimension rejection.
- Note-level indexing, duplicate quarantine, and multi-deck preservation.
- Trusted source-family counting.
- Deck-scoped tag, lexical, and semantic retrieval.
- Four-query semantic fusion.
- Ranked RRF versus unranked evidence boosts.
- Source localization and evidence-bundle boundaries.
- Missed-topic state transitions.
- Source-grounded query generation schema.
- Gap eligibility and unsupported-concept rejection.
- Generated-note provenance and deterministic markers.
- Tag-root policy, before/after diffs, and conflicting tag operations.
- Idempotent add/remove tag operations and protected-root rejection.
- Apply and sync state transitions.

### 22.2 Integration tests

- Incremental semantic refresh with unchanged, changed, deleted, and failed notes.
- Atomic semantic snapshot replacement with a simulated interrupted build.
- Companion rebuild from Hub-owned vectors plus mocked AnkiConnect metadata.
- FTS5 and semantic retrieval across the same eligible-note universe.
- Pass 1 miss followed by Pass 2 recovery.
- Pass 1 and Pass 2 miss followed by grounded card generation.
- Unsupported LCL concept corrected without card generation.
- Interrupted stage recovery using content-addressed artifacts.
- Leading sync failure with zero mutations.
- Trailing full-sync simulation after verified local writes.
- Replayed envelope with no duplicate tag or note effects.
- Concurrent tag change after review causing a safe stale-patch refusal.

### 22.3 Retrieval evaluation

Build a manually labeled gold set from at least three representative lectures and 40–60 concepts.

Measure:

- Recall@20 and Recall@50 for manually kept existing notes.
- Mean reciprocal rank.
- Candidate diversity by distinct note ID.
- Pass 2 recovery rate.
- Fraction of gaps avoided by Pass 2.
- False recovery rate: Pass 2 cards incorrectly accepted.
- Unnecessary generated-card rate.
- Per-concept and per-lecture latency.

Compare:

- Current FastEmbed semantic retrieval.
- Voyage semantic retrieval.
- Voyage plus lexical/tag fusion.
- Full V4 with source-grounded Pass 2.

No manually kept gold note may be absent from the combined candidate set without being recorded as a retrieval defect.

### 22.4 Real-system acceptance

Use a disposable Anki profile before the production collection:

1. Build the integrated Voyage index on the NUC.
2. Interrupt and resume an incremental refresh without damaging the last good snapshot.
3. Reconcile eligible note and vector coverage.
4. Run one real lecture through Pass 1 and Pass 2.
5. Review source evidence for every recovered and generated note.
6. Apply an envelope that adds and removes approved tags across three notes and creates one note.
7. Replay the envelope and confirm no duplicate effects.
8. Simulate transient and full-sync failures.
9. Confirm correct rendering with the production note type.
10. Confirm the Mac receives the changes after a safe sync.

## 23. Calibration gates

Before deterministic triage affects model calls:

- AUTO-INCLUDE agrees with model judgment at least 95%.
- AUTO-DROP false-drop rate remains below 2%.
- Both hold for two consecutive representative lectures.

Before generated cards are selected by default:

- Every generated fact is entailed by its recorded source bundle in the acceptance set.
- Duplicate generation rate is below 2%.
- Pass 2 has been shown to reduce unnecessary generated cards.

Before production apply:

- Optional-tag namespace survives AnkiHub sync.
- NUC/Mac full-sync recovery is rehearsed.
- Embedding coverage meets the threshold.
- Voyage credential storage and semantic snapshot recovery pass review.
- Protected-tag removal and stale tag-patch tests pass.
- Default test suite passes without suppressed resource warnings.

## 24. Reuse and retirement map

### Retain and adapt

- `oms_hub.anki.domain`
- `oms_hub.anki.models`
- `oms_hub.anki.repository`
- `oms_hub.anki.contracts`
- `oms_hub.anki.normalize`
- `oms_hub.anki.domains`
- `oms_hub.anki.paths`
- FTS and atomic vector-store patterns from `oms_hub.anki.index`
- `oms_anki_agent.ankiconnect`, moved behind a local Hub gateway
- Existing migrations and safety tests

### Replace

- `FastEmbedder` production wiring with the integrated `SemanticIndex`.
- Full/delta Mac snapshot ingestion with local Anki metadata reads and Hub-owned Voyage indexing.
- Per-query semantic loops with batched NumPy matrix retrieval.
- Tag-count `source_count` with distinct source-family counting.
- The existing add-only tag contract with reviewed add/remove tag patches and protected-root policy.
- The existing single-sync envelope assumption with explicit leading-sync preflight and honest post-write sync states.

### Retire after acceptance

- Mac agent service and CLI.
- launchd installer and plist.
- Hub-agent polling, heartbeat, bearer authentication, and snapshot transport.
- Tailscale-specific agent routes.

Retirement occurs only after the local NUC path passes real-system acceptance. Until then, the old branch remains a rollback reference.

## 25. Delivery phases

1. **Prerequisites and migration safety**
   Backups, optional-tag test, AnkiHub single-host policy, disposable profiles, NUC auto-start, credential decision.

2. **Integrated semantic subsystem**
   Local Anki metadata export, Voyage document indexing, content-hash refresh, atomic vectors, deck preservation, coverage report.

3. **Companion and source indexes**
   Port normalization/FTS, correct source counting, local Anki metadata, slide/transcript segmentation and search.

4. **Local Anki runtime, tag policy, and apply coordinator**
   Move the tested AnkiConnect client, add tag add/remove contracts, protected roots, preflight, leading sync, idempotent writes, honest sync states, verification.

5. **LCL V4**
   Add stable source references, hypothetical card, and paraphrases.

6. **Retrieval Pass 1**
   Batched Voyage queries, semantic sub-fusion, lexical/tag evidence, provenance, shadow logging.

7. **Missed-topic rescue and Pass 2**
   Source localization, evidence assessment, source-grounded queries, second retrieval and judgment.

8. **Dedupe and grounded gap generation**
   Combine both passes, apply survivor rules, generate only supported gaps, validate provenance.

9. **Review UI and audited apply**
   Display pass/source distinctions, warnings, edits, envelope, receipt, and sync-blocked recovery.

10. **Calibration and production enablement**
    Gold-set evaluation, threshold fitting, disposable-profile acceptance, production migration, V2 transport retirement.

The first meaningful milestone is the end of Phase 4: a validated Hub-owned semantic snapshot, companion index, source index, and a hand-written tag-add/tag-remove envelope can be applied safely on the NUC. The first quality milestone is the end of Phase 7: a real missed concept can be recovered from an existing card through lecture-source re-querying.

## 26. Architecture acceptance criteria

The V4 architecture is implemented successfully when:

1. The Hub can build and incrementally refresh its own Voyage index without a semantic-search add-on.
2. Every vector used for retrieval maps deterministically to one note and all of that note's deck memberships.
3. Pass 1 and Pass 2 search the same declared eligible-note universe.
4. An uncovered concept is re-searched using cited lecture evidence before generation.
5. Unsupported or weakly supported concepts do not produce automatic cards.
6. Every generated card has durable source provenance and passes source-entailment validation.
7. Existing cards recovered in Pass 2 are distinguishable in review and audit artifacts.
8. Apply is idempotent and reports whether failure occurred before writes, after local writes, or during sync.
9. The system never chooses a full-sync direction.
10. Reviewed tag additions and removals are policy-checked, stale-safe, idempotent, and verified after sync.
11. Retrieval and generation quality meet the calibration gates before production automation is enabled.
