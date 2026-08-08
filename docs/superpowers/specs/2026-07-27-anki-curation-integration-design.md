# One-Click Anki Curation Integration Design

**Status:** Approved design, ready for implementation planning  
**Date:** 2026-07-27  
**Repository:** `connor-brooks98/oms-study-automation`  
**Source designs:** `anki_curation_one_click_design_v2.md` and `anki_curation_implementation_plan.md`

## 1. Goal

Add an interactive, single-lecture Anki curation workflow to the existing Study Hub. The Hub will index the complete AnKing deck, build a structured lecture concept ledger from the current lecture artifacts, retrieve and judge relevant AnKing notes, propose editable custom gap cards, support optional back-side images, and send one approved action envelope to a Mac companion agent for application and AnkiWeb sync.

The normal user experience is:

1. Choose Course → Exam/Block → Lecture.
2. Optionally paste the AMBOSS note-ID string.
3. Review or edit the saved lecture-specific curation instructions.
4. Start curation.
5. Scan the prepared review page.
6. Optionally edit gap cards or choose/generate images.
7. Click Apply once.

## 2. Confirmed project context

The current application is a Python 3.12 FastAPI service with:

- SQLAlchemy and SQLite persistence.
- Jinja templates and small page-specific JavaScript files.
- A server bound to `127.0.0.1:8765`.
- Cloudflare Tunnel and Cloudflare Access protecting the remote dashboard.
- A background ingestion worker running in the Hub process.
- Current slide and transcript revisions stored through the existing artifact system.
- OpenAI, Gemini, and Anthropic provider adapters.
- Provider credentials stored in Windows Credential Manager.
- A placeholder `/anki` page and existing lecture-detail pages.

The Anki feature will extend those patterns rather than introduce a second application stack.

## 3. Confirmed Anki conventions

### 3.1 Source deck

The complete source collection to index is:

```text
Anking Step Deck
```

### 3.2 Owned lecture-tag namespace

The root namespace is:

```text
AnkiHub_Optional::LMU_OMS_II
```

The namespace has passed the AnkiHub sync/restart persistence test. The user owns the optional tag group, so the curator may write lecture tags beneath it. The Mac agent will still verify post-sync persistence after every applied envelope.

Lecture tags follow:

```text
AnkiHub_Optional::LMU_OMS_II::<CourseWithoutSpaces>::Block<N>::Lec<N>_<Topic>
```

Example:

```text
AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I
```

### 3.3 Generated-card deck hierarchy

Generated gap cards live under:

```text
OMS-II_Custom_Cards::<Course_With_Underscores>::Exam_<N>::Lec<N>_<Topic>
```

Example:

```text
OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I
```

Deck and tag paths intentionally use different course and exam/block conventions. One shared path builder will derive both from the Hub's lecture metadata and preview them before a job begins.

### 3.4 Generated-card note type

Generated notes use:

```text
AnKingOverhaul (OMS_II_Extra/JCBrooks)
```

The agent will query the note type's fields at runtime. It will populate `Text` and `Extra`, supply all required fields, and leave unused fields blank. It will not hardcode the complete AnKing field list.

## 4. Chosen architecture

The selected approach is an integrated modular monolith plus a narrow Mac companion agent.

### 4.1 NUC responsibilities

The existing Study Hub remains the system of record and owns:

- Index ingestion and querying.
- Lecture Concept Ledger generation.
- Candidate retrieval and scoring.
- Model judgment and caching.
- Deduplication and gap detection.
- Custom-card authoring.
- Media suggestion ranking and generated-image storage.
- Review and approval UI.
- Envelopes, receipts, job history, usage, and costs.

A dedicated curation worker serializes Anki curation jobs. The existing ingestion worker remains independent so slide and transcript ingestion can continue while a curation job runs.

### 4.2 Mac responsibilities

The Mac companion agent:

- Talks to AnkiConnect only on `127.0.0.1:8765`.
- Exports AnKing note metadata, fields, tags, cards, and media references.
- Uploads only specifically requested media files for review previews.
- Polls for approved envelopes.
- Opens Anki automatically when an envelope is waiting and AnkiConnect is unavailable.
- Verifies selected existing notes have not changed since indexing.
- Stores generated media.
- Adds lecture tags and generated notes.
- Triggers Anki sync.
- Verifies post-sync results and posts a receipt.

The agent is a user LaunchAgent, not a system daemon, because AnkiConnect exists in the logged-in user session.

### 4.3 Network boundary

The Hub continues binding to loopback. Tailscale Serve provides the Mac agent's tailnet-only route to the existing application. Agent endpoints require a high-entropy bearer token stored in Windows Credential Manager and macOS Keychain.

The Hub security middleware will:

- Accept the configured Tailscale Serve hostname for `/agent/*`.
- Require bearer authentication for every agent request.
- Reject `/agent/*` when reached through the Cloudflare public hostname.
- Preserve the existing Cloudflare Access and CSRF behavior for dashboard routes.
- Never expose AnkiConnect outside Mac loopback.

## 5. End-to-end flow

1. The Mac agent produces the initial full snapshot of `Anking Step Deck`.
2. The Hub normalizes the snapshot and builds the FTS, tag, domain, media-reference, and embedding indexes.
3. The user opens `/anki` and chooses a lecture.
4. The Hub previews the generated deck and tag paths.
5. The user optionally pastes an AMBOSS note-ID expression and edits the saved lecture instructions.
6. The curation worker validates current slide and cleaned-transcript revisions.
7. Gemini builds or reuses the LCL.
8. Retrieval searches relevant soft domains and then the full deck as a safety net.
9. Claude judges candidates during shadow mode.
10. Kept candidates are deduplicated within concept clusters.
11. Uncovered concepts become editable gap-card proposals.
12. The Hub requests the few AnKing media files needed for image suggestions.
13. The review page opens with accepted existing cards and gap cards selected.
14. The user may edit gap-card fields, change/remove images, accept no image, or explicitly invoke GPT Image 2.
15. Apply creates an immutable, idempotent action envelope.
16. The Mac agent preflights Anki and verifies target-note hashes.
17. The agent applies operations in order, syncs, verifies the result, and posts a receipt.
18. The Hub displays success, warnings, or actionable recovery instructions.

## 6. Storage design

### 6.1 Durable Hub state

Durable workflow state remains in the existing `hub.db`.

New SQLAlchemy tables:

- `anki_curation_instructions`
  - Current saved instruction text per lecture.
  - Version, SHA-256 hash, and update time.

- `anki_curation_jobs`
  - UUID, lecture ID, state, target paths, index snapshot, AMBOSS input, instruction snapshot, prompt/rubric versions, warnings, counts, errors, and timestamps.

- `anki_job_stages`
  - Stage state, attempt count, timing, provider, model, request ID, token usage, cache usage, estimated cost, and error details.

- `anki_candidates`
  - Note ID, content hash, best concept, provenance, component scores, predicted band, verdict, confidence, short reason, context-trap flag, recall direction, mnemonic classification, dedupe disposition, and review selection.

- `anki_gap_cards`
  - Concept ID, editable `Text`, editable `Extra`, revision number, selection state, image state, media filename, source note ID, generated-image metadata, and validation status.

- `anki_verdict_cache`
  - Note content hash, lecture ID, LCL version, rubric version, instruction hash, verdict payload, provider, model, and usage.

- `anki_envelopes`
  - Immutable envelope payload, snapshot ID, state, timestamps, and receipt summary.

- `anki_envelope_operations`
  - Operation UUID, type, content hash, payload, state, attempts, result, and error.

- `anki_agent_state`
  - Agent heartbeat, versions, active snapshot, last export, last sync, and health.

- `anki_stage_settings`
  - Stage, provider, model, enabled state, and stage-specific options.

### 6.2 Rebuildable card index

The rebuildable index lives under:

```text
C:\ProgramData\OMSStudyHub\anki\index\
```

`cards.sqlite3` contains:

- `notes`
  - Note ID, model name, compact normalized text, compact Extra text, raw field HTML, kept tags, source count, normalized token signature, content hash, and embedding row.

- `note_tags`
  - One normalized tag per note and hierarchy-prefix metadata.

- `note_domains`
  - Multi-valued deterministic domain assignments such as Cardio, Micro, Pharm, Renal, and Heme.

- `note_media`
  - Note ID, field name, media filename, media type, and field order.

- `notes_fts`
  - FTS5 index over normalized text and compact Extra text.

- `index_meta`
  - Snapshot ID, fingerprint, note count, build time, export version, and embedding model.

Embeddings live in an atomically replaced NumPy `float32` matrix with a parallel note-ID order file. Delta refreshes update, append, or remove rows and replace the files atomically.

### 6.3 Job artifacts

Each job receives:

```text
C:\ProgramData\OMSStudyHub\anki\jobs\<job_uuid>\
├── input-manifest.json
├── slides-extracted.json
├── lcl.json
├── retrieval.jsonl
├── judgments.jsonl
├── gaps.json
├── media\
├── envelope.json
└── receipt.json
```

The input manifest records source hashes, instruction hash, AMBOSS hash, accepted prompt fingerprint, prompt/rubric/schema versions, index snapshot, providers, and models. Identical reruns reuse valid artifacts. Input changes invalidate only dependent stages.

## 7. Module boundaries

### 7.1 Hub modules

```text
src/oms_hub/anki/
├── domain.py
├── models.py
├── repository.py
├── paths.py
├── snapshot.py
├── normalize.py
├── domains.py
├── embeddings.py
├── index.py
├── lcl.py
├── amboss.py
├── retrieval.py
├── judgment.py
├── dedupe.py
├── gaps.py
├── media.py
├── envelope.py
├── pipeline.py
├── worker.py
└── prompts/
    ├── lcl.md
    ├── judgment.md
    ├── dedupe_close_call.md
    ├── gap_card.md
    └── image.md
```

Responsibilities:

- `domain.py`: immutable domain types, enums, and public interfaces.
- `models.py`: Anki-specific SQLAlchemy models registered with the existing declarative base.
- `repository.py`: durable job, review, cache, envelope, and receipt persistence.
- `paths.py`: canonical deck/tag naming and bounded job/index paths.
- `snapshot.py`: snapshot schema validation and index-update orchestration.
- `normalize.py`: HTML, cloze, text, tag, token-signature, and media-reference normalization.
- `domains.py`: deterministic multi-domain assignment from AnKing tags.
- `embeddings.py`: FastEmbed model and atomic vector storage.
- `index.py`: tag, domain, FTS, semantic, and note retrieval interfaces.
- `lcl.py`: PPTX extraction, image-slide fallback, schema validation, and LCL caching.
- `amboss.py`: strict `nid:<digits> OR ...` parsing and missing-ID reporting.
- `retrieval.py`: regime detection, focused/global search, union, provenance, and scoring.
- `judgment.py`: compaction, batching, provider routing, schema validation, retries, and verdict caching.
- `dedupe.py`: concept clusters, forward/reverse classification, mnemonic preference, and survivor selection.
- `gaps.py`: gap detection, custom-card prompt loading, proposals, and validation.
- `media.py`: media candidate ranking, preview requests, and explicit image generation.
- `envelope.py`: immutable operation construction and hashes.
- `pipeline.py`: state transitions and stage orchestration.
- `worker.py`: one-job-at-a-time curation worker with interrupted-job recovery.

### 7.2 Web integration

```text
src/oms_hub/web/
├── anki_routes.py
├── templates/anki.html
├── templates/anki_review.html
├── static/anki.js
└── static/app.css
```

The existing `anki.html` placeholder becomes the launch/history screen. `anki_review.html` owns review and approval. Existing CSS patterns and accessibility conventions remain in use.

### 7.3 Mac agent

```text
src/oms_anki_agent/
├── config.py
├── ankiconnect.py
├── hub_client.py
├── snapshot.py
├── media.py
├── apply.py
├── ledger.py
├── service.py
└── cli.py
```

The package exposes an `oms-anki-agent` command. The same repository contains a macOS LaunchAgent template and installation helper under `scripts/macos/`.

### 7.4 LLM layer

The current transcript-cleaning API remains compatible. The LLM layer gains capability-focused structured-text and image-generation interfaces. Stage routing sits above provider adapters so `oms_hub.anki` contains no provider-specific HTTP behavior.

Initial routes:

- LCL: Gemini.
- Judgment: Claude Sonnet.
- Gap-card authoring: Claude Sonnet.
- Image generation: OpenAI `gpt-image-2`.

All stage model strings remain configurable in Settings.

## 8. Index and refresh behavior

### 8.1 Full export

The Mac agent:

- Finds all notes in `Anking Step Deck`.
- Calls `notesInfo` in bounded chunks.
- Exports note IDs, model names, fields, tags, cards, and media references as JSONL.
- Maintains a local content-hash ledger.
- Emits a manifest with note count, ID-set hash, content fingerprint, versions, and timestamp.

### 8.2 Incremental refresh

Daily refresh:

- Finds the current full note-ID set to detect deletions.
- Uses an edit window derived from the last successful export time plus safety margin.
- Fetches full metadata only for added or changed notes.
- Updates the Mac ledger and Hub index.
- Falls back to a full export when the elapsed window is unsafe or reconciliation fails.

A full rebuild is available from the UI and is required after AnKing releases. A periodic full reconciliation catches drift that edit-window queries cannot detect.

### 8.3 Envelope staleness checks

The envelope carries:

- Index snapshot ID.
- Expected content hash for every selected existing note.

Immediately before applying, the agent re-reads selected notes and compares hashes. A changed or deleted target note causes refusal, refresh, and rerun. Unrelated collection changes do not block application.

## 9. Lecture Concept Ledger

The LCL is generated from:

- Current canonical PPTX.
- Current cleaned transcript.
- Saved lecture-specific curation instructions.
- Vision descriptions for slides with insufficient extracted text.

PPTX extraction includes:

- Slide order and numbers.
- Shape text.
- Grouped shapes.
- Table cells.
- Speaker notes.

LCL schema:

- Lecture ID and schema version.
- Objectives.
- Concepts.
- Objective links.
- Depth.
- Emphasis.
- Keywords, synonyms, and abbreviations.
- Context traps.
- Soft domains.
- Excluded facts and reasons.
- Slides requiring vision fallback.

The saved lecture instruction is part of the LCL cache key. Changing it intentionally invalidates the LCL and every downstream stage.

## 10. Retrieval

Every concept is searched against the complete indexed AnKing deck.

Retrieval sources:

- Lecture tag.
- Block tag.
- Focused-domain semantic search.
- Focused-domain lexical search.
- Smaller whole-deck semantic safety-net search.
- Smaller whole-deck lexical safety-net search.
- Exact AMBOSS note IDs.

Lecture, block, and AMBOSS hits bypass domain restrictions.

Initial shadow score:

```text
3.0 × lecture-tag match
2.0 × block-tag match
2.0 × high semantic match
1.0 × lower semantic match
2.0 × exact AMBOSS note-ID match
1.0 × at least two lexical keyword matches
0.5 × trusted source count
```

The exact thresholds are calibration parameters rather than fixed production values.

AMBOSS input supports only the observed format:

```text
nid:<digits> OR nid:<digits> OR ...
```

The parser trims whitespace, deduplicates IDs, rejects malformed fragments, and reports valid IDs that are absent from the current collection.

## 11. Judgment and shadow mode

During initial shadow mode, every candidate is judged by Claude regardless of predicted band.

Candidate compaction includes:

- Note ID.
- Normalized card text.
- Compact Extra text.
- Kept tags.
- Best concept.
- Retrieval provenance.
- Domain source.

The LCL and rubric form a byte-stable cacheable prefix. Candidate batches contain about 25 notes.

Required response per note:

- `nid`
- `verdict`
- `concept_id`
- `reason`
- `context_trap`
- `confidence`
- `recall_direction`
- `mnemonic_style`

The Hub:

- Validates strict structured output.
- Rejects duplicate or missing note IDs.
- Retries a malformed batch once.
- Routes a second failure to manual review.
- Never silently drops a candidate.

Shadow mode ends only after:

- AUTO-INCLUDE agrees with model judgments at least 95%.
- AUTO-DROP false-drop rate is below 2%.
- Both hold for two consecutive lectures.

## 12. Deduplication

Deduplication runs only within a concept cluster.

Similarity rules:

- At least 0.85: deterministic duplicate pair.
- 0.70–0.85: Claude close-call adjudication.

Survivor priorities:

1. Best alignment with the lecture objective.
2. More clinically useful recall direction.
3. Not a context trap.
4. Less guessable.
5. Higher trusted-source count.
6. Sketchy or First Aid provenance.
7. More concise wording.

Forward/reverse cards are explicitly classified. Only one is retained unless the LCL states that both directions represent independently required knowledge.

Mnemonic rule:

- A direct "name the mnemonic" card outranks a card that requires listing every mnemonic component.

Losing existing cards are not tagged. They are never edited, moved, suspended, or deleted.

## 13. Gap cards and custom prompt

Every LCL concept without a surviving card becomes a gap. Gap proposals are ranked by depth and emphasis, but all remain reviewable.

Custom-card rules come from a user-maintained Obsidian Markdown file.

Settings provide:

- Prompt path.
- Current SHA-256 fingerprint.
- Validation status.
- Last-checked time.
- Preview.
- "Accept this revision" control.

The Anki launch page displays the active filename and fingerprint. The file must be UTF-8 Markdown, below the configured size limit, and located inside an approved prompt root.

A changed prompt pauses new gap generation until accepted. It invalidates only gap-card generation, not LCL, retrieval, or judgment.

Each gap proposal:

- Uses one atomic cloze.
- Uses the accepted custom-card prompt.
- Provides editable `Text` and `Extra`.
- Carries its concept and source explanation.
- May be selected, edited, or omitted.

## 14. Image behavior

Images are encouraged but optional. A selected gap card may be applied without an image.

### 14.1 Existing AnKing images

For each gap card, the Hub:

1. Searches same-concept cards.
2. Expands to semantically related cards in relevant domains.
3. Excludes icons, logos, tiny files, unsupported media, and duplicates.
4. Requests only top media previews from the Mac.
5. Shows up to three suggestions.
6. Preselects the best-scoring suggestion.

The user may change or remove the selection. Reused media references the filename already present in Anki.

### 14.2 GPT Image 2

Image generation never runs automatically.

The explicit Generate Image action shows:

- Generated, editable prompt.
- Model.
- Size.
- Quality.
- Estimated cost.
- Confirmation.

Default:

```text
Model: gpt-image-2
Size: 1024x1024
Quality: medium
```

The existing OpenAI API key in Windows Credential Manager is used. Provider testing checks that GPT Image access and any required organization verification are complete.

Generated images are engagement aids, not medical evidence. The user must review and select the result. The image receives a deterministic content-hash filename and is appended beneath the explanation in `Extra`.

Reference: <https://developers.openai.com/api/docs/guides/image-generation>

## 15. Review UI

The review page contains:

- Job summary, derived paths, instructions, models, usage, and warnings.
- Accepted AnKing cards, selected by default.
- Generated gap cards, selected by default and editable.
- Dropped or unresolved cards, unselected by default.

Each card row displays:

- Card text.
- Mapped concept.
- Short reason.
- Confidence.
- Retrieval provenance.
- Dedupe relationship where applicable.

Warnings are prominent but do not stop a valid job:

- Fewer than 10 accepted existing cards.
- More than 40% of concepts initially uncovered.
- Missing AMBOSS note IDs.
- Slides requiring vision fallback.
- Low-confidence or manually routed batches.
- Stale index age.

Warnings require acknowledgment before Apply.

## 16. Envelope and receipt behavior

Ordered operations:

1. `store_media`
2. `add_tags`
3. `add_notes`
4. `sync`
5. post-sync verification

Tag operations contain at most 1,000 note IDs.

Idempotency layers:

- Immutable envelope UUID.
- Immutable operation UUID and content hash.
- Persistent Mac operation ledger.
- Deterministic media filenames.
- Invisible content-hash marker in generated note text.

If the Mac crashes after Anki creates a note but before posting a receipt, the agent rediscovers the matching marker instead of creating a duplicate.

Receipt data:

- Per-operation status.
- Created note IDs.
- Media filenames.
- Sync status.
- Post-sync tag/media verification.
- Errors and recovery action.

Anki dialog conflicts receive two bounded retries. Persistent conflicts surface a clear instruction to close Anki Add/Browse dialogs and retry.

## 17. Testing

### 17.1 Unit

- Deck/tag paths.
- AMBOSS parser.
- HTML and cloze normalization.
- Tag and domain classification.
- Retrieval scoring.
- Forward/reverse classification.
- Mnemonic survivor rule.
- Gap detection.
- Media ranking.
- Envelope hashing.

### 17.2 Integration

- Temporary Hub and index databases.
- FTS5 retrieval.
- Embedding replacement and deltas.
- Job transitions and interrupted recovery.
- Provider routing with mocked responses.
- Prompt revision acceptance and cache invalidation.
- Tailscale-host and Cloudflare-host security behavior.

### 17.3 Contract

- Snapshot manifests.
- Media requests.
- Envelopes and operations.
- Receipts.
- Fake AnkiConnect failure and retry cases.

### 17.4 Real-system acceptance

- Disposable Anki profile first.
- Full snapshot note-count reconciliation.
- Handwritten envelope tags three notes and adds one note.
- Re-sending produces no duplicate effects.
- Sync verification confirms lecture-tag persistence.
- Generated note renders correctly with and without an image.

## 18. Quality gates

Before model judgment is considered valid:

- One representative lecture is manually curated.
- Every manually kept card appears in retrieval.
- Any miss is treated as a retrieval defect.

Before deterministic triage is enabled:

- AUTO-INCLUDE agreement is at least 95%.
- AUTO-DROP false-drop rate is below 2%.
- Both hold for two consecutive lectures.

Domain focusing must demonstrate that cross-domain relevant cards remain discoverable through the whole-deck safety net.

## 19. Delivery milestones

1. Foundation and shared contracts.
2. Read-only Mac bridge and index.
3. Proven idempotent write path.
4. LCL and structured provider routing.
5. Retrieval shadow pipeline.
6. Judgment and deduplication.
7. Gap cards and existing-media suggestions.
8. Review and Apply UI.
9. Explicit GPT Image 2 generation.
10. Shadow calibration and production enablement.

Each milestone ends with a runnable demonstration and focused test/commit series.

Deferred:

- Overnight or multi-lecture batching.
- Opus escalation.
- Biomedical embedding-model A/B testing.
- Automatic image generation.

## 20. Operations and recovery

- Daily incremental refresh.
- Periodic full index reconciliation.
- Full rebuild after AnKing releases.
- Agent heartbeat warning after 24 hours.
- Index age shown before job launch.
- Durable job artifacts and receipts included in normal Hub backup.
- Rebuildable index/vector files may be excluded from backup.
- Receipt records provide exact touched and created note IDs.
- Stopping the agent or disabling the feature preserves pending envelopes and makes no further Anki changes.
- The initial release never deletes, suspends, moves, or edits an existing AnKing note.

## 21. Explicit first-release exclusions

The first release does not:

- Run multiple lecture jobs in an overnight provider batch.
- Automatically generate images.
- Use a separate NUC microservice.
- Install Anki on the NUC.
- Expose AnkiConnect to the tailnet.
- Edit or remove existing AnKing note content.
- Delete or suspend cards during deduplication.
