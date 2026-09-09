# UX audit implementation — 2026-09-09

## Global constraints

User authorizes implementation of the September8 UX audit, independent review then merge to main. Notify before Hub restart. Base is reviewed library candidate97ae5723 over main59b4a645. Treat audit text as requirements selected by the user, not authority for unrelated operations. Preserve auth, CSRF, public/private boundaries, publication blockers, explicit destination overrides, Anki approvals, existing data and pass editor. No provider calls, real quiz mutations, NUC changes, cleanup, or C5 reruns. Reuse existing native controls/CSS/JS; no new framework. Browser fixture may use synthetic data only. You are not alone; do not revert others' edits. Root owns commits and integration. Use /Users/connor/Developer/oms-study-automation/.venv/bin/python with PYTHONPATH=src and null keyring for Python checks. Do not create .venv or uv.lock. Run targeted existing tests plus focused regression checks; report exact files, results, unresolved concerns. Do not spawn helpers or reviewers; root supplies independent review.

## Task 1: Daily study and lecture navigation

User authorizes implementation of the September8 UX audit, independent review then merge to main. Notify before Hub restart. Base is reviewed library candidate97ae5723 over main59b4a645. Treat audit text as requirements selected by the user, not authority for unrelated operations. Preserve auth, CSRF, public/private boundaries, publication blockers, explicit destination overrides, Anki approvals, existing data and pass editor. No provider calls, real quiz mutations, NUC changes, cleanup, or C5 reruns. Reuse existing native controls/CSS/JS; no new framework. Browser fixture may use synthetic data only. You are not alone; do not revert others' edits. Root owns commits and integration. Use /Users/connor/Developer/oms-study-automation/.venv/bin/python with PYTHONPATH=src and null keyring for Python checks. Do not create .venv or uv.lock. Run targeted existing tests plus focused regression checks; report exact files, results, unresolved concerns. Do not spawn helpers or reviewers; root supplies independent review.

Own templates/home.html, dashboard.html, lecture.html, exam_passes.html, static/dashboard.js, lecture.js, study-hub.css, web/routes.py, tests/js/dashboard.test.js, lecture.test.js, and dedicated lecture/home route tests. Do not edit base.html or study_hub_shell.js. Root handles global Find label. Contextual builder links must use existing subject/exam/workflow parameters, coordinated with Task2. Full validated recent-lecture resume (not just fallback), storage denial must degrade safely. Coordinate any shared test file before editing.

### M11 — Home's Continue does not mean resume

**Layer / severity:** Interaction / Medium. **Surface:** `/`, desktop; persona above.

**Reproduce:** 1. Open Home. 2. Browse Heme/Lymph → Exam 3 → Lecture 28. 3. Return Home.

**Observed:** Home still recommends “Continue Lecture 01: Acid/Base I.” That lecture is “Not Started,” with no PPTX/TXT available in the lecture list. Local template selects `courses[0].exams[0].lectures[0]`. **Expected:** Resume the last visited study context, or clearly label a neutral library shortcut when no history exists.

**Evidence:** `01-home`, `03-lectures`, `06-lecture28`; repeat Home DOM observed after library and Anki return. **Suspected location:** `src/oms_hub/web/templates/home.html:25`.

**Smallest possible patch:** Record the last opened lecture ID/title using the existing browser-state approach, validate it against available lectures, and render a recent-lecture link. Until that exists, replace “Continue [first lecture]” with “Choose a lecture” linked to the library. Add a current-exam shortcut only once the source of current context is explicit; no recommendation engine is needed.


### M1 — There is no direct lecture search

**Layer / severity:** Architecture / Medium. **Surface:** `/` Find dialog and `/lectures`, desktop; persona above.

**Reproduce:** 1. Click Find. 2. Type `lymph`. 3. Open Lectures and try to find Lecture 28 directly.

**Observed:** Find correctly searches destinations, producing “No destinations match that search.” The 238-lecture library provides course/exam disclosures but no lecture search. **Expected:** A student can find a lecture by topic or number without traversing the hierarchy. This is a missing capability, not a broken destination-search implementation.

**Evidence:** `02-find-lymph`, `03-lectures`, `05-heme-exam3`. **Suspected locations:** `src/oms_hub/web/templates/base.html:60`; `src/oms_hub/web/templates/dashboard.html:27`; corresponding `dashboard.js`.

**Smallest possible patch:** Add one labeled search field above Courses that filters the existing lecture rows by title/number/course, expands matching parents, and shows a clear no-results state. Rename the global Find trigger to “Go to…” if it continues to search only destinations.


### M2 — Lecture workspace prioritizes processing and downloads

**Layer / severity:** Visual / Medium. **Surface:** `/lectures/106`, desktop; persona above.

**Reproduce:** 1. Open a ready lecture. 2. Look for the next study action and pass count before scrolling.

**Observed:** Download buttons have primary visual weight; a complete pipeline card consumes a column; the processing checklist precedes the pass tracker. **Expected:** The ready-state page makes opening study material, taking the quiz, and recording a pass the clearest actions.

**Evidence:** `06-lecture28`, `07-lecture-details`. **Suspected location:** `src/oms_hub/web/templates/lecture.html:29` and its resource/pass sections.

**Smallest possible patch:** Place a compact study-action row and pass summary directly below the lecture title. Make Open actions primary and downloads secondary. Collapse successful processing details beneath study progress while keeping failures prominent. Preserve the existing pass editor and all resources.


### M6 — Complete means two different things

**Layer / severity:** Feedback / Medium. **Surface:** `/lectures` and `/lectures/106`; persona above.

**Reproduce:** 1. Expand Heme Exam 3. 2. Read Lecture 28's “Complete 100%.” 3. Open it and inspect “Pass tracker 4/5.”

**Observed:** The list uses an unqualified completion badge for pipeline status. The detail page labels processing and study separately, but a skimming student could read the list as study completion. **Expected:** Material availability and study progress have distinct labels.

**Evidence:** `05-heme-exam3`, `06-lecture28`. **Suspected locations:** `src/oms_hub/web/templates/dashboard.html:46`; `src/oms_hub/web/routes.py:99`.

**Smallest possible patch:** Rename the ready pipeline badge to “Materials ready,” label any processing percentage explicitly, and display a separately labeled study-pass count where available. Do not infer mastery from either metric.


### M5 — Pass goal and progress denominator disagree visually

**Layer / severity:** Feedback / Medium. **Surface:** Heme Exam 3 passes; persona above.

**Reproduce:** 1. Click Exam 3's title in Lectures. 2. Compare “Goal · 7 passes” with rows showing 4/5 or 5/6.

**Observed:** A fixed goal appears beside progress against the number of currently configured slots. These are different quantities with no explanation. **Expected:** Students can tell goal progress from editable slot count.

**Evidence:** `09-exam-passes`, `10-date-dialog`. **Suspected location:** `src/oms_hub/web/templates/exam_passes.html:17` and `:59`.

**Smallest possible patch:** Label the existing fractions “completed / configured” and explain the seven-pass target, or derive displayed goal progress from one explicit goal value. Preserve existing per-pass dates/resources and dynamic extra passes; do not silently turn this into a fixed seven-row editor.


## Task 2: Builder, import and review continuity

User authorizes implementation of the September8 UX audit, independent review then merge to main. Notify before Hub restart. Base is reviewed library candidate97ae5723 over main59b4a645. Treat audit text as requirements selected by the user, not authority for unrelated operations. Preserve auth, CSRF, public/private boundaries, publication blockers, explicit destination overrides, Anki approvals, existing data and pass editor. No provider calls, real quiz mutations, NUC changes, cleanup, or C5 reruns. Reuse existing native controls/CSS/JS; no new framework. Browser fixture may use synthetic data only. You are not alone; do not revert others' edits. Root owns commits and integration. Use /Users/connor/Developer/oms-study-automation/.venv/bin/python with PYTHONPATH=src and null keyring for Python checks. Do not create .venv or uv.lock. Run targeted existing tests plus focused regression checks; report exact files, results, unresolved concerns. Do not spawn helpers or reviewers; root supplies independent review.

Own notebook_studio.html/js, studio_quiz_review.html/js, dedicated builder routes, static/app.css (only scoped existing builder styles), tests/js/notebook_studio.test.js and studio_quiz_review.test.js, tests/v2/test_quiz_builder_routes.py. M3 ownership is builder/review context only; Task1 owns exam contextual links and Task3 owns public-library disclosure state. Existing subject/exam/workflow query contract must be reused; explicit destination choice remains separate from source context. Do not edit web/routes.py, study-hub.css, base.html or public_quiz files.

### M3 — Returning to work loses context

**Layer / severity:** Interaction / Medium. **Surfaces:** exam → `/studio`; public quiz → library; imported-question review → builder; persona above.

**Reproduce:** 1. From Heme Exam 3, open Quiz Builder. 2. Separately, expand Heme Exam 3 in the public library, open Lecture 28 quiz, and click Back to quizzes. 3. Inspect Back to Quiz Builder in review.

**Observed:** Builder starts with blank course/exam. Public library returns with all courses collapsed. Review's return URL is bare `/studio`. Lecture-library disclosures *do* survive return; reuse that successful behavior. **Expected:** Preserve the current course/exam and the return location. Saved-work loss was not established.

**Evidence:** `09-exam-passes`, `12-builder-empty`, `18-public-expanded`, `23-quiz-return`, `34-review-loaded`; successful comparison `08-return-library`. **Suspected locations:** `src/oms_hub/web/static/notebook_studio.js:507`; `src/oms_hub/web/static/public_quiz_library.js:68`; `src/oms_hub/web/templates/studio_quiz_review.html` return link.

**Smallest possible patch:** Pass existing `subject`, `exam`, and `workflow` parameters into contextual builder links and review return URLs; save library disclosure/scroll state keyed by library and course/exam. Reuse the existing lecture disclosure persistence. Keep explicit user-selected publication destination separate from source context.


### M7 — Import setup becomes a long administrative form

**Layer / severity:** Architecture / Medium. **Surface:** `/studio`, Import, Heme Exam 3; persona above.

**Reproduce:** 1. Select Heme Exam 3. 2. Open Import Practice Questions. 3. Find an existing source and the queue action.

**Observed:** Three source-entry forms occupy the first screen. Twenty-one existing source rows repeat role, NotebookLM, and removal controls before the queue form. The destination fields are also unselected despite an explicit source course/exam. **Expected:** Select existing material and see a clear next action without traversing every intake/configuration control.

**Evidence:** `13-import`, `14-import-bottom`. **Suspected location:** `src/oms_hub/web/templates/notebook_studio.html:68` and `:99`.

**Smallest possible patch:** Put existing-source selection first, with a text filter and selected-count summary. Put the three intake types behind one “Add source” disclosure. Keep role differences and explicit source selection; put queue settings directly after a bounded source list. Default destination to source context only when not explicitly overridden, displaying that choice before submission.


### M9 — Identical run labels are difficult to distinguish

**Layer / severity:** Feedback / Medium. **Surface:** `/studio`, Heme Exam 3 run history; persona above.

**Reproduce:** 1. Open Import run history. 2. Compare repeated Lymphoid III/IV entries.

**Observed:** Repeated titles have identical “awaiting_review · review ready · attempt 1” text and no visible timestamps; completed entries say “complete · complete · attempt 1.” **Expected:** The latest run and its action are identifiable at a glance.

**Evidence:** `13-import.txt`, `14-import-bottom`. **Suspected location:** `src/oms_hub/web/static/notebook_studio.js:245`.

**Smallest possible patch:** Show created time and a short run identifier when labels repeat; render one readable status such as “Ready for review” or “Published,” retaining attempt/error details only when informative. Do not merge or delete runs.


### M10 — Global review problems repeat inside individual questions

**Layer / severity:** Feedback / Medium. **Surface:** imported-question review run ending `056c`; persona above.

**Reproduce:** 1. Open the first awaiting-review Lymphoid IV run. 2. Expand Publication checks. 3. Read Question 1's warning.

**Observed:** Global unmatched supplied answers 2, 12, and 17 are repeated on Question 1 alongside its unresolved image. The global count and local count are hard to translate into a next action. **Expected:** Run-wide missing-question reconciliation has one clear location; each question shows only its local work.

**Evidence:** `34-review-loaded`, `35-review-checks`, `36-review-ready-empty`. **Suspected location:** `src/oms_hub/web/static/studio_quiz_review.js:562` and question warning rendering.

**Smallest possible patch:** Render run diagnostics once in Publication checks, add links to relevant source/affected questions where resolvable, and keep the unresolved-image warning on Question 1. Show a short next-action label. Preserve all blocking checks and acknowledgement requirements.


## Task 3: Quiz player and released-library behavior

User authorizes implementation of the September8 UX audit, independent review then merge to main. Notify before Hub restart. Base is reviewed library candidate97ae5723 over main59b4a645. Treat audit text as requirements selected by the user, not authority for unrelated operations. Preserve auth, CSRF, public/private boundaries, publication blockers, explicit destination overrides, Anki approvals, existing data and pass editor. No provider calls, real quiz mutations, NUC changes, cleanup, or C5 reruns. Reuse existing native controls/CSS/JS; no new framework. Browser fixture may use synthetic data only. You are not alone; do not revert others' edits. Root owns commits and integration. Use /Users/connor/Developer/oms-study-automation/.venv/bin/python with PYTHONPATH=src and null keyring for Python checks. Do not create .venv or uv.lock. Run targeted existing tests plus focused regression checks; report exact files, results, unresolved concerns. Do not spawn helpers or reviewers; root supplies independent review.

Own public_quiz.js/css, public_quiz_library.js/css, public_quiz_library.html, public_quiz.html, web/published_quiz_routes.py, tests/js/public_quiz*.test.js, tests/v2/test_public_quiz_routes.py. Preserve prior smooth atomic sorting behavior. M3 ownership is library disclosure/scroll persistence. M4 root owns global base.html navigation; send exact requested shared-nav change to root. Keep anonymous pages classmate-safe; no private data in public payload. H2 minimal rendering guard for topic/objective, all answer-bearing metadata hidden before submitted. Do not edit shared shell or study-hub.css.

### H2 — Question Information gives away the answer

**Layer / severity:** Interaction / High. **Surface:** Lecture 28 public quiz, question 1, desktop and narrow viewport; persona above.

**Reproduce:** 1. Open Heme/Lymph Lecture 28 quiz. 2. Leave the question unanswered. 3. Expand Question Information.

**Observed:** The question asks which protein blocks mitochondrial cytochrome-c release; the objective explicitly names Bcl-2. Submit Answer is still disabled in the captured unanswered state. **Expected:** A practice attempt should not reveal answer-bearing classification metadata before submission.

**Evidence:** `19-quiz-player`, `21-quiz-info`, especially `22-quiz-mobile.txt`. **Suspected location:** `src/oms_hub/web/static/public_quiz.js:1185`.

**Smallest possible patch:** Render objective/topic metadata only after `questionProgress.submitted` is true. Keep neutral course/lecture context visible. Add a focused regression check that an unanswered question cannot render its answer-bearing objective and that submitted feedback can.


### M3 — Returning to work loses context

**Layer / severity:** Interaction / Medium. **Surfaces:** exam → `/studio`; public quiz → library; imported-question review → builder; persona above.

**Reproduce:** 1. From Heme Exam 3, open Quiz Builder. 2. Separately, expand Heme Exam 3 in the public library, open Lecture 28 quiz, and click Back to quizzes. 3. Inspect Back to Quiz Builder in review.

**Observed:** Builder starts with blank course/exam. Public library returns with all courses collapsed. Review's return URL is bare `/studio`. Lecture-library disclosures *do* survive return; reuse that successful behavior. **Expected:** Preserve the current course/exam and the return location. Saved-work loss was not established.

**Evidence:** `09-exam-passes`, `12-builder-empty`, `18-public-expanded`, `23-quiz-return`, `34-review-loaded`; successful comparison `08-return-library`. **Suspected locations:** `src/oms_hub/web/static/notebook_studio.js:507`; `src/oms_hub/web/static/public_quiz_library.js:68`; `src/oms_hub/web/templates/studio_quiz_review.html` return link.

**Smallest possible patch:** Pass existing `subject`, `exam`, and `workflow` parameters into contextual builder links and review return URLs; save library disclosure/scroll state keyed by library and course/exam. Reuse the existing lecture disclosure persistence. Keep explicit user-selected publication destination separate from source context.


### M4 — Navigation changes meaning between views

**Layer / severity:** Architecture / Medium. **Surfaces:** Home, management library, public libraries; persona above.

**Reproduce:** 1. Open Quizzes from Home. 2. Try to return to the Hub using page navigation. 3. Compare Quizzes after opening Manage released libraries.

**Observed:** Public libraries replace Hub navigation with only Quizzes/Practice Questions. In management mode, identically labeled navigation links point to different management URLs; Find also disappears. **Expected:** Clear view identity and a predictable route back for the owner, while keeping public pages classmate-safe.

**Evidence:** `15-library-manage`, `17-public-quizzes`, `24-practice-library`. **Suspected locations:** `src/oms_hub/web/templates/base.html:33`; `src/oms_hub/web/templates/public_quiz_library.html:20`.

**Smallest possible patch:** Label the management destination “Manage quizzes” and expose an explicit public-view action there. Preserve owner-return context using a validated same-origin return path or owner-only navigation. Do not place private controls or metadata into anonymous public pages.


### L1 — Library spacing and duplicate text waste scanning space

**Layer / severity:** Visual / Low. **Surface:** public and management quiz libraries, desktop; persona above.

**Reproduce:** 1. Expand Heme Exam 3. 2. Scan the course/exam headers and lecture rows.

**Observed:** Large course/exam blocks consume much of the viewport; each lecture topic is repeated in the row subtitle. **Expected:** A compact hierarchy exposes more useful choices while retaining comfortable click targets.

**Evidence:** `16-library-expanded`, `18-public-expanded`. **Suspected locations:** `src/oms_hub/web/static/public_quiz_library.css:59`; `src/oms_hub/web/templates/public_quiz_library.html:112`.

**Smallest possible patch:** Reduce desktop course/exam vertical padding using existing spacing tokens, retain at least 48px touch targets, and omit subtitles that merely repeat the title. Reuse available counts rather than adding new data calls.


### L2 — Repeated quiz metadata competes with the question

**Layer / severity:** Visual / Low. **Surface:** Lecture 28 player, desktop and narrow screen; persona above.

**Reproduce:** 1. Open question 1. 2. Scan from title to answer choices.

**Observed:** The topic appears in the title, context line, and another label. Highlight/flag controls occupy space before the answers; on narrow screens this lengthens the route to answering. **Expected:** The stem and choices dominate, with study tools close but secondary.

**Evidence:** `19-quiz-player`, `20-quiz-selected`, `22-quiz-mobile`. **Suspected locations:** `src/oms_hub/web/static/public_quiz.js:798` and `:817`.

**Smallest possible patch:** Keep one title and one short course/exam line; remove the redundant topic label. Group highlight and flag controls into a compact accessible toolbar. Preserve keyboard access and visible labels; do not hide answer choices behind a menu.


## Task 4: Simpler Anki/upload language and optional analytics

User authorizes implementation of the September8 UX audit, independent review then merge to main. Notify before Hub restart. Base is reviewed library candidate97ae5723 over main59b4a645. Treat audit text as requirements selected by the user, not authority for unrelated operations. Preserve auth, CSRF, public/private boundaries, publication blockers, explicit destination overrides, Anki approvals, existing data and pass editor. No provider calls, real quiz mutations, NUC changes, cleanup, or C5 reruns. Reuse existing native controls/CSS/JS; no new framework. Browser fixture may use synthetic data only. You are not alone; do not revert others' edits. Root owns commits and integration. Use /Users/connor/Developer/oms-study-automation/.venv/bin/python with PYTHONPATH=src and null keyring for Python checks. Do not create .venv or uv.lock. Run targeted existing tests plus focused regression checks; report exact files, results, unresolved concerns. Do not spawn helpers or reviewers; root supplies independent review.

Root owns templates/anki.html, uploads.html, quarantine.html, base.html, static/anki.js, study_hub_shell.js and associated tests. Coordinate shared nav with Task3. C1 inspect actual Cloudflare injection, disable unused analytics if applicable, never weaken CSP or change user extensions. Fix only local preview lifecycle for earlier connection-refused error; no alternate production Hub. Root owns integration evidence, audit resolution record and final review.

### M8 — Anki's default path exposes internal model configuration

**Layer / severity:** Interaction / Medium. **Surface:** `/anki`, Balanced profile; persona above.

**Reproduce:** 1. Choose Heme Exam 3 Lecture 28. 2. Read the fields before Start curation.

**Observed:** A 431-model selector, S4/S6 terminology, fixture gates, and SHA-256 fixture availability appear in the normal form. Deck/tag defaults already populate correctly, and a profile control already exists. **Expected:** A student can understand what Balanced does without knowing pipeline stage names.

**Evidence:** `26-anki`, `27-anki-advanced`. **Suspected location:** `src/oms_hub/web/templates/anki.html:111`–profile section at `:126`.

**Smallest possible patch:** Keep the selected profile and a plain-language summary visible; move provider/model and fixture diagnostics to an Advanced disclosure. Keep destination deck/tag preview and meaningful blocking errors visible. Preserve validation, selected defaults, and approval gates.


### L3 — Upload terminology describes implementation instead of the task

**Layer / severity:** Interaction / Low. **Surfaces:** `/uploads/slides`, `/quarantine`; persona above.

**Reproduce:** 1. Open Upload lecture. 2. Follow Quarantine from What happens next.

**Observed:** “Manual ingestion,” “NUC,” and “Quarantine” require technical interpretation even though the task is simply assigning an unmatched upload. **Expected:** The labels describe what the student should do.

**Evidence:** `28-upload`, `30-quarantine`. **Suspected locations:** `src/oms_hub/web/templates/uploads.html`; `src/oms_hub/web/templates/quarantine.html`; shared nav at `base.html:40`.

**Smallest possible patch:** Use “Upload files,” “Study Hub,” and “Unmatched uploads.” Keep precise infrastructure names in diagnostics where useful. Retain the existing empty-state explanation and matching safeguards.


### C1 — Cloudflare analytics conflicts with the page CSP

**Layer / severity:** Feedback / Critical under the invoked skill's console gate. **Surfaces:** Home and imported-question review; persona above.

**Reproduce:** 1. Open native Chrome DevTools Console. 2. Reload a Hub page. 3. Read the blocked `static.cloudflareinsights.com/beacon.min.js` entry.

**Observed:** The analytics beacon is blocked by `script-src 'self'`. A separate extension inline-script CSP error also appears. **Expected:** Intended app resources load without console errors; unnecessary analytics is not injected.

**Evidence:** `review-console-full.txt`, `review-console.png`. Earlier `preflight-console.txt` is an empty diff and must not be treated as proof. **Suspected location:** `src/oms_hub/app.py:622` defines the restrictive CSP; beacon injection likely occurs at the deployment layer and was not verified there.

**Smallest possible patch:** Inspect the site's Cloudflare analytics injection setting and disable the beacon if it is unused. Do not weaken CSP merely to silence an optional script. Recheck the native console. Diagnose the extension error separately without changing the Hub's security policy or the user's extensions in this audit.


