# Task 3.3 implementation report

## Result

Implemented the isolated pre-submit Ask intent and answer-leak protection surface on
`sol3/ask-backend`, based on integration base
`ac8c4c8bf731f4e95eca25fbe81caba936b3b41f` (tree
`c58ca108a10f1cdc96b78c8ab4d3b7ce9b4a48a0`). The required implementation commit subject is
`feat: prevent pre-submit Ask answer leakage`.

## Interfaces

`src/oms_hub/ask/intent.py` provides:

- `AskIntent`: exactly `concept_hint`, `definition`, `mechanism`, `source_excerpt`,
  `compare_concepts`, `request_answer`, `request_option_elimination`, and `other`.
- `classify_pre_submit_intent(query: str) -> AskIntent`: deterministic NFKC/casefold/token
  classification. Direct answer, diagnosis, and option-elimination requests run before
  benign concept/definition/mechanism/source/compare rules. Fullwidth text, case, Unicode
  whitespace, punctuation, and common punctuation variants are handled locally.

`src/oms_hub/ask/leakage.py` provides:

- Frozen-slots `LeakResult` containing only `leaked` and a generic reason.
- `detect_answer_leak(text, protected_answers)`: ignores blank values; applies NFKC,
  casefolding, tokenization, whitespace/punctuation/hyphen normalization, option-label
  normalization, and punctuation-separated abbreviation handling. Matching is contiguous
  token matching, so `war` does not match `warm`, `heparin` does not match `heparinase`,
  and whitespace-separated `w a r` does not become `war`. Supplied variants are matched
  exactly after normalization; no synonym table or fuzzy edit distance exists.
- `safe_pre_submit_refusal() -> GroundedAnswer`: returns exactly:

  `Submit the question first. I can still explain the underlying concept or point you to the relevant source.`

  It has no claims, citations, provider request ID, or retrieval run ID, and carries the
  generic deterministic reason `pre_submit_answer_protection`. The accepted `GroundedAnswer`
  contract has no action field, so the sentence's concept/source choices remain semantic
  offers (`concept_hint` and `show_source`) rather than a new payload.

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

## Verification evidence

1. Focused tests: `40 passed`.
2. Affected Ask/models/contracts command:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
     python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py tests/ask/test_models.py tests/contracts -q
   ```

   Result: `97 passed` (`40` Task 3.3, `16` Task 3.1 model, `41` contract tests).
3. Exact Ruff:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH ruff check src tests scripts
   ```

   Result: `All checks passed!`.
4. Source mypy:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH mypy src
   ```

   Result: `Success: no issues found in 180 source files`.
5. Task-owned test mypy:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH MYPYPATH=$PWD/src \
     mypy tests/ask/test_intent.py tests/ask/test_leakage.py
   ```

   Result: `Success: no issues found in 2 source files`.
6. Static safety checks:

   - An AST scan of both production modules reported no provider/model API, network, or
     logging imports/calls. The only project import is the required `GroundedAnswer` model
     contract used by the refusal builder.
   - A protected-answer literal scan over both production modules found no fixture medical
     answers. Protected values occur only in hand-derived tests.
   - A credential/private-content scan found no API keys, bearer credentials, private lecture
     text, Anki content, or production identifiers in the owned implementation/tests.
7. `git diff --check`: passed.
8. The exact base-to-head scope check is limited to the authorized files:

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
- The refusal is fail-closed and provenance-free. `GroundedAnswer` defaults supply empty
  claims/citations and null provider/retrieval IDs; no accepted model or package export was
  changed.
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
- Native Windows and provider/live acceptance remain unrun by design.

## Commit identity

The handoff and this report retain symbolic `SELF`/`SELF_TREE` until the non-amended commit
is created. The final report must be completed with the resulting `git rev-parse HEAD` and
`git rev-parse 'HEAD^{tree}'` values after commit, and the worktree must be clean.
