# Anki Prompt Catalog and Review Workspace Design

## Purpose

Make Anki curation prompt versions easy to manage from an Obsidian-synced
Markdown directory, while making the curation form and review screen practical
for real lecture-sized result sets. This design applies to the existing
`codex/anki-v4-implementation` branch.

The desired workflow is:

1. Choose one prompt directory in Settings.
2. Keep individual, versioned Markdown prompt files in that directory.
3. Select a valid LCL prompt, coverage rubric, and card-generation prompt for
   each curation run.
4. Choose a course, exam, and lecture; confirm that all three source types are
   ready; configure ordered existing-card deck search and editable new-card
   defaults.
5. Review only proposed changes by default, and open the larger candidate pool
   only when needed.

## Goals

- Support a user-selected prompt directory such as
  `C:\Users\conbr\Documents\Main Vault\Anki AI Prompts`.
- Validate prompt files with the same loader and include rules used at job
  preflight.
- Expose only compatible, valid prompt versions in the three curation
  selectors.
- Preserve frozen prompt contents and hashes for every submitted job.
- Remove the unnecessary block-label control from the curation form.
- Make existing-card deck selection ordered and meaningful during retrieval.
- Make lecture-source readiness obvious before a run starts.
- Default card review to the small set of proposed changes rather than every
  candidate returned by retrieval.

## Non-goals

- Browser uploading, editing, or versioning of prompt files. Obsidian and the
  configured directory remain the authoring surface.
- Changing prompt files after a job has started.
- Changing source-tag protection rules or the apply/envelope safety gate.
- Replacing the existing course, exam, and lecture selector.

## Prompt directory and catalog

### Directory structure

The configured folder contains selectable prompt files at its top level. An
optional subdirectory may hold shared include files and is not itself a source
of selector options.

```text
Anki AI Prompts/
├── lecture-concept-ledger.md
├── lcl-v1.md
├── coverage-rubric.md
├── judgment-v1.md
├── gap-card-generation.md
├── gap-v1.md
└── _shared/
    └── common-instructions.md
```

Selectable files must use the existing frontmatter contract. Their `schema`
classifies them as follows:

| Schema values | Curation selector |
| --- | --- |
| `lcl_v1`, `lcl_v2` | LCL prompt |
| `coverage_v1`, `coverage_v2` | Coverage rubric |
| `gap_cards_v1`, `gap_cards_v2` | Card-generation prompt |

The catalog scans top-level `*.md` files only. Include files can be nested,
must remain inside the configured root, and follow the existing depth and cycle
rules.

The selected directory supplies only the three user-selectable prompt roles.
The internal card-relevance-audit and paraphrase-expansion prompts stay bundled
with Study Hub, are not shown in the catalog, and are pinned alongside the
three selected prompts. A three-file Obsidian directory is therefore sufficient
for a usable curation run.

### Settings workflow

Settings gains one **Anki curation prompts** path control following the existing
NotebookLM prompt-path pattern:

- The displayed value is the active directory.
- **Select Folder** opens a native Windows folder picker.
- **Save Path** persists the chosen folder.
- **Test / Refresh** scans the folder and reports valid prompt choices and
  actionable warnings.

The persisted setting overrides the deployment environment directory at
runtime. If it has not been set, the existing environment-configured directory
remains the fallback. The setting is stored in the existing generic prompt
settings persistence rather than a new schema solely for this feature.

The catalog resolves the saved path on every Settings test, Anki bootstrap, and
job preflight. A Settings change therefore takes effect without restarting the
Hub.

### Validation and warnings

The catalog uses `AnkiPromptLibrary` to load every top-level candidate. This
ensures frontmatter, prompt ID/file-name matching, supported includes, resolved
content, and hash generation behave exactly as preflight does.

Malformed or unsupported files are not selectable. Settings and the Anki page
show a concise diagnostic for each problem, including malformed frontmatter,
unsupported schema, missing or cyclic includes, duplicate IDs, and empty
content. A catalog warning does not block a valid option in another file.

Each selector displays:

```text
prompt-id — vVersion · resolved-hash
```

The V2 built-ins remain preferred defaults when available; otherwise the first
valid option in that category is used. Start Curation is disabled only if a
required category has no valid selection. If a previously selected option is
removed or becomes invalid, the user must choose a replacement.

### Job immutability

The submitted contract continues to send prompt IDs. Before execution,
preflight optionally synchronizes the active prompt directory only when prompt
Git sync has explicitly been configured for that same directory. A normal local
or cloud-synced Obsidian directory is never subjected to an implicit Git pull.
Preflight then reloads the three selected prompts and the bundled internal
prompts, and freezes IDs, versions, resolved hashes, resolved contents, source
paths, and metadata in the job artifact. A later directory or file change
cannot alter that run.

## Curation form

### Block label

Remove **Block label** from the page and submit no `block_id`. The database
field remains nullable for historical jobs and backward compatibility. It is an
optional tag-based retrieval preference, not a necessary consequence of the
selected exam, and does not belong in the first-release workflow.

### Ordered existing-card decks

Replace the comma-separated deck field with an ordered multi-select control:

1. Select a deck from the available indexed decks.
2. It is added once to a visible priority list.
3. Move controls change its priority; remove controls delete it from the list.
4. The list order is the exact retrieval order and is persisted without
   sorting.

For each concept and retrieval pass, search the first listed deck. Only if it
has no candidate that survives the current acceptance/judgment rules does the
pipeline search the next deck. Earlier accepted decks therefore take precedence
over later decks; lower-priority decks are not merely blended into one broad
search. The optional existing-card tag scope remains an advanced filter.

### Lecture sources

Slides, transcript, and NotebookLM outline cards are rendered from first page
load. Before a lecture is chosen they show a neutral unavailable state. After
selection, each card independently shows a green check when its current source
is available and a red X when it is missing or stale.

The outline receives the same visual status treatment as slides and transcript.
Curation starts only when all three cards are green. The locked source revisions
and outline IDs/hashes remain pinned into the job exactly as today.

### Editable new-card defaults

When a lecture is selected, target deck and lecture tag fields receive the
computed values from the existing `target_deck(LectureIdentity)` and
`target_tag(LectureIdentity)` helpers as actual input values. They are editable
before submission. These values no longer appear only as placeholder text.

## Review workspace

The Review step is reduced to two views, selected with a top-level switcher.

### Final proposed changes

This is the default view. It shows only the cards currently selected for an
Anki change:

- Existing notes selected for retagging, whether found initially or during
  missed-topic recovery.
- Selected generated cards, if any, in a separate **New cards to create**
  section.

Thus, a run with hundreds of candidates but roughly seventy selected retags
opens to those seventy proposed changes, not to the whole candidate set.

### Candidates

This view combines Pass 1 matches and missed-topic recovery into one list.
Each card may show a small provenance label such as **Initial match** or
**Recovered**, but pass origin is not a separate review workflow. Selection
changes here update the Final proposed changes count and contents immediately.

### Search and tags

A text field at the top filters the active view by note ID, card front, extra
text, and tags. Filtering is client-side over the already-loaded review payload
and must not discard pending selection, generated-card edits, or editable tag
changes.

Every existing-card row has a collapsed **Tags** disclosure. It contains the
locked source-managed tags and the editable lecture-tag field. Tags remain
available for inspection and modification but do not dominate the default
review layout.

## API and compatibility

- Bootstrap returns the prompt catalog grouped by selector role, catalog
  warnings, and indexed deck choices in addition to its current data.
- Settings exposes directory select, save, and test/refresh operations.
- `deck_allowlist` remains the job field but becomes ordered. Normalization
  removes blanks and duplicates while preserving the first occurrence; tag
  scopes can retain their existing canonical behavior.
- The job-create request keeps nullable `block_id` for compatibility, but the
  new form supplies `null`.
- Existing saved jobs, frozen job artifacts, review revisions, and envelopes
  remain readable.

## Error handling

- A missing, unreadable, or invalid prompt directory produces a clear Settings
  warning and blocks only prompt categories with no usable file.
- Directory paths outside the permitted local filesystem or include paths that
  escape the prompt root are rejected.
- An unavailable deck catalog leaves the ordered picker disabled with a clear
  Anki-index/AnkiConnect remediation message; it must not silently submit an
  empty deck list.
- Missing slides, transcript, or outline produces an individual red X and a
  specific remediation message while leaving the selected lecture visible.
- A stale prompt selection or source bundle is rejected again by server-side
  preflight, with a reload-and-reselect message.

## Acceptance tests

Automated coverage will verify:

- Catalog role classification, frontmatter failures, include failures,
  duplicate IDs, V1/V2 option labels, and default selection behavior.
- Saved directory override, environment fallback, Settings test/refresh, and
  immediate bootstrap visibility without a process restart.
- Preflight pinning after synchronization and rejection of a stale or invalid
  selected prompt.
- Top-level-only selector discovery while allowing nested shared includes.
- Block label omission; ordered deck add/remove/reorder behavior; request and
  repository order preservation; and sequential priority retrieval semantics.
- Three lecture source cards in neutral, ready, and missing states, including
  the NotebookLM check/X, with submission gated on all-ready.
- Editable target deck and lecture-tag values after lecture selection.
- Final proposed changes as the default review view; combined Candidates;
  live final-list counts after selection edits; active-view search; and
  collapsed tag disclosure with existing protected-tag behavior intact.
- Existing job and review payload compatibility.

## Rollout

The feature ships on the Anki integration branch and is validated on the test
instance before merging to `main`. Existing configured prompt assets continue
to function through the environment fallback until the Obsidian directory is
selected in Settings.
