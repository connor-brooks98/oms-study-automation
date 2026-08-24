# Task 3.3 implementation report

## Result

Implemented the isolated pre-submit Ask intent and answer-leak protection surface on
`sol3/ask-backend`, based on integration base
`ac8c4c8bf731f4e95eca25fbe81caba936b3b41f` (tree
`c58ca108a10f1cdc96b78c8ab4d3b7ce9b4a48a0`). The initial implementation is committed as
`68c1fd6a3d4a1ccdbb9441d44943515f136343c0` (tree
`640ef6da61ccab24c69ac878266101bc5cbb4969`), subject
`feat: prevent pre-submit Ask answer leakage`. Fix Round 1 is the separate non-amended
commit `SELF` / tree `SELF_TREE`, subject `fix: harden Ask pre-submit protection`.

## Interfaces

`src/oms_hub/ask/intent.py` provides:

- `AskIntent`: exactly `concept_hint`, `definition`, `mechanism`, `source_excerpt`,
  `compare_concepts`, `request_answer`, `request_option_elimination`, and `other`.
- `classify_pre_submit_intent(query: str) -> AskIntent`: deterministic NFKC/casefold/token
  classification. Direct answer, diagnosis, and option-elimination requests run before
  benign concept/definition/mechanism/source/compare rules, except generic test-taking
  strategy language, which is a benign concept hint. Fullwidth text, case, Unicode format
  characters, whitespace, punctuation, and common punctuation variants are handled locally.

`src/oms_hub/ask/leakage.py` provides:

- Frozen-slots `LeakResult` containing only `leaked` and a generic reason.
- `detect_answer_leak(text, protected_answers)`: ignores blank values; applies NFKC,
  casefolding, Unicode `Cf` removal, tokenization, whitespace/punctuation/hyphen
  normalization, letter/decimal option-label normalization, and punctuation-separated
  abbreviation handling. A string outer `protected_answers` value is treated as one
  protected answer. Matching is contiguous token matching, so `war` does not match `warm`,
  `heparin` does not match `heparinase`, whitespace-separated `w a r` does not become
  `war`, and `1` does not match `12`. Supplied variants are matched exactly after
  normalization; no synonym table or fuzzy edit distance exists.
- `safe_pre_submit_refusal() -> GroundedAnswer`: returns exactly:

  `Submit the question first. I can still explain the underlying concept or point you to the relevant source.`

  It has no claims, citations, provider request ID, or retrieval run ID, and carries the
  generic deterministic reason `pre_submit_answer_protection`, and
  `insufficient_evidence=False` because this is a policy refusal rather than missing
  evidence. The accepted `GroundedAnswer` contract has no action field, so the
  sentence's concept/source choices remain semantic offers (`concept_hint` and
  `show_source`) rather than a new payload.

## TDD record

Tests were added first in `tests/ask/test_intent.py` and `tests/ask/test_leakage.py`.

RED command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Before the production modules existed, test collection exited `2` with:

```text
ModuleNotFoundError: No module named 'oms_hub.ask.intent'
ModuleNotFoundError: No module named 'oms_hub.ask.leakage'
```

GREEN command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: `40 passed`.

The focused tests use hand-derived literals and cover all required enum values, the five
brief examples, diagnosis/option-elimination paraphrases, direct-before-benign precedence,
fullwidth NFKC input, case/whitespace/punctuation, punctuation/hyphen changes, option-label
formats, dotted abbreviations, short-token boundaries, substring false positives, blank
inputs, immutable/non-sensitive results, and refusal metadata.

## Fix Round 1

The requested fix tests were added before changing production code while the worktree was
at the clean initial implementation commit `68c1fd6a3d4a1ccdbb9441d44943515f136343c0` /
tree `640ef6da61ccab24c69ac878266101bc5cbb4969`.

RED command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: exit `1` with 8 failures:

- generic test-taking strategy text was classified as `request_option_elimination` or
  `request_answer` instead of benign `concept_hint`;
- zero-width/format characters bypassed answer matching and intent matching;
- decimal option labels were not recognized;
- a string outer `protected_answers` value was iterated character-by-character;
- the refusal incorrectly set `insufficient_evidence=True`.

GREEN command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: `49 passed`.

Fix Round 1 behavior now removes all Unicode `Cf` format characters after NFKC/casefold in
both normalizers, recognizes one-token decimal option labels alongside ASCII letters,
preserves boundaries for longer digit tokens, treats an outer string as one protected
value, classifies generic test-taking strategy as `concept_hint` while keeping
question-scoped elimination protected, and marks policy refusal as
`insufficient_evidence=False`.

## Verification evidence

1. Initial focused tests: `40 passed`.
2. Fix Round 1 focused tests: `49 passed`.
3. Affected Ask/models/contracts command:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
     python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py tests/ask/test_models.py tests/contracts -q
   ```

   Result: `106 passed` (`49` Task 3.3, `16` Task 3.1 model, `41` contract tests).
4. Exact Ruff:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH ruff check src tests scripts
   ```

   Result: `All checks passed!`.
5. Source mypy:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH mypy src
   ```

   Result: `Success: no issues found in 180 source files`.
6. Task-owned test mypy:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH MYPYPATH=$PWD/src \
     mypy tests/ask/test_intent.py tests/ask/test_leakage.py
   ```

   Result: `Success: no issues found in 2 source files`.
7. Static safety checks:

   - An AST scan of both production modules reported no provider/model API, network, or
     logging imports/calls. The only project import is the required `GroundedAnswer` model
     contract used by the refusal builder.
   - A protected-answer literal scan over both production modules found no fixture medical
     answers. Protected values occur only in hand-derived tests.
   - A credential/private-content scan found no API keys, bearer credentials, private lecture
     text, Anki content, or production identifiers in the owned implementation/tests.
8. `git diff --check`: passed.
9. The exact base-to-head scope check is limited to the authorized files:

   ```text
   src/oms_hub/ask/intent.py
   src/oms_hub/ask/leakage.py
   tests/ask/test_intent.py
   tests/ask/test_leakage.py
   docs/implementation/handoffs/3.3.md
   .superpowers/sdd/2026-08-20-study-hub-grounded-adaptive-learning/task-3.3-report.md
   ```

No PyMuPDF crash path, native Windows acceptance, provider/live call, network call, private
data access, Anki action, production mutation, push, merge, tag, or deploy was performed.

## Safety and scope decisions

- The implementation imports no provider SDK, network client, logging package, or LLM/model
  caller. It makes no external calls.
- Protected answer values are compared in memory and are never logged, included in
  `LeakResult`, placed in a reason, or serialized by production code.
- `LeakResult` is `@dataclass(frozen=True, slots=True)` and contains no answer text.
- The refusal is policy fail-closed and provenance-free, with
  `insufficient_evidence=False`. `GroundedAnswer` defaults supply empty claims/citations
  and null provider/retrieval IDs; no accepted model or package export was changed.
- No schema impact: frozen Ask v1 and the formal additive Ask v2 proposal remain unchanged;
  shared exporter/snapshot work remains pending Sol-0.
- No service, route, context, feature flag, dependency, persistence, or integration wiring
  was added. Task 3.7 owns future service-boundary use.

## Known limitations

- The intent classifier is intentionally bounded and deterministic. Unrecognized paraphrases
  return `other` or a benign intent; adding a new rule requires a focused test.
- Leak detection does not infer medical synonyms and does not perform fuzzy edit distance.
  Callers must supply any approved abbreviation/variant explicitly.
- Punctuation-separated single-letter abbreviations are normalized; whitespace-separated
  letters remain separate to preserve token boundaries.
- Generic test-taking strategy requests are intentionally treated as benign concept hints;
  question-scoped option elimination remains protected.
- Decimal option labels are supported only as one-token labels; embedded multi-digit tokens
  remain distinct. Unicode `Cf` format characters are removed after NFKC/casefold.
- A `str` outer `protected_answers` argument is treated as one protected value; non-string
  elements inside a sequence remain ignored safely.
- Native Windows and provider/live acceptance remain unrun by design.

## Commit identity

The initial implementation identity is fixed above. `SELF`/`SELF_TREE` intentionally refer
to the separate non-amended Fix Round 1 commit that contains this report update; the final
commit and tree are reported in the completion handoff.
