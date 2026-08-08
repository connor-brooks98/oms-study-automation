# Anki Curation Pipeline v2 — Architecture Plan

> **Superseded for `card_centric_v2` correction work:** This historical retrieval-v2
> plan does not govern the current card-centric correction. Its single-provider
> routing and primary-source-only summary rules are superseded by
> [Card-Centric v2 Correction Policy](anki-card-centric-v2-correction-policy.md),
> which authorizes persisted configurable model routes and grounded,
> explicitly labeled `summary_grounded` evidence. Its reconciliation/card-count
> language must not be used to reintroduce a quota or a 72/80 or 70–75 target.

Rewrite scope: add NotebookLM summary ingestion, externalize prompts to Obsidian
markdown, add the card relevance audit, close the convergence loop, and enforce
reconciliation assertions. Single provider (Anthropic) throughout.

Baseline for every claim below is the Lecture 07 (Anemia IV) run: 356 + 170
matched cards, 19 concepts, 12 unfilled gaps reported as zero, ~25% off-topic.

---

## 1. Stage map

Current:

```
ingest(pptx, transcript) → LCL → retrieve → judge → retrieve → judge
  → gap_gen [never fires] → envelope → review → apply
```

Target:

```
S0  ingest          pptx + transcript + NotebookLM summary
S1  lcl             concept ledger, depth/emphasis from summary
S2  converge        retrieve → judge, per-concept, until stable
S3  prior_gate      deterministic entity overlap, advisory only
S4  audit           per-card relevance verdict against sources
S5  recompute       rebuild coverage from surviving supports only
S6  dedupe          token-set, within concept cluster
S7  gap_gen         one call per concept, all missing facts
S8  assert          reconciliation, hard fail on violation
S9  envelope        review → apply
```

Two ordering constraints that are not negotiable:

**S4 precedes S5.** Rejecting a card that was propping up a concept must
*create* a gap. Auditing after gap-fill produces a cleaner and simultaneously
thinner deck with no signal that it happened.

**S6 precedes S7.** Dedupe against kept cards before writing new ones, then
dedupe again inside S8 including the generated cards. The v1 failure mode
(custom card duplicating an already-tagged AnKing card) gets worse when
generation is batched per concept.

---

## 2. NotebookLM summary ingestion

### 2.1 Source authority

The three sources are not peers. Encode this everywhere it matters:

| Source | Authority | Used for |
|---|---|---|
| Transcript | Highest | Emphasis, professor flags, time-on-topic, keep/drop |
| Slides | High | What was presented; may include skipped reference material |
| Summary | Derived | Concept index, depth map, emphasis flags. **Corroborates only.** |

The summary is model-generated and can hallucinate or over-generalize. Two hard
rules follow:

- A card may not be kept on summary support alone (S4).
- A generated card may not cite a summary passage as its sole evidence (S7).

### 2.2 What the summary actually buys you

The Lecture 07 summary carries three structured sections v1 ignored entirely:

- `CORE CONCEPTS` — a pre-extracted concept list with source citations
- `DEPTH MAP` — SURFACE / MEDIUM / DEEP classification
- `PROFESSOR EMPHASIS FLAGS` — direct quotes plus "Repeated 3+ Times"

That last section is the fix for the `importance` field, which v1 collected but
never populated meaningfully. `DEPTH MAP` is the fix for concept density: the
summary named HS genes, ↑MCHC/↑RDW, PNH NO-depletion, and the CD55/CD59 pairing
as DEEP, and none of them became concepts.

### 2.3 Passage namespace

Ingestion produces one passage table with a source-prefixed ID so authority is
readable off the ID alone:

```
SLD:07:0031     slide 31, one bullet block
TRX:07:0142     transcript segment 142, ~30s window
SUM:07:CORE:09  summary core-concept item 9
SUM:07:DEPTH:D3 summary depth-map DEEP item 3
SUM:07:EMPH:E1  summary emphasis flag 1
```

The NotebookLM output already carries bracketed citations (`[27, 28]`) pointing
back into its own source set. Parse them and store as `summary_backrefs` on the
passage row. Where they resolve to slide or transcript passages, the summary
item inherits corroboration for free and the audit gets a stronger signal.

### 2.4 Ingest contract

Required inputs per lecture: `*.pptx`, `*Transcript.txt`, `*Outline.pdf`
(the NotebookLM output). Job stays `queued` and the Curate button stays disabled
until all three exist. Do not let a run start with two of three — a missing
summary silently reverts you to v1 behavior.

Validation on the summary: must contain a `DEPTH MAP` heading and a
`PROFESSOR EMPHASIS FLAGS` heading, else `failed: summary_malformed`. Cheap
check, catches a truncated or wrong-template NotebookLM export before you spend
tokens.

**Effort:** 4–5 hrs (parser, passage table migration, validation, button gate).

---

## 3. Prompts as Obsidian markdown

### 3.1 Layout

```
vault/AnkiPipeline/prompts/
  _shared/
    card-house-rules.md
    source-authority.md
    output-contract.md
  lecture-concept-ledger.md
  coverage-rubric.md
  card-relevance-audit.md
  gap-card-generation.md
  paraphrase-expansion.md
```

`_shared/` exists because the judge and the generator need the *same* definition
of context trap, enumeration, and stat-cloze. Two copies drift, and when they
drift the generator writes cards the judge flags.

### 3.2 Frontmatter

```yaml
---
id: card-relevance-audit
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 8000
response_format: json
schema: audit_verdict_v2
includes:
  - _shared/source-authority.md
  - _shared/output-contract.md
cache_prefix: true
batch_size: 30
---
```

### 3.3 Loader

1. Read file, parse frontmatter.
2. Resolve `includes` recursively, depth-limited to 3, cycle-detected.
3. Concatenate → resolved prompt text.
4. `prompt_hash = sha256(resolved_text)[:12]`.

**Cache keys use `prompt_hash`, never `version`.** This is the whole point of
moving to files: you edit a rule in Obsidian, the hash changes, affected stage
caches invalidate automatically, and you re-run without hand-clearing anything.
`version` is for git history and humans.

Record `{id, version, prompt_hash}` for every stage in the run record. When a
run goes sideways you diff the prompt against the last good run's hash.

### 3.4 Sync Mac → NUC

Vault lives on the MacBook; the pipeline runs on the NUC. Git is the right
transport — Syncthing gives you no history and partial-write races during a job.

- Vault subfolder `AnkiPipeline/` is its own git repo.
- NUC pulls at job start, before S1.
- Pull failure is non-fatal: use last-known-good working copy, log
  `prompt_sync_stale: true` on the run, surface it in the review UI header.
- Never mid-job re-read. Load all prompts once at job start into memory so a
  save in Obsidian during a run can't split a job across two prompt versions.

**Effort:** 3–4 hrs (loader, include resolver, git hook, run-record fields).

---

## 4. Convergence loop (S2)

### 4.1 Per-concept, not global

v1 ran two global passes. Concepts converge at wildly different rates — the
Coombs/DAT concept was probably done after one pass while PNH clearly was not.
Track and stop independently.

```
for pass_n in 1..MAX_PASSES:
    active = [c for c in concepts if not c.converged]
    if not active: break
    for c in active:
        queries = c.paraphrases[pass_n]        # 3 per pass
        new_nids = retrieve(queries) - c.seen_nids
        c.seen_nids |= new_nids
        c.growth[pass_n] = len(new_nids) / max(len(c.seen_nids), 1)
        if c.growth[pass_n] < 0.05:
            c.converged = True
    judge(active)                               # batched, cached
```

- `MAX_PASSES = 5`
- Convergence: `< 5%` new unique nids relative to that concept's cumulative set
- Concepts still growing at pass 5 → `converged = False`, run flagged
  `needs_manual_review`, does not hard-fail

For calibration: Lecture 07 pass 2 added 48% new with zero overlap. That is
nowhere near 5% and should have triggered passes 3, 4, 5.

### 4.2 Paraphrase supply

Three paraphrases per pass × 5 passes = 15 needed; the LCL emits 3–6. When a
concept exhausts its supply, call `paraphrase-expansion.md` with the concept,
its existing paraphrases, and the nids already found, asking for 3 more that
target the residual. Cheap, and only fires for concepts that actually need it.

The entity-retention rule (every paraphrase contains the concept's primary
entity verbatim) applies to expansion too. Dropping the entity is how "PNH
clinical presentation" became "episodic dark urine worst in the morning" and
retrieved the cortisol card.

**Effort:** 4–5 hrs.

---

## 5. Prior gate (S3) — advisory only

Deterministic entity overlap between each card and the ledger. **It drops
nothing.** With prompt caching, auditing all 526 cards is cheap enough that
skipping any of them is a false economy, and a deterministic gate will
absolutely mis-drop the Heinz-bodies card that never says "G6PD."

Two real uses:

1. **Batch ordering.** Group likely-drops together. A batch of 30 obvious
   rejects returns fast and short.
2. **Disagreement logging.** Gate says zero overlap, audit says keep → log to
   `vocabulary_gaps`. That is a direct instruction for what aliases the LCL
   should be emitting next time. This is your cheapest source of retrieval
   improvement.

Alias source: LCL `aliases[]` per concept, plus a lecture glossary assembled
from summary `CORE CONCEPTS` headings.

**Effort:** 2 hrs.

---

## 6. Card relevance audit (S4)

### 6.1 Blindness requirement

The audit call receives the card and the sources. It does **not** receive:

- the concept it was matched to
- the search paraphrase that retrieved it
- the coverage judge's rationale
- the prior gate's verdict

Handing over the retrieval justification hands over the rationalization. Given
"this matched the G6PD inheritance concept," the model will confirm that the
hemophilia A card is indeed about X-linked recessive inheritance. The question
is not *does this card relate to the concept* — it is *does this card belong in
a deck for this lecture*.

### 6.2 Batching and cost

- Sources (slides + transcript + summary) in a cached prefix, `cache_control:
  ephemeral`, ~15–20k tokens. Billed once per run instead of 526 times.
- 30 cards per call. Larger loses attention down the list; smaller loses
  intra-batch near-duplicate detection.
- Verdict cache key: `(nid, lecture_id, audit_prompt_hash)`. Re-running S7
  after a gap-prompt tweak costs nothing at S4.

### 6.3 Verdict

```json
{
  "nid": 1234567890,
  "verdict": "keep | drop | uncertain",
  "primary_subject": "hereditary spherocytosis",
  "support": "transcript | slides | both | summary_only | none",
  "reason": "≤15 words",
  "structure_issue": ["context_trap", "enumeration", "stat_cloze", "over_cloze"]
}
```

`support: summary_only` forces `verdict != keep` — the summary corroborates, it
does not carry.

Auto-drop is safe by the same logic that made auto-dedupe safe: a drop means the
card does not receive your lecture tag. Nothing is deleted, nothing suspended,
the classmate's tags are untouched. A wrong drop costs one untagged card.
`uncertain` renders unchecked in review; `keep` and `drop` render pre-decided.

Structure flags run in the same call — you are already looking at every card
with the sources in context, so they are free. They apply to *existing AnKing
cards*, which v1 never structure-checked at all.

**Effort:** 5–6 hrs.

---

## 7. Coverage recompute (S5)

Rebuild each concept's coverage using only `verdict == keep` supports, then
re-run the coverage rubric for any concept whose support set changed.

```
for c in concepts:
    c.supports = [n for n in c.supports if audit[n].verdict == "keep"]
    if c.supports changed:
        c.missing_facts = rejudge(c)          # cached by support-set hash
    c.status = "covered" if not c.missing_facts else "partial"
```

Do not re-judge concepts whose support set is unchanged. Key the rejudge cache
on `(concept_id, sorted(support_nids), rubric_prompt_hash)`.

**Effort:** 2–3 hrs.

---

## 8. Gap generation (S7)

### 8.1 Routing fix

The v1 bug in one line: the orchestrator routed on the rubric *label* and only
`miss` reached the generator. Every concept came back `partial`, so nothing did.

**Route on `len(missing_facts) > 0`.** The label is display metadata. A partial
with three missing facts is operationally three misses.

### 8.2 Batch per concept

One call per *concept*, carrying all its missing facts — not one call per fact.
Lets the model see overlap between facts and split or merge sensibly, and cuts
call count roughly 3×. `gap-card-generation.md` grants explicit permission to
split, and requires an `unresolved` entry rather than silent omission when
evidence won't support a card.

### 8.3 Context trap inputs

Pass `lecture_entity_count` and `forbidden_cloze_targets[]`, both computed
deterministically from the LCL.

- Always forbidden: the lecture title.
- Forbidden only when `lecture_entity_count == 1`: the concept's primary entity.

Anemia IV has six competing diseases, so blanking "G6PD deficiency" is a
legitimate discrimination, not a trap. A single-organism micro lecture is the
opposite. The headless call cannot infer which regime it is in.

**Effort:** 3–4 hrs.

---

## 9. Reconciliation assertions (S8)

Run before the envelope renders. Any violation → job state `failed`, envelope
withheld, violation list surfaced.

| ID | Assertion | Severity |
|---|---|---|
| A1 | Every concept with non-empty `missing_facts` has ≥1 generated card or an `unresolved` entry | fail |
| A2 | Missing `fact_id`s exactly equal generated-or-unresolved `fact_id`s; one fact may map to multiple generated cards | fail |
| A3 | Every audited nid appears exactly once across keep/drop/uncertain | fail |
| A4 | Every concept has status `covered` or `intentional_gap` | fail |
| A5 | No generated card blanks a `forbidden_cloze_targets` string | fail |
| A6 | Audit drop rate ≤ 35% | fail |
| A7 | Kept card count ≥ 10 | fail |
| A8 | Concepts unresolved ≤ 40% | fail |
| A9 | Every source passage_id cited by ≥1 concept | warn |
| A10 | All concepts converged | warn |
| A11 | `prompt_sync_stale == false` | warn |

A1 and A2 are the ones that would have caught the Lecture 07 failure. A6 would
have fired at ~25% — borderline, which is about the right sensitivity: retrieval
noise above a third means the problem is upstream and you should be told loudly
rather than shipped a quietly-thinned deck.

A9 is a warn not a fail because some slides are genuinely non-testable (title,
references, image-only). But an uncited-passage list in the review header is the
fastest read on whether the ledger is too coarse — Lecture 07 would have shown a
long one.

**Effort:** 3 hrs.

---

## 10. Envelope and review UI

### 10.1 Envelope additions

```json
{
  "job_id": "...",
  "lecture_tag": "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Lec_07_Anemia_IV",
  "prompt_hashes": { "lcl": "a3f2...", "rubric": "...", "audit": "...", "gap": "..." },
  "convergence": { "passes_run": 4, "concepts_converged": 31, "concepts_total": 33 },
  "coverage_ledger": [
    { "concept_id": "C07", "statement": "...", "importance": "high",
      "depth": "deep", "emphasis_flag": true,
      "supports": [nid, ...], "rejected": [nid, ...],
      "missing_facts": [...], "generated": [...], "status": "covered" }
  ],
  "audit": { "keep": 291, "drop": 118, "uncertain": 22, "drop_rate": 0.27 },
  "assertions": { "passed": [...], "failed": [], "warned": ["A9", "A10"] },
  "add_tags": [...], "add_notes": [...], "unresolved": [...]
}
```

### 10.2 UI changes

**Rename the field.** "Why this matched" currently displays concept-level gap
analysis — text that says what is *not* covered — under a heading claiming it
explains a match. During review that is actively misleading. Split into two
fields: per-card `Audit verdict` (from S4) and per-concept
`Coverage assessment` (from S5), with the missing-facts clause visually flagged.

**Concept-first layout.** v1 rendered 526 cards flat. Render 33 concepts, each
expandable to its supports, rejects, missing facts, and generated cards. This is
the view that makes "12 gaps, 0 filled" visible at a glance.

**Drop the confidence percentage** or make it real. v1's 70/100 was the rubric
label leaking through as a per-card score, which is why 34 off-topic cards read
100%. Show the audit verdict instead.

**Header strip:** passes run, convergence, drop rate, assertion warnings,
uncited passage count, `prompt_sync_stale`.

**Effort:** 8–10 hrs (mostly the concept-first rewrite).

---

## 11. Build order

| Order | Item | Effort | Why here |
|---|---|---|---|
| 1 | §9 assertions | 3 hr | Catches every subsequent regression. Build first even though it fails the current run. |
| 2 | §8.1 route on `missing_facts` | 30 min | One-line fix, unblocks A1/A2 |
| 3 | §3 prompt loader | 3–4 hr | Everything downstream is prompt edits |
| 4 | §2 summary ingest | 4–5 hr | LCL rewrite depends on it |
| 5 | LCL v2 prompt + rubric v2 | 2 hr | Prompt-only once §3 and §2 land |
| 6 | §6 audit stage | 5–6 hr | Biggest quality win |
| 7 | §7 recompute | 2–3 hr | Meaningless without §6 |
| 8 | §4 convergence | 4–5 hr | Recall; can trail precision |
| 9 | §8.2/8.3 gap gen | 3–4 hr | |
| 10 | §5 prior gate | 2 hr | Optimization only |
| 11 | §10 UI | 8–10 hr | Last, schema must settle first |

Total ≈ 38–47 hrs.

Item 1 first is deliberate: build the assertions before the fixes, watch them
fail against the Lecture 07 envelope, then fix until they pass. That gives you a
regression test instead of a hope.

---

## 12. Validation

Re-run Lecture 07 end to end after items 1–7. Targets:

| Metric | v1 | Target |
|---|---|---|
| Concepts | 19 | 30–40 |
| Uncited passages | not measured | < 15% |
| Cards surviving audit | 526 (unfiltered) | 280–350 |
| Off-topic rate in kept set | ~25% | < 5% |
| Gaps detected | 12 | ≥ 12 |
| Gaps filled or unresolved | 0 | 100% |
| Passes to convergence | 2 (forced) | 3–5 (earned) |

Then hand-check the six items v1 missed entirely — Mitapivat, C. perfringens,
graft-vs-host, AHTR phase sequence, warm AIHA secondary causes, G6PD treatment
specifics. Each should appear as a tracked concept with either a kept card or a
generated one. That list is your ground truth for this lecture; keep it in the
repo next to the fixture.
