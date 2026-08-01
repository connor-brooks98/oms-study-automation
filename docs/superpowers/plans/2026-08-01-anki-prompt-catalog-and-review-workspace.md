# Anki Prompt Catalog and Review Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user select validated versioned Anki prompts from one Obsidian directory, use ordered existing-card deck priority, see unambiguous lecture-source readiness, and review only proposed Anki changes by default.

**Architecture:** A dynamic Anki prompt catalog resolves a saved local directory with the configured deployment directory as a fallback. It uses the existing Markdown loader for user-selectable prompts and keeps internal pipeline prompts bundled. Bootstrap provides validated prompt choices and indexed decks to the page; jobs still store prompt IDs and preflight freezes all resolved prompt data. The review payload remains unchanged while the browser renders it into Final proposed changes and Candidates views.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy/SQLite, Jinja2, vanilla JavaScript, Node test runner, pytest, Ruff, mypy.

## Global Constraints

- Prompt authoring stays in the configured local/Obsidian directory; do not add browser upload or editing.
- Only top-level Markdown files are selector candidates; nested files are valid includes only.
- User-selectable roles accept exactly `lcl_v1|lcl_v2`, `coverage_v1|coverage_v2`, and `gap_cards_v1|gap_cards_v2` respectively.
- Bundled card-relevance-audit and paraphrase-expansion prompts remain non-selectable system prompts.
- Preflight must pin selected IDs, versions, resolved hashes, contents, source paths, and metadata before a job runs.
- Preserve deck order from browser request through repository and retrieval; do not alphabetize deck priorities.
- A curation run remains disabled unless current slides, transcript, and NotebookLM outline are all ready.
- Existing jobs, review revisions, envelopes, and nullable historical `block_id` values must remain readable.
- Keep AnkiConnect and Study Hub port configuration behavior unchanged.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/oms_hub/anki/prompt_catalog.py` | Dynamic directory resolution, prompt inventory, role validation, and job prompt snapshot assembly. |
| `src/oms_hub/anki/prompts.py` | Reuse the existing strict Markdown parser; add only narrowly shared prompt-loader support required by the catalog. |
| `src/oms_hub/study_generation/repository.py` | Persist and read the single Anki prompt-directory override using the existing settings table. |
| `src/oms_hub/study_generation/path_picker.py` | Native Windows folder-picker protocol and implementation. |
| `src/oms_hub/web/generation_routes.py` | Directory select/save/test API routes. |
| `src/oms_hub/app.py` | Wire the catalog service, fallback directory, settings repository, and folder picker. |
| `src/oms_hub/web/settings_routes.py` | Supply the saved Anki prompt-directory state to Settings. |
| `src/oms_hub/web/templates/settings.html` | Render the Anki curation prompt-directory card. |
| `src/oms_hub/web/static/settings.js` | Drive Select Folder, Save Path, and Test / Refresh actions. |
| `src/oms_hub/anki/index.py` | List distinct indexed deck names for the ordered picker. |
| `src/oms_hub/web/anki_routes.py` | Add prompt catalog, deck options, source status, and target-deck default to bootstrap/page data. |
| `src/oms_hub/web/templates/anki.html` | Replace block/deck text controls with priority picker and prompt selectors. |
| `src/oms_hub/web/static/anki.js` | Form state, source indicators, ordered deck picker, prompt availability, and review workspace behavior. |
| `src/oms_hub/anki/contracts.py` | Preserve deck input order while retaining tag normalization and nullable `block_id`. |
| `src/oms_hub/anki/retrieval.py` | Retrieve candidates by deck priority rather than one blended deck filter. |
| `src/oms_hub/anki/stages.py` | Judge deck groups in priority order and stop lower-deck search once a valid supporting card exists. |
| `src/oms_hub/web/templates/anki_review.html` | Add review switcher, search input, and Candidate/Final containers. |
| `tests/anki/test_prompt_catalog.py` | Catalog, roles, warnings, fallback, and immutable snapshot unit coverage. |
| `tests/anki/test_contracts.py` | Deck priority request normalization coverage. |
| `tests/anki/test_index.py` | Distinct indexed deck catalog coverage. |
| `tests/anki/test_retrieval.py` and `tests/anki/test_stages.py` | Ordered retrieval and priority judgment coverage. |
| `tests/anki/test_web.py` | Bootstrap and source readiness integration coverage. |
| `tests/v2/test_generation_settings.py` | Saved directory, native selection, and test/refresh route coverage. |
| `tests/js/settings.test.js` and `tests/js/anki.test.js` | Browser behavior unit coverage. |

## Task 1: Build the dynamic prompt catalog and immutable prompt snapshots

**Files:**
- Create: `src/oms_hub/anki/prompt_catalog.py`
- Modify: `src/oms_hub/anki/prompts.py`
- Modify: `src/oms_hub/anki/stages.py`
- Modify: `src/oms_hub/app.py`
- Create: `tests/anki/test_prompt_catalog.py`
- Modify: `tests/anki/test_stages.py`

**Interfaces:**
- Consumes: `AnkiPromptLibrary.load()`, `AnkiPromptLibrary.load_many()`, `AnkiPrompt`, `AnkiPromptSnapshot`, `AnkiPromptConfigurationError`.
- Produces: `PromptRole`, `PromptChoice`, `PromptCatalogIssue`, `PromptCatalog`, and `AnkiPromptCatalogService.catalog()` / `AnkiPromptCatalogService.load_job_snapshot()`.
- Downstream use: Settings and `/api/anki/bootstrap` consume `PromptCatalog`; preflight consumes `load_job_snapshot(lcl_id, coverage_id, gap_id)`.

- [ ] **Step 1: Write the failing catalog tests**

```python
def test_catalog_groups_only_valid_top_level_role_prompts(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write_prompt(root / "lcl-v2.md", "lcl-v2", "2.0", "lcl_v2")
    _write_prompt(root / "_shared" / "hidden.md", "hidden", "2.0", "lcl_v2")
    _write_prompt(root / "bad.md", "bad", "2.0", "unknown_v9")

    catalog = AnkiPromptCatalogService(lambda: root, bundled_root()).catalog()

    assert [choice.id for choice in catalog.choices[PromptRole.LCL]] == ["lcl-v2"]
    assert catalog.choices[PromptRole.COVERAGE] == ()
    assert any(issue.path.name == "bad.md" for issue in catalog.issues)


def test_job_snapshot_uses_selected_directory_and_bundled_internal_prompts(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write_prompt(root / "lcl.md", "lcl", "2.0", "lcl_v2")
    _write_prompt(root / "coverage.md", "coverage", "2.0", "coverage_v2")
    _write_prompt(root / "gap.md", "gap", "2.0", "gap_cards_v2")

    snapshot = AnkiPromptCatalogService(lambda: root, bundled_root()).load_job_snapshot(
        lcl_id="lcl", coverage_id="coverage", gap_id="gap"
    )

    assert snapshot.require("lcl").path.parent == root
    assert snapshot.require("card-relevance-audit").path.parent == bundled_root()
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/anki/test_prompt_catalog.py -v`

Expected: FAIL because `AnkiPromptCatalogService` and `PromptRole` do not exist.

- [ ] **Step 3: Implement the catalog types and loader boundary**

```python
class PromptRole(StrEnum):
    LCL = "lcl"
    COVERAGE = "coverage"
    GAP_CARDS = "gap_cards"


@dataclass(frozen=True, slots=True)
class PromptChoice:
    id: str
    version: str
    prompt_hash: str
    schema_name: str


class AnkiPromptCatalogService:
    def catalog(self) -> PromptCatalog: ...

    def load_job_snapshot(
        self, *, lcl_id: str, coverage_id: str, gap_id: str
    ) -> AnkiPromptSnapshot: ...
```

Implement `catalog()` by iterating `root.glob("*.md")`, loading each stem with
`AnkiPromptLibrary`, classifying its validated schema, recording safe errors,
and rejecting duplicate IDs. Implement `load_job_snapshot()` by loading the
three selected prompts from the resolved external/fallback library and merging
them with the two bundled system prompts. Modify the runner dependency in
`stages.py` and `app.py` so preflight calls this service rather than a static
library. Preserve the existing prompt artifact shape.

- [ ] **Step 4: Run focused prompt and stage tests**

Run: `pytest tests/anki/test_prompt_catalog.py tests/anki/test_prompts.py tests/anki/test_stages.py -v`

Expected: PASS, including existing immutable snapshot and include-cycle tests.

- [ ] **Step 5: Commit the isolated catalog change**

```bash
git add src/oms_hub/anki/prompt_catalog.py src/oms_hub/anki/prompts.py src/oms_hub/anki/stages.py src/oms_hub/app.py tests/anki/test_prompt_catalog.py tests/anki/test_stages.py
git commit -m "feat: add dynamic Anki prompt catalog"
```

## Task 2: Persist the directory and expose native Settings controls

**Files:**
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/study_generation/path_picker.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/settings_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Modify: `tests/v2/test_generation_settings.py`
- Modify: `tests/js/settings.test.js`

**Interfaces:**
- Consumes: `StudyPromptSettingModel`, `GenerationRepository`, `AnkiPromptCatalogService`, and the Settings CSRF `postJson` helper.
- Produces: `GenerationRepository.anki_prompt_directory()`, `GenerationRepository.set_anki_prompt_directory(path)`, `PromptDirectoryPicker.select_directory()`, and `/settings/anki/prompts/directory` select/save/test routes.
- Downstream use: Task 3 bootstrap resolves the current directory through this persisted value; Task 1 catalog service receives it via a callback.

- [ ] **Step 1: Write failing route, repository, and JavaScript tests**

```python
def test_anki_prompt_directory_can_be_selected_saved_and_tested(tmp_path: Path) -> None:
    client, app = prepared_client(tmp_path)
    directory = tmp_path / "Main Vault" / "Anki AI Prompts"
    directory.mkdir(parents=True)
    _write_prompt(directory / "lcl.md", "lcl", "2.0", "lcl_v2")
    app.state.prompt_directory_picker = FakePromptDirectoryPicker(directory)

    selected = client.post("/settings/anki/prompts/directory/select", json={})
    saved = client.post("/settings/anki/prompts/directory", json={"path": str(directory)})
    tested = client.post("/settings/anki/prompts/directory/test")

    assert selected.json()["path"] == str(directory)
    assert saved.status_code == 200
    assert tested.json()["state"] == "valid"
    assert app.state.generation_repository.anki_prompt_directory() == str(directory)
```

```javascript
test("directory path action selects first, then saves, then reports catalog warnings", () => {
  assert.equal(settings.promptPathAction(""), "select");
  assert.equal(settings.promptPathAction("C:\\Vault\\Anki AI Prompts"), "save");
  assert.deepEqual(settings.catalogMessage({ state: "valid", choice_count: 3, issues: [{ message: "bad.md: unsupported schema" }] }),
    "3 prompt choices are ready. 1 warning: bad.md: unsupported schema");
});
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/v2/test_generation_settings.py -v && node --test tests/js/settings.test.js`

Expected: FAIL because the directory repository methods, picker, routes, and catalog message helper do not exist.

- [ ] **Step 3: Add directory persistence, picker, routes, and Settings card**

```python
ANKI_PROMPT_DIRECTORY_KEY = "anki_curation_prompt_directory"

def set_anki_prompt_directory(self, path: str) -> None: ...
def anki_prompt_directory(self) -> str | None: ...

class PromptDirectoryPicker(Protocol):
    def select_directory(self) -> Path | None: ...

class SystemPromptDirectoryPicker:
    def select_directory(self) -> Path | None: ...
```

Use the existing `study_prompt_settings` table with the fixed key above; reject
blank and non-directory values. Add a Windows `FolderBrowserDialog` picker that
returns an existing directory and gives the current NUC-only error on non-Windows
hosts. Add `select`, `save`, and `test` routes that return only path, state,
choice counts, and issue messages—not prompt contents. Render one Settings card
with `data-anki-prompt-directory`; use the existing Select/Save/Test interaction
pattern and CSRF helper in `settings.js`.

- [ ] **Step 4: Run Settings tests and lint the touched Python**

Run: `pytest tests/v2/test_generation_settings.py -v && node --test tests/js/settings.test.js && ruff check src/oms_hub/study_generation/repository.py src/oms_hub/study_generation/path_picker.py src/oms_hub/web/generation_routes.py src/oms_hub/web/settings_routes.py`

Expected: PASS with no lint findings.

- [ ] **Step 5: Commit the Settings workflow**

```bash
git add src/oms_hub/study_generation/repository.py src/oms_hub/study_generation/path_picker.py src/oms_hub/app.py src/oms_hub/web/generation_routes.py src/oms_hub/web/settings_routes.py src/oms_hub/web/templates/settings.html src/oms_hub/web/static/settings.js tests/v2/test_generation_settings.py tests/js/settings.test.js
git commit -m "feat: configure Anki prompt directory in settings"
```

## Task 3: Populate the Anki form with prompts, source states, decks, and editable defaults

**Files:**
- Modify: `src/oms_hub/anki/index.py`
- Modify: `src/oms_hub/web/anki_routes.py`
- Modify: `src/oms_hub/web/templates/anki.html`
- Modify: `src/oms_hub/web/static/anki.js`
- Modify: `tests/anki/test_index.py`
- Modify: `tests/anki/test_web.py`
- Modify: `tests/js/anki.test.js`

**Interfaces:**
- Consumes: `AnkiPromptCatalogService.catalog()`, `CompanionIndex`, `target_deck(LectureIdentity)`, and existing lecture source records.
- Produces: `CompanionIndex.list_deck_names()`, bootstrap `prompt_catalog` / `indexed_decks`, source-status helpers, and ordered deck-picker form values.
- Downstream use: Task 4 accepts the deck-picker order as `deck_allowlist`; Task 5 retains the existing review payload.

- [ ] **Step 1: Write failing companion, bootstrap, and browser tests**

```python
def test_list_deck_names_returns_case_insensitive_distinct_names(index: CompanionIndex) -> None:
    assert index.list_deck_names() == (
        "AnKing Step Deck", "Sketchy Pepper", "Zanki::Micro",
    )


def test_anki_bootstrap_includes_catalog_decks_target_deck_and_all_source_states(client) -> None:
    payload = client.get("/api/anki/bootstrap").json()
    lecture = payload["lectures"][0]
    assert payload["indexed_decks"] == ["AnKing Step Deck", "Sketchy Pepper"]
    assert payload["prompt_catalog"]["lcl"][0]["id"] == "lecture-concept-ledger"
    assert lecture["target_deck"].startswith("OMS-II_Custom_Cards::")
    assert lecture["source_status"] == {"slides": True, "transcripts": True, "summary": True}
```

```javascript
test("lecture source state always has slides transcript and outline cards", () => {
  assert.deepEqual(anki.sourceStatuses(null), {
    slides: "neutral", transcripts: "neutral", summary: "neutral",
  });
  assert.deepEqual(anki.sourceStatuses({ source_status: { slides: true, transcripts: false, summary: true } }), {
    slides: "ready", transcripts: "missing", summary: "ready",
  });
});

test("ordered deck helpers preserve first selection order", () => {
  assert.deepEqual(anki.addDeckPriority(["AnKing"], "Sketchy"), ["AnKing", "Sketchy"]);
  assert.deepEqual(anki.moveDeckPriority(["AnKing", "Sketchy"], 1, -1), ["Sketchy", "AnKing"]);
});
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/anki/test_index.py tests/anki/test_web.py -v && node --test tests/js/anki.test.js`

Expected: FAIL because deck listing, bootstrap fields, source-status helpers, and deck-priority helpers do not exist.

- [ ] **Step 3: Implement the form data and controls**

```python
def list_deck_names(self) -> tuple[str, ...]:
    """Return distinct deck names from the active companion snapshot."""

def _source_status(current_kinds: set[UploadKind], outline_available: bool) -> dict[str, bool]:
    return {
        "slides": UploadKind.SLIDES in current_kinds,
        "transcripts": UploadKind.TRANSCRIPTS in current_kinds,
        "summary": outline_available,
    }
```

Query `note_decks` with `SELECT DISTINCT deck_name ... ORDER BY deck_name COLLATE
NOCASE`. Add catalog choices, catalog issues, deck names, computed `target_deck`,
and all three source status booleans to page/bootstrap data. Replace Block label
with no control. Replace the existing deck text field with an add-deck select,
ordered selected deck list, move-up/move-down/remove buttons, and hidden ordered
form inputs. Replace the three free-text prompt version inputs with selects that
show `id — vVersion · hash`. Make slides/transcript/outline cards persist from
page load and render neutral/green/red status consistently. On lecture selection,
set both target deck and tag `.value` fields so either can be edited.

- [ ] **Step 4: Run focused UI and route tests**

Run: `pytest tests/anki/test_index.py tests/anki/test_web.py -v && node --test tests/js/anki.test.js`

Expected: PASS, including empty-index and missing-source states.

- [ ] **Step 5: Commit the curation form behavior**

```bash
git add src/oms_hub/anki/index.py src/oms_hub/web/anki_routes.py src/oms_hub/web/templates/anki.html src/oms_hub/web/static/anki.js tests/anki/test_index.py tests/anki/test_web.py tests/js/anki.test.js
git commit -m "feat: improve Anki curation form controls"
```

## Task 4: Preserve ordered deck priority through contracts and retrieval

**Files:**
- Modify: `src/oms_hub/anki/contracts.py`
- Modify: `src/oms_hub/anki/retrieval.py`
- Modify: `src/oms_hub/anki/stages.py`
- Modify: `src/oms_hub/anki/repository.py`
- Modify: `tests/anki/test_contracts.py`
- Modify: `tests/anki/test_retrieval.py`
- Modify: `tests/anki/test_stages.py`
- Modify: `tests/anki/test_anki_repository.py`

**Interfaces:**
- Consumes: ordered browser deck list and `CoverageJudgment.supporting_note_ids`.
- Produces: ordered `CreateCurationJobRequest.deck_allowlist`, per-deck retrieval groups, and priority-aware judged candidates.
- Downstream use: Task 5 reads the standard merged candidate review payload and does not need to know deck-search internals.

- [ ] **Step 1: Write failing order-preservation and priority-search tests**

```python
def test_deck_allowlist_dedupes_without_reordering() -> None:
    request = CreateCurationJobRequest.model_validate({
        **valid_payload(),
        "deck_allowlist": [" AnKing ", "Sketchy", "AnKing", "Zanki"],
    })
    assert request.deck_allowlist == ("AnKing", "Sketchy", "Zanki")


async def test_priority_retrieval_stops_after_first_deck_with_supported_candidate() -> None:
    result = await runner._judge_priority_decks(
        concept=concept(), deck_groups={"AnKing": [candidate(1)], "Sketchy": [candidate(2)]}
    )
    assert result.selected_note_ids == (1,)
    assert structured.calls == ["AnKing"]


async def test_priority_retrieval_tries_next_deck_when_first_judgment_has_no_support() -> None:
    result = await runner._judge_priority_decks(
        concept=concept(), deck_groups={"AnKing": [candidate(1)], "Sketchy": [candidate(2)]}
    )
    assert result.selected_note_ids == (2,)
    assert structured.calls == ["AnKing", "Sketchy"]
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/anki/test_contracts.py tests/anki/test_retrieval.py tests/anki/test_stages.py tests/anki/test_anki_repository.py -v`

Expected: FAIL because deck normalization sorts values and priority-judgment flow does not exist.

- [ ] **Step 3: Implement priority-safe normalization and retrieval/judgment flow**

```python
def _ordered_scope_values(values: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values:
        value = str(raw).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            ordered.append(value)
    return tuple(ordered)
```

Apply `_ordered_scope_values` to `deck_allowlist`; keep tag scope's existing
canonical behavior. Partition retrieval by one `CompanionFilters(deck_allowlist=(deck,))`
per selected deck and preserve those groups in the stage artifact. In the judgment
stage, evaluate each deck group in the stored order. Stop evaluating lower-priority
decks for that concept and pass when a judgment returns at least one
`supporting_note_id`; otherwise continue to the next deck. Persist only the
evaluated candidates/judgments into the merged review payload, so lower-priority
decks never appear merely because a higher-priority deck already supplied a
valid card. Preserve source-rescue and convergence passes by applying the same
per-deck rule to each retrieval pass.

- [ ] **Step 4: Run focused regression tests**

Run: `pytest tests/anki/test_contracts.py tests/anki/test_retrieval.py tests/anki/test_stages.py tests/anki/test_anki_repository.py tests/anki/test_pipeline.py -v`

Expected: PASS with deterministic deck order in request, stored job, stage artifact, and selected support.

- [ ] **Step 5: Commit deck priority semantics**

```bash
git add src/oms_hub/anki/contracts.py src/oms_hub/anki/retrieval.py src/oms_hub/anki/stages.py src/oms_hub/anki/repository.py tests/anki/test_contracts.py tests/anki/test_retrieval.py tests/anki/test_stages.py tests/anki/test_anki_repository.py
git commit -m "feat: prioritize ordered Anki deck retrieval"
```

## Task 5: Replace the long review scroll with Final proposed changes and Candidates

**Files:**
- Modify: `src/oms_hub/web/templates/anki_review.html`
- Modify: `src/oms_hub/web/static/anki.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `tests/js/anki.test.js`
- Modify: `tests/anki/test_web.py`

**Interfaces:**
- Consumes: existing review `groups.pass_1_matches`, `groups.recovered_in_pass_2`, `groups.generated_cards`, `groups.unresolved`, candidate selections, and tag policy.
- Produces: `reviewViews(review)`, `filterReviewItems(items, query)`, and two review containers named `final` and `candidates`.
- Downstream use: `collectReview()` continues to inspect the same checkbox, generated-card, and tag-editor data attributes.

- [ ] **Step 1: Write failing view-model and interaction tests**

```javascript
test("final proposed changes is the default and contains only selected changes", () => {
  const views = anki.reviewViews({
    groups: {
      pass_1_matches: [candidate(1, true), candidate(2, false)],
      recovered_in_pass_2: [candidate(3, true)],
      generated_cards: [generated("g1", true), generated("g2", false)],
      unresolved: [],
    },
  });
  assert.deepEqual(views.final.existing.map((item) => item.note_id), [1, 3]);
  assert.deepEqual(views.final.generated.map((item) => item.card_id), ["g1"]);
  assert.equal(views.active, "final");
});

test("candidate search matches card text note id extra and hidden tags", () => {
  assert.equal(anki.matchesReviewSearch(candidate(42, true, {
    text: "Iron deficiency", extra: "Low ferritin", tags: ["Heme::Anemia"],
  }), "ferritin"), true);
  assert.equal(anki.matchesReviewSearch(candidate(42, true), "42"), true);
  assert.equal(anki.matchesReviewSearch(candidate(42, true), "neuro"), false);
});
```

- [ ] **Step 2: Run the focused browser tests to verify they fail**

Run: `node --test tests/js/anki.test.js`

Expected: FAIL because the review view-model and search helpers do not exist.

- [ ] **Step 3: Implement the two-view review workspace**

```javascript
const reviewViews = (review) => ({
  active: "final",
  final: {
    existing: [...review.groups.pass_1_matches, ...review.groups.recovered_in_pass_2]
      .filter((candidate) => candidate.selected),
    generated: review.groups.generated_cards.filter((card) => card.selected),
  },
  candidates: [...review.groups.pass_1_matches, ...review.groups.recovered_in_pass_2],
});
```

Render a Final proposed changes tab and a Candidates tab with counts. Make Final
the initial active tab. Render selected existing cards and selected generated
cards separately in Final; render all initial/recovered candidates together in
Candidates and keep a small provenance badge. Add a top search input that filters
only the active view by note ID, front text, extra text, and all tag values.
Re-rendering or filtering must preserve DOM-backed selection and edit state;
update the in-memory review model on checkbox/text/tag input before any re-render.
Wrap `tagEditor()` in a closed `<details>` element labeled `Tags`, retaining all
existing protected/editable tag semantics. Do not surface unresolved items in
Final. Render them beneath Candidates inside one closed, non-actionable
`<details>` element labeled `Unresolved items (N)`; they are never selectable
or included in Final proposed changes.

- [ ] **Step 4: Run review tests and visual route coverage**

Run: `node --test tests/js/anki.test.js && pytest tests/anki/test_web.py -v`

Expected: PASS; the rendered review page includes Final proposed changes, Candidates, search, and collapsed Tags controls.

- [ ] **Step 5: Commit the review workspace**

```bash
git add src/oms_hub/web/templates/anki_review.html src/oms_hub/web/static/anki.js src/oms_hub/web/static/app.css tests/js/anki.test.js tests/anki/test_web.py
git commit -m "feat: streamline Anki curation review"
```

## Task 6: Run full verification and prepare test-instance rollout

**Files:**
- Modify: `docs/anki-curation-nuc-rollout.md`
- Modify: `README.md` only if the documented Settings path is listed there.
- Test: full Python, JavaScript, Ruff, and mypy suites.

**Interfaces:**
- Consumes: completed Tasks 1–5 and existing test-instance environment configuration.
- Produces: a reproducible rollout section documenting directory selection, catalog validation, Anki index availability, source readiness, and first-run checks.

- [ ] **Step 1: Add a failing/documentation checklist expectation**

```markdown
## Prompt directory and review acceptance

- [ ] Choose the Obsidian prompt directory in Settings and run Test / Refresh.
- [ ] Confirm at least one valid LCL, coverage, and card-generation choice.
- [ ] Confirm all three selected lecture sources show green checks.
- [ ] Verify Final proposed changes opens before Candidates.
```

Add this exact acceptance section to the NUC rollout document and, if a related
README section exists, link to the rollout document rather than duplicating the
operational steps.

- [ ] **Step 2: Run the complete automated verification suite**

Run: `pytest -q`

Expected: PASS with no failures.

Run: `node --test tests/js/*.test.js`

Expected: PASS with no failures.

Run: `ruff check src tests && mypy src`

Expected: PASS with no diagnostics.

- [ ] **Step 3: Perform the test-instance acceptance pass**

Run the branch on the Anki test port without changing the main Hub or AnkiConnect
port configuration. In Settings choose the Obsidian directory, test it, select
one valid prompt in each role, select an indexed deck order, choose a fully-ready
lecture, and queue a test run. Confirm the review page defaults to Final proposed
changes and that a selection change in Candidates updates Final before any apply
action is taken.

- [ ] **Step 4: Commit rollout documentation**

```bash
git add docs/anki-curation-nuc-rollout.md README.md
git commit -m "docs: add Anki prompt catalog rollout checks"
```

- [ ] **Step 5: Record the final commit set and request merge approval**

Run: `git log --oneline origin/main..HEAD`

Expected: the output contains the six focused implementation/documentation commits from this plan and no unrelated changes.

## Spec coverage review

| Specification requirement | Plan task |
| --- | --- |
| One Obsidian prompt directory, settings controls, fallback | Tasks 1–2 |
| Strict catalog validation, role selectors, bundled internal prompts, pinning | Task 1 |
| Remove block label, ordered decks, visible source readiness, editable defaults | Tasks 3–4 |
| True ordered deck search semantics | Task 4 |
| Final proposed changes, Candidates, search, collapsed tags | Task 5 |
| Regression suite and test-instance validation | Task 6 |
