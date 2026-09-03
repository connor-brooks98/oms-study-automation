# Grouped Matching Questions and Gemini Structured Output Design

**Date:** 2026-09-02

**Status:** Approved in chat; awaiting written-spec review

**Scope:** Direct-import practice questions, native quiz review/publication/player, and the Gemini structured-output transport

## 1. Summary

Study Hub will preserve a source matching set as one native quiz question instead of flattening it into separate multiple-choice questions. A matching question contains one stem, two to eight labeled prompts, a shared bank of two to eight choices, and one correct choice for every prompt. The review editor and public player render the set as one grouped interaction.

The change is additive. Existing multiple-choice quiz JSON, answer requests, grading, progress, publication, and cached historical data remain valid without a migration. Matching support initially applies only to direct-import practice-question workflows; NotebookLM-generated quizzes continue to request multiple-choice questions.

The Gemini failure discovered during this design is fixed once in the Gemini provider adapter. Raw GenerateContent REST requests must send the enum value `APPLICATION_JSON` in `generationConfig.responseFormat.text.mimeType`. The adapter must not branch on a Gemini model ID.

## 2. Source example and required behavior

The motivating source is Question 1 in `CNS tumor practice Qs-1.docx`:

- one stem asks the learner to match histologic descriptions to brain tumors;
- prompts are labeled A through G;
- the shared answer bank is numbered 1 through 7; and
- the answer key later in the document provides A-to-number mappings.

Study Hub must extract, review, publish, display, and grade that material as one question. It must not emit seven independent questions.

The source example has text prompts rather than prompt-specific images. This scope therefore retains the existing single optional image at the question-group level. Prompt-specific media is a non-goal.

## 3. Goals

- Preserve a matching set as one grouped interaction.
- Extract a question block and a separately located row-level answer key without guessing.
- Keep the current multiple-choice contract byte-shape compatible when serialized.
- Reuse the native quiz review, publication, progress, and player infrastructure.
- Keep structured extraction provider-neutral across OpenAI, Gemini, Anthropic, and OpenRouter.
- Make Gemini structured-output requests valid for compatible Gemini models without model-version conditionals.
- Retain answer-key withholding before submission and current same-origin/CSRF protections.

## 4. Non-goals

- Generating matching questions through the NotebookLM quiz-generation workflow.
- Partial-credit scoring.
- Enforcing one-time use of a choice. Choices may be reused.
- Matching-specific choice elimination or strike-through controls.
- Prompt-specific images, rationales, topics, objectives, or provenance displays.
- Automatically generating missing matching answers through NotebookLM or another provider.
- Adding multiple-select, true/false, or free-response support for other question types visible in the source document.
- Supporting more than eight prompts or eight choices in this first implementation.
- Building a generic plug-in framework for arbitrary future question types.
- Changing provider code for individual model IDs.
- Deploying, restarting the Hub, retrying the failed production import, or publishing content as part of implementation.

## 5. Canonical native quiz contract

The Pydantic input models and serializer in `src/oms_hub/study_generation/native_quiz.py` remain the authoritative machine-checkable persisted contract. The domain types in `src/oms_hub/study_generation/domain.py` represent the validated in-memory form. HTTP serializers derive their output from those domain types; JavaScript fixtures do not become a second source of truth.

### 5.1 Legacy multiple choice

The existing multiple-choice shape remains unchanged and continues to omit a `kind` field:

```json
{
  "stem": "Which statement is correct?",
  "choices": ["Choice A", "Choice B"],
  "correct_index": 0,
  "rationale": "Explanation",
  "image_ref": null
}
```

Missing `kind` always means the existing multiple-choice variant. Existing serialized quizzes must round-trip to the same shape.

### 5.2 Matching question

Only the new variant emits `kind`:

```json
{
  "kind": "matching",
  "stem": "Match each description with the diagnosis.",
  "prompts": [
    {"label": "A", "text": "Description one", "correct_index": 1},
    {"label": "B", "text": "Description two", "correct_index": 0}
  ],
  "choices": ["Diagnosis one", "Diagnosis two"],
  "rationale": "A matches diagnosis two; B matches diagnosis one.",
  "image_ref": null
}
```

Contract rules:

- `kind` is exactly `matching`.
- `stem`, every prompt label/text, every choice, and `rationale` are non-empty.
- There are two to eight prompts and two to eight distinct choices.
- Prompt labels are distinct after case-folding.
- Each prompt owns its `correct_index`; parallel mapping arrays are forbidden.
- Every `correct_index` identifies an available choice.
- Multiple prompts may identify the same choice.
- `area`, `learning_objective`, `topic`, and `image_ref` remain optional group-level fields with their existing meanings.

The domain layer adds `QuizMatchingPrompt` and `QuizMatchingQuestion` while leaving `QuizQuestion` unchanged. `NativeQuiz.questions` becomes a union of the two question variants. Runtime IDs remain deterministic: questions use `qN`, prompts use `pN`, and choices use `cN`.

`parse_native_quiz()` accepts both variants because it is the persisted native contract. A new `parse_notebook_quiz()` wrapper calls that parser and rejects any matching variant. Both Notebook-generation workers must use the wrapper. Direct-import extraction, review, and initial publication accept matching only when the run's `content_kind` is `practice_questions`; any other content kind fails validation before publication. Management replacement applies the same boundary to published records; lecture-quiz and exam-review records remain multiple-choice-only.

## 6. Direct-import extraction contract

`ExtractionPayload` in `practice_contracts.py` remains the authority for provider output. Its question and answer collections become additive unions. Existing provider output without `kind` remains the current multiple-choice variant.

### 6.1 Extracted matching question

```json
{
  "kind": "matching",
  "original_identifier": "1",
  "stem": "Match each description with the diagnosis.",
  "prompts": [
    {"original_identifier": "A", "text": "Description one", "supplied_correct_index": null},
    {"original_identifier": "B", "text": "Description two", "supplied_correct_index": null}
  ],
  "choices": ["Diagnosis one", "Diagnosis two"],
  "rationale": null,
  "source_segments": [{"source_id": "source-1", "segment_key": "block-1"}],
  "candidate_assets": [],
  "confidence": 0.99
}
```

Inline mappings may populate `supplied_correct_index`. A separate answer key is represented as one matching-answer record whose individual rows retain their own citations:

```json
{
  "kind": "matching",
  "original_identifier": "1",
  "matches": [
    {
      "prompt_identifier": "A",
      "correct_index": 1,
      "rationale": null,
      "source_segments": [{"source_id": "source-1", "segment_key": "block-20"}]
    },
    {
      "prompt_identifier": "B",
      "correct_index": 0,
      "rationale": null,
      "source_segments": [{"source_id": "source-1", "segment_key": "block-21"}]
    }
  ]
}
```

All extraction indexes are zero-based positions in the emitted `choices` array, even when the source answer bank is visibly numbered from 1. The extraction instruction must explicitly distinguish one grouped matching question from a sequence of independent questions. It must preserve source prompt labels and choice order, and must emit only citations present in the supplied canonical document data.

Prompt `text` excludes its leading source label (`A.`, `B.`, and so on). Choice text excludes a leading bank label or ordinal (`1.`, `2.`, and so on). Prompt labels are stored separately; choice order is authoritative and the UI generates choice ordinals. This prevents duplicated displays such as `1. 1. Diagnosis`.

A matching question with complete inline mappings may omit `original_identifier`. A separately located matching answer key must include a group identifier and prompt identifiers; otherwise it remains unmatched and blocks review. If chunking produces more than one matching-answer record for the same group, pairing merges disjoint or identical prompt mappings. Conflicting duplicate mappings remain blockers.

`ExtractionResult` adds `answer_source_refs`, aligned one-to-one with its extracted answer records. For a matching-answer record, the extractor flattens and resolves all row citations into that record's stable, de-duplicated `QuestionSourceRef` tuple. The extract artifact serializes this aligned field, and the worker passes it to pairing alongside `question_source_refs`. This is the defined transport from answer-key citations into a matching draft's group-level source references.

## 7. Pairing and review draft behavior

Matching answer pairing is deterministic:

1. Normalize and match the group question identifier.
2. Within that group, normalize and match every prompt identifier.
3. Compare inline and separate-key mappings when both exist.
4. Accept a mapping only when it is unique, in range, and non-conflicting.

The following are hard review blockers:

- duplicate or conflicting group identifiers;
- duplicate or unknown prompt identifiers;
- conflicting mappings for one prompt;
- an out-of-range choice index;
- any prompt without a supplied mapping; and
- an unmatched matching-answer group.

Matching drafts use a distinct `MatchingQuestionDraft` with nested prompt drafts. Each prompt draft owns its source label, text, and nullable correct index. Shared stem, choice bank, rationale, image, source references, provenance, confidence, diagnostics, and verification fields remain at group level.

Group source references are the stable, de-duplicated union of the question citations and all accepted answer-row citations. When source answer rows contain explanations, their non-empty rationales are combined in prompt order into the group rationale. When none exists, Study Hub must create the deterministic mapping summary described below.

Missing matching mappings go directly to manual review. They do not enter the scalar NotebookLM/fallback answer-resolution path. Review can complete or correct mappings manually; changing any mapping changes group provenance to `manually_corrected`.

Mixed imports are valid. If every matching group is complete but an ordinary multiple-choice draft lacks an answer, answer resolution passes matching drafts through unchanged and calls NotebookLM/fallback only for unresolved multiple-choice drafts. If any matching group is incomplete, the run stops at review before any missing-answer provider call.

When a complete source key has no rationale, Study Hub must create only a deterministic summary of the supplied mappings, such as `Source-marked matches: A -> Diagnosis two; B -> Diagnosis one.` This restates source data and is not generated medical reasoning. The `Source-marked matches:` prefix marks a synthesized summary. Whenever a prompt label, choice text/order, or mapping changes, the server regenerates a prefixed summary from the submitted group; a reviewer-authored rationale without that prefix is preserved.

Review updates are atomic. The server validates the full prompt list, choice bank, and mappings before replacing a draft. Removing or reordering choices cannot leave stale indexes. A rejected edit leaves the prior draft unchanged.

The matching diagnostic lifecycle is explicit:

- A valid full-group edit clears question-owned `missing-supplied-matching-answer`, `conflicting-supplied-matching-answer`, `supplied-matching-answer-out-of-bounds`, and `duplicate-matching-prompt-identifier` diagnostics only after the edited group passes full validation.
- `unmatched-matching-answer-group` and `unknown-matching-prompt-answer` are run-level blockers because no prompt safely owns them. They may be acknowledged only after every owned matching prompt has a complete valid mapping; acknowledgement records that the extra source key entry was deliberately excluded.
- New `duplicate-matching-question-identifier`, `conflicting-matching-question-identifier`, and `conflicting-matching-question-source-reference` diagnostics are run-level and are not cleared by a question edit. They retain the existing explicit run-diagnostic acknowledgement behavior used for their legacy counterparts. Existing diagnostic codes and override behavior are unchanged.

## 8. Artifact persistence and compatibility

Run artifacts, draft payloads, and published quizzes already store JSON in text columns, so no database migration is required.

Extraction, pairing, answer-resolution, and normalization signature versions must change wherever their cached payload shape or interpretation changes. Old cached artifacts remain historical evidence and are never parsed as the new variant. A new or retried run recomputes from the earliest affected stage.

Published multiple-choice quizzes retain their existing serialized shape and behavior. Matching questions require the new parser; publication versioning and existing progress/version binding invalidate incompatible saved browser state.

Private preview cannot continue using a constant version. Its version becomes an opaque `preview:<sha256>` fingerprint of the canonical serialized native quiz produced from the current review artifact. Published versions remain numeric. Public JavaScript compares versions through `String(saved.version) === String(content.version)`, preserving existing numeric saved progress while accepting preview fingerprints. Any review edit therefore receives a new preview storage key even when positional `pN` and `cN` IDs remain the same.

## 9. Private review interaction

The review API returns a discriminated matching variant. The editor renders:

- one question card;
- editable group stem and rationale;
- an editable shared choice bank;
- one row per prompt with editable label/text; and
- one native `<select>` per prompt for the correct choice.

The choice bank is displayed in source order with generated 1-based ordinals, so the motivating set remains visibly numbered 1 through 7 without storing duplicate display labels. Every select uses the same ordered options, has a prompt-specific accessible label, and has a placeholder when unresolved. The editor sends the entire matching group in one update so server validation remains atomic. Existing multiple-choice review rendering and focus behavior remain unchanged.

The matching review update body is:

```json
{
  "kind": "matching",
  "stem": "Match each description with the diagnosis.",
  "prompts": [
    {"id": "p1", "label": "A", "text": "Description one", "correct_index": 1},
    {"id": "p2", "label": "B", "text": "Description two", "correct_index": null}
  ],
  "choices": ["Diagnosis one", "Diagnosis two"],
  "rationale": "Source-marked matches: A -> Diagnosis two."
}
```

Prompt IDs are required and must exactly match the current draft's prompt IDs; adding or removing prompt rows is outside this first implementation. `correct_index` may be null while a draft remains unresolved. Choice indexes refer to the submitted choice array. A complete valid update clears only the repairable matching diagnostics defined above.

Image selection/upload, topic fields, provenance, verification, diagnostics, and publication blockers continue to operate at group level.

## 10. Public content, submission, and grading

Before submission, public matching content withholds every correct mapping:

```json
{
  "kind": "matching",
  "id": "q1",
  "stem": "Match each description with the diagnosis.",
  "prompts": [
    {"id": "p1", "label": "A", "text": "Description one"},
    {"id": "p2", "label": "B", "text": "Description two"}
  ],
  "choices": [
    {"id": "c1", "text": "Diagnosis one"},
    {"id": "c2", "text": "Diagnosis two"}
  ]
}
```

The matching answer request is additive; the existing multiple-choice request remains unchanged:

```json
{
  "kind": "matching",
  "question_id": "q1",
  "matches": {"p1": "c2", "p2": "c1"}
}
```

The server requires exactly one known choice ID for every known prompt ID. An unknown question ID returns 404, matching current behavior. A wrong request variant, missing/extra/unknown prompt ID, or unknown choice ID returns 422. Reusing a choice ID is valid.

Feedback is scoped to the submitted question:

```json
{
  "kind": "matching",
  "correct": true,
  "correct_matches": {"p1": "c2", "p2": "c1"},
  "row_results": {"p1": true, "p2": true},
  "rationale": "A matches diagnosis two; B matches diagnosis one."
}
```

The group earns one point only when every row is correct. `row_results` supplies per-row feedback without changing score semantics. Submission locks the group, reveals the correct selection for each row, and displays the group rationale.

The public player renders a shared, ordinal-numbered choice bank and one native `<select>` beside each prompt. Rows are two-column on wider screens and stack on narrow screens. Keyboard operation, visible focus, prompt-specific labels, and error/feedback announcements are required. Matching questions omit the multiple-choice strike-through control.

Saved progress stores selected choice IDs by prompt ID. Restoration discards a saved matching answer unless the quiz version, question ID, prompt IDs, and choice IDs remain valid. Restored feedback must also have `kind: matching`, a boolean `correct`, exact prompt-key sets for `correct_matches` and `row_results`, known choice IDs in `correct_matches`, boolean row results, and a string rationale. Corrupt or partial feedback is discarded rather than rendered.

## 11. Medical-accuracy review

The accuracy gate receives one matching group and every resolved prompt-to-choice pair. It reviews the group as one question and returns the existing group-level verdict/notes. It must not silently flatten the group into unrelated questions or omit mappings from its prompt.

## 12. Provider compatibility and Gemini fix

The application contract is provider-neutral. Task assignments continue to select a provider plus an arbitrary model ID from dynamically listed models or the existing custom-model escape hatch. No matching code branches on provider or model.

Each provider adapter remains responsible only for translating the canonical JSON Schema and generation options into its service's wire contract. A new compatible model under an existing provider requires configuration, not code. A genuinely new provider service still requires one adapter. Models that do not support structured JSON output are unsuitable for quiz extraction and must fail visibly rather than silently falling back.

The reproduced Gemini 3.8 request returned HTTP 400 `INVALID_ARGUMENT` with this field error:

```text
Invalid value at 'generation_config.response_format.text.mime_type' ... "application/json"
```

The [Gemini GenerateContent REST API reference](https://ai.google.dev/api/generate-content) defines that raw field as a `MimeType` enum. The adapter must transmit:

```json
{
  "generationConfig": {
    "responseFormat": {
      "text": {
        "mimeType": "APPLICATION_JSON",
        "schema": {}
      }
    }
  }
}
```

This is one provider-level constant with no model allowlist or version check. The initial implementation does not add speculative schema rewriting. After the local test passes, one separately authorized source-free request with the exact extraction schema verifies the corrected wire value. If that request exposes a second, schema-specific rejection, that response becomes a new root cause and the design must be amended before adding a Gemini schema normalizer.

## 13. Error handling and safety

- Provider HTTP rejection occurs before the extraction response-validation retry and remains a failed extraction stage.
- Malformed or contract-invalid provider output receives the existing single corrective retry.
- Unknown citations, ambiguous answer pairing, incomplete matching mappings, and invalid edits fail closed.
- Public content never includes answer mappings before submission.
- Public submission retains existing same-origin and CSRF validation.
- Implementation and automated tests do not call live providers.
- No production retry, deployment, restart, publication, or provider-setting mutation is included.

## 14. Testing strategy

Implementation follows test-driven development. Minimum coverage:

### Provider transport

- Gemini sends `APPLICATION_JSON` for structured output.
- The test uses an arbitrary model ID and asserts no model-specific branch.
- The caller's schema is transmitted without mutation.
- Existing Gemini text generation and model listing remain unchanged.
- The expanded `ExtractionPayload.model_json_schema()` is passed through mocked structured-generation calls for OpenAI, Gemini, Anthropic, and OpenRouter.
- Each provider test asserts its existing provider-specific normalization and confirms that the caller's canonical schema object is not mutated.

### Native contract

- Legacy multiple-choice parsing and serialization are unchanged.
- Matching parsing, validation, deterministic IDs, and serialization round-trip.
- Reused choices are accepted.
- Duplicate prompt labels, missing mappings, and out-of-range indexes are rejected.
- Public projection withholds mappings.
- Group grading is all-or-nothing and returns per-row results.

### Extraction and pairing

- A synthetic seven-prompt/seven-choice fixture represents the source's A-through-G and 1-through-7 structure without copying its medical content. It uses the same nontrivial source permutation: A-to-6, B-to-5, C-to-2, D-to-1, E-to-3, F-to-7, and G-to-4.
- A later answer-key block maps prompt labels to numeric choices.
- The group remains one extracted question and one review draft.
- Missing, duplicate, unknown, conflicting, and out-of-range mappings become blockers.
- Matching groups with missing mappings do not call NotebookLM or fallback generation.
- In a mixed matching/multiple-choice import, complete matching drafts pass through unchanged and only unresolved multiple-choice drafts call answer providers.
- Artifact round-trip and signature invalidation cover the new variants.

### Review and publication

- Matching edits validate and persist atomically.
- Incomplete groups cannot publish.
- Published-edit reconstruction preserves the group.
- Notebook-generation workers reject matching output; non-practice direct-import initial publication and non-practice management replacement reject matching payloads.
- Run-diagnostic acknowledgement fails while any owned matching prompt lacks a valid mapping; the existing acknowledgement and legacy override behavior remains unchanged for legacy diagnostics.
- A synthesized `Source-marked matches:` rationale regenerates after a prompt-label, choice-text/order, or mapping edit, while a reviewer-authored rationale remains unchanged.
- One mixed matching/multiple-choice fixture covers extraction, artifact round-trip, review, publication, and player behavior without assuming a homogeneous question collection.
- Public/preview routes validate matching submissions and preserve CSRF behavior.
- Preview versions change with the canonical review content fingerprint.
- Accuracy-review serialization includes every prompt-to-choice pair.

### JavaScript player/editor

- Review rendering and save payloads retain one grouped card.
- Player rendering, selection, restoration, submit completeness, feedback, score, and continuation work for matching.
- Corrupt, partial, or stale saved matching feedback is discarded.
- Numeric saved progress restores when the current published version is the equivalent string, proving version-string compatibility.
- Existing multiple-choice interaction tests remain green.
- Accessible labels and keyboard-native select behavior are present.

Focused suites must run before broader regression suites. Browser verification uses a local/offline quiz fixture; it is not evidence of production deployment.

## 15. Acceptance criteria

- The synthetic equivalent of source Question 1 imports as exactly one matching question with seven prompts, seven choices, and seven mappings.
- The public player presents one grouped interaction and scores it as one question.
- Each row reports correctness after submission, while no mapping is exposed beforehand.
- Choice reuse is accepted and every prompt is required.
- An incomplete or conflicting matching key blocks publication without invoking answer-generation providers.
- Existing multiple-choice serialized JSON and public answer requests are unchanged.
- No database migration is required.
- Gemini structured output sends `APPLICATION_JSON` for every model ID.
- No implementation branch names `gemini-3.8-flash` or another model version.
- Automated tests use mocked providers; any live verification is source-free and separately authorized.
- No production deployment, restart, retry, publication, or provider-setting change occurs without a new explicit authorization.

## 16. Implementation boundaries

The smallest complete implementation touches the existing contract/domain, extraction/pairing/cache, review, public API, player/editor, accuracy serialization, provider adapter, and their focused tests. It reuses current JSON persistence, quiz versioning, routes, styles, and native form controls. It adds no dependency, generic question plug-in framework, second quiz player, or speculative cross-provider abstraction.
