# Integrated Anki Curation NUC Rollout

This is the operating and acceptance guide for the V4 Anki workflow. Study Hub
owns the pipeline, indexes, review UI, mutation plan, and receipts. Anki remains
the collection system of record and is reached only through AnkiConnect on the
same NUC.

V4 does not call the semantic-search research add-on as a separate tool, import
its package, inspect its files at runtime, or require `sbm_smart_anki`. It also
does not perform AMBOSS retrieval. The old Mac bridge remains in the repository
only until copied-profile acceptance is approved; do not configure it for V4.

## 1. Safety boundary and port layout

Use these local ports on the NUC:

| Service | Binding | Purpose |
|---|---|---|
| Study Hub | `127.0.0.1:8787` | UI and owned curation worker |
| AnkiConnect | Configured loopback port (`8765` by default) | Local Anki API only |

The two services cannot share a port. V4 configuration rejects that collision
when Anki curation is enabled. Keep AnkiConnect on loopback; never point a
Cloudflare Tunnel, Tailscale Serve rule, firewall opening, or LAN binding at
its configured port. Cloudflare should target the Study Hub port, 8787. During
side-by-side acceptance, AnkiConnect may use another loopback port such as
8766 as long as its add-on and `OMS_HUB_ANKI_CONNECT_URL` settings match.

Anki is a desktop application. Keep the NUC signed in, start Anki in the
interactive Windows session, and disconnect rather than signing out of RDP.
The NUC is the only automated collection writer. The Mac remains the study and
review client through normal AnkiWeb synchronization.

## 2. Back up before installing

1. Sync the NUC collection and confirm Anki reports completion.
2. Close Anki.
3. Create a supported Anki backup/export from the profile. Do not copy a live
   collection database.
4. Stop the `OMS Study Hub V2` scheduled task.
5. Back up `C:\ProgramData\OMSStudyHub`, including `hub.db` and the existing
   `anki` directory.
6. Record the current Git commit with `git rev-parse HEAD`.

The semantic vectors are reproducible, but the Study Hub database, immutable
stage artifacts, apply envelopes, and receipts are audit records and should be
included in the backup.

## 3. Install and configure the one-package build

From an Administrator PowerShell window:

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"
$Branch = "codex/anki-v4-implementation"

Set-Location $ProjectRoot
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
git fetch origin
git switch $Branch
git pull --ff-only origin $Branch
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Set these values in `.env`:

```text
OMS_HUB_DASHBOARD_HOST=127.0.0.1
OMS_HUB_DASHBOARD_PORT=8787
OMS_HUB_ANKI_ENABLED=true
OMS_HUB_ANKI_DATA_DIR=C:\ProgramData\OMSStudyHub\anki
OMS_HUB_ANKI_CONNECT_URL=http://127.0.0.1:8765
OMS_HUB_ANKI_EXECUTABLE_PATH=C:\Program Files\Anki\anki.exe
OMS_HUB_ANKI_SEMANTIC_MODEL=voyage-4-large
OMS_HUB_ANKI_SEMANTIC_DIMENSIONS=1024
OMS_HUB_ANKI_SEMANTIC_MIN_COVERAGE=0.995
OMS_HUB_ANKI_SEMANTIC_BATCH_SIZE=128
OMS_HUB_ANKI_SEMANTIC_QUERY_CACHE_SIZE=512
OMS_HUB_ANKI_PROMPT_DIRECTORY=C:\Services\AnkiPipeline\prompts
OMS_HUB_ANKI_PROMPT_GIT_SYNC=true
OMS_HUB_ANKI_PROMPT_GIT_TIMEOUT_SECONDS=30
```

The prompt directory is a Git checkout of the `AnkiPipeline/prompts` folder
managed from the Obsidian vault. Study Hub pulls it once during job preflight,
resolves the Markdown includes, and pins the resulting content hashes for the
entire run. A pull failure uses the last readable checkout and displays a stale
prompt warning; missing or malformed local prompt files still block the run.

If Cloudflare Tunnel currently targets port 8765, change only its local Study
Hub origin to `http://127.0.0.1:8787`. The external hostname does not need to
change.

## 4. Configure local AnkiConnect

Install AnkiConnect in the NUC copy of Anki, then open its add-on configuration.
Keep the web bind address at `127.0.0.1`. Use port `8765` by default, or a
distinct loopback port such as `8766` for side-by-side acceptance, and set the
same port in `OMS_HUB_ANKI_CONNECT_URL`. Do not allow remote hosts.

With Anki open, verify the read-only version action:

```powershell
$AnkiConnectUrl = "http://127.0.0.1:8765" # Match the configured add-on port.
$Body = @{
    action = "version"
    version = 6
    params = @{}
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri $AnkiConnectUrl `
  -ContentType "application/json" `
  -Body $Body
```

The response should contain a numeric result and a null error. Study Hub
requires AnkiConnect API version 6 or newer.

## 5. Store credentials

Create a Voyage API key with access to `voyage-4-large`, then store it through
the package:

```powershell
.\.venv\Scripts\oms-hub.exe voyage-set-key
```

The prompt does not echo the value. The key is saved under Windows Credential
Manager service `OMSStudyHub`, account `voyage-api-key`; it is never written to
`.env`, SQLite, a job artifact, or the browser.

Configure the selected curation language-model provider under **Settings → AI
providers**. The lecture concept ledger, candidate judgments, rescue queries,
and card generation use that existing provider layer.

## 6. Build or refresh the Anki index

Keep Anki open and stop Study Hub while publishing a new collection
generation. Sync Anki first, then run:

```powershell
Stop-ScheduledTask -TaskName "OMS Study Hub V2" -ErrorAction SilentlyContinue

.\.venv\Scripts\oms-hub.exe anki-index-refresh `
  --deck "AnKing Step Deck"

Start-ScheduledTask -TaskName "OMS Study Hub V2"
```

Use `--deck` for ordinary deck refreshes, especially from Windows PowerShell.
Study Hub constructs the quoted Anki search internally; reserve `--query` for
advanced Anki searches.

The command reads note, card, deck, field, and tag metadata through local
AnkiConnect. It publishes the companion SQLite index and the float16 Voyage
snapshot under `C:\ProgramData\OMSStudyHub\anki`. It prints JSON containing the
active profile, both generation IDs, note counts, semantic coverage, and
duration, plus semantic-matrix size and traced peak memory. Coverage below
99.5% blocks publication as usable input.

The first run embeds every eligible note and can take time and incur Voyage
usage. Later runs reuse unchanged content hashes and embed only added or
changed notes. Refresh before curation when Anki content, deck membership, or
tags changed materially. A running job remains pinned to its original
collection and source generations; refresh between jobs, not during one.

## 7. Start Study Hub and curate a lecture

Start the scheduled task, then open
`http://127.0.0.1:8787/anki` locally or `/anki` through the protected Study Hub
hostname.

1. Choose the lecture. Study Hub requires and pins its current PowerPoint,
   cleaned transcript, and generated NotebookLM outline PDF. The start button
   remains disabled until all three are available.
2. Choose the existing-card deck and optional tag scope.
3. Set the target deck and lecture tag.
4. Choose the configured language-model provider and start curation.
5. Wait for the run to reach **Ready for review**.

For every lecture concept, V4 searches the existing-card index first. If
judgment says the concept is still missing or partial, it locates supporting
passages in the slides, transcript, and outline, generates evidence-grounded
rescue queries, and searches Anki again. The outline provides the concept index,
depth map, and emphasis flags, but it cannot independently justify keeping or
generating a card. A malformed outline missing `DEPTH MAP` or
`PROFESSOR EMPHASIS FLAGS` is rejected before any model tokens are spent.

New runs use the `lecture-concept-ledger` v2 prompt and schema. Its artifact
records the canonical statement, primary entity, aliases, three to six
paraphrases that retain the primary entity, depth, emphasis, importance, and
readable source IDs for each concept. Existing jobs pinned to `lcl-v1` remain
resumable.

New runs also use the `coverage-rubric` v2 contract. Missing facts retain stable
fact IDs and readable passage IDs so later card generation can reconcile every
fact explicitly. After retrieval and coverage judgment, Study Hub audits every
candidate without exposing its matched concept, retrieval query, score, or
earlier rationale. Audit results are cached against the note content, lecture
sources, prompt, provider, and model. Only `keep` verdicts remain selected;
`uncertain` and `drop` remain visible but unchecked.

Concepts with grounded missing facts now continue through convergence passes
3–5. Each pass is its own immutable, restart-safe artifact. Study Hub generates
three primary-entity-preserving search paraphrases, retrieves only previously
unseen notes for that concept, and measures growth as new unique notes divided
by the cumulative concept set. A concept stops independently when coverage is
complete or growth falls below 5%. Concepts still growing after pass 5 do not
fail the run; the review header marks them for manual review. Existing jobs
pinned to the legacy LCL contract retain their prior two-pass behavior.

Coverage is then recomputed from the surviving `keep` supports before dedupe or
gap generation. This ordering means an off-topic card removed by the audit can
create a real, source-grounded missing fact instead of silently making the deck
thinner.

## 8. Review cards and first-release tag edits

The review page groups existing matches, recovered matches, grounded gaps, and
unresolved items. Inspect the exact card text, reason, confidence, and source
citations.

- Select only existing notes that should receive the lecture tag.
- Edit pipeline-owned `OMS::...` tags or the approved
  `AnkiHub_Optional::LMU_OMS_II::...` tag family.
- Source-managed AnKing, AnkiHub, Pathoma, Sketchy, First Aid,
  Boards & Beyond, OME, and UWorld tags are locked.
- Review and edit generated cloze text and Extra content against its cited
  slide/transcript passages.

Saving review does not change Anki. Freeze the apply plan, inspect its exact
add/remove/create counts, and type `APPLY TO ANKI` only when it is correct.

## 9. Apply and recovery meanings

| State | What is true | Operator action |
|---|---|---|
| `failed_before_apply` | Leading sync, preflight, or staleness check failed; no writes occurred | Fix the reported issue, refresh/review if stale, then apply again |
| `complete` | Writes, trailing sync, and read-back verification completed | No recovery needed |
| `applied_local_sync_retryable` | Local writes exist; the trailing sync was temporarily unavailable | Do not reapply. Restore connectivity and choose **Retry sync** |
| `applied_local_sync_blocked` | Local writes exist; Anki requires manual sync resolution | Resolve the Anki full-sync/auth prompt, then choose **Retry sync** |
| `apply_partial` | At least one local operation may have started and verification did not finish cleanly | Stop new curation writes, inspect the receipt and Anki, then recover deliberately |

Operation intent and completion are durable. Generated notes carry a
deterministic envelope marker, so a retry locates the existing note instead of
creating a duplicate. Tag changes use add/remove diffs and re-read current tags
rather than replacing the whole tag list.

## 10. Mac study synchronization

The Mac no longer runs an indexing or curation agent. It does not need the
semantic-search research add-on for this workflow.

1. Let a NUC apply finish with state `complete`.
2. On the Mac, sync before studying or editing.
3. Study normally.
4. Sync the Mac when finished.
5. Sync the NUC before the next curation apply.

Avoid editing the same notes on both devices between syncs. If Anki asks for a
one-way/full sync, stop curation and resolve it in Anki before using **Retry
sync**. Never treat retry as permission to run the apply a second time.

## 11. Copied-profile acceptance gate

Do not run the first acceptance pass against the production profile.

1. With Anki closed, create a separate profile from a supported backup/export.
2. Name it clearly, such as `OMS V4 Acceptance Copy`, and make it active.
3. Ensure production is still backed up and untouched.
4. Run the full index and record snapshot size, full refresh duration, and peak
   memory.
5. Change one harmless note in the copy, rerun refresh, and record incremental
   duration.
6. Run one real lecture curation with current slides, transcript, and
   NotebookLM outline.
7. Review a permitted tag edit and one grounded generated card.
8. Apply, confirm the leading sync/no-write failure behavior in a controlled
   failure scenario, confirm the trailing-sync recovery state, retry, and
   verify the final tags/card by reading them back from Anki.
9. Re-run the same frozen envelope and confirm no duplicate note is created.
10. Disable or move both research add-ons out of the copied profile and confirm
    V4 still indexes, searches, reviews, and verifies.

Copy `tests/fixtures/anki/retrieval_gold.json` outside the repository, replace
the seed observations with the manually labeled copied-profile rankings,
latencies, index measurements, and acceptance booleans, and set:

```json
{
  "profile": {
    "kind": "copied_profile",
    "label_provenance": "manual_copied_profile",
    "copied_via_supported_backup": true,
    "production_profile_untouched": true
  }
}
```

Generate the auditable reports:

```powershell
$Evidence = "C:\ProgramData\OMSStudyHub\acceptance"
New-Item -ItemType Directory -Force $Evidence | Out-Null

.\.venv\Scripts\python.exe scripts\evaluate-anki-retrieval.py `
  --gold "$Evidence\copied-profile-observations.json" `
  --json-out "$Evidence\anki-v4-evaluation.json" `
  --markdown-out "$Evidence\anki-v4-evaluation.md" `
  --require-release-ready
```

The evaluator reports Recall@5, Recall@10, MRR, nDCG@10, Pass 1
precision/recall, Pass 2 recovery/false recovery, gap precision, semantic
coverage, refresh timing, exact-query p50/p95, memory, and a 68k-note
extrapolation. It also reports all six ablations: statement-only, four semantic
variants, FTS-only, semantic-only, fused, and fused with boosts.

The repository fixture is a deterministic contract regression only. Its
synthetic timings and rankings are never accepted as copied-profile evidence;
its report intentionally says `Copied-profile acceptance: pending` and
`Release ready: no`.

Stop here and obtain approval of both copied-profile report files. The legacy
agent package, endpoints, configuration, tests, and packaging entries must not
be removed before that approval.

## 12. Prompt directory and review acceptance

- [ ] Open Settings, choose the Obsidian `Anki AI Prompts` directory on the
  NUC, save it, and run **Test / Refresh**.
- [ ] Confirm the catalog reports at least one valid LCL, coverage, and
  card-generation prompt and that invalid Markdown files are warnings rather
  than selectable options.
- [ ] Open Anki curation and confirm the three prompt selectors show the
  expected ID, version, and resolved hash.
- [ ] Add at least two indexed decks, reorder them, and confirm the displayed
  order is retained when the run is created.
- [ ] Select a lecture and confirm slides, transcript, and NotebookLM outline
  each show an independent green check; verify a missing source shows a red X
  and keeps Start curation disabled.
- [ ] Confirm the generated-card deck and lecture tag are populated as normal,
  editable text.
- [ ] Complete a copied-profile run and verify **Final proposed changes** opens
  before **Candidates**.
- [ ] Search Candidates by card text, note ID, extra text, and a hidden tag.
- [ ] Change one candidate selection and confirm the Final count/list updates
  before saving the review.
- [ ] Open a card's collapsed **Tags** disclosure and confirm source-managed
  tags remain locked while permitted lecture tags remain editable.

## 13. Roll back

Stop Study Hub, switch to the recorded earlier commit, reinstall, and restart.
If an acceptance write must be discarded, delete only the disposable Anki
profile. Do not replace the production profile with the acceptance copy.

If production has already been enabled after approval, stop both Study Hub and
Anki before restoring their backups. A semantic index can be rebuilt, but the
Study Hub database and apply receipts should be restored together so the UI
does not lose its knowledge of local writes or pending sync recovery.
