# Grouped Matching Quiz Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve a direct-import matching set as one reviewable, publishable, and all-or-nothing graded quiz interaction while leaving legacy multiple-choice payloads unchanged.

**Architecture:** Add one explicit matching variant at each existing quiz boundary: provider extraction, review draft, canonical native quiz, private review API, and public answer API. Keep persistence in the existing JSON columns, dispatch by `kind == "matching"`, and reuse the current publication, image, progress, and scoring infrastructure. Notebook-generated quizzes remain multiple-choice-only through a narrow parser wrapper.

**Tech Stack:** Python 3.12, Pydantic 2, FastAPI, SQLAlchemy/SQLite JSON text artifacts, vanilla browser JavaScript, CSS, pytest, Node's built-in test runner, respx/httpx, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md`

## Global Constraints

- Complete `docs/superpowers/plans/2026-09-02-gemini-structured-output-fix.md` before Task 3; both plans touch `tests/llm/test_gemini.py`, so apply them serially.
- Missing `kind` means the existing multiple-choice variant. Its serialized question shape and scalar answer request stay unchanged.
- Only the new native and extraction variants emit `"kind": "matching"`.
- A matching group has 2–8 prompts and 2–8 case-fold-distinct choices; labels are case-fold-distinct; every published prompt has an in-range mapping; choices may be reused.
- Matching earns one point only when every prompt is correct. No partial credit, choice elimination, prompt-specific media, or generated missing matching answers.
- Matching is publishable only when `content_kind == "practice_questions"`; lecture quizzes, exam review, and both Notebook generation workers remain multiple-choice-only.
- Existing JSON text columns are sufficient. Add no migration, dependency, question plug-in framework, or provider/model branch.
- Bump exact stage versions to `practice-extraction-v4`, `supplied-answer-pairing-v4`, `practice-answer-resolution-v2`, and `question-draft-review-v2`.
- Complete Task 0's hash-bound four-document planning commit on local `main`, then create `codex/grouped-matching-quiz` in the external linked worktree from that exact commit before changing any implementation or test file.
- Automated provider tests use mocked HTTP only. This implementation plan makes no live provider request, retries no production import, and publishes no real content. Push, merge, and NUC deployment occur only after its verification gates under `docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md`, as explicitly authorized on 2026-09-02.
- Preserve unrelated working-tree changes and stage only the files named by the current task.

---

## File structure

- `src/oms_hub/study_generation/domain.py` — canonical in-memory matching question, prompt, and feedback values.
- `src/oms_hub/study_generation/native_quiz.py` — authoritative persisted parser/serializer, public projection, grading, and Notebook-only rejection wrapper.
- `src/oms_hub/study_generation/practice_contracts.py` — provider-neutral extraction unions.
- `src/oms_hub/study_generation/practice_extraction.py` — extraction instruction, merge behavior, and aligned question/answer citation transport.
- `src/oms_hub/study_generation/practice_domain.py` — matching review drafts and the draft union.
- `src/oms_hub/study_generation/practice_matching.py` — deterministic MCQ/matching pairing plus owned and run-level diagnostics.
- `src/oms_hub/study_generation/quiz_import_worker.py` — artifact round-trip, cache signatures, run diagnostics, and MCQ-only missing-answer routing.
- `src/oms_hub/study_generation/studio_repository.py` — accept the draft union when persisting review metadata.
- `src/oms_hub/study_generation/practice_review.py` — atomic matching edits, blockers, synthesized rationale, and native conversion.
- `src/oms_hub/study_generation/repository.py` — content-kind publication/replacement boundary.
- `src/oms_hub/llm/openrouter.py` — serialize a matching group and all proposed mappings into one accuracy-review request.
- `src/oms_hub/web/studio_routes.py` — private review/preview request and response variants plus preview fingerprint.
- `src/oms_hub/web/templates/studio_quiz_preview.html` — expose the same preview fingerprint in page metadata.
- `src/oms_hub/web/published_quiz_routes.py` — reconstruct matching review drafts from published practice content.
- `src/oms_hub/web/public_quiz_routes.py` — public matching answer request validation and feedback.
- `src/oms_hub/web/static/studio_quiz_review.js`, `src/oms_hub/web/static/app.css`, and `src/oms_hub/web/templates/studio_quiz_review.html` — grouped matching editor and cache-busting asset reference.
- `src/oms_hub/web/static/public_quiz.js` and `src/oms_hub/web/static/public_quiz.css` — grouped player, saved progress, feedback, and responsive layout.
- Existing focused tests under `tests/study_generation`, `tests/llm`, `tests/v2`, and `tests/js` own regression coverage; no production fixture file is added.

---

### Task 0: Commit the planning baseline and create the isolated feature worktree

**Files:**
- Commit: `docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md`
- Commit: `docs/superpowers/plans/2026-09-02-gemini-structured-output-fix.md`
- Commit: `docs/superpowers/plans/2026-09-02-grouped-matching-implementation.md`
- Commit: `docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md`

**Interfaces:**
- Consumes: the four approved planning documents currently present in the dirty `main` checkout.
- Produces: one exact planning commit on local `main` whose `Planning-SHA256` trailer binds those four files, followed by branch `codex/grouped-matching-quiz` in `/Users/connor/Developer/worktrees/oms-study-automation-grouped-matching-quiz` at that exact commit.

- [ ] **Step 1: Commit exactly the four planning documents on local `main`**

Run this in the current local checkout. It fails closed if the index already contains work, if the checkout is not normal local `main`, or if a tracked path other than the approved spec is modified. It stages no unrelated path and proves unrelated untracked state is unchanged:

```bash
set -euo pipefail
oms_source_root=/Users/connor/Developer/oms-study-automation
cd "$oms_source_root"
oms_git_dir=$(cd "$(git rev-parse --git-dir)" && pwd -P)
oms_git_common=$(cd "$(git rev-parse --git-common-dir)" && pwd -P)
test "$oms_git_dir" = "$oms_git_common"
test -z "$(git rev-parse --show-superproject-working-tree 2>/dev/null)"
test "$(git -C "$oms_source_root" branch --show-current)" = main
oms_planning_paths=(
  docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md
  docs/superpowers/plans/2026-09-02-gemini-structured-output-fix.md
  docs/superpowers/plans/2026-09-02-grouped-matching-implementation.md
  docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md
)
for oms_path in "${oms_planning_paths[@]}"; do
  test -f "$oms_path"
done
test -z "$(git diff --cached --name-only)"
oms_expected_tracked_change=docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md
test "$(git diff --name-only)" = "$oms_expected_tracked_change"
oms_unrelated_before=$(git -C "$oms_source_root" status --porcelain=v1 --untracked-files=all -- . \
  ':(exclude)docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md' \
  ':(exclude)docs/superpowers/plans/2026-09-02-gemini-structured-output-fix.md' \
  ':(exclude)docs/superpowers/plans/2026-09-02-grouped-matching-implementation.md' \
  ':(exclude)docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md')
git add -- "${oms_planning_paths[@]}"
oms_expected_paths=$(printf '%s\n' "${oms_planning_paths[@]}" | LC_ALL=C sort)
oms_staged_paths=$(git diff --cached --name-only | LC_ALL=C sort)
test "$oms_staged_paths" = "$oms_expected_paths"
git diff --cached --check
oms_planning_manifest=$(shasum -a 256 "${oms_planning_paths[@]}")
oms_planning_digest=$(printf '%s\n' "$oms_planning_manifest" | shasum -a 256 | awk '{print $1}')
git commit \
  -m "docs: bind grouped matching implementation baseline" \
  -m "Planning-SHA256: $oms_planning_digest"
oms_planning_commit=$(git rev-parse HEAD)
oms_committed_paths=$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)
test "$oms_committed_paths" = "$oms_expected_paths"
test "$(git show -s --format=%B HEAD | sed -n 's/^Planning-SHA256: //p')" = \
  "$oms_planning_digest"
oms_committed_manifest=$(
  for oms_path in "${oms_planning_paths[@]}"; do
    oms_file_digest=$(git show "HEAD:$oms_path" | shasum -a 256 | awk '{print $1}')
    printf '%s  %s\n' "$oms_file_digest" "$oms_path"
  done
)
oms_committed_digest=$(printf '%s\n' "$oms_committed_manifest" | shasum -a 256 | awk '{print $1}')
test "$oms_committed_digest" = "$oms_planning_digest"
git diff --quiet
git diff --cached --quiet
oms_unrelated_after=$(git -C "$oms_source_root" status --porcelain=v1 --untracked-files=all -- . \
  ':(exclude)docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md' \
  ':(exclude)docs/superpowers/plans/2026-09-02-gemini-structured-output-fix.md' \
  ':(exclude)docs/superpowers/plans/2026-09-02-grouped-matching-implementation.md' \
  ':(exclude)docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md')
test "$oms_unrelated_before" = "$oms_unrelated_after"
printf 'Planning commit: %s\nPlanning-SHA256: %s\n%s\n' \
  "$oms_planning_commit" "$oms_planning_digest" "$oms_committed_manifest"
```

- [ ] **Step 2: Create the external linked worktree from that exact commit**

Do not use the repository-local untracked `worktrees/` directory. From local `main`, create the explicitly selected external worktree and feature branch directly from the planning commit:

```bash
set -euo pipefail
oms_source_root=/Users/connor/Developer/oms-study-automation
cd "$oms_source_root"
oms_planning_commit=$(git rev-parse HEAD)
oms_planning_digest=$(git show -s --format=%B HEAD | sed -n 's/^Planning-SHA256: //p')
test -n "$oms_planning_digest"
oms_worktree_path=/Users/connor/Developer/worktrees/oms-study-automation-grouped-matching-quiz
oms_branch=codex/grouped-matching-quiz
test "$(git branch --show-current)" = main
test ! -e "$oms_worktree_path"
if git show-ref --verify --quiet "refs/heads/$oms_branch"; then
  printf 'Branch already exists; inspect it instead of overwriting it: %s\n' "$oms_branch"
  exit 1
fi
oms_unrelated_before=$(git status --porcelain=v1 --untracked-files=all)
git worktree add -b "$oms_branch" "$oms_worktree_path" "$oms_planning_commit"
test "$(git -C "$oms_worktree_path" branch --show-current)" = "$oms_branch"
test "$(git -C "$oms_worktree_path" rev-parse HEAD)" = "$oms_planning_commit"
test -z "$(git -C "$oms_worktree_path" status --porcelain=v1)"
oms_worktree_git_dir=$(cd "$(git -C "$oms_worktree_path" rev-parse --git-dir)" && pwd -P)
oms_worktree_git_common=$(cd "$(git -C "$oms_worktree_path" rev-parse --git-common-dir)" && pwd -P)
test "$oms_worktree_git_dir" != "$oms_worktree_git_common"
test -z "$(git -C "$oms_worktree_path" rev-parse --show-superproject-working-tree 2>/dev/null)"
test "$(git -C "$oms_worktree_path" show -s --format=%B HEAD | sed -n 's/^Planning-SHA256: //p')" = \
  "$oms_planning_digest"
test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$oms_planning_commit"
oms_unrelated_after=$(git status --porcelain=v1 --untracked-files=all)
test "$oms_unrelated_before" = "$oms_unrelated_after"
printf 'Worktree: %s\nBranch: %s\nPlanning commit: %s\nPlanning-SHA256: %s\n' \
  "$oms_worktree_path" "$oms_branch" "$oms_planning_commit" "$oms_planning_digest"
```

- [ ] **Step 3: Run baseline tests only in the linked worktree**

Change the execution directory to the linked worktree. Set up its ignored local environment if needed, run both baseline suites there, and do not begin Task 1 if either suite fails:

```bash
set -euo pipefail
oms_source_root=/Users/connor/Developer/oms-study-automation
oms_worktree_path=/Users/connor/Developer/worktrees/oms-study-automation-grouped-matching-quiz
cd "$oms_worktree_path"
oms_git_dir=$(cd "$(git rev-parse --git-dir)" && pwd -P)
oms_git_common=$(cd "$(git rev-parse --git-common-dir)" && pwd -P)
test "$oms_git_dir" != "$oms_git_common"
test -z "$(git rev-parse --show-superproject-working-tree 2>/dev/null)"
test "$(git branch --show-current)" = codex/grouped-matching-quiz
oms_planning_digest=$(git show -s --format=%B HEAD | sed -n 's/^Planning-SHA256: //p')
test -n "$oms_planning_digest"
test -z "$(git status --porcelain=v1)"
if [ ! -x .venv/bin/pytest ]; then
  python3.13 -m venv .venv
  .venv/bin/python -m pip install -e '.[dev]'
fi
.venv/bin/pytest
node --test tests/js/*.test.js
test -z "$(git status --porcelain=v1)"
git rev-parse HEAD
git rev-parse HEAD^{tree}
printf 'Planning-SHA256: %s\n' "$oms_planning_digest"
```

Expected: both baseline suites pass and `git status --short` is empty. If setup or a baseline test fails, report the exact failure and stop before Task 1; do not blur a pre-existing failure into feature work.

---

### Task 1: Add the canonical native matching contract

**Files:**
- Modify: `tests/study_generation/test_native_quiz.py`
- Modify: `src/oms_hub/study_generation/domain.py:195-232`
- Modify: `src/oms_hub/study_generation/native_quiz.py:158-420`

**Interfaces:**
- Consumes: existing `QuizChoice`, `QuizImageRef`, `QuizQuestion`, and `NativeQuiz`.
- Produces: `QuizMatchingPrompt`, `QuizMatchingQuestion`, `QuizQuestionValue`, `QuizMatchingFeedback`, `parse_native_quiz(raw: str) -> NativeQuiz`, and byte-shape-compatible `serialize_native_quiz(quiz: NativeQuiz) -> str`.

- [ ] **Step 1: Write failing parser, validation, ID, reuse, and legacy-shape tests**

Add this fixture and tests to `tests/study_generation/test_native_quiz.py` and import `serialize_native_quiz` plus the new domain types:

```python
def _matching_payload(**overrides: object) -> dict[str, object]:
    question: dict[str, object] = {
        "kind": "matching",
        "stem": "Match each description with its term.",
        "prompts": [
            {"label": "A", "text": "Description alpha", "correct_index": 1},
            {"label": "B", "text": "Description beta", "correct_index": 1},
        ],
        "choices": ["Term one", "Term two"],
        "rationale": "Source-marked matches: A -> Term two; B -> Term two.",
        "image_ref": None,
    }
    question.update(overrides)
    return {"title": "Matching set", "questions": [question]}


def test_matching_quiz_round_trips_with_stable_group_prompt_and_choice_ids() -> None:
    quiz = parse_native_quiz(json.dumps(_matching_payload()))
    question = quiz.questions[0]

    assert isinstance(question, QuizMatchingQuestion)
    assert question.id == "q1"
    assert tuple(prompt.id for prompt in question.prompts) == ("p1", "p2")
    assert tuple(choice.id for choice in question.choices) == ("c1", "c2")
    assert tuple(prompt.correct_choice_id for prompt in question.prompts) == ("c2", "c2")
    assert serialize_native_quiz(quiz) == json.dumps(
        _matching_payload(), ensure_ascii=False, separators=(",", ":")
    )


def test_legacy_multiple_choice_serialization_does_not_gain_a_kind_field() -> None:
    quiz = parse_native_quiz(json.dumps(_payload()))
    serialized = json.loads(serialize_native_quiz(quiz))

    assert serialized["questions"] == [
        {
            **_payload()["questions"][0],
            "image_ref": None,
        }
    ]
    assert "kind" not in serialized["questions"][0]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"prompts": [{"label": "A", "text": "One", "correct_index": 0}]}, "prompts"),
        ({"prompts": [
            {"label": "A", "text": "One", "correct_index": 0},
            {"label": "a", "text": "Two", "correct_index": 1},
        ]}, "labels must be distinct"),
        ({"prompts": [
            {"label": "A", "text": "One", "correct_index": 0},
            {"label": "B", "text": "Two"},
        ]}, "correct_index"),
        ({"prompts": [
            {"label": "A", "text": "One", "correct_index": 0},
            {"label": "B", "text": "Two", "correct_index": 2},
        ]}, "available choice"),
    ],
)
def test_invalid_matching_native_contract_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(QuizContractError, match=message):
        parse_native_quiz(json.dumps(_matching_payload(**overrides)))
```

- [ ] **Step 2: Run the native contract tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_native_quiz.py -v
```

Expected: the new imports/classes are absent or matching input is rejected as an extra-field/union validation failure; the existing MCQ tests remain green.

- [ ] **Step 3: Add the minimal domain variants**

In `domain.py`, leave `QuizQuestion` unchanged and add:

```python
@dataclass(frozen=True, slots=True)
class QuizMatchingPrompt:
    id: str
    label: str
    text: str
    correct_choice_id: str


@dataclass(frozen=True, slots=True)
class QuizMatchingQuestion:
    id: str
    stem: str
    prompts: tuple[QuizMatchingPrompt, ...]
    choices: tuple[QuizChoice, ...]
    rationale: str
    image_ref: QuizImageRef | None = None
    area: str | None = None
    learning_objective: str | None = None
    topic: str | None = None


QuizQuestionValue = QuizQuestion | QuizMatchingQuestion


@dataclass(frozen=True, slots=True)
class NativeQuiz:
    title: str
    questions: tuple[QuizQuestionValue, ...]


@dataclass(frozen=True, slots=True)
class QuizMatchingFeedback:
    kind: Literal["matching"]
    correct: bool
    correct_matches: dict[str, str]
    row_results: dict[str, bool]
    rationale: str
```

Add `from typing import Literal`. Do not add `kind` to `QuizQuestion` or `QuizFeedback`.

- [ ] **Step 4: Add matching Pydantic input models and union dispatch**

In `native_quiz.py`, import `Literal`, `model_validator`, `QuizMatchingPrompt`, and `QuizMatchingQuestion`. Add:

```python
class _MatchingPromptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: _Text
    text: _Text
    correct_index: int = Field(ge=0)


class _MatchingQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["matching"]
    stem: _Text
    prompts: Annotated[list[_MatchingPromptInput], Field(min_length=2, max_length=8)]
    choices: Annotated[list[_Text], Field(min_length=2, max_length=8)]
    rationale: _Text
    area: _Dimension | None = None
    learning_objective: _Dimension | None = Field(
        default=None, validation_alias=AliasChoices("learning_objective", "objective")
    )
    topic: _Dimension | None = None
    image_ref: _ImageRefInput | None = None

    @field_validator("choices")
    @classmethod
    def choices_are_distinct(cls, choices: list[str]) -> list[str]:
        if len({choice.casefold() for choice in choices}) != len(choices):
            raise ValueError("choices must be distinct")
        return choices

    @model_validator(mode="after")
    def prompt_contract_is_valid(self) -> "_MatchingQuestionInput":
        if len({prompt.label.casefold() for prompt in self.prompts}) != len(self.prompts):
            raise ValueError("prompt labels must be distinct after case-folding")
        if any(prompt.correct_index >= len(self.choices) for prompt in self.prompts):
            raise ValueError("correct_index must identify an available choice")
        return self


_QuestionValueInput = _QuestionInput | _MatchingQuestionInput


class _QuizInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _Title
    questions: Annotated[list[_QuestionValueInput], Field(min_length=1, max_length=100)]
```

Replace the question construction inside `parse_native_quiz()` with an exact helper that preserves shared image and metadata fields:

```python
def _domain_question(
    question: _QuestionValueInput, question_index: int
) -> QuizQuestion | QuizMatchingQuestion:
    choices = tuple(
        QuizChoice(f"c{choice_index}", choice)
        for choice_index, choice in enumerate(question.choices, start=1)
    )
    image_ref = (
        QuizImageRef(
            question.image_ref.key,
            question.image_ref.source_title,
            question.image_ref.locator,
            question.image_ref.description,
        )
        if question.image_ref is not None
        else None
    )
    if isinstance(question, _MatchingQuestionInput):
        return QuizMatchingQuestion(
            f"q{question_index}",
            question.stem,
            tuple(
                QuizMatchingPrompt(
                    f"p{prompt_index}",
                    prompt.label,
                    prompt.text,
                    f"c{prompt.correct_index + 1}",
                )
                for prompt_index, prompt in enumerate(question.prompts, start=1)
            ),
            choices,
            question.rationale,
            image_ref,
            question.area,
            question.learning_objective,
            question.topic,
        )
    return QuizQuestion(
        f"q{question_index}",
        question.stem,
        choices,
        f"c{question.correct_index + 1}",
        question.rationale,
        image_ref,
        question.area,
        question.learning_objective,
        question.topic,
    )
```

Construct `NativeQuiz(validated.title, tuple(_domain_question(question, index) ...))` with indexes starting at 1.

- [ ] **Step 5: Serialize the union while preserving the legacy MCQ dictionary**

Add `_serialized_question()` and use it in `serialize_native_quiz()`:

```python
def _serialized_question(
    question: QuizQuestion | QuizMatchingQuestion,
) -> dict[str, object]:
    shared: dict[str, object] = {
        "stem": question.stem,
        "choices": [choice.text for choice in question.choices],
        "rationale": question.rationale,
        **({"area": question.area} if question.area is not None else {}),
        **(
            {"learning_objective": question.learning_objective}
            if question.learning_objective is not None
            else {}
        ),
        **({"topic": question.topic} if question.topic is not None else {}),
        "image_ref": asdict(question.image_ref) if question.image_ref is not None else None,
    }
    if isinstance(question, QuizMatchingQuestion):
        index_by_choice_id = {
            choice.id: index for index, choice in enumerate(question.choices)
        }
        return {
            "kind": "matching",
            "stem": question.stem,
            "prompts": [
                {
                    "label": prompt.label,
                    "text": prompt.text,
                    "correct_index": index_by_choice_id[prompt.correct_choice_id],
                }
                for prompt in question.prompts
            ],
            **{key: value for key, value in shared.items() if key != "stem"},
        }
    return {
        "stem": question.stem,
        "choices": [choice.text for choice in question.choices],
        "correct_index": next(
            index
            for index, choice in enumerate(question.choices)
            if choice.id == question.correct_choice_id
        ),
        **{key: value for key, value in shared.items() if key not in {"stem", "choices"}},
    }
```

Import `asdict` from `dataclasses`. Keep `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` unchanged.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_native_quiz.py -v
.venv/bin/ruff check src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/native_quiz.py tests/study_generation/test_native_quiz.py
.venv/bin/mypy src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/native_quiz.py
```

Expected: all commands pass; matching reuses `c2`, IDs are deterministic, invalid mappings fail, and the MCQ serialized dictionary has no `kind`.

```bash
git add src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/native_quiz.py tests/study_generation/test_native_quiz.py
git diff --cached --check
git commit -m "feat(quiz): add native matching contract"
```

---

### Task 2: Withhold matching answers, grade groups, and reject matching from Notebook generation

**Files:**
- Modify: `tests/study_generation/test_native_quiz.py`
- Modify: `tests/study_generation/test_worker.py`
- Modify: `tests/study_generation/test_studio_worker.py`
- Modify: `src/oms_hub/study_generation/native_quiz.py:326-376`
- Modify: `src/oms_hub/study_generation/worker.py:259-278`
- Modify: `src/oms_hub/study_generation/studio_worker.py:193-215`

**Interfaces:**
- Consumes: `QuizMatchingQuestion` from Task 1.
- Produces: `parse_notebook_quiz(raw: str) -> NativeQuiz`, `grade_matching_answer(quiz: NativeQuiz, question_id: str, matches: Mapping[str, str]) -> QuizMatchingFeedback`, and matching-safe `public_quiz_content()`.

- [ ] **Step 1: Add failing public-projection, group-grading, and Notebook-boundary tests**

Add to `test_native_quiz.py`:

```python
def test_matching_public_content_withholds_every_mapping() -> None:
    content = public_quiz_content(parse_native_quiz(json.dumps(_matching_payload())))

    assert content["questions"] == [{
        "kind": "matching",
        "id": "q1",
        "stem": "Match each description with its term.",
        "prompts": [
            {"id": "p1", "label": "A", "text": "Description alpha"},
            {"id": "p2", "label": "B", "text": "Description beta"},
        ],
        "choices": [
            {"id": "c1", "text": "Term one"},
            {"id": "c2", "text": "Term two"},
        ],
    }]
    assert "correct" not in repr(content)


def test_matching_grading_is_all_or_nothing_with_row_feedback_and_choice_reuse() -> None:
    quiz = parse_native_quiz(json.dumps(_matching_payload()))

    correct = grade_matching_answer(quiz, "q1", {"p1": "c2", "p2": "c2"})
    wrong = grade_matching_answer(quiz, "q1", {"p1": "c1", "p2": "c2"})

    assert correct.correct is True
    assert correct.row_results == {"p1": True, "p2": True}
    assert wrong.correct is False
    assert wrong.correct_matches == {"p1": "c2", "p2": "c2"}
    assert wrong.row_results == {"p1": False, "p2": True}


@pytest.mark.parametrize(
    "matches",
    [
        {"p1": "c2"},
        {"p1": "c2", "p2": "c2", "p3": "c1"},
        {"p1": "c2", "p9": "c2"},
        {"p1": "c9", "p2": "c2"},
    ],
)
def test_matching_grading_rejects_partial_extra_unknown_or_invalid_maps(
    matches: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="matching answer"):
        grade_matching_answer(
            parse_native_quiz(json.dumps(_matching_payload())), "q1", matches
        )


def test_notebook_parser_rejects_matching_but_native_parser_accepts_it() -> None:
    raw = json.dumps(_matching_payload())
    assert len(parse_native_quiz(raw).questions) == 1
    with pytest.raises(QuizContractError, match="multiple-choice"):
        parse_notebook_quiz(raw)
```

Add worker-boundary regressions using the existing fakes in each test module:

```python
# Add this neutral fixture constant to both worker test modules.
MATCHING_QUIZ_JSON = json.dumps({
    "title": "Matching set",
    "questions": [{
        "kind": "matching",
        "stem": "Match each description with its term.",
        "prompts": [
            {"label": "A", "text": "Alpha", "correct_index": 1},
            {"label": "B", "text": "Beta", "correct_index": 0},
        ],
        "choices": ["Term one", "Term two"],
        "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
        "image_ref": None,
    }],
})


# tests/study_generation/test_worker.py
def test_generation_worker_rejects_matching_notebook_output(tmp_path: Path) -> None:
    publisher = Publisher()
    worker, repository, _, _ = _worker(
        tmp_path,
        _job(
            stage=GenerationStage.QUIZ_VALIDATE,
            notebook_answer=MATCHING_QUIZ_JSON,
        ),
        publisher,
    )
    attempts: list[tuple[object, ...]] = []
    failures: list[tuple[str, str, bool]] = []
    repository.record_attempt = lambda *values: attempts.append(values)
    repository.contract_failure_count = lambda _job_id: 2
    repository.fail = lambda job_id, error, paused=False: failures.append(
        (job_id, error, paused)
    )

    assert worker.run_once() is True
    assert publisher.calls == []
    assert attempts and "multiple-choice" in str(attempts[0][-1])
    assert failures and "multiple-choice" in failures[0][1]


# tests/study_generation/test_studio_worker.py
class _MatchingGateway(_SuccessfulGateway):
    def ask_studio(self, subject, exam_number, prompt, remote_source_ids):
        del subject, exam_number, prompt, remote_source_ids
        self.ask_calls += 1
        return "matching-notebook", MATCHING_QUIZ_JSON


def test_studio_notebook_worker_rejects_matching_output(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = StudioRepository(database)
    publisher = GenerationRepository(database)
    run = _queued_run(repository)
    worker = StudioWorker(
        repository,
        _MatchingGateway(),
        object(),
        _FakeConnection(),
        publisher=publisher,
    )

    assert worker.run_once() is True
    rejected = repository.get_run(run.id)
    assert rejected.state is StudioRunState.RETRYING
    assert rejected.diagnostic_source == DiagnosticSource.CONTRACT.value
    assert publisher.published_quizzes(frozenset({QuizContentKind.EXAM_REVIEW})) == ()
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_native_quiz.py tests/study_generation/test_worker.py tests/study_generation/test_studio_worker.py -v
```

Expected: the native test fails because matching projection/grading and the Notebook wrapper are absent, and both worker regressions fail because the generation workers still accept matching Notebook output.

- [ ] **Step 3: Add projection, grading, and Notebook-only validation**

In `native_quiz.py`, branch inside `public_quiz_content()` before creating the MCQ item:

```python
if isinstance(question, QuizMatchingQuestion):
    item = {
        "kind": "matching",
        "id": question.id,
        "stem": question.stem,
        "prompts": [
            {"id": prompt.id, "label": prompt.label, "text": prompt.text}
            for prompt in question.prompts
        ],
        "choices": [
            {"id": choice.id, "text": choice.text} for choice in question.choices
        ],
    }
else:
    item = {
        "id": question.id,
        "stem": question.stem,
        "choices": [
            {"id": choice.id, "text": choice.text} for choice in question.choices
        ],
    }
```

Keep the existing group image and classification-field append logic after this branch. Add:

```python
def parse_notebook_quiz(raw: str) -> NativeQuiz:
    quiz = parse_native_quiz(raw)
    if any(isinstance(question, QuizMatchingQuestion) for question in quiz.questions):
        raise QuizContractError("NotebookLM quiz JSON must contain only multiple-choice questions")
    return quiz


def grade_matching_answer(
    quiz: NativeQuiz,
    question_id: str,
    matches: Mapping[str, str],
) -> QuizMatchingFeedback:
    question = next((item for item in quiz.questions if item.id == question_id), None)
    if question is None:
        raise KeyError(question_id)
    if not isinstance(question, QuizMatchingQuestion):
        raise ValueError("matching answer was submitted for a multiple-choice question")
    prompt_ids = {prompt.id for prompt in question.prompts}
    choice_ids = {choice.id for choice in question.choices}
    if set(matches) != prompt_ids or any(choice_id not in choice_ids for choice_id in matches.values()):
        raise ValueError("matching answer must identify one available choice for every prompt")
    correct_matches = {
        prompt.id: prompt.correct_choice_id for prompt in question.prompts
    }
    row_results = {
        prompt_id: matches[prompt_id] == correct_choice_id
        for prompt_id, correct_choice_id in correct_matches.items()
    }
    return QuizMatchingFeedback(
        "matching", all(row_results.values()), correct_matches, row_results, question.rationale
    )
```

Also make `grade_answer()` raise `ValueError("multiple-choice answer was submitted for a matching question")` after it finds a `QuizMatchingQuestion`, so a wrong request variant cannot access `correct_choice_id`.

- [ ] **Step 4: Route both Notebook-generation workers through the wrapper**

Replace only the imports and calls in `worker.py` and `studio_worker.py`:

```python
from oms_hub.study_generation.native_quiz import parse_notebook_quiz

# legacy generation worker
quiz = parse_notebook_quiz(answer.text)

# Studio Notebook generation worker
quiz = replace(parse_notebook_quiz(answer), title=run.label)
```

Do not replace persisted-payload calls to `parse_native_quiz()` in repositories or management routes.

- [ ] **Step 5: Run focused and worker regression tests, then commit**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_native_quiz.py tests/study_generation/test_worker.py tests/study_generation/test_studio_worker.py -v
rg -n "parse_native_quiz\(answer" src/oms_hub/study_generation/worker.py src/oms_hub/study_generation/studio_worker.py
```

Expected: pytest passes and `rg` returns no matches. Persisted native parser call sites remain unchanged.

```bash
git add src/oms_hub/study_generation/native_quiz.py src/oms_hub/study_generation/worker.py src/oms_hub/study_generation/studio_worker.py tests/study_generation/test_native_quiz.py tests/study_generation/test_worker.py tests/study_generation/test_studio_worker.py
git diff --cached --check
git commit -m "feat(quiz): grade matching groups and guard Notebook output"
```

---

### Task 3: Expand the extraction contract and verify all four provider adapters

**Files:**
- Modify: `tests/study_generation/test_practice_contracts.py`
- Modify: `src/oms_hub/study_generation/practice_contracts.py`
- Modify: `tests/llm/test_openai.py`
- Modify: `tests/llm/test_gemini.py`
- Modify: `tests/llm/test_anthropic.py`
- Modify: `tests/llm/test_openrouter_provider.py`

**Interfaces:**
- Consumes: the Gemini MIME correction from `2026-09-02-gemini-structured-output-fix.md` and existing provider schema normalizers.
- Produces: `ExtractedMatchingPrompt`, `ExtractedMatchingQuestion`, `ExtractedMatchingAnswerRow`, `ExtractedMatchingAnswer`, `ExtractedQuestionValue`, and `ExtractedAnswerValue`; `ExtractionPayload.model_json_schema()` remains the sole canonical provider schema.

- [ ] **Step 1: Write failing extraction-union tests**

Add imports for the four new models and this test to `test_practice_contracts.py`:

```python
def test_matching_extraction_contract_preserves_group_rows_and_zero_based_indexes() -> None:
    payload = ExtractionPayload.model_validate({
        "questions": [{
            "kind": "matching",
            "original_identifier": "1",
            "stem": "Match each prompt.",
            "prompts": [
                {"original_identifier": "A", "text": "A. Alpha description", "supplied_correct_index": None},
                {"original_identifier": "B", "text": "B) Beta description", "supplied_correct_index": 0},
            ],
            "choices": ["1. First term", "2) Second term"],
            "rationale": None,
            "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
            "candidate_assets": [],
            "confidence": 0.99,
        }],
        "answers": [{
            "kind": "matching",
            "original_identifier": "1",
            "matches": [
                {"prompt_identifier": "A", "correct_index": 1, "rationale": None,
                 "source_segments": [{"source_id": "source-1", "segment_key": "answer-a"}]},
                {"prompt_identifier": "B", "correct_index": 0, "rationale": "Source explanation.",
                 "source_segments": [{"source_id": "source-1", "segment_key": "answer-b"}]},
            ],
        }],
    })

    question = payload.questions[0]
    answer = payload.answers[0]
    assert isinstance(question, ExtractedMatchingQuestion)
    assert tuple(prompt.text for prompt in question.prompts) == (
        "Alpha description", "Beta description"
    )
    assert question.choices == ("First term", "Second term")
    assert isinstance(answer, ExtractedMatchingAnswer)
    assert answer.matches[0].correct_index == 1
```

Add parameterized rejection cases for fewer than two prompts, more than eight choices, blank prompt labels/text, duplicate choices after prefix stripping, and an unknown `kind`. Keep duplicate prompt labels and out-of-range row indexes schema-valid so deterministic pairing can produce review diagnostics instead of triggering a provider retry.

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_contracts.py -v
```

Expected: the matching models are absent and the payload union rejects `kind`.

- [ ] **Step 3: Implement the additive extraction models without a discriminator**

In `practice_contracts.py`, import `Literal`, `re`, and add:

```python
_PROMPT_PREFIX = re.compile(r"^\s*([^\s).:]+)\s*[).:]\s*")
_CHOICE_PREFIX = re.compile(r"^\s*(?:\d+|[A-H])\s*[).:]\s*", re.IGNORECASE)


class ExtractedMatchingPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    original_identifier: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10_000)
    supplied_correct_index: int | None = Field(default=None, ge=0, le=7)

    @field_validator("original_identifier")
    @classmethod
    def label_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("matching prompt label must not be blank")
        return stripped

    @field_validator("text")
    @classmethod
    def normalize_text(cls, text: str, info: object) -> str:
        text = text.strip()
        if not text:
            raise ValueError("matching prompt text must not be blank")
        identifier = getattr(info, "data", {}).get("original_identifier")
        match = _PROMPT_PREFIX.match(text)
        if not isinstance(identifier, str) or match is None or match.group(1).casefold() != identifier.casefold():
            return text
        stripped = text[match.end():].strip()
        if not stripped:
            raise ValueError("matching prompt text must not be blank")
        return stripped


class ExtractedMatchingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["matching"]
    original_identifier: str | None = Field(default=None, max_length=100)
    stem: str = Field(min_length=1, max_length=10_000)
    prompts: tuple[ExtractedMatchingPrompt, ...] = Field(min_length=2, max_length=8)
    choices: tuple[str, ...] = Field(min_length=2, max_length=8)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segments: tuple[SegmentCitation, ...] = Field(min_length=1, max_length=50)
    candidate_assets: tuple[AssetCitation, ...] = Field(default=(), max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("prompts", "choices", "source_segments", "candidate_assets", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("choices")
    @classmethod
    def normalize_distinct_choices(cls, choices: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_CHOICE_PREFIX.sub("", choice, count=1).strip() for choice in choices)
        if any(not choice for choice in normalized):
            raise ValueError("matching choices must not be blank")
        if len({choice.casefold() for choice in normalized}) != len(normalized):
            raise ValueError("choices must be distinct after case-folding")
        return normalized

    @field_validator("stem")
    @classmethod
    def stem_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("matching stem must not be blank")
        return stripped


class ExtractedMatchingAnswerRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_identifier: str | None = Field(default=None, max_length=100)
    correct_index: int = Field(ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segments: tuple[SegmentCitation, ...] = Field(min_length=1, max_length=50)

    @field_validator("source_segments", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("prompt_identifier")
    @classmethod
    def normalize_optional_prompt_identifier(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else None


class ExtractedMatchingAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["matching"]
    original_identifier: str | None = Field(default=None, max_length=100)
    matches: tuple[ExtractedMatchingAnswerRow, ...] = Field(min_length=1, max_length=8)

    @field_validator("matches", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("original_identifier")
    @classmethod
    def normalize_optional_group_identifier(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else None


ExtractedQuestionValue = ExtractedQuestion | ExtractedMatchingQuestion
ExtractedAnswerValue = ExtractedAnswer | ExtractedMatchingAnswer
```

Set `ExtractionPayload.questions: tuple[ExtractedQuestionValue, ...]` and `answers: tuple[ExtractedAnswerValue, ...]`. Do not use Pydantic's discriminator because legacy MCQ objects omit `kind`. The optional identifiers deliberately allow a malformed separate key to survive provider validation and become an unmatched/unknown review blocker rather than disappearing into a corrective retry. Update `validate_source_references()` to validate matching group citations/assets and every matching answer row's citations.

- [ ] **Step 4: Add mocked expanded-schema transport tests for every provider**

In each provider test file, import `copy`, `ExtractionPayload`, and its existing normalizer where applicable. Use the file's existing successful response shape and add one test named `test_<provider>_sends_expanded_extraction_schema_without_mutating_source`. Each test must execute `provider.generate_text(..., output_schema=schema)` with:

```python
schema = ExtractionPayload.model_json_schema()
original = copy.deepcopy(schema)
```

Assert the exact captured schema at the existing wire path:

```python
# tests/llm/test_openai.py
assert payload["text"]["format"]["schema"] == openai_output_schema(original)
assert schema == original

# tests/llm/test_openrouter_provider.py
assert payload["response_format"]["json_schema"]["schema"] == openai_output_schema(original)
assert schema == original

# tests/llm/test_anthropic.py
assert payload["output_config"]["format"]["schema"] == anthropic_output_schema(original)
assert schema == original

# tests/llm/test_gemini.py
assert payload["generationConfig"]["responseFormat"] == {
    "text": {"mimeType": "APPLICATION_JSON", "schema": original}
}
assert schema == original
```

For OpenAI return `resp-extraction-schema` with `status: completed`; for OpenRouter return `gen-extraction-schema`; for Anthropic return `message-extraction-schema`; for Gemini return `modelVersion: "gemini-schema-model"`. All response text is `'{"questions":[],"answers":[]}'`. Add `anthropic_output_schema` and `openai_output_schema` to imports rather than reproducing their algorithms.

- [ ] **Step 5: Run contract and provider tests, then commit**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_contracts.py tests/llm/test_openai.py tests/llm/test_gemini.py tests/llm/test_anthropic.py tests/llm/test_openrouter_provider.py -v
.venv/bin/ruff check src/oms_hub/study_generation/practice_contracts.py tests/study_generation/test_practice_contracts.py tests/llm/test_openai.py tests/llm/test_gemini.py tests/llm/test_anthropic.py tests/llm/test_openrouter_provider.py
.venv/bin/mypy src/oms_hub/study_generation/practice_contracts.py
```

Expected: all mocked transports accept the expanded union, preserve the caller schema, and Gemini uses `APPLICATION_JSON`; no live request occurs.

```bash
git add src/oms_hub/study_generation/practice_contracts.py tests/study_generation/test_practice_contracts.py tests/llm/test_openai.py tests/llm/test_gemini.py tests/llm/test_anthropic.py tests/llm/test_openrouter_provider.py
git diff --cached --check
git commit -m "feat(import): define matching extraction schema"
```

---

### Task 4: Extract one seven-row group and carry answer-key citations

**Files:**
- Modify: `tests/study_generation/test_practice_extraction.py`
- Modify: `tests/study_generation/test_practice_review.py`
- Modify: `tests/study_generation/test_quiz_import_worker.py`
- Modify: `tests/v2/test_quiz_builder_acceptance.py`
- Modify: `src/oms_hub/study_generation/practice_extraction.py:13-25,34-42,94-242,281-407`
- Modify: `src/oms_hub/study_generation/practice_review.py:140-155`
- Modify: `src/oms_hub/study_generation/quiz_import_worker.py:891-940`

**Interfaces:**
- Consumes: `ExtractedQuestionValue` and `ExtractedAnswerValue` from Task 3.
- Produces: `ExtractionResult.answer_source_refs: tuple[tuple[QuestionSourceRef, ...], ...]`, aligned one-to-one with `answers`, and a matching-aware extraction prompt/merge.

- [ ] **Step 1: Write the failing synthetic seven-by-seven extraction test**

Add a `matching_extraction_json()` helper and test to `test_practice_extraction.py`:

```python
def matching_extraction_json() -> str:
    labels = tuple("ABCDEFG")
    mapping = (5, 4, 1, 0, 2, 6, 3)
    return json.dumps({
        "questions": [{
            "kind": "matching",
            "original_identifier": "1",
            "stem": "Match each neutral description with its neutral term.",
            "prompts": [
                {"original_identifier": label, "text": f"{label}. Description {label}",
                 "supplied_correct_index": None}
                for label in labels
            ],
            "choices": [f"{number}. Term {number}" for number in range(1, 8)],
            "rationale": None,
            "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
            "candidate_assets": [],
            "confidence": 0.99,
        }],
        "answers": [{
            "kind": "matching",
            "original_identifier": "1",
            "matches": [
                {
                    "prompt_identifier": label,
                    "correct_index": correct_index,
                    "rationale": None,
                    "source_segments": [
                        {"source_id": "source-1", "segment_key": f"answer-{label.lower()}"}
                    ],
                }
                for label, correct_index in zip(labels, mapping, strict=True)
            ],
        }],
    })


def test_extractor_keeps_a_seven_row_matching_set_grouped_and_resolves_answer_refs(
    tmp_path: Path,
) -> None:
    segments = (
        ParsedSegment(
            "question-1", SegmentKind.PARAGRAPH, "1. Match A through G to 1 through 7",
            DocumentLocator("page 1", page_number=1),
        ),
        *(
            ParsedSegment(
                f"answer-{label.lower()}", SegmentKind.PARAGRAPH,
                f"{label} maps to the source choice", DocumentLocator("page 4", page_number=4),
            )
            for label in "ABCDEFG"
        ),
    )
    generator = StructuredGenerator([matching_extraction_json()])

    result = PracticeQuestionExtractor(generator).extract(
        (_document(tmp_path, segments=segments),)
    )

    assert len(result.questions) == len(result.answers) == 1
    assert len(result.questions[0].prompts) == len(result.questions[0].choices) == 7
    assert tuple(prompt.text for prompt in result.questions[0].prompts) == tuple(
        f"Description {label}" for label in "ABCDEFG"
    )
    assert result.questions[0].choices == tuple(f"Term {number}" for number in range(1, 8))
    assert tuple(ref.segment_key for ref in result.answer_source_refs[0]) == tuple(
        f"answer-{label.lower()}" for label in "ABCDEFG"
    )
    assert "one grouped matching question" in generator.requests[0].instruction
    assert "zero-based" in generator.requests[0].instruction
```

Add one merge test using two `ExtractedMatchingAnswer` records for group `1`, with disjoint A–C and D–G rows, and assert both answer records and both aligned citation tuples survive chunk merging. Add one matching duplicate-identity test and assert the exact run code is `conflicting-matching-question-identifier` rather than a legacy code.

- [ ] **Step 2: Run extraction tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_extraction.py -v
```

Expected: `ExtractionResult` has no `answer_source_refs`, matching questions reach MCQ-only style-answer code, or the new prompt assertions fail.

- [ ] **Step 3: Make extraction collections and citation resolution union-aware**

Change the result signature to:

```python
@dataclass(frozen=True, slots=True)
class ExtractionResult:
    questions: tuple[ExtractedQuestionValue, ...]
    answers: tuple[ExtractedAnswerValue, ...]
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...]
    answer_source_refs: tuple[tuple[QuestionSourceRef, ...], ...]
    provider_metadata: tuple[ExtractionProviderMetadata, ...]
    diagnostics: tuple[DraftDiagnostic, ...]
```

Replace `_EXTRACTION_INSTRUCTION` with text that retains the existing style guidance and appends these exact requirements:

```python
"""Extract supplied practice questions and answer-key entries.
Return only JSON matching the provided schema. Preserve source wording, cite every
question and answer with document-qualified source_segments, and cite only
document-qualified candidate_assets present in the input. Do not invent
questions, answers, references, or assets. Preserve a matching set as one grouped
matching question with its shared ordered choice bank; do not flatten its prompts
into independent questions. Store each source prompt label separately from prompt
text, remove leading source labels and choice ordinals, and use zero-based indexes
into the emitted choices array. A separately located matching key is one matching
answer record with one cited row per prompt. Treat source_style_metadata as evidence:
when a slide repeats the preceding multiple-choice question and exactly one option
is emphasized by color, highlighting, bold, underline, or italics, record it as
supplied_correct_index. Explicit answer-key labels remain stronger evidence;
ordinary emphasized medical terms and slides with multiple emphasized options are
not answers."""
```

Use one citation resolver for both unions:

```python
def _resolve_source_refs(
    citations: tuple[SegmentCitation, ...],
    documents: tuple[ParsedDocument, ...],
) -> tuple[QuestionSourceRef, ...]:
    documents_by_id = {document.source_id: document for document in documents}
    references: list[QuestionSourceRef] = []
    for citation in dict.fromkeys(citations):
        document = documents_by_id[citation.source_id]
        segment = next(item for item in document.segments if item.key == citation.segment_key)
        references.append(
            QuestionSourceRef(citation.source_id, citation.segment_key, segment.locator.label)
        )
    return tuple(references)


def _answer_citations(answer: ExtractedAnswerValue) -> tuple[SegmentCitation, ...]:
    if isinstance(answer, ExtractedMatchingAnswer):
        return tuple(
            dict.fromkeys(
                citation
                for match in answer.matches
                for citation in match.source_segments
            )
        )
    return answer.source_segments
```

Build aligned tuples immediately before returning:

```python
question_source_refs = tuple(
    _resolve_source_refs(question.source_segments, canonical_documents)
    for question in questions
)
answer_source_refs = tuple(
    _resolve_source_refs(_answer_citations(answer), canonical_documents)
    for answer in answers
)
return ExtractionResult(
    questions,
    tuple(answers),
    question_source_refs,
    answer_source_refs,
    tuple(metadata),
    tuple(diagnostics),
)
```

Guard `_apply_unique_styled_answers()` so `ExtractedMatchingQuestion` values pass through unchanged; update its input/return types to `tuple[ExtractedQuestionValue, ...]`.

- [ ] **Step 4: Emit matching-specific extraction identity diagnostics**

When the existing chunk merge detects a non-identical duplicate, identifier/source mismatch, or source-reference/identifier mismatch, choose these codes for `ExtractedMatchingQuestion`:

```python
duplicate_code = (
    "conflicting-matching-question-identifier"
    if isinstance(question, ExtractedMatchingQuestion)
    else "conflicting-duplicate-question"
)
identifier_code = (
    "conflicting-matching-question-identifier"
    if isinstance(question, ExtractedMatchingQuestion)
    else "conflicting-question-identifier"
)
source_code = (
    "conflicting-matching-question-source-reference"
    if isinstance(question, ExtractedMatchingQuestion)
    else "conflicting-question-source-reference"
)
```

Keep exact duplicate extraction values de-duplicated; do not create a second matching group from overlapping chunks.

- [ ] **Step 5: Update direct `ExtractionResult(...)` test fixtures, run, and commit**

Update every constructor reported by this command to insert `answer_source_refs` after `question_source_refs`:

```bash
rg -n "ExtractionResult\(" tests src
```

Use `answer_source_refs=()` when `answers=()`. When a fixture includes answers, supply one tuple per answer. Add `answer_source_refs` to `_extraction_json()`. In `_extraction_from_json()`, retain compatibility with pre-feature MCQ artifacts that are still read by awaiting-review runs:

```python
def _extracted_question_from_json(item: object) -> ExtractedQuestionValue:
    if isinstance(item, dict) and item.get("kind") == "matching":
        return ExtractedMatchingQuestion.model_validate(item)
    return ExtractedQuestion.model_validate(item)


def _extracted_answer_from_json(item: object) -> ExtractedAnswerValue:
    if isinstance(item, dict) and item.get("kind") == "matching":
        return ExtractedMatchingAnswer.model_validate(item)
    return ExtractedAnswer.model_validate(item)


answers = tuple(_extracted_answer_from_json(item) for item in payload["answers"])
stored_answer_refs = payload.get("answer_source_refs")
answer_source_refs = (
    tuple(
        tuple(QuestionSourceRef(**item) for item in refs)
        for refs in stored_answer_refs
    )
    if isinstance(stored_answer_refs, list)
    else tuple(() for _ in answers)
)
```

Pass `answers` and `answer_source_refs` into `ExtractionResult`. In `test_practice_review.py`, import `ExtractedAnswer` and define the legacy artifact fixture explicitly:

```python
def _legacy_extraction_result() -> ExtractionResult:
    question_ref = QuestionSourceRef("source-1", "question-1", "page 1")
    answer_ref = QuestionSourceRef("source-1", "answer-1", "page 4")
    return ExtractionResult(
        questions=(
            ExtractedQuestion(
                original_identifier="1",
                stem="Which term is correct?",
                choices=("Term one", "Term two"),
                source_segments=(
                    SegmentCitation(source_id="source-1", segment_key="question-1"),
                ),
                confidence=0.9,
            ),
        ),
        answers=(
            ExtractedAnswer(
                original_identifier="1",
                correct_index=1,
                rationale="The source key selects Term two.",
                source_segments=(
                    SegmentCitation(source_id="source-1", segment_key="answer-1"),
                ),
            ),
        ),
        question_source_refs=((question_ref,),),
        answer_source_refs=((answer_ref,),),
        provider_metadata=(),
        diagnostics=(),
    )
```

Then add this regression using the current service/repository fixtures:

```python
def test_awaiting_review_can_read_a_legacy_extract_artifact_without_answer_refs(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))
    legacy = json.loads(_extraction_json(_legacy_extraction_result()))
    legacy.pop("answer_source_refs", None)
    service.repository.save_run_artifact(
        "run-1", "extract", "a" * 64, json.dumps(legacy)
    )

    assert service.candidates_by_question("run-1", service.review("run-1")) == {
        "q1": ()
    }
```

Removing the serialized field is what exercises backward compatibility. New extraction always writes one aligned tuple per answer. Then run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_review.py tests/study_generation/test_quiz_import_worker.py tests/v2/test_quiz_builder_acceptance.py -v
.venv/bin/ruff check src/oms_hub/study_generation/practice_extraction.py src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/quiz_import_worker.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_review.py tests/study_generation/test_quiz_import_worker.py tests/v2/test_quiz_builder_acceptance.py
.venv/bin/mypy src/oms_hub/study_generation/practice_extraction.py src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/quiz_import_worker.py
```

Expected: all tests pass; one seven-row set remains one question and answer citations retain their page-4 locators.

```bash
git add src/oms_hub/study_generation/practice_extraction.py src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/quiz_import_worker.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_review.py tests/study_generation/test_quiz_import_worker.py tests/v2/test_quiz_builder_acceptance.py
git diff --cached --check
git commit -m "feat(import): extract grouped matching sets"
```

---

### Task 5: Pair matching keys into one review draft with deterministic diagnostics

**Files:**
- Modify: `tests/study_generation/test_practice_domain.py`
- Modify: `tests/study_generation/test_practice_matching.py`
- Modify: `tests/study_generation/test_practice_extraction.py`
- Modify: `tests/study_generation/test_practice_review.py`
- Modify: `src/oms_hub/study_generation/practice_domain.py`
- Modify: `src/oms_hub/study_generation/practice_matching.py`
- Modify: `src/oms_hub/study_generation/quiz_import_worker.py:348-405`

**Interfaces:**
- Consumes: matching extraction unions and aligned answer refs from Tasks 3–4.
- Produces: `MatchingPromptDraft`, `MatchingQuestionDraft`, `QuestionDraftValue`, `PairingResult`, `matching_summary()`, and `pair_supplied_answers(...) -> PairingResult`.

- [ ] **Step 1: Write failing pairing tests for the source-equivalent permutation**

In `test_practice_matching.py`, add `import pytest` and import `ExtractedMatchingAnswer`, `ExtractedMatchingAnswerRow`, `ExtractedMatchingPrompt`, `ExtractedMatchingQuestion`, `QuestionSourceRef`, and `PairingResult`. Add these fixtures, which split the later answer key into A–C and D–G records while keeping citations aligned one-to-one with answer records:

```python
def _seven_by_seven_matching_fixture() -> tuple[
    ExtractedMatchingQuestion,
    tuple[ExtractedMatchingAnswer, ...],
    tuple[QuestionSourceRef, ...],
    tuple[tuple[QuestionSourceRef, ...], ...],
]:
    labels = tuple("ABCDEFG")
    mapping = (5, 4, 1, 0, 2, 6, 3)
    question = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match each neutral description with its neutral term.",
        prompts=tuple(
            ExtractedMatchingPrompt(
                original_identifier=label,
                text=f"Description {label}",
                supplied_correct_index=None,
            )
            for label in labels
        ),
        choices=tuple(f"Term {number}" for number in range(1, 8)),
        rationale=None,
        source_segments=(
            SegmentCitation(source_id="questions", segment_key="question-1"),
        ),
        candidate_assets=(),
        confidence=0.99,
    )

    def answer_group(group_labels: tuple[str, ...]) -> ExtractedMatchingAnswer:
        rows = tuple(
            ExtractedMatchingAnswerRow(
                prompt_identifier=label,
                correct_index=mapping[labels.index(label)],
                rationale=None,
                source_segments=(
                    SegmentCitation(
                        source_id="answers",
                        segment_key=f"answer-{label.lower()}",
                    ),
                ),
            )
            for label in group_labels
        )
        return ExtractedMatchingAnswer(
            kind="matching", original_identifier="1", matches=rows
        )

    groups = (labels[:3], labels[3:])
    answers = tuple(answer_group(group) for group in groups)
    answer_refs = tuple(
        tuple(
            QuestionSourceRef(
                "answers", f"answer-{label.lower()}", f"page {page_number}"
            )
            for label in group
        )
        for page_number, group in enumerate(groups, start=4)
    )
    question_refs = (
        QuestionSourceRef("questions", "question-1", "page 1"),
    )
    return question, answers, question_refs, answer_refs


def _mutated_matching_pairing(mutation: str) -> PairingResult:
    question, answers, question_refs, answer_refs = _seven_by_seven_matching_fixture()
    questions = [question]
    question_ref_groups = [question_refs]
    answer_groups = list(answers)
    answer_ref_groups = list(answer_refs)

    if mutation == "missing":
        answer_groups[1] = answer_groups[1].model_copy(
            update={"matches": answer_groups[1].matches[:-1]}
        )
        answer_ref_groups[1] = answer_ref_groups[1][:-1]
    elif mutation == "duplicate_prompt":
        prompts = list(question.prompts)
        prompts[1] = prompts[1].model_copy(update={"original_identifier": "A"})
        questions[0] = question.model_copy(update={"prompts": tuple(prompts)})
    elif mutation == "conflict":
        prompts = list(question.prompts)
        prompts[0] = prompts[0].model_copy(update={"supplied_correct_index": 0})
        questions[0] = question.model_copy(update={"prompts": tuple(prompts)})
    elif mutation == "out_of_range":
        rows = list(answer_groups[0].matches)
        rows[0] = rows[0].model_copy(update={"correct_index": 7})
        answer_groups[0] = answer_groups[0].model_copy(update={"matches": tuple(rows)})
    elif mutation == "unknown_prompt":
        unknown = ExtractedMatchingAnswerRow(
            prompt_identifier="Z",
            correct_index=0,
            rationale=None,
            source_segments=(
                SegmentCitation(source_id="answers", segment_key="answer-z"),
            ),
        )
        answer_groups[1] = answer_groups[1].model_copy(
            update={"matches": (*answer_groups[1].matches, unknown)}
        )
        answer_ref_groups[1] = (
            *answer_ref_groups[1],
            QuestionSourceRef("answers", "answer-z", "page 5"),
        )
    elif mutation == "unmatched_group":
        unmatched = ExtractedMatchingAnswerRow(
            prompt_identifier="A",
            correct_index=0,
            rationale=None,
            source_segments=(
                SegmentCitation(source_id="answers", segment_key="answer-group-2"),
            ),
        )
        answer_groups.append(
            ExtractedMatchingAnswer(
                kind="matching", original_identifier="2", matches=(unmatched,)
            )
        )
        answer_ref_groups.append(
            (QuestionSourceRef("answers", "answer-group-2", "page 6"),)
        )
    elif mutation == "duplicate_group":
        questions.append(question.model_copy(update={"stem": "Second matching group"}))
        question_ref_groups.append(question_refs)
    else:
        raise AssertionError(f"unknown matching mutation: {mutation}")

    return pair_supplied_answers(
        tuple(questions),
        tuple(answer_groups),
        question_source_refs=tuple(question_ref_groups),
        answer_source_refs=tuple(answer_ref_groups),
    )
```

Assert:

```python
def test_matching_pairing_merges_later_key_rows_into_one_complete_group() -> None:
    question, answers, question_refs, answer_refs = _seven_by_seven_matching_fixture()

    result = pair_supplied_answers(
        (question,), answers,
        question_source_refs=(question_refs,),
        answer_source_refs=answer_refs,
    )

    assert result.diagnostics == ()
    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert isinstance(draft, MatchingQuestionDraft)
    assert tuple(prompt.id for prompt in draft.prompts) == tuple(f"p{i}" for i in range(1, 8))
    assert tuple(prompt.correct_index for prompt in draft.prompts) == (5, 4, 1, 0, 2, 6, 3)
    assert draft.rationale == (
        "Source-marked matches: A -> Term 6; B -> Term 5; C -> Term 2; "
        "D -> Term 1; E -> Term 3; F -> Term 7; G -> Term 4."
    )
    assert draft.source_refs == tuple(dict.fromkeys((question_refs[0], *answer_refs[0], *answer_refs[1])))
    assert draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE
```

Add exact cases and expected diagnostic ownership:

```python
@pytest.mark.parametrize(
    ("mutation", "code", "run_level"),
    [
        ("missing", "missing-supplied-matching-answer", False),
        ("duplicate_prompt", "duplicate-matching-prompt-identifier", False),
        ("conflict", "conflicting-supplied-matching-answer", False),
        ("out_of_range", "supplied-matching-answer-out-of-bounds", False),
        ("unknown_prompt", "unknown-matching-prompt-answer", True),
        ("unmatched_group", "unmatched-matching-answer-group", True),
        ("duplicate_group", "duplicate-matching-question-identifier", True),
    ],
)
def test_matching_pairing_fails_closed_with_stable_diagnostic_codes(
    mutation: str, code: str, run_level: bool
) -> None:
    result = _mutated_matching_pairing(mutation)
    owned_codes = {
        diagnostic.code
        for draft in result.drafts
        for diagnostic in draft.diagnostics
    }
    run_codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert code in (run_codes if run_level else owned_codes)
    assert code not in (owned_codes if run_level else run_codes)
```

Also retain one legacy MCQ test asserting its draft values and diagnostic codes are unchanged after accessing `result.drafts`.

- [ ] **Step 2: Run pairing tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_domain.py tests/study_generation/test_practice_matching.py -v
```

Expected: matching draft/result classes are absent and the current function accepts only MCQ values.

- [ ] **Step 3: Add explicit matching draft types**

In `practice_domain.py`, leave `QuestionDraft` unchanged and add:

```python
@dataclass(frozen=True, slots=True)
class MatchingPromptDraft:
    id: str
    label: str
    text: str
    correct_index: int | None


@dataclass(frozen=True, slots=True)
class MatchingQuestionDraft:
    question_id: str
    original_identifier: str | None
    stem: str
    prompts: tuple[MatchingPromptDraft, ...]
    choices: tuple[str, ...]
    rationale: str | None
    image_ref: QuizImageRef | None
    source_refs: tuple[QuestionSourceRef, ...]
    answer_provenance: AnswerProvenance | None
    extraction_confidence: float
    diagnostics: tuple[DraftDiagnostic, ...]
    verification_required: bool
    verified_at: str | None

    @property
    def blocking_diagnostics(self) -> tuple[str, ...]:
        return tuple(
            item.message
            for item in self.diagnostics
            if item.severity is DiagnosticSeverity.BLOCKER
        )


QuestionDraftValue = QuestionDraft | MatchingQuestionDraft
```

- [ ] **Step 4: Preserve the MCQ algorithm and dispatch matching values separately**

Rename the current implementation without changing its body or keyword behavior; only its name changes and its signature remains:

```python
def _pair_multiple_choice_answers(
    questions: tuple[ExtractedQuestion, ...],
    answers: tuple[ExtractedAnswer, ...],
    *,
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...] | None = None,
) -> tuple[QuestionDraft, ...]:
```

Add:

```python
@dataclass(frozen=True, slots=True)
class PairingResult:
    drafts: tuple[QuestionDraftValue, ...]
    diagnostics: tuple[DraftDiagnostic, ...]


def pair_supplied_answers(
    questions: tuple[ExtractedQuestionValue, ...],
    answers: tuple[ExtractedAnswerValue, ...],
    *,
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...] | None = None,
    answer_source_refs: tuple[tuple[QuestionSourceRef, ...], ...] | None = None,
) -> PairingResult:
    if question_source_refs is not None and len(question_source_refs) != len(questions):
        raise ValueError("question_source_refs must align with questions")
    if answer_source_refs is not None and len(answer_source_refs) != len(answers):
        raise ValueError("answer_source_refs must align with answers")
    question_refs = question_source_refs or tuple(() for _ in questions)
    answer_refs = answer_source_refs or tuple(() for _ in answers)
    mcq_positions = tuple(i for i, item in enumerate(questions) if isinstance(item, ExtractedQuestion))
    matching_positions = tuple(
        i for i, item in enumerate(questions) if isinstance(item, ExtractedMatchingQuestion)
    )
    mcq_drafts = _pair_multiple_choice_answers(
        tuple(cast(ExtractedQuestion, questions[i]) for i in mcq_positions),
        tuple(item for item in answers if isinstance(item, ExtractedAnswer)),
        question_source_refs=tuple(question_refs[i] for i in mcq_positions),
    )
    matching = _pair_matching_answers(
        tuple(cast(ExtractedMatchingQuestion, questions[i]) for i in matching_positions),
        tuple(item for item in answers if isinstance(item, ExtractedMatchingAnswer)),
        tuple(question_refs[i] for i in matching_positions),
        tuple(
            refs
            for item, refs in zip(answers, answer_refs, strict=True)
            if isinstance(item, ExtractedMatchingAnswer)
        ),
    )
    by_position: dict[int, QuestionDraftValue] = dict(zip(mcq_positions, mcq_drafts, strict=True))
    by_position.update(dict(zip(matching_positions, matching.drafts, strict=True)))
    return PairingResult(
        tuple(by_position[index] for index in range(len(questions))), matching.diagnostics
    )
```

Import `cast` and `dataclass`. Update legacy call sites/tests to use `.drafts`.

At this task boundary, make the worker's existing `_pair()` call pass both aligned ref collections and return only the drafts while Task 6 adds run-diagnostic persistence:

```python
return pair_supplied_answers(
    extracted.questions,
    extracted.answers,
    question_source_refs=extracted.question_source_refs,
    answer_source_refs=extracted.answer_source_refs,
).drafts
```

Widen the stable group-ID helper before calling it with a matching extraction value:

```python
def _question_id(
    index: int,
    question: ExtractedQuestion | ExtractedMatchingQuestion,
    identifier: str | None,
) -> str:
```

- [ ] **Step 5: Implement deterministic matching-group resolution**

Add the helper with this exact signature:

```python
def _pair_matching_answers(
    questions: tuple[ExtractedMatchingQuestion, ...],
    answers: tuple[ExtractedMatchingAnswer, ...],
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...],
    answer_source_refs: tuple[tuple[QuestionSourceRef, ...], ...],
) -> PairingResult:
```

Its body must use these exact operations rather than source order guessing:

```python
answer_groups: dict[str, list[tuple[ExtractedMatchingAnswer, tuple[QuestionSourceRef, ...]]]] = defaultdict(list)
for answer, refs in zip(answers, answer_source_refs, strict=True):
    answer_groups[normalize_identifier(answer.original_identifier) or ""].append((answer, refs))

question_ids = tuple(normalize_identifier(item.original_identifier) for item in questions)
question_counts = Counter(item for item in question_ids if item is not None)
used_groups: set[str] = set()
run_diagnostics: list[DraftDiagnostic] = []
drafts: list[MatchingQuestionDraft] = []
for question_index, (question, group_id, source_refs) in enumerate(
    zip(questions, question_ids, question_source_refs, strict=True)
):
    owned: list[DraftDiagnostic] = []
    if group_id is not None and question_counts[group_id] > 1:
        run_diagnostics.append(
            _blocker("duplicate-matching-question-identifier", "duplicate matching question identifier")
        )
    grouped_answers = answer_groups.get(group_id or "", []) if group_id is not None else []
    if grouped_answers:
        used_groups.add(group_id or "")
    rows_with_refs: list[
        tuple[ExtractedMatchingAnswerRow, tuple[QuestionSourceRef, ...]]
    ] = []
    for answer, refs in grouped_answers:
        refs_by_key = {(ref.source_id, ref.segment_key): ref for ref in refs}
        for row in answer.matches:
            rows_with_refs.append((
                row,
                tuple(
                    refs_by_key[(citation.source_id, citation.segment_key)]
                    for citation in row.source_segments
                    if (citation.source_id, citation.segment_key) in refs_by_key
                ),
            ))
    rows = tuple(row for row, _ in rows_with_refs)
    prompt_counts = Counter(normalize_identifier(row.prompt_identifier) for row in rows)
    prompt_label_counts = Counter(
        normalize_identifier(prompt.original_identifier) for prompt in question.prompts
    )
    if any(count > 1 for count in prompt_label_counts.values()):
        owned.append(
            _blocker(
                "duplicate-matching-prompt-identifier",
                "duplicate matching prompt identifier",
            )
        )
    known_labels = set(prompt_label_counts)
    for row_label in set(prompt_counts) - known_labels:
        run_diagnostics.append(
            _blocker(
                "unknown-matching-prompt-answer",
                f"matching answer references unknown prompt: {row_label or 'without an identifier'}",
            )
        )
```

For each source prompt in order, collect its inline index and every row with the same normalized label. Apply exactly these branches:

```python
indexes = {
    index
    for index in (
        prompt.supplied_correct_index,
        *(row.correct_index for row in rows if normalize_identifier(row.prompt_identifier) == label),
    )
    if index is not None
}
if len(indexes) > 1 or prompt_counts[label] > 1 and len(indexes) != 1:
    owned.append(_blocker("conflicting-supplied-matching-answer", f"{prompt.original_identifier}: conflicting supplied matching answer"))
    correct_index = None
elif not indexes:
    owned.append(_blocker("missing-supplied-matching-answer", f"{prompt.original_identifier}: supplied matching answer is missing"))
    correct_index = None
else:
    correct_index = indexes.pop()
    if correct_index >= len(question.choices):
        owned.append(_blocker("supplied-matching-answer-out-of-bounds", f"{prompt.original_identifier}: supplied matching answer is outside the available choices"))
        correct_index = None
```

Track `accepted_answer_refs` and `accepted_rationales` only after a prompt has one in-range, non-conflicting index. For each accepted row whose normalized identifier equals the current prompt label and whose index equals `correct_index`, append that row's resolved refs and append `f"{prompt.original_identifier}: {row.rationale.strip()}"` when its rationale is non-empty. Unknown, conflicting, and out-of-range rows produce diagnostics but contribute neither provenance nor rationale. Add a test containing one valid row and one unknown/conflicting row; assert only the valid row's segment key appears in `draft.source_refs`.

Create prompt IDs as `p1` through `pN`, keep group question IDs through `_question_id()`, and build group provenance/rationale as:

```python
group_source_refs = tuple(dict.fromkeys((*source_refs, *accepted_answer_refs)))
group_rationale = (
    "; ".join(accepted_rationales)
    if accepted_rationales
    else matching_summary(tuple(prompt_drafts), question.choices)
)
```

The fallback summary is:

```python
def matching_summary(
    prompts: tuple[MatchingPromptDraft, ...], choices: tuple[str, ...]
) -> str | None:
    resolved = tuple(
        f"{prompt.label} -> {choices[prompt.correct_index]}"
        for prompt in prompts
        if prompt.correct_index is not None and 0 <= prompt.correct_index < len(choices)
    )
    return f"Source-marked matches: {'; '.join(resolved)}." if resolved else None
```

After all questions, emit `unmatched-matching-answer-group` for every normalized answer group not in `used_groups`. Set provenance to `PROVIDED_BY_SOURCE` only when every prompt is mapped without a question-owned blocker; matching drafts never set AI verification.

- [ ] **Step 6: Run pairing tests and commit**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_domain.py tests/study_generation/test_practice_matching.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_review.py tests/study_generation/test_quiz_import_worker.py -v
.venv/bin/ruff check src/oms_hub/study_generation/practice_domain.py src/oms_hub/study_generation/practice_matching.py src/oms_hub/study_generation/quiz_import_worker.py tests/study_generation/test_practice_domain.py tests/study_generation/test_practice_matching.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_review.py
.venv/bin/mypy src/oms_hub/study_generation/practice_domain.py src/oms_hub/study_generation/practice_matching.py src/oms_hub/study_generation/quiz_import_worker.py
```

Expected: source-equivalent `(5,4,1,0,2,6,3)` pairing passes, choice reuse remains valid, and every ambiguous case has the specified ownership/code.

```bash
git add src/oms_hub/study_generation/practice_domain.py src/oms_hub/study_generation/practice_matching.py src/oms_hub/study_generation/quiz_import_worker.py tests/study_generation/test_practice_domain.py tests/study_generation/test_practice_matching.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_review.py
git diff --cached --check
git commit -m "feat(import): pair matching answer keys"
```

---

### Task 6: Persist matching artifacts, invalidate stale signatures, and isolate answer generation

**Files:**
- Modify: `tests/study_generation/test_quiz_import_worker.py`
- Modify: `src/oms_hub/study_generation/quiz_import_worker.py:59-72,141-173,289-473,661-686,786-999`
- Modify: `src/oms_hub/study_generation/studio_repository.py:35-42,1295-1337`

**Interfaces:**
- Consumes: `PairingResult` and `QuestionDraftValue` from Task 5.
- Produces: restart-safe matching extraction/draft artifact JSON, combined run diagnostics, bumped stage signatures, and MCQ-only `AnswerResolver.resolve()` calls.

- [ ] **Step 1: Write failing artifact and orchestration tests**

Extend `test_artifact_serializers_round_trip_full_provenance` with one `ExtractedMatchingQuestion`, one `ExtractedMatchingAnswer`, aligned answer refs, and one `MatchingQuestionDraft`; assert both `_extraction_from_json(_extraction_json(...))` and `_drafts_from_json(_drafts_json(...))` are exact.

In `test_quiz_import_worker.py`, import `ExtractedMatchingAnswer`, `ExtractedMatchingAnswerRow`, `ExtractedMatchingPrompt`, and `ExtractedMatchingQuestion` from `practice_contracts`; `MatchingPromptDraft` and `MatchingQuestionDraft` from `practice_domain`; `SourceDocument` from `practice_extraction`; and `StudioRun` from `studio_domain`. Then add these exact fakes and builders:

```python
class _StaticExtractor:
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result

    def extract(self, documents: tuple[SourceDocument, ...]) -> ExtractionResult:
        del documents
        return self.result


class _RecordingAnswers:
    def __init__(self) -> None:
        self.calls: list[tuple[QuestionDraft, object]] = []

    def resolve(self, draft: QuestionDraft, scope: object) -> QuestionDraft:
        self.calls.append((draft, scope))
        return replace(
            draft,
            correct_index=0,
            rationale="The source supports the first choice.",
            answer_provenance=AnswerProvenance.NOTEBOOKLM,
            verification_required=False,
        )


def _matching_worker_fixture(
    tmp_path: Path,
    *,
    complete: bool,
    content_kind: QuizContentKind = QuizContentKind.PRACTICE_QUESTIONS,
) -> tuple[QuizImportWorker, StudioRepository, StudioRun, _RecordingAnswers]:
    repository = _repository(tmp_path)
    source = _ready_source(repository, tmp_path, "Matching questions")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Imported matching practice",
        "Neuro",
        1,
        content_kind,
        (ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),),
    )
    citation = SegmentCitation(source_id=source.id, segment_key="block-1")
    source_ref = QuestionSourceRef(source.id, "block-1", "block 1")
    matching = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match each description with its term.",
        prompts=(
            ExtractedMatchingPrompt(
                original_identifier="A",
                text="Alpha description",
                supplied_correct_index=1,
            ),
            ExtractedMatchingPrompt(
                original_identifier="B",
                text="Beta description",
                supplied_correct_index=0 if complete else None,
            ),
        ),
        choices=("Term one", "Term two"),
        rationale=None,
        source_segments=(citation,),
        candidate_assets=(),
        confidence=0.99,
    )
    extraction = ExtractionResult(
        questions=(matching,),
        answers=(),
        question_source_refs=((source_ref,),),
        answer_source_refs=(),
        provider_metadata=(),
        diagnostics=(),
    )
    answers = _RecordingAnswers()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _StaticExtractor(extraction),
        answers,
        object(),
        tmp_path / "assets",
    )
    return worker, repository, run, answers


def _mixed_worker_fixture(
    tmp_path: Path,
) -> tuple[QuizImportWorker, StudioRepository, StudioRun, _RecordingAnswers]:
    repository = _repository(tmp_path)
    source = _ready_source(repository, tmp_path, "Mixed questions")
    supporting = _ready_source(repository, tmp_path, "Mixed supporting reference")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Imported mixed practice",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(source.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(
                supporting.id,
                ImportSourceRole.SUPPORTING_REFERENCE,
                attach_to_notebook=True,
            ),
        ),
    )
    citation = SegmentCitation(source_id=source.id, segment_key="block-1")
    source_ref = QuestionSourceRef(source.id, "block-1", "block 1")
    matching = ExtractedMatchingQuestion(
        kind="matching",
        original_identifier="1",
        stem="Match each description with its term.",
        prompts=(
            ExtractedMatchingPrompt(
                original_identifier="A",
                text="Alpha description",
                supplied_correct_index=1,
            ),
            ExtractedMatchingPrompt(
                original_identifier="B",
                text="Beta description",
                supplied_correct_index=0,
            ),
        ),
        choices=("Term one", "Term two"),
        rationale=None,
        source_segments=(citation,),
        candidate_assets=(),
        confidence=0.99,
    )
    mcq = ExtractedQuestion(
        original_identifier="2",
        stem="Which option is correct?",
        choices=("Yes", "No"),
        supplied_correct_index=None,
        rationale=None,
        source_segments=(citation,),
        candidate_assets=(),
        confidence=0.9,
    )
    extraction = ExtractionResult(
        questions=(matching, mcq),
        answers=(),
        question_source_refs=((source_ref,), (source_ref,)),
        answer_source_refs=(),
        provider_metadata=(),
        diagnostics=(),
    )
    answers = _RecordingAnswers()
    worker = QuizImportWorker(
        repository,
        _Parser(),
        _StaticExtractor(extraction),
        answers,
        _AttachingNotebook(),
        tmp_path / "assets",
    )
    return worker, repository, run, answers
```

Add these worker tests:

```python
def test_incomplete_matching_group_stops_before_any_answer_provider_call(tmp_path: Path) -> None:
    worker, repository, run, answers = _matching_worker_fixture(tmp_path, complete=False)

    worker.run(repository.claim_next_run())

    assert answers.calls == []
    assert repository.get_run(run.id).state is StudioRunState.AWAITING_REVIEW
    assert "missing-supplied-matching-answer" in repository.run_artifact(
        run.id, "normalized"
    ).payload_json


def test_mixed_import_resolves_only_the_unanswered_mcq(tmp_path: Path) -> None:
    worker, repository, run, answers = _mixed_worker_fixture(tmp_path)

    worker.run(repository.claim_next_run())

    assert [
        (draft.original_identifier, draft.stem) for draft, _scope in answers.calls
    ] == [("2", "Which option is correct?")]
    drafts = _drafts_from_json(repository.run_artifact(run.id, "normalized").payload_json)
    matching = next(item for item in drafts if isinstance(item, MatchingQuestionDraft))
    assert tuple(prompt.correct_index for prompt in matching.prompts) == (1, 0)


def test_non_practice_direct_import_rejects_matching_before_review(
    tmp_path: Path,
) -> None:
    worker, repository, run, answers = _matching_worker_fixture(
        tmp_path,
        complete=True,
        content_kind=QuizContentKind.EXAM_REVIEW,
    )

    worker.run(repository.claim_next_run())

    rejected = repository.get_run(run.id)
    assert answers.calls == []
    assert rejected.state is StudioRunState.FAILED
    assert rejected.error == "matching questions require practice-question content"
```

Add a signature assertion that changing any of the four exact version strings changes the relevant signature and invalidates its downstream cached artifact.

- [ ] **Step 2: Run worker tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_quiz_import_worker.py -v
```

Expected: union serialization is absent, `pair_supplied_answers` return handling is wrong, or matching drafts reach the scalar resolver.

- [ ] **Step 3: Serialize the additive draft artifact variant explicitly**

Retain Task 4's extraction union dispatch and legacy `answer_source_refs` fallback unchanged. In `_drafts_json()`, branch matching drafts to emit:

```python
{
    "kind": "matching",
    "question_id": draft.question_id,
    "original_identifier": draft.original_identifier,
    "stem": draft.stem,
    "prompts": [asdict(prompt) for prompt in draft.prompts],
    "choices": list(draft.choices),
    "rationale": draft.rationale,
    "image_ref": asdict(draft.image_ref) if draft.image_ref else None,
    "source_refs": [asdict(item) for item in draft.source_refs],
    "answer_provenance": draft.answer_provenance.value if draft.answer_provenance else None,
    "extraction_confidence": draft.extraction_confidence,
    "diagnostics": [
        {**asdict(item), "severity": item.severity.value} for item in draft.diagnostics
    ],
    "verification_required": draft.verification_required,
    "verified_at": draft.verified_at,
}
```

In `_drafts_from_json()`, use missing `kind` for the current `QuestionDraft` constructor and `kind == "matching"` for `MatchingQuestionDraft` with `MatchingPromptDraft(**prompt)`.

- [ ] **Step 4: Use pairing diagnostics and exact new signatures**

Change `QuizImportWorker.__init__` default to `extraction_prompt_version="practice-extraction-v4"`. In `_pair()`:

```python
prompt_version="supplied-answer-pairing-v4"

paired = pair_supplied_answers(
    extracted.questions,
    extracted.answers,
    question_source_refs=extracted.question_source_refs,
    answer_source_refs=extracted.answer_source_refs,
)
run_diagnostics = (*extracted.diagnostics, *paired.diagnostics)
drafts = paired.drafts
```

Serialize `run_diagnostics` into `review:run-diagnostics`. Its `overridable` set is exactly:

```python
{
    "conflicting-duplicate-question",
    "conflicting-question-identifier",
    "conflicting-question-source-reference",
    "incomplete-sequential-question-extraction",
    "unmatched-matching-answer-group",
    "unknown-matching-prompt-answer",
    "duplicate-matching-question-identifier",
    "conflicting-matching-question-identifier",
    "conflicting-matching-question-source-reference",
}
```

Set the answer and review signature literals to `practice-answer-resolution-v2` and `question-draft-review-v2`.

- [ ] **Step 5: Keep matching drafts out of scalar answer generation**

Use explicit type checks:

```python
def _requires_review_before_resolution(draft: QuestionDraftValue) -> bool:
    if isinstance(draft, MatchingQuestionDraft):
        return any(prompt.correct_index is None for prompt in draft.prompts) or bool(
            draft.blocking_diagnostics
        )
    return any(
        item.severity is DiagnosticSeverity.BLOCKER
        and item.code not in {"missing-supplied-answer", "unmatched-question"}
        for item in draft.diagnostics
    )
```

In `_resolve_answers()`, derive only unresolved MCQs and pass matching drafts through:

```python
missing = tuple(
    draft
    for draft in drafts
    if isinstance(draft, QuestionDraft) and draft.correct_index is None
)
if not missing:
    return drafts

resolved_by_id = {
    draft.question_id: _without_resolved_missing_answer_diagnostics(
        self.answers.resolve(draft, scope), was_missing=True
    )
    for draft in missing
}
resolved = tuple(resolved_by_id.get(draft.question_id, draft) for draft in drafts)
```

Apply the same guard in the no-support branch so it appends `notebook-support-not-selected` only when `isinstance(draft, QuestionDraft) and draft.correct_index is None`; a complete matching draft is returned unchanged.

At the start of `_review()`, enforce the direct-import content boundary before saving the normalized/review artifact:

```python
if (
    any(isinstance(draft, MatchingQuestionDraft) for draft in drafts)
    and run.content_kind is not QuizContentKind.PRACTICE_QUESTIONS
):
    raise ValueError("matching questions require practice-question content")
```

Import `QuizContentKind` from `practice_domain`. Keep the repository publication checks in Task 7 as defense in depth for reconstructed or management-submitted payloads.

Update `QuestionExtractor`, `_pair`, `_resolve_answers`, `_review`, `_drafts_json`, `_drafts_from_json`, and `StudioRepository.await_import_review/save_question_reviews` annotations from `QuestionDraft` to `QuestionDraftValue`. The SQL review rows already store only common group-level fields, so do not add a migration.

- [ ] **Step 6: Run worker/artifact tests and commit**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_quiz_import_worker.py tests/study_generation/test_practice_answers.py -v
.venv/bin/ruff check src/oms_hub/study_generation/quiz_import_worker.py src/oms_hub/study_generation/studio_repository.py tests/study_generation/test_quiz_import_worker.py
.venv/bin/mypy src/oms_hub/study_generation/quiz_import_worker.py src/oms_hub/study_generation/studio_repository.py
```

Expected: artifact round-trips are exact, stale stage versions invalidate, incomplete matching makes zero answer calls, mixed imports resolve only the MCQ, and a non-practice matching import fails before review persistence.

```bash
git add src/oms_hub/study_generation/quiz_import_worker.py src/oms_hub/study_generation/studio_repository.py tests/study_generation/test_quiz_import_worker.py
git diff --cached --check
git commit -m "feat(import): persist matching review artifacts"
```

---

### Task 7: Make server review, publication, preview, grading, and accuracy matching-aware

**Files:**
- Modify: `tests/study_generation/test_practice_review.py`
- Modify: `tests/study_generation/test_repository.py`
- Modify: `tests/llm/test_medical_accuracy_gate.py`
- Modify: `tests/v2/test_quiz_builder_routes.py`
- Modify: `tests/v2/test_public_quiz_routes.py`
- Modify: `src/oms_hub/study_generation/practice_review.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/llm/openrouter.py:379-458`
- Modify: `src/oms_hub/web/studio_routes.py:1-95,317-414,485-504,763-999`
- Modify: `src/oms_hub/web/templates/studio_quiz_preview.html:31-38`
- Modify: `src/oms_hub/web/published_quiz_routes.py:1-20,216-269`
- Modify: `src/oms_hub/web/public_quiz_routes.py:1-15,65-68,408-509`

**Interfaces:**
- Consumes: `MatchingQuestionDraft`, `QuizMatchingQuestion`, `grade_matching_answer()`, and matching artifact reconstruction.
- Produces: atomic whole-group review edits, matching-only practice publication, preview fingerprints, additive matching answer APIs, and one-group medical accuracy serialization.

- [ ] **Step 1: Write failing review-service tests for atomic edits and diagnostic lifecycle**

In `test_practice_review.py`, import `MatchingPromptDraft` and `MatchingQuestionDraft` from `practice_domain`, plus `ExtractedMatchingPrompt` and `ExtractedMatchingQuestion` from `practice_contracts`, then add this fixture beside the current `_draft()`:

```python
def _matching_draft(question_id: str = "matching-1") -> MatchingQuestionDraft:
    return MatchingQuestionDraft(
        question_id=question_id,
        original_identifier="1",
        stem="Match each description with its term.",
        prompts=(
            MatchingPromptDraft("p1", "A", "Alpha", 1),
            MatchingPromptDraft("p2", "B", "Beta", 0),
        ),
        choices=("Term one", "Term two"),
        rationale="Source-marked matches: A -> Term two; B -> Term one.",
        image_ref=None,
        source_refs=(QuestionSourceRef("source-1", "question-1", "page 1"),),
        answer_provenance=AnswerProvenance.PROVIDED_BY_SOURCE,
        extraction_confidence=0.99,
        diagnostics=(),
        verification_required=False,
        verified_at=None,
    )
```

Add:

```python
def test_matching_edit_is_atomic_and_regenerates_a_prefixed_summary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_matching_draft(),))

    updated = service.update_question("run-1", "matching-1", {
        "kind": "matching",
        "stem": "Updated group stem",
        "prompts": [
            {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 0},
            {"id": "p2", "label": "B", "text": "Beta", "correct_index": 1},
        ],
        "choices": ["Renamed one", "Renamed two"],
        "rationale": "Source-marked matches: stale",
    })

    assert isinstance(updated.draft, MatchingQuestionDraft)
    assert updated.draft.rationale == (
        "Source-marked matches: A -> Renamed one; B -> Renamed two."
    )
    before = service.question("run-1", "matching-1")
    with pytest.raises(ValueError, match="prompt IDs"):
        service.update_question("run-1", "matching-1", {
            "kind": "matching", "stem": "Rejected",
            "prompts": [{"id": "p9", "label": "A", "text": "Alpha", "correct_index": 0}],
            "choices": ["One", "Two"], "rationale": "Custom",
        })
    assert service.question("run-1", "matching-1") == before


def test_matching_custom_rationale_survives_mapping_edit_and_complete_edit_clears_only_owned_codes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    draft = replace(
        _matching_draft(),
        rationale="Reviewer-authored explanation.",
        diagnostics=(
            DraftDiagnostic("missing-supplied-matching-answer", "missing", DiagnosticSeverity.BLOCKER),
            DraftDiagnostic("source-warning", "uncertain text", DiagnosticSeverity.WARNING),
        ),
    )
    service.store("run-1", (draft,))

    updated = service.update_question("run-1", "matching-1", {
        "kind": "matching", "stem": draft.stem,
        "prompts": [
            {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 0},
            {"id": "p2", "label": "B", "text": "Beta", "correct_index": 1},
        ],
        "choices": list(draft.choices), "rationale": draft.rationale,
    })

    assert updated.draft.rationale == "Reviewer-authored explanation."
    assert updated.draft.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert tuple(item.code for item in updated.draft.diagnostics) == ("source-warning",)
```

Add a parameterized companion test that starts from a prefixed synthesized rationale and separately changes each mapping-dependent input:

```python
@pytest.mark.parametrize("change", ["prompt_label", "choice_text", "choice_order", "mapping"])
def test_matching_synthesized_rationale_regenerates_for_every_mapping_edit(
    tmp_path: Path, change: str
) -> None:
    service = _service(tmp_path)
    draft = _matching_draft()
    service.store("run-1", (draft,))
    prompts = [
        {"id": item.id, "label": item.label, "text": item.text,
         "correct_index": item.correct_index}
        for item in draft.prompts
    ]
    choices = list(draft.choices)
    if change == "prompt_label":
        prompts[0]["label"] = "Alpha"
    elif change == "choice_text":
        choices[0] = "Renamed term"
    elif change == "choice_order":
        choices.reverse()
    else:
        prompts[0]["correct_index"] = 0

    updated = service.update_question("run-1", "matching-1", {
        "kind": "matching", "stem": draft.stem, "prompts": prompts,
        "choices": choices, "rationale": draft.rationale,
    })

    assert updated.draft.rationale == matching_summary(
        updated.draft.prompts, updated.draft.choices
    )
    assert updated.draft.rationale != draft.rationale
```

Add a run-diagnostic test that stores `unknown-matching-prompt-answer`, asserts acknowledgement fails while one prompt is `None`, completes the mapping, acknowledges successfully, and confirms a `conflicting-matching-question-identifier` record is not removed by the question edit.

Add an image-identity regression whose matching draft has both question and answer refs while extraction has only the question ref:

```python
def test_matching_answer_refs_do_not_hide_group_image_candidates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    question_ref = QuestionSourceRef("source-1", "question-1", "page 1")
    answer_ref = QuestionSourceRef("source-1", "answer-1", "page 4")
    extracted = ExtractedMatchingQuestion(
        kind="matching", original_identifier="1", stem="Match them",
        prompts=(
            ExtractedMatchingPrompt(original_identifier="A", text="Alpha"),
            ExtractedMatchingPrompt(original_identifier="B", text="Beta"),
        ),
        choices=("One", "Two"),
        source_segments=(SegmentCitation(source_id="source-1", segment_key="question-1"),),
        candidate_assets=(AssetCitation(source_id="source-1", asset_key="asset-1"),),
        confidence=1.0,
    )
    extraction = ExtractionResult(
        (extracted,), (), ((question_ref,),), (), (), ()
    )
    draft = replace(_matching_draft(), source_refs=(question_ref, answer_ref))

    assert service._candidate_asset_keys("run-1", draft, extraction) == frozenset({
        ("source-1", "asset-1")
    })
```

Also call the verification route for that matching draft and assert status 409 with `matching answers do not require generated-answer verification`, not 500.

- [ ] **Step 2: Write failing route, publication-boundary, preview-version, and accuracy tests**

In `test_quiz_builder_routes.py`, create a direct practice review containing one `MatchingQuestionDraft`. Assert review data contains `kind`, prompt IDs, and nullable indexes; a PATCH with a missing/unknown prompt is 422; the previous review is unchanged; a valid full PATCH is 200; preview content contains no mapping; two different canonical review payloads return different `preview:<64 hex>` versions; the preview page's `data-quiz-version` equals the current content fingerprint; and preview matching answer returns `kind`, `correct_matches`, and `row_results`.

In `test_public_quiz_routes.py`, publish a matching practice quiz and assert:

```python
content = client.get(f"/public/quizzes/{token}/content")
assert content.json()["questions"][0]["kind"] == "matching"
assert "correct_index" not in content.text
assert "correct_matches" not in content.text
assert client.post(f"/public/quizzes/{token}/answer", json={
    "kind": "matching", "question_id": "q1", "matches": {"p1": "c2", "p2": "c1"},
}).json() == {
    "kind": "matching", "correct": True,
    "correct_matches": {"p1": "c2", "p2": "c1"},
    "row_results": {"p1": True, "p2": True},
    "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
}
```

Assert unknown question returns 404, while scalar-for-matching, matching-for-MCQ, missing/extra/unknown prompt, and unknown choice return 422. Exercise the route through the existing TestClient origin/CSRF setup so current protection remains active.

In `test_repository.py`, assert matching publishes for `PRACTICE_QUESTIONS` and is rejected without mutation for `LECTURE_QUIZ`, `EXAM_REVIEW`, `publish_quiz()`, and non-practice `replace_published_quiz_payload()`. In `test_public_quiz_routes.py`, assert `create_quiz_edit_run()` reconstructs matching prompts and mappings for a published practice quiz.

In `test_medical_accuracy_gate.py`, add this field immediately after the current `calls` field:

```python
inputs: list[str] = field(default_factory=list)
```

In the existing `generate_text()` method, append the actual prompt immediately after the existing `self.calls.append((model, api_key))` line:

```python
self.inputs.append(input_text)
```

Import `QuizMatchingPrompt` and `QuizMatchingQuestion`, then add the concrete quiz and gate invocation:

```python
def _matching_quiz() -> NativeQuiz:
    return NativeQuiz(
        title="Matching quiz",
        questions=(
            QuizMatchingQuestion(
                id="q1",
                stem="Match each description with its term.",
                prompts=(
                    QuizMatchingPrompt("p1", "A", "Description alpha", "c2"),
                    QuizMatchingPrompt("p2", "B", "Description beta", "c1"),
                ),
                choices=(
                    QuizChoice("c1", "Term one"),
                    QuizChoice("c2", "Term two"),
                ),
                rationale="Source-marked matches: A -> Term two; B -> Term one.",
            ),
        ),
    )


def test_matching_accuracy_request_contains_one_group_and_every_mapping(tmp_path) -> None:
    gate, llm_settings, _, providers = prepared_gate(tmp_path)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini-review-model",
    )

    gate.validate(_matching_quiz())

    inputs = providers[ProviderName.GEMINI].inputs
    assert len(inputs) == 1
    assert inputs[0].count("Question:\n") == 1
    assert "Matching prompts and proposed matches:" in inputs[0]
    assert "p1 (A): Description alpha -> c2: Term two" in inputs[0]
    assert "p2 (B): Description beta -> c1: Term one" in inputs[0]
```

- [ ] **Step 3: Run all new server tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_review.py tests/study_generation/test_repository.py tests/llm/test_medical_accuracy_gate.py tests/v2/test_quiz_builder_routes.py tests/v2/test_public_quiz_routes.py -v
```

Expected: matching review conversion, request models, route dispatch, content boundary, preview fingerprint, and accuracy serialization are not implemented.

- [ ] **Step 4: Add atomic matching edits and matching-native conversion**

Change `ReviewQuestion.draft` and service method annotations to `QuestionDraftValue`. Define:

```python
_MANUALLY_RESOLVED_MATCHING_DIAGNOSTIC_CODES = frozenset({
    "missing-supplied-matching-answer",
    "conflicting-supplied-matching-answer",
    "supplied-matching-answer-out-of-bounds",
    "duplicate-matching-prompt-identifier",
})


def _matching_is_complete(draft: MatchingQuestionDraft) -> bool:
    return (
        2 <= len(draft.prompts) <= 8
        and 2 <= len(draft.choices) <= 8
        and len({item.label.casefold() for item in draft.prompts}) == len(draft.prompts)
        and len({item.casefold() for item in draft.choices}) == len(draft.choices)
        and all(
            item.correct_index is not None
            and 0 <= item.correct_index < len(draft.choices)
            for item in draft.prompts
        )
    )


def _nullable_matching_index(value: object, choice_count: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("matching correct index is invalid")
    if not 0 <= value < choice_count:
        raise ValueError("matching correct index is outside the available choices")
    return value
```

At the start of `update_question()`, dispatch matching drafts before the MCQ path:

```python
current = self.question(run_id, question_id)
if isinstance(current.draft, MatchingQuestionDraft):
    return self._update_matching_question(run_id, question_id, current, values)
```

The pure update helper must validate before `_update_matching_question()` calls `_save()`:

```python
def _updated_matching_draft(
    draft: MatchingQuestionDraft, values: dict[str, object]
) -> MatchingQuestionDraft:
    if values.get("kind") != "matching":
        raise ValueError("matching question edit requires kind matching")
    allowed = {"kind", "stem", "prompts", "choices", "rationale", "topic", "area", "learning_objective"}
    if set(values) - allowed:
        raise ValueError("question edit contains unsupported fields")
    choices = _choices(values.get("choices", draft.choices))
    raw_prompts = values.get("prompts")
    if not isinstance(raw_prompts, list):
        raise ValueError("matching prompts must be a list")
    if tuple(item.get("id") for item in raw_prompts if isinstance(item, dict)) != tuple(
        item.id for item in draft.prompts
    ):
        raise ValueError("matching prompt IDs must exactly match the current draft")
    prompts = tuple(
        MatchingPromptDraft(
            current.id,
            _required_text(raw["label"], "prompt label"),
            _required_text(raw["text"], "prompt text"),
            _nullable_matching_index(raw.get("correct_index"), len(choices)),
        )
        for current, raw in zip(draft.prompts, raw_prompts, strict=True)
        if isinstance(raw, dict)
    )
    if len(prompts) != len(draft.prompts):
        raise ValueError("matching prompts are invalid")
    if len({item.label.casefold() for item in prompts}) != len(prompts):
        raise ValueError("matching prompt labels must be distinct")
    submitted_rationale = _optional_text(values.get("rationale", draft.rationale))
    mapping_fields_changed = (
        choices != draft.choices
        or tuple((item.label, item.correct_index) for item in prompts)
        != tuple((item.label, item.correct_index) for item in draft.prompts)
    )
    rationale = (
        matching_summary(prompts, choices)
        if mapping_fields_changed and (submitted_rationale or "").startswith("Source-marked matches:")
        else submitted_rationale
    )
    candidate = replace(
        draft,
        stem=_required_text(values.get("stem", draft.stem), "stem"),
        prompts=prompts,
        choices=choices,
        rationale=rationale,
        answer_provenance=(
            AnswerProvenance.MANUALLY_CORRECTED
            if mapping_fields_changed or rationale != draft.rationale
            else draft.answer_provenance
        ),
        verification_required=False,
        verified_at=None,
    )
    if _matching_is_complete(candidate):
        candidate = replace(
            candidate,
            rationale=candidate.rationale or matching_summary(candidate.prompts, candidate.choices),
            diagnostics=tuple(
                item for item in candidate.diagnostics
                if item.code not in _MANUALLY_RESOLVED_MATCHING_DIAGNOSTIC_CODES
            ),
        )
    return candidate
```

`_issues()` must branch for matching: one blocker per missing/out-of-range prompt, one missing-rationale blocker, existing diagnostics, generated-verification state, and group image state. `_native_quiz()` must emit `QuizMatchingQuestion` with stable `qN`, `pN`, and `cN` IDs and translate indexes to choice IDs.

In `acknowledge_run_diagnostic()`, before setting acknowledgement for `unmatched-matching-answer-group` or `unknown-matching-prompt-answer`, reject unless every matching draft returned by `review(run_id)` satisfies `_matching_is_complete()`.

Make `_candidate_asset_keys()` identify extraction questions by containment because a matching draft's source refs also include accepted answer rows:

```python
matches = [
    index
    for index, question in enumerate(extraction.questions)
    if set(extraction.question_source_refs[index]).issubset(set(draft.source_refs))
    and (
        question.original_identifier == draft.original_identifier
        or question.stem == draft.stem
    )
]
```

At the top of `verify_generated_answer()`, reject the non-generated variant before reading `correct_index`:

```python
if isinstance(draft, MatchingQuestionDraft):
    raise ValueError("matching answers do not require generated-answer verification")
```

`_update_matching_question()` wraps `_updated_matching_draft()` without losing group metadata:

```python
def _update_matching_question(
    self,
    run_id: str,
    question_id: str,
    current: ReviewQuestion,
    values: dict[str, object],
) -> ReviewQuestion:
    assert isinstance(current.draft, MatchingQuestionDraft)
    updated_draft = _updated_matching_draft(current.draft, values)
    updated = ReviewQuestion(
        updated_draft,
        current.topic if "topic" not in values else _optional_text(values["topic"]),
        current.area if "area" not in values else _optional_text(values["area"]),
        (
            current.learning_objective
            if "learning_objective" not in values
            else _optional_text(values["learning_objective"])
        ),
        current.chosen_image,
        current.selected_candidate_id,
        current.image_not_needed,
    )
    questions = tuple(
        updated if item.draft.question_id == question_id else item
        for item in self.review(run_id)
    )
    self._save(run_id, questions)
    return updated
```

- [ ] **Step 5: Add private review payload and request variants**

In `studio_routes.py`, import `Literal`, `hashlib`, `serialize_native_quiz`, `grade_matching_answer`, and matching types. Define:

```python
class MatchingPromptEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, StringConstraints(pattern=r"^p[1-8]$", max_length=2)]
    label: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10_000)
    correct_index: int | None = Field(default=None, ge=0, le=7)


class MatchingQuestionEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["matching"]
    stem: str = Field(min_length=1, max_length=10_000)
    prompts: list[MatchingPromptEditInput] = Field(min_length=2, max_length=8)
    choices: list[str] = Field(min_length=2, max_length=8)
    rationale: str | None = Field(default=None, max_length=20_000)
    topic: str | None = Field(default=None, max_length=300)
    area: str | None = Field(default=None, max_length=300)
    learning_objective: str | None = Field(default=None, max_length=1_000)


class MatchingPreviewAnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["matching"]
    question_id: str = Field(pattern=r"^q[0-9]{1,3}$", max_length=4)
    matches: dict[str, str] = Field(min_length=2, max_length=8)
```

Set route annotations to `QuestionEditInput | MatchingQuestionEditInput` and `PreviewAnswerSubmission | MatchingPreviewAnswerSubmission`. Dump the selected Pydantic object with `exclude_unset=True`.

In `_review_question_payload()`, first build the existing common group fields (`id`, original identifier, stem, choices, rationale, provenance, verification, source refs, classification, and image state). Do not read `draft.correct_index` until after type dispatch. Then emit legacy fields unchanged for `QuestionDraft`; for matching add:

```python
if isinstance(question.draft, MatchingQuestionDraft):
    payload.update({
        "kind": "matching",
        "prompts": [
            {"id": item.id, "label": item.label, "text": item.text, "correct_index": item.correct_index}
            for item in question.draft.prompts
        ],
    })
else:
    payload["correct_index"] = question.draft.correct_index
```

Add and use for both direct and Notebook previews:

```python
def _preview_version(quiz: NativeQuiz) -> str:
    digest = hashlib.sha256(serialize_native_quiz(quiz).encode("utf-8")).hexdigest()
    return f"preview:{digest}"
```

Pass `preview_version=_preview_version(quiz)` into both preview-page template contexts and replace the hard-coded template attribute with:

```html
data-quiz-version="{{ preview_version }}"
```

The content endpoint and page must derive the value from the same validated `NativeQuiz`; do not hash independently serialized response dictionaries.

In `preview_quiz_answer()`, dispatch `MatchingPreviewAnswerSubmission` to `grade_matching_answer()`, return `asdict(feedback)`, catch `KeyError` as 404 and `ValueError` as 422. Keep the MCQ request path and response shape unchanged.

- [ ] **Step 6: Enforce content-kind boundaries and reconstruct published matching drafts**

In `repository.py`, add:

```python
def _validate_question_kinds(quiz: NativeQuiz, content_kind: str) -> None:
    if (
        any(isinstance(question, QuizMatchingQuestion) for question in quiz.questions)
        and content_kind != QuizContentKind.PRACTICE_QUESTIONS.value
    ):
        raise ValueError("matching questions are limited to practice-question content")
```

Call it before mutation in `publish_quiz()` with `LECTURE_QUIZ.value`, in `_publish_studio_quiz_in_session()` and `_publish_direct_import_in_session()` with `run.content_kind`, and inside `replace_published_quiz_payload()` after loading the active model and before changing version/payload. In `publish_reviewed_studio_quiz()`, call it immediately after `to_native_quiz_in_session()` and before `_validate_accuracy()` so invalid non-practice matching content cannot trigger the accuracy provider. In `move_published_quiz()`, parse the current payload and call it with `target_kind` before assigning `model.content_kind`; a matching practice quiz cannot be moved into the quiz/exam-review library.

In `published_quiz_routes.py`, replace the inline `QuestionDraft(...)` construction with:

```python
def _published_review_draft(question: QuizQuestionValue) -> QuestionDraftValue:
    if isinstance(question, QuizMatchingQuestion):
        index_by_id = {choice.id: index for index, choice in enumerate(question.choices)}
        return MatchingQuestionDraft(
            question.id, question.id, question.stem,
            tuple(
                MatchingPromptDraft(
                    prompt.id, prompt.label, prompt.text, index_by_id[prompt.correct_choice_id]
                )
                for prompt in question.prompts
            ),
            tuple(choice.text for choice in question.choices), question.rationale,
            question.image_ref, (), AnswerProvenance.PROVIDED_BY_SOURCE, 1.0, (), False, None,
        )
    return QuestionDraft(
        question.id, question.id, question.stem,
        tuple(choice.text for choice in question.choices),
        next(index for index, choice in enumerate(question.choices) if choice.id == question.correct_choice_id),
        question.rationale, question.image_ref, (), AnswerProvenance.PROVIDED_BY_SOURCE,
        1.0, (), False, None,
    )
```

Reuse the existing group-level metadata/image upload loop.

- [ ] **Step 7: Add public request dispatch and accuracy serialization**

In `public_quiz_routes.py`, make `AnswerSubmission` `extra="forbid"` and add:

```python
class MatchingAnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["matching"]
    question_id: _PublicId
    matches: dict[_PublicId, _PublicId] = Field(min_length=2, max_length=8)
```

Set `submission: AnswerSubmission | MatchingAnswerSubmission`; dispatch matching to `grade_matching_answer()`, return `asdict(feedback)`, map unknown question to 404, and map wrong variants/invalid mappings to 422. Do not change public content version or `QuestionFlagSubmission.version: int`.

In `openrouter.py`, widen `assess()`/`_assess()`/`_question_text()` to `QuizQuestionValue` and branch:

```python
if isinstance(question, QuizMatchingQuestion):
    choice_by_id = {choice.id: choice.text for choice in question.choices}
    matches = "\n".join(
        f"{prompt.id} ({prompt.label}): {prompt.text} -> "
        f"{prompt.correct_choice_id}: {choice_by_id[prompt.correct_choice_id]}"
        for prompt in question.prompts
    )
    choices = "\n".join(f"{choice.id}. {choice.text}" for choice in question.choices)
    return (
        f"Question:\n{question.stem}\n\nChoices:\n{choices}\n\n"
        f"Matching prompts and proposed matches:\n{matches}\n"
        f"Rationale: {question.rationale}"
    )
```

The gate still calls the provider once per group; do not flatten prompts into separate calls.

- [ ] **Step 8: Run server integration tests and commit**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_practice_review.py tests/study_generation/test_repository.py tests/llm/test_medical_accuracy_gate.py tests/v2/test_quiz_builder_routes.py tests/v2/test_public_quiz_routes.py -v
.venv/bin/ruff check src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/repository.py src/oms_hub/llm/openrouter.py src/oms_hub/web/studio_routes.py src/oms_hub/web/published_quiz_routes.py src/oms_hub/web/public_quiz_routes.py tests/study_generation/test_practice_review.py tests/study_generation/test_repository.py tests/llm/test_medical_accuracy_gate.py tests/v2/test_quiz_builder_routes.py tests/v2/test_public_quiz_routes.py
.venv/bin/mypy src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/repository.py src/oms_hub/llm/openrouter.py src/oms_hub/web/studio_routes.py src/oms_hub/web/published_quiz_routes.py src/oms_hub/web/public_quiz_routes.py
```

Expected: matching edits are atomic, publication boundaries fail closed, public/preview content withholds mappings, invalid answer variants return 422, unknown questions return 404, preview fingerprints change, and accuracy sees all mappings.

```bash
git add src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/repository.py src/oms_hub/llm/openrouter.py src/oms_hub/web/studio_routes.py src/oms_hub/web/templates/studio_quiz_preview.html src/oms_hub/web/published_quiz_routes.py src/oms_hub/web/public_quiz_routes.py tests/study_generation/test_practice_review.py tests/study_generation/test_repository.py tests/llm/test_medical_accuracy_gate.py tests/v2/test_quiz_builder_routes.py tests/v2/test_public_quiz_routes.py
git diff --cached --check
git commit -m "feat(quiz): review and publish matching groups"
```

---

### Task 8: Render one grouped editor/player interaction and prove the mixed workflow

**Files:**
- Modify: `tests/js/studio_quiz_review.test.js`
- Modify: `tests/js/public_quiz.test.js`
- Modify: `tests/v2/test_quiz_builder_acceptance.py`
- Modify: `src/oms_hub/web/static/studio_quiz_review.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/web/templates/studio_quiz_review.html:37`
- Modify: `src/oms_hub/web/static/public_quiz.js`
- Modify: `src/oms_hub/web/static/public_quiz.css`

**Interfaces:**
- Consumes: Task 7 review/public content and answer JSON.
- Produces: one accessible grouped matching card in editor/player, safe matching progress restoration, and a mixed MCQ/matching import-to-public acceptance proof.

- [ ] **Step 1: Write failing grouped-editor tests**

Add this matching review payload fixture in `studio_quiz_review.test.js` beside the existing `question()` helper:

```javascript
const matchingQuestion = (id) => {
  const item = {
    ...question(id, "Match each description with its term."),
    kind: "matching",
    prompts: [
      { id: "p1", label: "A", text: "Alpha", correct_index: 1 },
      { id: "p2", label: "B", text: "Beta", correct_index: 0 },
    ],
    choices: ["Term one", "Term two"],
    rationale: "Source-marked matches: A -> Term two; B -> Term one.",
  };
  delete item.correct_index;
  return item;
};
```

Then assert:

```javascript
test("matching review renders one card and submits the entire group", () => {
  const { page, questions } = reviewPage();
  const item = matchingQuestion("matching-1");
  review.render(documentRef, page, {
    blockers: ["matching-1: answer is missing"], issues: [], preview_url: null,
    questions: [item],
  });
  const cards = questions.querySelectorAll("[data-question-id]");
  assert.equal(cards.length, 1);
  assert.equal(cards[0].querySelectorAll("[data-matching-prompt]").length, 2);
  assert.deepEqual(review.normalizedMatchingEditPayload({
    stem: " Match them ",
    prompts: [
      { id: "p1", label: " A ", text: " Alpha ", correct_index: "1" },
      { id: "p2", label: " B ", text: " Beta ", correct_index: "" },
    ],
    choices: [" One ", " Two "], rationale: " ",
  }), {
    kind: "matching", stem: "Match them",
    prompts: [
      { id: "p1", label: "A", text: "Alpha", correct_index: 1 },
      { id: "p2", label: "B", text: "Beta", correct_index: null },
    ],
    choices: ["One", "Two"], rationale: null,
  });
  const firstPrompt = cards[0].querySelectorAll("[data-matching-prompt]")[0];
  assert.equal(firstPrompt.dataset.promptId, "p1");
  assert.equal(firstPrompt.querySelector("select")["aria-label"], "Correct choice for prompt A");
});
```

Extend the test fake's `Element.matches()` with the two selectors used by that assertion:

```javascript
if (selector === "[data-matching-prompt]") {
  return element.dataset.matchingPrompt === "true";
}
if (selector === "[data-matching-prompts]") {
  return element.dataset.matchingPrompts === "true";
}
if (selector === ".studio-review-matching-bank") {
  return element.className.split(" ").includes("studio-review-matching-bank");
}
if (selector === "select") return element.tagName === "select";
```

Add tests that removing choice 1 decrements higher prompt indexes and makes a prompt selecting the removed choice unresolved; adding a choice regenerates all select options; prompt rows cannot be added/removed; focus keys remain unique; and MCQ rendering/payload remains unchanged.

- [ ] **Step 2: Write failing player state/render/request/restoration tests**

In `public_quiz.test.js`, add these exact fixtures with one MCQ and one matching group:

```javascript
const mixedContent = () => ({
  token: "m".repeat(64),
  version: 1,
  title: "Mixed practice",
  questions: [
    {
      id: "q1",
      stem: "Which option is correct?",
      choices: [{ id: "c1", text: "Yes" }, { id: "c2", text: "No" }],
    },
    {
      kind: "matching",
      id: "q2",
      stem: "Match each description with its term.",
      prompts: [
        { id: "p1", label: "A", text: "Alpha" },
        { id: "p2", label: "B", text: "Beta" },
      ],
      choices: [
        { id: "c1", text: "Term one" },
        { id: "c2", text: "Term two" },
      ],
    },
  ],
});

const matchingFeedback = (correct) => ({
  kind: "matching",
  correct,
  correct_matches: { p1: "c2", p2: "c1" },
  row_results: correct ? { p1: true, p2: true } : { p1: false, p2: true },
  rationale: "Source-marked matches: A -> Term two; B -> Term one.",
});
```

Assert:

```javascript
test("matching state requires every prompt and scores the group once", () => {
  let state = quiz.createQuizState(mixedContent());
  state = quiz.selectMatch(state, "q2", "p1", "c2");
  assert.throws(
    () => quiz.recordFeedback(state, "q2", matchingFeedback(true)),
    /every prompt/,
  );
  state = quiz.selectMatch(state, "q2", "p2", "c1");
  state = quiz.recordFeedback(state, "q2", matchingFeedback(true));
  const repeated = quiz.recordFeedback(state, "q2", matchingFeedback(true));
  assert.equal(state.score, 1);
  assert.equal(repeated.score, 1);
});


test("matching answer request sends one group body", async () => {
  let sent;
  await quiz.answerRequest(async (_url, options) => {
    sent = JSON.parse(options.body);
    return { ok: true, async json() { return matchingFeedback(true); } };
  }, "/answer", "q2", { p1: "c2", p2: "c1" }, "csrf");
  assert.deepEqual(sent, {
    kind: "matching", question_id: "q2", matches: { p1: "c2", p2: "c1" },
  });
});
```

Add restoration cases proving `String(saved.version) === String(content.version)` restores numeric `1` against string `"1"`; exact prompt/choice sets plus valid matching feedback restore; stale IDs, partial selections marked submitted, bad `kind`, non-boolean row results, bad correct choice IDs, or missing rationale reset to fresh state. Drive `initialize()` through the fake DOM and assert one matching card, two native selects with prompt-specific labels, no strike-through controls, per-row feedback, and disabled selects after submission.

The current fake has only `findByClass()`, so add this recursive collector beside it and use a real `buildQuizApp()` root for the wrong-row reveal assertion:

```javascript
const findAllByClass = (node, className) => {
  const matches = node?.className?.split(" ").includes(className) ? [node] : [];
  return [
    ...matches,
    ...(node?.children || []).flatMap((child) => findAllByClass(child, className)),
  ];
};

test("submitted wrong matching row reveals the correct bank choice", async () => {
  const rendered = mixedContent();
  let saved = quiz.createQuizState(rendered);
  saved = quiz.selectMatch(saved, "q2", "p1", "c1");
  saved = quiz.selectMatch(saved, "q2", "p2", "c1");
  saved = quiz.recordFeedback(saved, "q2", matchingFeedback(false));
  saved = { ...saved, currentIndex: 1 };
  const storage = makeQuizStorage();
  storage.setItem(
    `oms-study-hub-quiz:${rendered.token}:v${rendered.version}`,
    quiz.serializeProgress(saved),
  );
  const { documentRef, app } = buildQuizApp();
  documentRef.defaultView = { localStorage: storage };

  await quiz.initialize(documentRef, async () => ({
    ok: true,
    async json() { return rendered; },
  }));

  const matchingRows = findAllByClass(app, "quiz-matching-row");
  assert.equal(matchingRows.length, 2);
  assert.equal(matchingRows[0].classList.contains("is-incorrect"), true);
  assert.match(matchingRows[0].textContent, /Correct answer: 2\. Term two/);
});
```

- [ ] **Step 3: Run JavaScript tests and verify RED**

Run:

```bash
node --test tests/js/studio_quiz_review.test.js tests/js/public_quiz.test.js
```

Expected: matching normalization/state/render helpers are absent and numeric/string versions do not restore.

- [ ] **Step 4: Render and serialize the grouped review editor**

In `studio_quiz_review.js`, add:

```javascript
const normalizedMatchingEditPayload = (values) => {
  const payload = {
    kind: "matching",
    stem: String(values.stem || "").trim(),
    prompts: (values.prompts || []).map((prompt) => {
    const index = prompt.correct_index === "" || prompt.correct_index === null
      ? null : Number(prompt.correct_index);
    return {
      id: String(prompt.id),
      label: String(prompt.label || "").trim(),
      text: String(prompt.text || "").trim(),
      correct_index: Number.isInteger(index) ? index : null,
    };
    }),
    choices: (values.choices || []).map((choice) => String(choice).trim()).filter(Boolean),
    rationale: String(values.rationale || "").trim() || null,
  };
  ["topic", "area", "learning_objective"].forEach((key) => {
    if (Object.prototype.hasOwnProperty.call(values, key)) {
      payload[key] = String(values[key] || "").trim() || null;
    }
  });
  return payload;
};

const matchingPromptRow = (documentRef, prompt, choices, questionId) => {
  const row = documentRef.createElement("div");
  row.className = "studio-review-matching-prompt";
  row.dataset.matchingPrompt = "true";
  row.dataset.promptId = prompt.id;
  const labelField = input(
    documentRef,
    "Label",
    "prompt_label",
    prompt.label,
    "text",
    `question:${questionId}:prompt:${prompt.id}:label`,
  );
  const textField = input(
    documentRef,
    "Prompt",
    "prompt_text",
    prompt.text,
    "text",
    `question:${questionId}:prompt:${prompt.id}:text`,
  );
  const mapping = documentRef.createElement("label");
  mapping.append(text(documentRef, "span", "Correct choice", "sh-field-label"));
  const select = documentRef.createElement("select");
  select.name = "correct_index";
  select.className = "sh-select";
  select.dataset.focusKey = `question:${questionId}:prompt:${prompt.id}:correct`;
  select.setAttribute("aria-label", `Correct choice for prompt ${prompt.label}`);
  const unresolved = documentRef.createElement("option");
  unresolved.value = "";
  unresolved.textContent = "Unresolved";
  const options = choices.map((choice, index) => {
    const option = documentRef.createElement("option");
    option.value = String(index);
    option.textContent = `${index + 1}. ${choice}`;
    return option;
  });
  select.append(unresolved, ...options);
  select.value = Number.isInteger(prompt.correct_index)
    ? String(prompt.correct_index)
    : "";
  mapping.append(select);
  row.append(labelField, textField, mapping);
  return row;
};
```

In `renderQuestion()`, branch on `question.kind === "matching"`; render one shared ordinal-numbered editable choice bank, all `matchingPromptRow()` values, stem/rationale/classification/image controls, and the existing single save/status footer.

On form submit, branch by `form.dataset.questionKind`. Gather prompt objects from `[data-matching-prompt]`, permit `null` mappings, require two-to-eight prompts/choices with non-empty labels/text, and send the full `normalizedMatchingEditPayload`. Mark the matching bank with `.studio-review-matching-bank`, and add this explicit add/remove path so the removed index is captured before the row leaves the DOM while ordinary addition preserves every current selection:

```javascript
const reindexMatchingChoiceRows = (
  documentRef, group, promptContainer, removedIndex = null, questionId = "",
) => {
  reindexChoiceRows(group, questionId);
  const choices = Array.from(group.querySelectorAll('input[name="choice"]'))
    .map((field) => field.value);
  Array.from(promptContainer.querySelectorAll("select")).forEach((select) => {
    const current = select.value === "" ? null : Number(select.value);
    const nextIndex = removedIndex === null
      ? current
      : current === removedIndex
        ? null
        : current > removedIndex ? current - 1 : current;
    const unresolved = documentRef.createElement("option");
    unresolved.value = "";
    unresolved.textContent = "Unresolved";
    const options = choices.map((choice, index) => {
      const option = documentRef.createElement("option");
      option.value = String(index);
      option.textContent = `${index + 1}. ${choice}`;
      return option;
    });
    select.replaceChildren(unresolved, ...options);
    select.value = nextIndex === null ? "" : String(nextIndex);
  });
};

const removeMatchingChoiceRow = (documentRef, remove) => {
  const row = remove.closest?.(".studio-review-choice");
  const group = remove.closest?.(".studio-review-matching-bank");
  const card = remove.closest?.("[data-question-id]");
  const promptContainer = card?.querySelector?.("[data-matching-prompts]");
  const rows = Array.from(group?.querySelectorAll?.(".studio-review-choice") || []);
  if (!row || !group || !card || !promptContainer || rows.length <= 2) return false;
  const removedIndex = rows.indexOf(row);
  row.remove();
  card.dataset.dirty = "true";
  reindexMatchingChoiceRows(
    documentRef, group, promptContainer, removedIndex, card.dataset.questionId || "",
  );
  return true;
};
```

In the existing delegated click handler, call `removeMatchingChoiceRow(documentRef, remove)` when `remove.closest(".studio-review-matching-bank")` is present and otherwise keep `removeChoiceRow(remove)`. After inserting a matching choice, call `reindexMatchingChoiceRows(documentRef, group, promptContainer, null, questionId)`; after inserting an MCQ choice, retain `reindexChoiceRows(group, questionId)`. Export both matching helpers for Node tests. Add only these focused styles to `app.css`:

```css
.studio-review-matching-prompts { display: grid; gap: var(--sp-3); }
.studio-review-matching-prompt { display: grid; grid-template-columns: 6rem minmax(0, 1fr) minmax(12rem, .7fr); gap: var(--sp-3); align-items: end; }
.studio-review-matching-bank { counter-reset: matching-choice; }
@media (max-width: 720px) {
  .studio-review-matching-prompt { grid-template-columns: 1fr; }
}
```

Bump the explicit review-editor asset query in `studio_quiz_review.html` so deployed browsers cannot reuse the pre-matching script:

```html
{% block scripts %}<script type="module" src="/static/studio_quiz_review.js?v=20260902.1"></script>{% endblock %}
```

- [ ] **Step 5: Add matching player state, strict restoration, and one-card rendering**

Branch `questionState()`:

```javascript
if (question.kind === "matching") {
  return {
    kind: "matching",
    promptIds: question.prompts.map((prompt) => prompt.id),
    choiceIds: question.choices.map((choice) => choice.id),
    selectedChoiceIds: Object.fromEntries(question.prompts.map((prompt) => [prompt.id, null])),
    highlights: [], submitted: false, feedback: null, flagReason: null,
  };
}
```

Add and export:

```javascript
const selectMatch = (state, questionId, promptId, choiceId) => (
  updateQuestion(state, questionId, (question) => {
    if (question.kind !== "matching" || !question.promptIds.includes(promptId)) {
      throw new Error(`Unknown prompt: ${promptId}`);
    }
    if (!question.choiceIds.includes(choiceId)) throw new Error(`Unknown choice: ${choiceId}`);
    if (question.submitted) return question;
    return {
      ...question,
      selectedChoiceIds: { ...question.selectedChoiceIds, [promptId]: choiceId },
    };
  })
);
```

In `recordFeedback()`, require every matching prompt selection before setting submitted and incrementing score once. Generalize `answerRequest()` without changing scalar callers:

```javascript
const submission = typeof answer === "string"
  ? { question_id: questionId, choice_id: answer }
  : { kind: "matching", question_id: questionId, matches: answer };
```

In `restoreProgress()`, compare versions with `String(saved.version) !== String(content.version)`. For matching, require exact prompt and choice sets; selection keys exactly equal prompt IDs; each selection null or known; and submitted feedback satisfies:

```javascript
const exactKeys = (value, expected) => (
  value
  && typeof value === "object"
  && !Array.isArray(value)
  && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0")
);

feedback.kind === "matching"
&& typeof feedback.correct === "boolean"
&& exactKeys(feedback.correct_matches, baseline.promptIds)
&& exactKeys(feedback.row_results, baseline.promptIds)
&& Object.values(feedback.correct_matches).every((id) => validChoices.has(id))
&& Object.values(feedback.row_results).every((value) => typeof value === "boolean")
&& typeof feedback.rationale === "string"
&& feedback.rationale.length > 0
```

Before restoring a submitted matching question, also require every saved selection to be known; `null` remains valid only for an unsubmitted question:

```javascript
const selectedValues = Object.values(candidate.selectedChoiceIds);
if (submitted && selectedValues.some((id) => !validChoices.has(id))) return fresh;
```

Render matching before the MCQ answer-button branch: an `<ol class="quiz-matching-bank">` with generated 1-based ordinals and a `<div class="quiz-matching-prompts">` containing one label/text/select row per prompt. Each select has `aria-label="Choice for prompt ${label}"`, is disabled after submit, and shows row-level correct/incorrect status from `row_results`. When a submitted row is wrong, append the concrete correct bank entry from `correct_matches`:

```javascript
const correctChoiceId = questionProgress.feedback?.correct_matches?.[prompt.id];
const correctChoice = question.choices.find((choice) => choice.id === correctChoiceId);
if (
  questionProgress.submitted
  && questionProgress.feedback?.row_results?.[prompt.id] === false
  && correctChoice
) {
  row.append(element(
    documentRef,
    "p",
    "quiz-matching-correct-answer",
    `Correct answer: ${question.choices.indexOf(correctChoice) + 1}. ${correctChoice.text}`,
  ));
}
```

Omit `.quiz-strike`; reuse the existing group feedback, submit, navigation, highlight, and flag controls. Before creating the shared submit button, select the request value and completeness check by variant:

```javascript
const selectedAnswer = question.kind === "matching"
  ? questionProgress.selectedChoiceIds
  : questionProgress.selectedChoiceId;
const answerComplete = question.kind === "matching"
  ? questionProgress.promptIds.every((id) => selectedAnswer[id])
  : Boolean(selectedAnswer);
submit.disabled = !answerComplete;
// In the click handler:
const feedbackResult = await answerRequest(
  fetchImpl, app.dataset.answerUrl, question.id, selectedAnswer, csrfToken(documentRef),
);
```

Add:

```css
.quiz-matching-bank { display: grid; gap: .5rem; padding-left: 2rem; }
.quiz-matching-prompts { display: grid; gap: .75rem; }
.quiz-matching-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(12rem, .65fr); gap: 1rem; align-items: center; }
.quiz-matching-row.is-correct { border-color: var(--st-ok-dot); }
.quiz-matching-row.is-incorrect { border-color: var(--st-err-dot); }
.quiz-matching-correct-answer { color: var(--st-err-tx); margin: .5rem 0 0; }
@media (max-width: 720px) {
  .quiz-matching-row { grid-template-columns: 1fr; }
}
```

- [ ] **Step 6: Add one mixed import-to-public acceptance test**

In `test_quiz_builder_acceptance.py`, import `ExtractedMatchingAnswer`, `ExtractedMatchingAnswerRow`, `ExtractedMatchingPrompt`, and `ExtractedMatchingQuestion` from `practice_contracts`, plus `QuestionSourceRef` from `practice_domain`. Then add this extractor with one complete matching group, one complete MCQ, and one aligned citation tuple per question/answer record:

```python
class MixedFixtureExtractor:
    def extract(self, documents: tuple[SourceDocument, ...]) -> ExtractionResult:
        question_source = next(item for item in documents if item.role == "questions")
        answer_source = next(item for item in documents if item.role == "answer_key")
        question_citation = SegmentCitation(
            source_id=question_source.document.source_id,
            segment_key="block-1",
        )
        answer_citation = SegmentCitation(
            source_id=answer_source.document.source_id,
            segment_key="block-1",
        )
        question_ref = QuestionSourceRef(
            question_source.document.source_id, "block-1", "block 1"
        )
        answer_ref = QuestionSourceRef(
            answer_source.document.source_id, "block-1", "block 1"
        )
        labels = tuple("ABCDEFG")
        mapping = (5, 4, 1, 0, 2, 6, 3)
        matching = ExtractedMatchingQuestion(
            kind="matching",
            original_identifier="1",
            stem="Match each neutral description with its neutral term.",
            prompts=tuple(
                ExtractedMatchingPrompt(
                    original_identifier=label,
                    text=f"Description {label}",
                    supplied_correct_index=None,
                )
                for label in labels
            ),
            choices=tuple(f"Term {number}" for number in range(1, 8)),
            rationale=None,
            source_segments=(question_citation,),
            candidate_assets=(),
            confidence=0.99,
        )
        matching_answer = ExtractedMatchingAnswer(
            kind="matching",
            original_identifier="1",
            matches=tuple(
                ExtractedMatchingAnswerRow(
                    prompt_identifier=label,
                    correct_index=correct_index,
                    rationale=None,
                    source_segments=(answer_citation,),
                )
                for label, correct_index in zip(labels, mapping, strict=True)
            ),
        )
        mcq = ExtractedQuestion(
            original_identifier="2",
            stem="Which option is correct?",
            choices=("Yes", "No"),
            supplied_correct_index=None,
            rationale=None,
            source_segments=(question_citation,),
            candidate_assets=(),
            confidence=0.9,
        )
        mcq_answer = ExtractedAnswer(
            original_identifier="2",
            correct_index=0,
            rationale="The source key selects Yes.",
            source_segments=(answer_citation,),
        )
        return ExtractionResult(
            questions=(matching, mcq),
            answers=(matching_answer, mcq_answer),
            question_source_refs=((question_ref,), (question_ref,)),
            answer_source_refs=((answer_ref,), (answer_ref,)),
            provider_metadata=(),
            diagnostics=(),
        )
```

Use the seven-row permutation `(5, 4, 1, 0, 2, 6, 3)` in the acceptance assertion:

```python
def test_mixed_import_stays_grouped_through_review_publication_and_grading(
    tmp_path: Path,
) -> None:
    app = acceptance_app(
        tmp_path,
        notebook=FailIfCalledNotebook(),
        fallback=FailIfCalledFallback(),
        supplied_answer=True,
        extractor=MixedFixtureExtractor(),
    )
    client = TestClient(app)
    headers = _csrf_headers(client)
    run_id = _queue_import(client, headers, answer_mode="supplied")

    _drain_studio_worker(app)
    review = client.get(f"/studio/runs/{run_id}/review/data").json()
    assert len(review["questions"]) == 2
    assert review["questions"][0]["kind"] == "matching"
    assert len(review["questions"][0]["prompts"]) == 7
    published = client.post(f"/studio/runs/{run_id}/publication", headers=headers)
    assert published.status_code == 200
    token = published.json()["token"]
    content = client.get(f"/public/quizzes/{token}/content").json()
    assert [item.get("kind", "multiple_choice") for item in content["questions"]] == [
        "matching", "multiple_choice"
    ]
    assert "correct_index" not in json.dumps(content)
    matching_answer = client.post(
        f"/public/quizzes/{token}/answer",
        headers=headers,
        json={
            "kind": "matching", "question_id": "q1",
            "matches": {
                f"p{row}": f"c{choice + 1}"
                for row, choice in enumerate((5, 4, 1, 0, 2, 6, 3), 1)
            },
        },
    )
    mcq_answer = client.post(
        f"/public/quizzes/{token}/answer",
        headers=headers,
        json={"question_id": "q2", "choice_id": "c1"},
    )
    assert matching_answer.json()["correct"] is True
    assert len(matching_answer.json()["row_results"]) == 7
    assert mcq_answer.json()["correct"] is True
```

Allow `acceptance_app(..., extractor: object | None = None)` and use `extractor or FixtureExtractor(...)`; do not create a second app harness.

- [ ] **Step 7: Run focused UI and mixed acceptance tests, then commit**

Run:

```bash
node --test tests/js/studio_quiz_review.test.js tests/js/public_quiz.test.js
.venv/bin/pytest tests/v2/test_quiz_builder_acceptance.py -v
.venv/bin/ruff check tests/v2/test_quiz_builder_acceptance.py
```

Expected: one grouped card is rendered in each UI, both native selects are keyboard-operable, corrupt state is discarded, numeric/string versions restore, mixed content publishes and grades, and existing MCQ tests stay green.

```bash
git add src/oms_hub/web/static/studio_quiz_review.js src/oms_hub/web/static/app.css src/oms_hub/web/templates/studio_quiz_review.html src/oms_hub/web/static/public_quiz.js src/oms_hub/web/static/public_quiz.css tests/js/studio_quiz_review.test.js tests/js/public_quiz.test.js tests/v2/test_quiz_builder_acceptance.py
git diff --cached --check
git commit -m "feat(quiz): render grouped matching interactions"
```

- [ ] **Step 8: Run final verification and obtain a fresh review of the exact tree**

Run focused suites first:

```bash
.venv/bin/pytest tests/study_generation/test_native_quiz.py tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_matching.py tests/study_generation/test_quiz_import_worker.py tests/study_generation/test_practice_review.py tests/study_generation/test_repository.py tests/llm/test_gemini.py tests/llm/test_openai.py tests/llm/test_anthropic.py tests/llm/test_openrouter_provider.py tests/llm/test_medical_accuracy_gate.py tests/v2/test_quiz_builder_routes.py tests/v2/test_public_quiz_routes.py tests/v2/test_quiz_builder_acceptance.py -v
node --test tests/js/public_quiz.test.js tests/js/studio_quiz_review.test.js
```

Then run repository gates:

```bash
.venv/bin/pytest
node --test tests/js/*.test.js
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
git diff --check
git status --short
git rev-parse HEAD
git rev-parse HEAD^{tree}
```

Expected: every command passes with no skips introduced by this work. Confirm the diff contains no database migration, dependency change, model-ID branch, live-provider call, deployment, restart, or production data mutation. Request a fresh read-only review against the printed exact HEAD/tree; if that review changes code, rerun this entire step and obtain another review for the corrected tree.

Do not run a live Gemini request, retry the rejected import, or publish real content. After the exact implementation tree passes every gate and fresh review, continue with the separately authorized push, merge, and NUC procedure in `docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md`.
