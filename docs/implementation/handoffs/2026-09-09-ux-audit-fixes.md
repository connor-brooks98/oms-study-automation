# Study Hub UX audit implementation — 2026-09-09

Connor authorized all sixteen improvements from `study-hub-ux-audit-2026-09-08.md`, Sol implementation helpers, independent review, then merge to main. Notify before the Hub is restarted. This candidate includes the previously reviewed practice-library/reordering commit `97ae572377bcaadf835c73cbbdaa8aa597a12afd` over production/main `59b4a645c0c623556bc1115cdc2dc378e4cfc3f2`.

## Resolution map

| Audit ID | Result | Main regression evidence |
|---|---|---|
| M11 | Home resumes a locally recorded lecture only after validating its ID against current catalog data; otherwise offers Choose a lecture. | daily-study route tests, dashboard JS, browser lecture28 → Home |
| M1 | Lecture title/number/course/exam search reveals matches, reports no results, and restores prior disclosures when cleared; global destination search is Go to… | dashboard mixed-state/denied-storage tests; browser keyboard clear |
| H2 | Answer-bearing area, objective, and topic are hidden until that question is submitted. | public player JS; synthetic answer POST and before/after browser check |
| M3 | Exam/lecture links retain source course, exam, and import workflow; review returns to that scope; libraries restore expansion and scroll. | builder route/JS and library state tests; browser navigation |
| M2 | Study actions and pass summary precede processing; Open is primary and downloads secondary; failures keep processing details visible. | daily-study/lecture-generation routes and UI-standard tests |
| M6 | Materials readiness/processing and study-pass counts have separate labels. | daily-study routes and rendered library |
| M7 | Existing sources come first with filter, selected count, bounded scrolling, and Add source disclosure; destination follows source defaults until explicitly overridden. | notebook studio JS; 21-source browser fixture |
| M4 | Management navigation is explicit on desktop/mobile; public views can retain an allowlisted manager return path. Anonymous controls and authorization remain unchanged. | remote/anonymous route tests; public → manager return browser check |
| M5 | Counts say completed/configured, separately explaining the seven-pass target; dynamic pass editing remains. | daily-study routes; exam-page browser check |
| M10 | Unmatched supplied answers belong in global publication checks; questions retain local work. Historical artifacts are projected at run scope without read-time rewrites, and old pair caches are invalidated. | production pairing/worker/review path and legacy and acknowledgement-failure regression checks |
| M8 | Profile and plain-language summary remain visible; provider/model/stage/fixture details use Advanced. Blockers remain visible; Custom and invalid required controls reveal Advanced. | Anki HTML/JS tests; native disclosure browser check |
| M9 | Run history uses persisted creation time plus short ID and one readable status, including runs with no attempt. | repository/routes/JS; two similarly named fixture runs |
| L1 | Compact hierarchy/rows omit duplicate subtitles; course/exam and row controls retain at least 48px targets. | library routes/styles and desktop/mobile browser checks |
| L2 | One title and short context lead the quiz; highlighting and flagging share a compact accessible toolbar. | player JS; desktop and 390px visual checks |
| L3 | Upload files, Study Hub, and Unmatched uploads replace internal terminology in the affected flows/navigation. | shell/upload/Anki/UI-standard checks |
| C1 | Cloudflare's optional analytics beacon is disabled only for studyhub.perch-bird.com; CSP is unchanged. | independent rule review and authenticated browser verification below |

## Cloudflare configuration receipt

Rule `461fbd80f7e849b699d2cd0178c270a8`, **Study Hub - disable optional analytics beacon**, is active with exactly `(http.host eq "studyhub.perch-bird.com")` and `disable_rum=true`. The disabled draft was independently approved before activation. This does not change Access policies, identity providers, HTTPS, WAF, cache, or CSP. Configuration-rule exclusions override Web Analytics inclusion: [Cloudflare settings](https://developers.cloudflare.com/rules/configuration-rules/settings/) and [Web Analytics rules](https://developers.cloudflare.com/web-analytics/configuration-options/rules/).

The first immediate sample still contained the beacon during propagation. Later authenticated Home and Lectures pages contained zero Cloudflare beacon nodes and only same-origin script URLs. Native Chrome DevTools showed no beacon CSP violation; an extension content-script CSP error and asynchronous message-channel error remained visible and were not suppressed. An unauthenticated curl that returned an Access redirect was explicitly excluded from acceptance evidence.

## Verification and review

The isolated synthetic preview at `http://127.0.0.1:4567/studio` was restored as a detached local process after the old preview had no listener. It uses a temporary database, sample sources/questions, fake provider/model adapters, and no background workers. Generation, uploads, and Anki actions are blocked there. Only sample answer submissions and sample reorder PATCHes were used. Keyboard and pointer reordering both saved and persisted across reload. Desktop and 390px quiz/library checks showed no horizontal overflow; temporary viewport overrides were reset.

Changed internal scripts reuse the shared `20260909.2` release version. The first broad test run overlapped the version correction and ended with 3351 passes, two expected Windows-only skips, and one stale compiled version assertion; that run is not final acceptance. Final frozen-source test results and exact review/commit/tree are recorded in the release receipt. Scoped independent reviews and correction evidence remain in `.superpowers/sdd/2026-09-09-ux-audit-implementation/` in the UX worktree; final review and operational receipts are copied to `tmp/ux-release-20260909/` in the main checkout.

## Integration and operational boundaries

Use a source-only fast-forward release after independent approval and fresh full-suite verification. Dependencies, schema31, and installer/task definitions do not change. Before cutover, verify the exact old/new commit and tree, task XML, environment hash, one owned loopback listener, idle healthy workers, and fresh online database/source backups. Notify Connor immediately before stopping the Hub. Preserve the live database and use guarded source rollback on failure.

The pinned task **Audit Sol 1–10 handoff state** owns Google/Gate2B implementation and its separately authorized native proofs. It has SSH access via `nuc`. Its `efdae69a` proof has finished and released its resource hold, but every task28/oms-task28 proof root, runtime, private/control/diagnostic artifact, and prior cleanup exclusion remains protected. No provider request, proof rerun, Anki collection write, or Gate opening is authorized by this UX handoff. This release does not establish Gate2B acceptance.

Workflow rulings: independent file-owned Sol groups ran in parallel as Connor requested; the complete review includes the earlier library candidate because these fixes build on that pending work. The user's explicit merge instruction supplies the integration choice. Keep the worktree and synthetic preview evidence while they remain useful; do not force-remove untracked evidence.
