# GPT Study Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved subscription-first GPT Study Hub in independent, reviewed increments, with an orchestrator that creates and coordinates its own worker tasks.

**Architecture:** Preserve the Hub's storage, ingestion, review, media and player. B builds one Codex managed-session backend and lecture quizzes; Q builds normalized bank imports and exact AnKing links; C builds source-scoped chat and progress. O owns contracts, shared wiring, integration, evidence and release preparation.

**Tech Stack:** Current Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic, existing document/image/PDF tools, vanilla JS, pytest, Ruff/mypy; documented pinned Codex app-server over stdio. No new orchestration service, queue broker or vector database.

**Spec:** `docs/superpowers/specs/2026-09-11-gpt-study-platform-design.md`

## Global Constraints

- Subscription login first; no silent API fallback, credit purchase/reset or account rotation.
- Windows NUC is the intended host; preserve the current port-8765 owner and live database. No production restart, deploy, push/main merge, retained-proof cleanup or Anki mutation under this plan alone.
- Existing source revision/hash, review, publication and image safety checks remain. Private material enters the provider only through an explicit source-scoped user operation or a separately approved live test.
- O creates worker tasks itself. Do not ask Connor to paste prompts into each worker, manually create worktrees or relay handoffs.
- Contract/local evidence is distinct from real Windows/provider evidence. Optional AMBOSS access and missing vendor exports cannot block unrelated core work.
- Use local commits in isolated worktrees. Never borrow uncommitted files across worktrees or compose untracked runtime bundles as an integration method.
- Preserve source-managed Anki tags and existing card scheduling. Full curation is deferred; read-only candidate export is in scope.
- This file supersedes the required-Google direction for the new candidate only. It neither opens Gate 2B nor authorizes changes to its retained resources or active tasks.

## Read these together

1. Approved spec above.
2. [B: backend and quizzes](2026-09-11-gpt-backend-and-quizzes.md).
3. [Q: bank imports and Anki links](2026-09-11-question-bank-and-anki-imports.md).
4. [C: chat and progress](2026-09-11-chat-and-study-progress.md).
5. [AMBOSS evidence](../../implementation/2026-09-11-amboss-access-research.md) and [sent inquiry](../../implementation/2026-09-11-amboss-product-inquiry.md).
6. `docs/future-implementation-ideas.md` and `docs/implementation/handoffs/2026-09-09-ux-audit-fixes.md` for scope reconciliation, not new release authorization.

## O0: Bootstrap, task creation and exact base

**Files:** Create execution-only `docs/implementation/gpt-platform-status.json` and handoffs under `docs/implementation/gpt-platform-handoffs/`. Do not use the old Sol implementation dashboard as the new program's state store.

- [ ] Read repository `AGENTS.md`, current skills and Git status. Run `git status --short`, `git branch --show-current`, `git rev-parse HEAD`, `git worktree list --porcelain`, and inspect recent main ancestry. Record the dirty-file list without changing it. The planning checkout was `f487c622`; do not treat it as today's production or latest accepted release.
- [ ] Use read-only source/deployment receipts to select the current accepted base; if remote freshness is needed, use an ordinary authorized Git fetch. If latest candidate work exists, identify its exact owner/commit and integrate only accepted descendant changes. Do not reset the shared checkout to a remembered hash.
- [ ] Create a clean integration worktree/branch `codex/gpt-study-platform` from the verified base. Bring only this plan/spec/research package's committed documentation into it. Record commit/tree. A name collision resumes a matching existing worktree after identity verification, not a second one.
- [ ] Create a compact JSON status file with the schema below. Write with `Path.write_text` to a sibling temp file then `Path.replace`; O is its sole writer. Workers return evidence to O, not race on status.
- [ ] Discover existing Codex tasks first (`list_threads`). Reuse program tasks only if their saved role, project and program ID match. Never resume unrelated old Sol workers. Call `list_projects`, choose the returned Git project matching this repository, then `create_thread` with project worktree environment, explicit starting branch=`codex/gpt-study-platform`, `onMissing='error'`, and title `GPT Study Hub — B Backend`, `GPT Study Hub — Q Imports`, or `GPT Study Hub — C Chat and Progress`.
- [ ] Creation is asynchronous: the initial creation prompt identifies the program/role and asks the worker to report its own cwd/HEAD and await O's exact assignment. Store returned `clientThreadId` until resolved, never use it as `threadId`. Resolve queued setup through app task listings matched to the unique program role/project/host, then use real thread ID/host for messages and compact `wait_threads`. Read its returned cwd/HEAD before filling the dispatch contract below. O sends the ready assignment without another user prompt.
- [ ] If `create_thread` is unavailable, use native `spawn_agent` with the same self-contained dispatch and explicit worktree cwd. This is a supported fallback, not a reason to stop or require manual task creation. Do not create duplicate sidebar tasks and native agents for the same assignment.

Initial status structure (values are filled from observed tool results, not guessed):

```json
{
  "schema_version": 1,
  "program": "gpt-study-platform-2026-09-11",
  "base_commit": "observed_git_rev_parse_HEAD",
  "integration_branch": "codex/gpt-study-platform",
  "workers": {},
  "tasks": {},
  "external": {"amboss": "reply_pending", "uworld_sample": "not_supplied", "truelearn_sample": "not_supplied"},
  "release": {"candidate": null, "live_authorized": false}
}
```

The example's `base_commit` is an instruction to insert the actual hash; the persisted file must contain a valid 40-character commit. Per-task record: `state` (`ready`, `running`, `review`, `accepted_local`, `blocked_external`, `failed`), `worker`, `commit`, `tests`, `review`, `blocker`, `next_action`. Record `provider_verified` and `windows_verified` separately as booleans with evidence paths. An optional external pending status is never silently upgraded to acceptance.

## Worker topology and scheduling

Default budget: O plus at most **two active worker leaders and one active helper/reviewer**. Other leaders wait. When the current surface has fewer slots, reduce active lanes; never ask each leader to consume all available subagent slots. Workers must request the helper token from O before spawning. A helper owns one bounded file/test task, reads a self-contained prompt and returns evidence; it cannot create grandchildren. Use the current model unless Connor explicitly changes it; do not infer a preferred model from the old Sol names.

Where sidebar tasks are supported, O creates B and Q first. C is created when C1 becomes ready, or created idle with no autonomous implementation until dispatched. O stays active and uses `wait_threads` on active leaders with cursors; poll compact status rather than repeatedly re-reading full task histories. Do not install recurring automations. Resume orchestration after context compaction from the committed plan, status and worker receipts.

| Task | Owner | Local dependency | External activation requirement |
| --- | --- | --- | --- |
| B1 | B | O0 | Managed login and bounded synthetic proof for readiness |
| B2 | B | B1 local protocol contract | None for fixtures |
| B3 | B | O0 | No private provider call needed for extraction |
| B4 | B | B2, B3 | B1 live capability for live generation |
| B5 | B | B4, O1 shared schema | None for local review integration |
| B6 | B/O | B5 | Actual Windows/provider evidence for activation |
| B7 | B | B5 | None for accepted synthetic export |
| Q1 | Q | O0 | None |
| Q2 | Q | Q1 | Real supported vendor export sample per vendor |
| Q3 | Q | Q1, O1 shared schema | None; Q2 is not a prerequisite |
| Q4 | Q | Q1 | Existing/supplied AnKing index for real mapping acceptance |
| Q5 | Q | Q3, Q4 | None for fixture UI |
| Q6 | Q | Q5 | Vendor-specific acceptance tracked separately from normalized import |
| C1 | C | B2 interface | B1 live capability for activation |
| C2 | C | C1 | Documented AMBOSS access only for its live adapter |
| C3 | C | Q3, O1 shared schema | None for fixtures |
| C4 | C | C3, B5 | None for accepted local questions |
| O2 | O | Each completed task | Fresh independent review |
| O3 | O | B6, B7; completed optional slices selected for release | Core Windows/provider proof |

B1 local schema/fixture work may be accepted while its live capability is pending; B3/Q/C offline development continues. Split Q2 by vendor, so one missing export does not hold the other. Do not schedule waiting tasks simply to keep every agent busy. If a shared dependency changes, rebase only its consumers and rerun affected checks; do not repeat the entire program.

## Exact worker dispatch contract

O sends this filled message through `create_thread` or `send_message_to_thread` (native fallback: `spawn_agent`). Replace bracketed fields with observed values; no incomplete template is dispatched.

```text
You are worker [B/Q/C] for gpt-study-platform-2026-09-11.
Repository: /Users/connor/Developer/oms-study-automation.
Your worktree: [actual worker cwd]. Base commit: [40-character accepted hash].
Orchestrator task ID/host: [observed O thread ID and host, or native parent agent path].
Read the approved spec docs/superpowers/specs/2026-09-11-gpt-study-platform-design.md,
the main orchestration plan docs/superpowers/plans/2026-09-11-gpt-study-platform.md,
and your subplan [exact B/Q/C relative path].
Implement only task [task ID] now, following its concrete interfaces and tests.
You are not alone: preserve others' work and do not revert or overwrite their changes.
Own only your subplan files. Shared app/config/models/migrations/navigation belong to O;
submit exact patches and tests to O before you depend on those changes.
You may request one bounded subagent from O; do not spawn until O grants the shared token.
Use isolated local work, focused tests, and local commits. No push, live deployment,
provider/private-source call, account purchase, Anki write or resource cleanup.
Report exact commit/tree, files, test commands/results, contract changes, and any external
blocker. Classify evidence local/Windows/provider. On completion, wait for O's next task.
Do not ask Connor to relay messages. Send the result through the task tools to O.
```

O grants separately authorized synthetic/live tasks with their exact allowed inputs/call count rather than copying broad historical permissions. User login requires the user's browser action; ask once with the actual device/browser link after preparing it. Do not request passwords. Vendor samples require actual files or inspection of supported export controls—not speculative schema guessing.

## O1: Shared contracts and narrow integration patches

**Files:** `src/oms_hub/app.py`, `config.py`, `models.py`, `migrations.py`, `llm/service.py`, `llm/domain.py`, `study_generation/domain.py`, `service.py`, `repository.py`, `studio_repository.py`, `practice_review.py`, existing route registration/navigation and shared tests. Paths after `study_generation/domain.py` refer to that same directory. O owns these for the duration of the program. B's `studio_worker.py` dispatch patches are coordinated with B, never edited concurrently. Q5's `create_bank_import_review` transaction and `bank_review_questions` join are O-owned integration changes, using Q's exact contract.

- [ ] Freeze the exact contracts already specified by B/Q/C; these plans define proposed interfaces, not existing methods. Verify existing call sites before modifying a shared signature. Maintain a single model-turn client, not one per request or feature.
- [ ] B submits the new job backend selector, immutable manifest identity, provider thread/turn fields and paused/error state; Q submits source/product/question/attempt constraints; C submits owner conversation/session fields. Add `StudioRepository.record_gpt_lifecycle(run_id: str, event: SessionLifecycle) -> None` as the synchronous transaction boundary used by B5. Import `SessionLifecycle` from B; store request ID, phase, thread/turn IDs and timestamps, validate request/run ownership, and reject impossible transitions. C1's equivalent `ChatRepository.record_lifecycle` uses the same event contract. O chooses the next migration number from actual current `migrations.py` and uses the project's existing forward migration style. Do not hardcode schema31 from old receipts.
- [ ] Add failing migration and dispatch tests. Required examples: existing NotebookLM job retains its backend and remains readable; new GPT job queues while Google is disconnected; duplicate bank attempt cannot insert twice; public quiz answer does not create an owner event; restored DB contains existing quiz/media rows unchanged.
- [ ] Implement only these shared deltas, then run focused migration, generation, import and route tests. Give workers the resulting accepted commit before they continue dependent work. Workers rebase on the integrated parent; do not copy source files manually between worktrees.
- [ ] Wire B's `CodexSessionClient` singleton at application lifespan. Match shutdown to `close`; no auto-login or live probe at import/startup. Instantiate chat/generation using this same object. Config records separate account connection and capability readiness. Plain API adapters remain explicit and unchanged unless selected by the user.
- [ ] Route new GPT transcript cleanup and optional outline/quiz operations without NotebookLM auth checks. Historical jobs dispatch using recorded backend. Keep optional NotebookLM UI/artifacts readable; do not delete old dependencies until a separate retirement decision.
- [ ] Wire routes under existing owner/CSRF controls. Use application-level selected-source validation and per-request staging; the model cannot call unrestricted filesystem/network/shell/Anki tools. Verify prevention before tool execution, not merely detection afterward.
- [ ] Run `python -m pytest tests/llm tests/study_generation tests/v2 -q`, `ruff check src tests scripts`, `mypy src`; record actual failures and narrow regressions. Commit only the accepted shared delta.

## O2: Review and integrate every independently testable task

- [ ] Reserve the helper slot for a fresh reviewer, distinct from the implementer. Reviewer receives exact base/head, relevant spec sections, diff and test evidence. Review correctness, scope, source/owner isolation, lifecycle recovery and interface compatibility; do not require two ceremonial reviews for every trivial change.
- [ ] Return actionable failures to the owning worker. A reviewer cannot mark provider acceptance from mock tests or code inspection. Confirm test result provenance and exact tree.
- [ ] Integrate only a clean reviewed commit into the candidate branch. If conflicts occur, O owns conflict resolution and reruns impacted tests. Never merge user dirty files, untracked diagnostics or private source material.
- [ ] Update task status with commit and review receipt. Dispatch newly ready tasks automatically. A missing export or AMBOSS reply marks only the affected capability externally pending, with the concrete next input; do not reopen broad gate reviews.

Required worker receipt (`docs/implementation/gpt-platform-handoffs/<task-id>.md`): task/base/head/tree, files, interfaces produced/consumed, exact verification commands/results, local/Windows/provider evidence classification, reviewer verdict/fixes, current limitation and next task. One current receipt per task, not repeated copies of historical prose.

## O3: End-to-end verification and release preparation

- [ ] Select coherent release increments. A first quiz-only release may include B1–B7; label it an early increment, not core program completion. Core completion requires B1–B7 plus C1 signed-in lecture/general chat. Continue accepted Q/C3/C4 work to its defined deliverables or concrete external blockers; do not abandon those planned tasks merely because an early release is ready. AMBOSS live access and full curation are not core prerequisites. Generate a release matrix listing included capabilities and externally pending ones.
- [ ] Run repository CI-equivalent checks on the exact candidate: `ruff check src tests scripts`, `mypy src`, `pytest -q -m "not windows_office"`, `node --test "tests/js/*.test.js"`; use the configured environment. Do not install or upgrade unrelated dependencies. Native Windows tests cover owned process reaping, restricted tool execution, login/session storage ownership and restart recovery.
- [ ] Prepare at most three synthetic, source-free provider test cases: structured text result, synthetic image+schema result, and explicit interruption/limit/error handling as supported. Record executable/model and safe timing/status evidence. Run only under current explicit live authorization; a transport failure stops that proof, not unrelated implementation. Fix the root cause before proposing a bounded rerun. No private lecture corpus is part of these synthetic checks.
- [ ] Prepare the Heme-like acceptance fixture: independent cases, full objective coverage, real fixture figures and source locators, intentionally wrong citation/missing image/answer-label cases, no cross-exam leakage, and native JSON/ZIP/PDF parity. Verify the authenticated preview in browser at desktop/mobile sizes. For actual lecture materials, obtain the specific source-scoped user operation instead of treating a fake case as medical acceptance.
- [ ] Recheck the 16-item UX resolution behaviors and current local lecture-action removal; preserve whichever changes are in the selected accepted base. Avoid implementing old audit findings twice. Record any remaining actual regressions.
- [ ] Prepare deployment/rollback using existing scripts and fresh read-only listener/process/task/readiness identity. Back up live DB and source before a separately approved release. Preserve queued jobs and Gate 2B proof artifacts. No guessed historical hash is a rollback target.
- [ ] Deliver exact candidate commit, tests, feature matrix, unresolved external inputs, downtime/rollback procedure, and one concise request for release authorization. Do not claim deployment while only local integration exists.

## Resumption and stop rules

O resumes from status + verified Git + worker task states, not from a stale narrative. After a crash, reconcile running task status and owned process IDs before retry. Never duplicate an in-flight provider turn or import commit. If a worker disappears, inspect its clean accepted commits before creating a replacement. Preserve uncommitted work and report owner-bound conflicts.

Login, real export samples, AMBOSS's reply, live mutations and deployment are legitimate user/external boundaries. Everything else—worker creation, task dispatch, tests, review, local conflict resolution and candidate preparation—is O's responsibility. No need to ask Connor to choose execution mode: the approved choice is an orchestrator with bounded task workers and subagents.

## Scope reconciliation

| Discussed idea | Covered by | Status intent |
| --- | --- | --- |
| GPT subscription backend replacing required Google | B1/B2/B6, O1 | Core |
| Real lecture-image quizzes, objective coverage, readable rationales | B3–B5/B7 | Core |
| Optional outlines; preserve existing artifacts | B6/O1 | Core |
| UWorld/TrueLearn imports | Q1–Q3/Q5/Q6 | Normalized first; real formats sample-bound |
| Existing QID-to-AnKing workflows and exports | Q4/Q5 | Read-only first |
| AMBOSS medical chatbot and lecture matches | C2/Q4 | Access-conditional / manual candidates |
| General and lecture-source chat | C1 | Independent of AMBOSS |
| Competency tracker, weak areas, cumulative blocks | C3/C4 | After attempt identity |
| NBOME COMLEX versus USMLE taxonomy | C3/C4 | Correct the old backlog naming |
| Prior UX cleanup and pass tracker | O3 regression | Reuse accepted implementation |
| Full automatic Anki curation | Spec deferral | Not reactivated by this plan |
| ExtendLM and mandatory Google File Search | Superseded core direction | No new dependency |

## Planning self-review

Before handing this package off, verify all linked files exist, all B/Q/C task IDs in the dependency table occur in subplans, shared contract names agree, and no optional-access edge blocks core generation. Check that no code changes/provider calls are misrepresented as performed. This package is an executable work specification; runtime protocol and real vendor format acceptance remain explicit tasks, not invented implementation facts.
