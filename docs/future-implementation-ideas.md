# Future Implementation Ideas

This is the running backlog for ideas to consider after the current work. An entry records intent, not approval to implement it.

## Ideas

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

Decide during implementation:

- Which official exam guide/version applies and how guide updates are handled.
- Whether question-to-topic mapping comes from the question source, user tagging, or automated classification.
- How recency, question difficulty, repeated questions, and minimum sample size affect strength estimates.
- Where the tracker belongs in the Study Hub and how it relates to existing practice-question history.

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

### 2026-09-11 — AMBOSS-assisted AnKing candidate selection

**Status:** Explore later; Anki curation deferred

Connor reports that AMBOSS AI accepts lecture slides and supporting materials and returns matching AnKing note IDs. Explore using those matches as the starting candidate set for a lecture deck instead of rebuilding all card retrieval in the Hub. AMBOSS publicly documents lecture uploads and Anki recommendations in AI Mode Learning; the exact note-ID export and programmatic access still need verification.

Core behavior: obtain AMBOSS recommendations for a lecture, resolve the returned IDs against the installed AnKing collection, and review relevance and objective coverage before any deck/tag/suspension changes. Start with a manual exported search or ID list if available.

Decide during implementation: supported export/API access, account requirements, identifier compatibility, note-to-card handling for cloze siblings, and whether the recommendations cover professor-specific material well enough. This idea does not authorize account uploads, Anki changes, or resuming curation implementation.

### 2026-09-08 — Study Hub UX cleanup

**Status:** Implementation recorded on 2026-09-09; verify current integrated behavior rather than duplicate the work

Make daily studying easier by improving resume behavior, lecture discovery, navigation continuity, and the prominence of study actions. The [16-item implementation resolution map](implementation/handoffs/2026-09-09-ux-audit-fixes.md) records the changes; the GPT migration treats these as regression requirements and checks the actual accepted base before proposing more work.

Start with useful Continue behavior, lecture search, hiding answer-bearing quiz metadata until submission, preserving course/exam context, and prioritizing study actions on lecture pages.

Decide during implementation: persistence scope for recent study context, final navigation labels/layout, pass-goal semantics, and the deployment source of blocked analytics. The audit records untested accessibility, performance, and write workflows separately; it does not authorize implementation or deployment.
