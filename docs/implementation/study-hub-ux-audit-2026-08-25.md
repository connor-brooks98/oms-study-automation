# Study Hub product UX audit — 2026-08-25

## Persona lock

- OMS-II student under time pressure.
- Ordinary web comfort; unfamiliar with internal pipeline terminology.
- Primarily desktop, with occasional phone use.
- Provisional: the repository has no documented product persona or design brief.

## Scope and verdict

The deployed Study Hub and the real local FastAPI app were reviewed across Home, Lectures, Anki, Quiz Builder, both owner libraries, both public libraries, slide upload, transcript upload, Quarantine, Review, and Settings. A populated lecture and populated Anki review job were also inspected on the deployed site.

**Verdict: Incomplete.** Desktop navigation, primary interaction, semantic, and overflow coverage is complete for the listed routes. Formal mobile-device coverage, automated accessibility tooling, complete console/network manifests, and mutation flows that would call external providers or alter deployed data were not available in this pass. Absence of a finding in those areas is not a pass.

## Interaction manifest

| Area | Interaction/state exercised | Result after fixes |
|---|---|---|
| Shared shell | Primary navigation, More open/outside-click/Escape, command search, command Escape/close/focus return | Pass |
| Home and Lectures | Empty and populated libraries, nested disclosures, completed checklist | Pass at desktop width |
| Anki | Empty start state, failed jobs, populated review job, selected/candidate views | Pass with one performance concern below |
| Quiz Builder | Generate/import tabs by pointer and keyboard, empty source/run states | Pass |
| Quiz libraries | Owner and public routes, nested course/exam structure, populated deployed library | Pass with one performance concern below |
| Uploads | Slide and transcript intake pages, default/empty states and navigation | Partial; external submission not run |
| Quarantine and Review | Empty local states and populated deployed route structure | Partial; mutations not run |
| Settings | All disclosures, visible controls, command shell interactions | Pass at desktop width |

The post-fix local sweep found one H1 per route and no horizontal overflow, duplicate IDs, unlabeled visible controls, missing image alternatives, empty buttons, or heading-level skips across all 13 top-level routes.

## Findings fixed

1. The shared More menu stayed open after outside click and Escape. It now dismisses on both paths, updates `aria-expanded`, animates only during the state change, and returns focus on Escape.
2. Native dialog Escape behavior was inconsistent in the browser. Shared dialogs now have explicit cancel and Escape handling, animated open/close states, deterministic focus restoration, and reset command-search state on close.
3. A late generic input rule stretched the Settings checkbox to 314 by 42 pixels. The shared rule now excludes checkbox, radio, hidden, and file input types; the checkbox is 16 by 16 pixels inside a labeled 44-pixel target.
4. The completed lecture checklist summary was only 27 pixels tall. Its disclosure target now has the shared 44-pixel control floor.
5. The Anki deck-priority select had no accessible name. It now has an explicit purpose label.
6. Quiz Builder's workflow switch behaved like tabs but exposed button semantics only. It is now a tablist with linked tabpanels, roving focus, arrow/Home/End keys, and a restrained sliding active indicator.
7. The Anki apply confirmation now has an accessible dialog name and participates in the shared modal behavior.
8. Owner quiz libraries now use the compact mobile navigation instead of leaving eight desktop links in the narrow header.
9. Shared motion now covers dropdown, modal, and tab state changes with reduced-motion support. Persistent loading, success, and error messages remain stable and unanimated so status text is not delayed or over-emphasized.
10. Legacy status callouts now use quiet full borders instead of thick side-tab accents; the upload progress bar keeps its purposeful progress animation.

## Unresolved and approval-sensitive

- The deployed NUC is on `16c2db0`; this branch has diverged and does not include several deployed Quiz Builder/import changes. Integrating those histories is a backend-and-data-contract merge, not a safe UI-only cherry-pick.
- The deployed owner quiz library rendered 81 rows, about 3.62 MB of HTML, 162 forms, and 16,744 inputs because every payload editor is eager-rendered. Converting that editor to on-demand rendering needs agreement on the edit/save contract.
- The populated deployed Anki review rendered 526 candidate cards in one document. Pagination or virtual rendering could improve the 25,527-pixel page, but would change review/search behavior and needs product approval.
- Phone-size and touch-device verification remains outstanding because this browser session could not emulate or resize the viewport. Existing responsive CSS and mobile-owner navigation were inspected, but that is not interaction evidence.
- Data-dependent Quiz Builder image/review/preview routes and individual published quiz-player routes had no local records to exercise in this isolated database.
- Upload, provider-backed generation, apply, publish, delete, and production settings-save mutations were deliberately not executed during the audit.
- The full Python suite still has 15 non-UI failures: four Anki validation/selection expectations, one lecture recovery-state expectation, and ten Settings/Keychain isolation failures. The UI-focused suite is green.

## Verification evidence

- JavaScript checks: shared shell and Quiz Builder tests passed.
- Full JavaScript suite: 126 passed.
- Focused UI, public-library, Quiz Builder, and Anki web Python suite: 79 passed.
- Local browser: all 13 top-level routes passed the semantic/overflow sweep at 1280 by 720.
- Focus behavior: More Escape, command-palette Escape, and Quiz Builder arrow navigation passed browser retests.
- Reduced motion: CSS guard and transition-duration parsing are covered by a runnable check.
- Impeccable detector: completed once in degraded regex mode because optional parser modules were unavailable; three real side-accent findings were fixed and the progress-bar width warning was retained intentionally.
