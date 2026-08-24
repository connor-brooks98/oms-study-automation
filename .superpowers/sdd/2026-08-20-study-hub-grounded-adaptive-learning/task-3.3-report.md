# Task 3.3 implementation report

## Result

Implemented the isolated pre-submit Ask intent and answer-leak protection surface on
`sol3/ask-backend`, based on integration base
`ac8c4c8bf731f4e95eca25fbe81caba936b3b41f` (tree
`c58ca108a10f1cdc96b78c8ab4d3b7ce9b4a48a0`). The initial implementation is committed as
`68c1fd6a3d4a1ccdbb9441d44943515f136343c0` (tree
`640ef6da61ccab24c69ac878266101bc5cbb4969`), subject
`feat: prevent pre-submit Ask answer leakage`. Fix Round 1 is the separate non-amended
commit `430e6e73d12e40effcc1f9f76eed7e81017d3884` / tree
`bdefdcd6d661c0e590aa694420292c79cf087374`, subject
`fix: harden Ask pre-submit protection`. Fix Round 2 is the separate implementation/test
commit `a0830c702a1b8140ee0fd5c56dc896aa7e95bcf0` / tree
`2fb3faeff96d688afeddf02d2bc44d909c7db7ff`, subject
`fix: close Ask pre-submit bypasses`. This report update is a separate docs-only correction
record commit; that record commit is not self-referenced here. Fix Round 3 is the separate
implementation/test commit `944b3de52514090f85a1c1c848d3c7c21b9843d2` / tree
`a4e62823cd7c096d7ad7f88faea8c6f98eeb6781`, subject
`fix: enforce Ask elimination precedence`. Fix Round 4 is the separate implementation/test
commit `e7bdd642a448e9020057971bec66e9c775462500` / tree
`e021f4e6061dd60794611f5e4de6963062fdd866`, subject
`fix: block labeled Ask strategy bypass`.

## Interfaces

`src/oms_hub/ask/intent.py` provides:

- `AskIntent`: exactly `concept_hint`, `definition`, `mechanism`, `source_excerpt`,
  `compare_concepts`, `request_answer`, `request_option_elimination`, and `other`.
- `classify_pre_submit_intent(query: str) -> AskIntent`: deterministic NFKC/casefold/token
  classification. Direct answer, diagnosis, and option-elimination requests run before
  benign concept/definition/mechanism/source/compare rules, except explicit general
  test-taking framing with instructional wording, which is a benign concept hint. Direct or
  option-elimination requests remain protected even when they mention exams or strategy.
  Fullwidth text, case, Unicode format characters, whitespace, punctuation, and common
  punctuation variants are handled locally.

`src/oms_hub/ask/leakage.py` provides:

- Frozen-slots `LeakResult` containing only `leaked` and a generic reason.
- `detect_answer_leak(text, protected_answers)`: ignores blank values; applies NFKC,
  casefolding, Unicode `Cf` removal, tokenization, whitespace/punctuation/hyphen
  normalization, letter/decimal option-label normalization (including multi-digit decimal
  labels), and punctuation-separated abbreviation handling. A string outer
  `protected_answers` value is treated as one protected answer; malformed non-Sequence
  outers return generic no-match safely. Matching is contiguous token matching, so `war`
  does not match `warm`, `heparin` does not match `heparinase`, whitespace-separated `w a r`
  does not become `war`, and exact token matching keeps `1` distinct from `12`. Supplied
  variants are matched exactly after normalization; no synonym table or fuzzy edit distance
  exists.
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

## Fix Round 2

The requested Fix Round 2 tests were added before production changes while the worktree was
at the clean Fix Round 1 implementation commit `430e6e73d12e40effcc1f9f76eed7e81017d3884` /
tree `bdefdcd6d661c0e590aa694420292c79cf087374`. A transient missing-`pytest` test import
was corrected before the recorded RED run.

RED command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: exit `1` with 8 failures:

- generic `strategy` wording bypassed policy-sensitive mixed answer/option requests;
- multi-digit decimal option labels were not recognized;
- malformed non-Sequence outer protected-answer values raised instead of returning generic
  no-match.

GREEN command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: `57 passed`.

Fix Round 2 now recognizes benign strategy language only with explicit general test-taking
context, while policy checks remain first for mixed phrasing. Decimal option labels accept
any normalized decimal token and exact token matching preserves `1` versus `12`. Malformed
outer values return `LeakResult(False, "no_match")`; valid sequences still ignore non-string
members; and the controller ruling remains `insufficient_evidence=False` for policy refusal.

## Fix Round 3

The Fix Round 3 tests were added before production changes while the worktree was at the clean
Fix Round 2 implementation commit `dace31880f6d5c0c246f0bed91ee76ee12ba72ed` / tree
`03ed02a897710c154574eb82e524a71092b383d1`.

RED command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: exit `1` with 2 failures: `Can you rule out the choices for me on exams?` and
`Can you rule out the choices for me on a general test?` were incorrectly classified as
`concept_hint`.

GREEN command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: `61 passed`.

Fix Round 3 requires both explicit general test/exam context and instructional framing for
the benign strategy exception. Direct-answer and option-elimination checks now run before
that exception; the unconditional `rule out` direct-answer shortcut was replaced by a
narrower diagnostic-exclusion rule that excludes option terms and generic test context.
Ambiguous language therefore remains protected rather than becoming a benign hint.

## Verification evidence

1. Initial focused tests: `40 passed`.
2. Fix Round 1 focused tests: `49 passed`.
3. Fix Round 2 focused tests: `57 passed`.
4. Fix Round 3 focused tests: `61 passed`.
5. Fix Round 4 focused tests: `64 passed`.
6. Affected Ask/models/contracts command:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
     python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py tests/ask/test_models.py tests/contracts -q
   ```

   Result: `121 passed` (`64` Task 3.3, `16` Task 3.1 model, `41` contract tests).
7. Exact Ruff:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH ruff check src tests scripts
   ```

   Result: `All checks passed!`.
8. Source mypy:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH mypy src
   ```

   Result: `Success: no issues found in 180 source files`.
9. Task-owned test mypy:

   ```text
   PATH=/tmp/studyhub-task01-venv/bin:$PATH MYPYPATH=$PWD/src \
     mypy tests/ask/test_intent.py tests/ask/test_leakage.py
   ```

   Result: `Success: no issues found in 2 source files`.
10. Static safety checks:

   - An AST scan of both production modules reported no provider/model API, network, or
     logging imports/calls. The only project import is the required `GroundedAnswer` model
     contract used by the refusal builder.
   - A protected-answer literal scan over both production modules found no fixture medical
     answers. Protected values occur only in hand-derived tests.
   - A credential/private-content scan found no API keys, bearer credentials, private lecture
     text, Anki content, or production identifiers in the owned implementation/tests.
11. `git diff --check`: passed.
12. The exact base-to-head scope check is limited to the authorized files:

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

## Fix Round 4

The fix tests were added first against the clean Fix Round 3 documentation record commit
`b283c35f7e730120d643ed74f3ae92a32240dc8c` / tree
`5bb1125bba134d30c0eb6e937f54ed709e4b1de5`.

RED command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: exit `1` with 3 failures: `How can I rule out choice B on exams?`,
`What strategies help eliminate option 12 on tests?`, and the fullwidth-label form all
incorrectly returned `concept_hint`.

GREEN command:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src \
  python -m pytest tests/ask/test_intent.py tests/ask/test_leakage.py -q
```

Result: `64 passed`.

Fix Round 4 keeps general instructional strategy requests benign only when no concrete
option label is present: `_is_generic_strategy_query()` now rejects an `answer`, `choice`,
`choices`, `option`, or `options` token immediately followed by the existing normalized
letter-or-decimal `_is_option_label()` rule. The subsequent elimination check then protects
the labeled request. This is intentionally safe for ambiguity and adds no parallel parser.

## Known limitations

- The intent classifier is intentionally bounded and deterministic. Unrecognized paraphrases
  return `other` or a benign intent; adding a new rule requires a focused test.
- Leak detection does not infer medical synonyms and does not perform fuzzy edit distance.
  Callers must supply any approved abbreviation/variant explicitly.
- Punctuation-separated single-letter abbreviations are normalized; whitespace-separated
  letters remain separate to preserve token boundaries.
- Generic test-taking strategy requests are intentionally treated as benign concept hints;
  only explicit general test-taking framing with instructional wording qualifies, and
  question-scoped or mixed policy-sensitive phrasing remains protected.
- Decimal option labels accept any normalized decimal token; exact token matching keeps
  embedded values distinct. Unicode `Cf` format characters are removed after NFKC/casefold.
- A `str` outer `protected_answers` argument is treated as one protected value; malformed
  non-Sequence outers return generic no-match; non-string elements inside valid sequences
  remain ignored safely.
- The broad `rule out` shortcut is removed. Generic strategy recognition requires both
  explicit instructional wording and general test/exam context; ambiguous elimination
  phrasing errs toward the protected path.
- Fix Round 4 decision: an immediately labeled answer/choice/option term is never general
  strategy framing, including fullwidth letters and multi-digit decimal labels after NFKC.
  The existing option-label helper remains the single label grammar.
- Native Windows and provider/live acceptance remain unrun by design.

## Commit identity

The initial through Fix Round 4 implementation identities are fixed above. This report update
is intentionally delivered as a separate docs-only correction record commit; that record
commit is not self-referenced here.
