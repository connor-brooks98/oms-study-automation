# Future Implementation Ideas

This is the running backlog for ideas to consider after the current work. An entry records intent, not approval to implement it.

## Ideas

### 2026-10-08 — Clinical Notebook becomes the primary notebook application

**Status:** Direct Hub-to-Clinical Notebook file transfer requested now; full NotebookLM replacement is a future step.

Connor intends Clinical Notebook to eventually overtake NotebookLM. The current cleanup should make Hub file completion independent of notebook delivery, keep ExtendLM available for NotebookLM, and add a seamless, explicit file-transfer API to Clinical Notebook.

Future behavior: move the primary notebook workflow to Clinical Notebook after its file import, source provenance, reading/indexing, and everyday study flows are accepted. Preserve original lecture files, cleaned transcripts, course/exam mapping, and source identity during any migration.

Decide when replacement is requested: migration of existing notebooks and sources, the default destination, cross-device access, retention of ExtendLM, source-grounded chat behavior, and whether any old remote content should be removed. This entry does not authorize a bulk migration, deletion of NotebookLM content, or retiring ExtendLM now.

### 2026-09-15 — NotebookLM resources for the Hub chatbot through ExtendLM MCP

**Status:** Idea; chatbot integration deferred, retain MCP support in the upload workflow

Connor wants the ExtendLM MCP connection to support the Hub chatbot as well as easy exam-folder uploads. Use uploaded lecture slides and transcripts for direct, cited answers; allow Connor to add textbooks as supplementary reference knowledge. If those resources do not answer a question, keep the existing GPT connection available as a fallback.

Core behavior: prefer the selected lecture/exam resources, distinguish textbook references, and clearly identify when an answer comes from GPT fallback rather than the uploaded sources. Preserve citations and source boundaries. One-time ExtendLM setup belongs in Settings; sending a lecture should require choosing its existing exam notebook. Connor clarified that all lectures for an exam can share that notebook; separate lecture notebooks and automated folder creation are unnecessary.

Decide during implementation: which resources to search for a given question, supported MCP retrieval/query tools and citation contracts, how textbook coverage is selected, when source evidence is insufficient and fallback should run, and how browser availability affects answers. This entry does not authorize implementing the chatbot or uploading textbooks now.

### 2026-09-13 — Optional Windows application control or app isolation

**Status:** Consider later; not approved for installation or configuration

Connor disabled Smart App Control himself after it blocked the private Codex build
and asked to retain free, open-source alternatives as a future idea. Consider
[AppControl Manager](https://github.com/HotCakeX/Harden-Windows-Security#appcontrol-manager)
for explicit application policies using Windows' enforcement engine, or
[Sandboxie](https://github.com/sandboxie-plus/Sandboxie) for isolating selected apps.
These serve different purposes; neither is required for GPT login or Hub delivery.

Decide during implementation: whether added protection is needed, current licensing
and feature limits, compatibility with local builds and Hub workers, maintenance
cost, and a reviewed audit/trial and rollback plan. Keep this separate from the
current runtime acceptance work; do not install tools or change host policy now.

### 2026-09-02 — Board competency tracker (NBOME COMLEX / USMLE)

**Status:** Idea

Use the NBOME COMLEX Level 1 blueprint and a separate USMLE Step 1 content framework for a board-prep tracker. The original "NBME Level 1" wording conflated two exams; corrected during 2026-09-11 planning. As practice questions are completed, update performance by competency or topic so strong and weak areas are visible and study time can be directed toward the areas needing the most work.

Core behavior:

- Represent the two official exam frameworks separately, with versioned topic identifiers.
- Associate each completed practice question with one or more competencies or topics.
- Support tagging authorized questions against the applicable exam framework.
- Update strength and weakness estimates from question results.
- Show the evidence behind each estimate, including question count and recent performance, so small samples are not overstated.
- Prioritize weak or under-practiced areas for focused board review.

**2026-09-15 refinement — Ora AI review:** Connor particularly likes Ora's Performance section. Use one personal attempt history with separate board and in-house exam views. Link supported UWorld/TrueLearn results and verified question identifiers to reviewed objectives, retaining source, product, date, and question version. Show right/wrong history, first attempts versus repeats, unique-question counts, recency, and untested objectives. Drill from a weak objective into focused practice and its supporting material. A bare provider QID does not establish objective coverage, and video/card completion does not establish question mastery. Keep NBOME competency/presentation dimensions and USMLE taxonomy separate. See the [feature review and screenshots](research/2026-09-15-ora-ai-feature-review.md).

Decide during implementation:

- Which official exam guide/version applies and how guide updates are handled.
- Whether question-to-topic mapping comes from the question source, user tagging, or automated classification.
- How recency, question difficulty, repeated questions, and minimum sample size affect strength estimates.
- Where the tracker belongs in the Study Hub and how it relates to existing practice-question history.
- Supported UWorld/TrueLearn result export or manual-entry fields; mapping review and correction workflow; which genuinely tested objectives receive evidence from a multi-topic question; and how the board and exam views share history without double-counting.

Candidate dataset inspected on 2026-09-02:

- Repository: `https://github.com/epaces2017-sketch/MyUWORLDQBANK`
- Snapshot: commit `82cde8725a0a4136928f666fc9ad9cde07714e62`
- `questions.json`: 3,671 unique USMLE Step 1-style medical questions; SHA-256 `7fe152c74584172262cf9a178b083a8b95a54afedca0ca322c0f1a5e495abeec`
- Not ingested. The repository provides no verified provenance or license for the apparent UWorld-derived content; use requires a source or export Connor is authorized to store and process.

### 2026-09-08 — Google AI cloud API integration

**Status:** Superseded as the required backend by the approved GPT direction on 2026-09-11; existing Google artifacts retained

Move the Hub's AI-backed study workflow toward Google AI cloud API access through the existing Sol 1-10 integrations while retaining its lecture, study-generation, review, and Anki workflows. Defer ExtendLM setup because it would likely be a short-lived intermediate integration.

Decide during implementation: the provider/API, authentication approach, source handling, migration sequence, and deployment requirements.

**2026-09-11 decision:** Connor approved GPT as the core provider with managed ChatGPT/Codex login preferred and Google/NotebookLM optional. The [approved architecture](superpowers/specs/2026-09-11-gpt-study-platform-design.md) and [orchestrated implementation plan](superpowers/plans/2026-09-11-gpt-study-platform.md) define the replacement. This planning decision does not establish deployment or authorize changing the running pipeline during plan preparation.

### 2026-09-11 — Lecture-image quizzes with optional outlines

**Status:** Idea; implementation deferred

Connor prefers the clinical-vignette flow and actual lecture images in the "Create Heme Exam 3 question set" task and rarely reads generated lecture outlines. Generate lecture quizzes directly from slides, cleaned transcripts, learning objectives, and professor emphasis; retain lecture/slide citations, correct-answer and distractor explanations, and actual source images when relevant. Consider making the student-facing outline optional rather than a routine deliverable.

The current lecture-quiz generation path does not consume an outline, but Anki curation currently requires a current outline PDF. Connor explicitly deferred Anki curation on 2026-09-11; its outline dependency should not block the proposed quiz-first workflow. Preserve existing curation artifacts and revisit its input requirements when curation resumes. Also decide objective coverage, image selection/review, provider validation, and whether a compact internal coverage map is useful.

Supplementary board practice may use verified question-bank records and AnKing question-ID mappings. Connor has school UWorld access and TrueLearn access for DO-focused practice; account ingestion/export and mapping methods remain undecided. The GitHub bank matches the local JSON, lacks its referenced images, and has unresolved answer selections; its historical IDs change between versions, so they are not verified UWorld/AnKing identifiers. Keep this separate from lecture-only quiz sourcing.

**2026-09-15 refinement — in-house objective performance:** Extract each lecture's explicit objectives with stable identity, source revision and slide citations; retain course/exam/lecture scope. Tag generated questions to the objectives actually tested, and track right/wrong results throughout exam preparation. Make every objective visible, including no-question coverage gaps and objectives not yet attempted. Offer focused practice and direct source review from each row. In Ora, Connor's 23-page Cardiac Cycle upload displayed five source objectives and 4 linked videos, 45 questions and 76 cards, but the inspected management view did not expose item-to-objective coverage. Borrow the source viewer and resource preview; improve them with visible mapping evidence and reviewable corrections. Decide objective extraction review, cross-lecture duplicates, source updates, exam-window filtering, and mapping corrections before implementation. Counts are not coverage proof.

### 2026-09-15 — Ora-inspired study flow refinements

**Status:** Ideas; implementation deferred

Connect performance drill-down, objective-filtered quizzes, source-linked answer review, and contextual tutoring. Reuse the approved GPT chat path with the selected lecture/question/objective as explicit context; do not add another AI backend. Consider a small daily session mixing weak, untested and due material within a chosen time budget and exam date, after objective tracking is trustworthy. Keep all objectives visible when narrowing today's workload. Anki remains the existing review system and its curation remains deferred.

Decide during implementation: which behaviors already exist in the integrated Hub, the smallest navigation changes, source-context visibility, and a transparent daily-practice selection rule. The [Ora review](research/2026-09-15-ora-ai-feature-review.md) is research, not approval to implement or deploy.

### 2026-09-15 — Optional NAS video references

**Status:** Low-priority idea; implementation deferred

Connor has an offline video library that may move to the NAS. Eventually link relevant videos or timestamps from lecture/objective pages. Start with a searchable metadata catalog and links to an existing media server/player; the Hub need not store or transcode the media itself.

Decide during implementation: final NAS/media-server location, supported browser playback and authentication, stable identifiers, local versus remote availability, and whether transcripts/resume/timestamp links materially help. Do not make video integration a dependency of quiz or performance work.

### 2026-09-11 — AMBOSS-assisted AnKing candidate selection

**Status:** Deferred by Connor on 2026-09-14; AMBOSS excluded from the current Hub release

Decision update (2026-09-14): do not incorporate AMBOSS for now. Retain this idea and
the existing inquiry history for possible later reconsideration; pursue no integration
or follow-up unless Connor asks to resume. Full Anki curation remains deferred.

Connor reports that AMBOSS AI accepts lecture slides and supporting materials and returns matching AnKing note IDs. Explore using those matches as the starting candidate set for a lecture deck instead of rebuilding all card retrieval in the Hub. AMBOSS publicly documents lecture uploads and Anki recommendations in AI Mode Learning; the exact note-ID export and programmatic access still need verification.

Core behavior: obtain AMBOSS recommendations for a lecture, resolve the returned IDs against the installed AnKing collection, and review relevance and objective coverage before any deck/tag/suspension changes. Start with a manual exported search or ID list if available.

Decide during implementation: supported export/API access, account requirements, identifier compatibility, note-to-card handling for cloze siblings, and whether the recommendations cover professor-specific material well enough. This idea does not authorize account uploads, Anki changes, or resuming curation implementation.

### 2026-09-08 — Study Hub UX cleanup

**Status:** Implementation recorded on 2026-09-09; verify current integrated behavior rather than duplicate the work

Make daily studying easier by improving resume behavior, lecture discovery, navigation continuity, and the prominence of study actions. The [16-item implementation resolution map](implementation/handoffs/2026-09-09-ux-audit-fixes.md) records the changes; the GPT migration treats these as regression requirements and checks the actual accepted base before proposing more work.

Start with useful Continue behavior, lecture search, hiding answer-bearing quiz metadata until submission, preserving course/exam context, and prioritizing study actions on lecture pages.

Decide during implementation: persistence scope for recent study context, final navigation labels/layout, pass-goal semantics, and the deployment source of blocked analytics. The audit records untested accessibility, performance, and write workflows separately; it does not authorize implementation or deployment.

### 2026-09-15 — Possible rented Linux server migration

**Status:** Deferred for several months by Connor; continue Windows worker implementation now.

Intent: potentially host the Study Hub on a rented server while keeping account access and exam notebooks available from any computer. Retain the existing worker/provider boundaries; do not implement migration infrastructure now.

Decide when migration resumes: hosting provider, Linux document conversion replacing Windows Office, persistent storage/backups, private access, and supported managed ChatGPT login on the chosen host.
