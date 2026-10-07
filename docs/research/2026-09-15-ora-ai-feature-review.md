# Ora AI feature review for Study Hub

Reviewed September 15, 2026. Research and future ideas only; no Hub implementation or deployment.

The highest-value thing to borrow is Ora's connected workflow: performance identifies a topic, the topic opens directly into practice, and question review brings explanations and related material together. For the Hub, make the learning objective the connection between those steps. Use one attempt history with two views: longitudinal board preparation and exam-specific lecture preparation.

## Scope and evidence

Inspected all ten primary study sections in the signed-in Chrome tab, representative topic/article/video/question details, preclinical setup and shared exams, Connor's newly uploaded Cardiac Cycle lecture, the COMLEX 1 OMM supplement dashboard, and account settings/profile/billing surfaces. Most detailed board inspection used USMLE Step 1. Other exam workspaces were listed, not exhaustively traversed. This is a feature review, not a full accessibility, medical-content, or account-security audit.

No answers, card ratings, chat messages, purchases, uploads by the agent, or settings changes were submitted. Connor uploaded the cardio lecture during the review. Opening AI Chat displayed a blank new conversation; no prompt was sent. Existing question review was opened and exited. Self-assessment results require completing the assessment and were not accessible. Video playback, AI response quality, scheduling behavior over time, and Anki synchronization were not tested. The tab was left on USMLE Step 1 Performance.

Screenshots and selected page observations are in [the evidence folder](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/). They document the inspected account and may contain study history or lecture material; they are local review evidence, not a public content bundle. Personal billing/profile details were not included in this report or saved as screenshots.

Evidence storage updated October 7, 2026 during local repository cleanup. The links above and below point to the preserved private archive on Connor’s Mac; the image and raw-text captures are not included in Git. The original report and captures also remain in the pre-cleanup repository snapshot.

## Page-by-page findings

| Visit | Page / workflow | Features observed and what works well | Limits or opportunity for the Hub |
|---|---|---|---|
| 1 | [Performance overview](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/01-performance.jpg) and [topic breakdown](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/02-topic-performance.jpg) | Activity totals, QBank correctness, date-filtered trends, completion by resource type, streak calendar, card-state distribution, system/subject breakdown, and exam/global views. Expandable topic rows expose times seen/correct/incorrect; links lead to topic study. | Strongest pattern to borrow. Put sample counts beside percentages without requiring expansion. Keep untested, weak, and stale evidence distinct. Historical attempts existed while the selected monthly graph was empty; that is a time-filter distinction, not proof of lost data. |
| 2 | [Topics and subtopics](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/03-topic-study.jpg) | Searchable hierarchy, breadcrumbs, resource counts and direct starts for questions, cards and videos at different levels. Followed an anemia weakness into the relevant topic. | A weakness becomes actionable with little navigation. Our equivalent should reach the precise lecture/board objective and its source, rather than stop at a broad system label. |
| 3 | [Board Dashboard](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/04-dashboard.jpg) | Today's Session, exam date, topic selection, workload setting, new/due counts, time estimates, and immediate start buttons. Upcoming-topic calendar entry and modify/snooze controls. | Reduces daily planning effort. The interface says selection uses performance and yield; that does not verify the scheduling algorithm. Calendar contents were not successfully inspected. Avoid hiding full objective coverage behind a high-yield preset. |
| 4 | [QBank builder](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/05-qbank.jpg) | Block size, new/due selection, topic tree and search, scheduling-state counts, and previous blocks with scores and Review. | Reuse our existing block/resume capabilities where present. Timed mode was disabled in the inspected builder; its functioning was not verified. |
| 5 | [Existing question review](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/06-block-review.jpg) | Question-status navigator, flags, notes/calculator/lab-value controls, retry mode, variants, split question/explanation view, correct and distractor explanations, and an explicit Learning Objective. Related Library, Flashcards and Videos tabs; contextual AI entry. A pathology example included attributed images. | Excellent place to connect a miss to remediation. The card tab described automatically assigning cards after misses. Borrow suggested follow-up, while preserving existing Anki scheduling and approval boundaries. Reviewing one example does not validate the whole bank. |
| 6 | [Flashcards](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/07-flashcards.jpg) | Deck/Card views, exam decks, nested subdecks, new/due counts, progress, and Anki sync entry. | Good organization; building a second scheduler is unnecessary for our Hub. Import/sync and card rating were not exercised. |
| 7 | [Library and article](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/08-library-article.jpg) | Search, suggested articles for today's session, taxonomy browsing, concise summary and detailed sections, jump navigation, image gallery, related videos, and resource starts. | Keep source material within reach of practice. Prefer our lecture slides/transcripts and supported references over writing a competing medical encyclopedia. Clinical correctness of the article was not audited. |
| 8 | [Self-Assessments](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/09-assessment.jpg) | Step 1, Step 2 and shelf options. The Step 1 detail lists 160 questions, eight 20-question blocks, 30 minutes each, and a single attempt. Instructions describe category/block results, timing and review after completion. | Borrow practice structure and review filters. The described readiness/pass estimate was not accessible or validated. Do not imply this matches an official exam's full format. |
| 9 | [AI Chat](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/10-ai-chat.jpg) | Conversation history, attachments, dictation, concise option, study/performance/help prompts, daily quota, and contextual entry from study material. | Context is more useful than another standalone chat destination. Response quality, citations and access to accurate performance calculations remain untested. |
| 10 | [Videos](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/12-videos.jpg) and [video detail](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/13-video-detail.jpg) | Search and topic/provider filters; popular/history/unseen/bookmarked views. Detail includes source link, embedded YouTube player, watched/bookmark controls, topic summaries and related question/card starts. Multiple outside providers appeared in the catalog. | Useful discovery and linking. A provider listing does not establish access to its whole proprietary library. Playback was not tested; the selected detail thumbnail remained a placeholder during inspection. |
| 11 | [Games](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/11-games.jpg) | Pokémon or Drug, Survival, Doctordle, Question of the Day, Buzzwords and DDx Doc. | Menu inspected, games not played. Low relevance to the requested objective-tracking workflow. |
| 12 | [Preclinical entry](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/14-preclinical.jpg) and [shared school exam](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/15-shared-exam.jpg) | Add documents/exams, shared school exams, lecture selection, dates and resource counts. A populated shared neuroanatomy exam showed five lecture rows and related study actions. | Reusable course/exam/lecture organization. Some shared-source viewing was locked. Mapping output was visible, but extraction accuracy could not be established from shared resources. |
| 13 | [Cardiac Cycle upload](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/16-cardio-lecture.jpg), [source viewer](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/17-cardio-document.jpg), [Manage Content](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/18-cardio-linked-content.jpg) | The uploaded 23-page PPTX was available under Cardio 1, with 4 videos, 45 QBank items and 76 cards. Original slides render beside document-specific chat. Study actions remain accessible in the viewer. Manage Content previews the 125 linked resources and provides removal controls. | Especially relevant. Five objectives are visible on the first source page, but the inspected resource-management interface does not expose item-to-objective or slide-citation mapping. Assignment is proven; whether every item was generated, retrieved, or mixed is not. Full correctness and coverage were not validated. |
| 14 | [Settings](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/19-settings.jpg), profile and billing | Study weekdays/timezone; QBank theme, retries, auto-submit, tutor hints, default block size, time accommodations and objective placement. Card display/response choices, reminder controls, high contrast and sensitive-image controls. Advanced section separates scheduling reset from response history in its description. Profile and billing provide conventional account management. | Borrow useful accessibility and study controls selectively. None were changed; billing actions were not followed. Help was listed in account navigation but its support interaction was not exercised. |
| 15 | [COMLEX 1 OMM Supplement](/Users/connor/Developer/repo-archives/oms-study-automation-pre-review-2026-10-07/private-research-evidence/ora-ai-2026-09-15/20-comlex1.jpg) | Separate OMM workspace with a question session and topic groups covering foundations, techniques, spine/extremities, ribs, pelvis, cranial, autonomics, Chapman points, lymphatics and clinical applications. | This is explicitly an OMM supplement, not evidence that Ora exposes the entire NBOME blueprint. Our board taxonomy should cover the official framework, including its distinct dimensions. |

Some screenshots show the hover-expanded sidebar over the page edge. That is capture/navigation state, not an established layout defect. No broad usability pass/fail claim is made.

## What Ora does well

1. **Connects assessment to the next study action.** Topic performance links into the same hierarchy used for practice and reference material. The useful product behavior is the short route from a weakness to work on that weakness.
2. **Brings remediation into question review.** Explanation, objective, images and related material sit together. A student does not have to manually search every resource after a miss.
3. **Turns an uploaded lecture into an actionable study page.** The cardio example has visible resource counts, previews, source access and study starts in one place.
4. **Makes today's workload concrete.** Counts and approximate time are easier to act on than an undifferentiated content library. Our implementation should also show how much of the full objective set remains uncovered.
5. **Maintains context across resource types.** A topic can lead to questions, cards, reference text or video. Document chat is particularly relevant to our lecture workflow.
6. **Offers practical study preferences.** Time accommodations, high contrast, explanation placement and study-day controls are useful details, even if we should avoid copying every setting.

## Recommended Hub features, in priority order

### 1. Objective performance with separate board and in-house views

Use a shared record of attempts, with filters that answer two different questions:

| View | Organizing structure | Main question |
|---|---|---|
| Boards | Official framework/version → competency or topic → linked questions and attempts | What am I persistently weak in across board preparation? |
| In-house | Course → exam → lecture/revision → exact learning objective → questions and attempts | Which objectives need work before this exam, and which have never been tested? |

For terminology, COMLEX Level 1 uses **NBOME**; USMLE Step 1 has its own framework. Keep the taxonomies separate and use explicit crosswalks where appropriate. NBOME describes both competency and clinical-presentation dimensions. [NBOME Level 1](https://www.nbome.org/assessments/comlex-usa/level-1/), [USMLE Step 1 content outline](https://www.usmle.org/exam-resources/step-1-materials/step-1-content-outline-and-specifications).

Each objective should show: first-attempt right/wrong counts, unique questions seen, repeat performance separately, recent history, last practiced date, and source breakdown. Separate no evidence, limited evidence and demonstrated difficulty. A missed question reviewed three times should not become four independent demonstrations of mastery. Watching a video or reading a slide is activity, not a correct answer.

For UWorld/TrueLearn, begin with supported result exports or manual result/QID entry once actual samples are available. Results can be tracked without importing complete question content. Preserve provider/product IDs and reviewed mappings; a QID alone does not reveal what a question tests. Do not pool provider questions and generated lecture questions as though their difficulty is interchangeable.

### 2. Visible lecture-objective coverage

Extract the complete objective list with stable identifiers and source citations. Attach questions to the objectives actually tested; incidental vignette details should not award performance evidence to unrelated objectives. Let a user inspect and correct the mappings.

For the uploaded cardio lecture, show five objective rows and how the assigned/generated questions cover each one. A useful row could read: **Heart-sound mechanisms — limited evidence — 2/5 first attempts correct — last practiced yesterday — Review source / Practice objective.** These numbers are an illustrative design example, not Ora results.

Show objectives with no available questions as coverage gaps, and available-but-unattempted objectives separately. Topic inference can suggest subtopics, but must not silently replace the professor's explicit objective list. Broad objectives may require more than one question to cover their different parts.

### 3. One-click targeted practice and source-linked review

From any objective, offer focused questions, incorrect/flagged review and relevant source passages or figures. Keep course/exam selection intact when returning from review. The review screen should display the tested objective, correct-answer reasoning, all distractor explanations and resolvable slide citations after submission. Hide answer-bearing objective labels in the pre-answer view where they would give away the answer.

This mostly strengthens the presentation of our existing planned quiz/objective/citation model; it does not require a new question platform.

### 4. Contextual AI tutoring

Reuse the approved GPT chat path. Open chat with the selected lecture, question, answer state and objective as explicit context. Useful actions include explaining a miss, comparing two distractors, or locating the relevant slide. Make lecture-source versus general/reference mode visible. Performance summaries should use stored calculations; the model should explain those values, not invent scores.

The document viewer beside chat is worth borrowing. A second independent chatbot/backend is unnecessary.

### 5. A small daily practice plan

After reliable tracking exists, assemble a manageable session from weak objectives, untested objectives and due follow-up. Use exam date and a chosen time budget, with a short explanation of why each objective appears. Reuse existing resume/custom-block behavior before introducing a new planner.

The cardio assignment illustrates why workload controls matter: Ora estimated about 2 hours 15 minutes for 45 questions, plus about 19 minutes for 76 cards. Those are interface estimates, not measured study times. One uploaded lecture should not force all associated material into today's session.

### 6. Optional resource links, including NAS video

Start with title, topic/objective, duration and a stable link into the existing NAS/media player. Later consider timestamps, transcripts and resume state if useful. The video files can remain on the NAS; the Hub does not need to become a storage/transcoding service. Hosting, authentication, browser playback and remote access remain future decisions, not verified capabilities.

Likewise, preserve Anki as the existing review system. Related-card suggestions may eventually be useful, but do not turn this research into resuming deferred curation, automatic unsuspension or a second flashcard scheduler.

## What to ignore or postpone

- **A competing general medical question bank or encyclopedia.** Our advantage is lecture grounding and combining trustworthy results from existing resources. A large item count does not establish quality or professor-specific coverage.
- **Games and competitive gamification.** They can be enjoyable, but do not solve the requested exam-prep problem. A simple optional activity history is sufficient initially.
- **Unvalidated readiness and pass-probability scores.** Show observed evidence and official assessment results when supplied; do not invent a probability from local quiz percentages.
- **Decorative radar charts as the primary diagnostic view.** A sortable objective table with counts, recent misses and direct practice actions is more actionable. A chart can be added if it answers a real question.
- **Completion as mastery.** Resource counts, streaks and repeated correct answers are different signals. Do not combine them into one unexplained strength score.
- **Opaque high-yield filtering or automatic assignment of large review piles.** Prioritize today's work without suppressing objectives or implying that unpracticed content is covered.
- **A new SRS, Anki replacement, or full NAS streaming stack.** Reuse existing systems; postpone these until the core quiz/performance loop works and there is a concrete unmet need.

## Public research and how much weight to give it

Ora's product explainer describes adaptive daily planning, question variants, linked cards, FSRS, Anki import and interactive video recall. These help explain the connected interface, but are vendor descriptions; this review did not verify import fidelity, long-term scheduling or video recall. Public quota descriptions differed from the inspected account, so no fixed access promise is inferred. [Ora's product explainer](https://www.oraai.com/blog/what-is-ora).

Two Ora-specific Reddit threads give mixed, sparse feedback. In a July 2025 thread, a study participant liked the adaptive workflow but criticized question simplicity and card quality; company replies acknowledged earlier quality concerns and described improvements. An August/September 2026 thread contained praise for integrated resources and a positive longer-term anecdote, alongside questions about reliability. These are self-selected reports, not a representative comparison, and older comments may describe an earlier product. Founder comments are company statements. [2025 discussion](https://www.reddit.com/r/Step2/comments/1lwk6op/is_ora_ai_useful/), [2026 discussion](https://www.reddit.com/r/Step2/comments/1w10rhm/ora_ai_has_anybody_used_it/).

Ora's research brief reports 155 enrolled students, 121 in its per-protocol analysis, and 51 long-term respondents. It labels the work pending peer review. The Step 1 pass comparison was 13/13 versus 10/11 and not statistically significant; long-term scores were self-reported. Its reported Step 2 comparison uses predominant exposure. The short intervention and follow-up limitations prevent treating the headline as established superiority for our use case. The separately linked full-preprint URL could not be retrieved during this review. [Ora research brief](https://www.oraai.com/research/rct).

My assessment: Ora is useful product inspiration for connected practice and remediation. This evidence does not justify replacing UWorld/TrueLearn or using Ora's marketing outcomes as validation of the Hub's future analytics.

## Relationship to existing Hub plans

The [September 11 architecture](../superpowers/specs/2026-09-11-gpt-study-platform-design.md) already specifies stable objectives, cited/image-grounded quizzes, separate provider attempts, reviewed tags, first/repeat distinctions, and a shared GPT chat path. This review adds concrete interface references and clarifies the two performance views; it should refine that work rather than launch a parallel architecture. The document is planning evidence, not proof that every capability is currently deployed.

Updated the existing board-tracker and lecture-quiz entries in [future implementation ideas](../future-implementation-ideas.md), plus deferred study-flow refinements and low-priority NAS links. No changes to Hub application code, the active runtime or existing unrelated work were required.
