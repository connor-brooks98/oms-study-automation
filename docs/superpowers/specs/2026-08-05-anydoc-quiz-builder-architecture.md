# Anydoc and Quiz Builder Architecture

**Status:** Approved design

**Date:** 2026-08-05

**Scope:** Main Study Hub document processing, quiz generation, and practice-question import

**Anki boundary:** The tested Anki implementation remains unchanged unless a later, Anki-specific design and test cycle approves a migration.

## 1. Summary

NotebookLM Studio becomes **Quiz Builder**, with two explicit workflows that share one review and publishing system:

1. **Generate Quiz** sends selected source material and instructions to NotebookLM to create a new quiz.
2. **Import Practice Questions** parses existing question sets and answer keys directly into the native Study Hub quiz contract without requiring a NotebookLM round trip.

Anydoc is introduced behind a format-neutral document-processing interface. It becomes the preferred parser for supported Office formats only after it passes a shadow-mode comparison on real Study Hub documents. Existing page-aware PDF processing and the slide-aware Anki PowerPoint extractor remain in place.

Direct imports may use a configurable, low-cost model to segment questions, pair answer keys, and associate images. When an imported question has no supplied answer, Quiz Builder asks NotebookLM. If NotebookLM successfully reports that its selected sources cannot answer, Study Hub may generate an answer with a configured fallback model. That answer is visibly marked as AI-generated and cannot be published until the user explicitly verifies it.

Published content uses one quiz engine and appears in one of two library views:

- **Quizzes** for lecture and exam-review quizzes.
- **Practice Questions** for imported professor, website, and question-bank material.

## 2. Context and technical findings

The current Hub uses PDF-Inspector as a narrow PDF classifier with a `pypdf` fallback. PowerPoint-to-PDF conversion remains necessary for NotebookLM, Goodnotes, and existing artifact workflows. The current Anki source extractor uses `python-pptx` to preserve exact slide numbers and speaker-note locations.

[Firecrawl Anydoc](https://github.com/firecrawl/anydoc) provides one parser and shared document model for Word, PowerPoint, spreadsheet, OpenDocument, RTF, EPUB, CSV, and PDF formats. It can expose embedded Office assets and speaker notes. However:

- Anydoc still depends on PDF-Inspector for PDF processing, so it does not eliminate PDF-Inspector conceptually.
- Local Anydoc cannot OCR scanned PDF pages; Firecrawl's hosted Parse service adds OCR separately.
- The Python interface does not expose PDFs through the same asset-bearing document model used by Office documents.
- PowerPoint text, notes, and assets are emitted in order, but the shared model does not reliably attach a slide number to every block.
- The project was newly released at the time of this design, so production adoption requires pinned dependencies, corpus evaluation, and fallback paths.

These constraints make an immediate wholesale replacement unsafe. The design adopts Anydoc incrementally and preserves format-specific provenance where the Hub needs it.

## 3. Goals

- Rename NotebookLM Studio to Quiz Builder.
- Preserve the current NotebookLM-based quiz-generation workflow.
- Import existing practice questions from files and web pages without reimporting them into NotebookLM.
- Accept separately identified question and answer-key sources.
- Extract text, tables, supplied answers, explanations, speaker notes, and relevant images.
- Preserve source, page, slide, block, and image provenance through review.
- Ask NotebookLM for answers that are absent from imported material.
- Permit Study Hub to generate an answer only after NotebookLM has successfully reported insufficient support.
- Require explicit verification of every AI-generated answer before publication.
- Add Anydoc through a replaceable parser interface and migrate Office formats only after measured validation.
- Reuse the existing native quiz contract, image review, medical-accuracy gate, player, progress, and publication infrastructure.
- Keep Anki implementation and testing isolated.

## 4. Non-goals

- Replacing the NotebookLM integration.
- Sending every imported practice-question document through NotebookLM.
- Allowing Anydoc to generate quiz JSON directly.
- Treating Anydoc as an OCR engine for scanned PDFs.
- Removing PowerPoint-to-PDF conversion used by current artifact and delivery workflows.
- Automatically resolving ambiguous question-answer or question-image relationships.
- Publishing AI-generated answers without question-level human verification.
- Replacing the Anki PowerPoint extractor in this project.
- Building separate quiz players, grading systems, or media stores for practice questions.

## 5. User experience

### 5.1 Quiz Builder landing page

The existing NotebookLM Studio navigation label and page title become **Quiz Builder**. The page begins with two workflow choices:

#### Generate Quiz

This retains the current NotebookLM behavior:

- Add files, URLs, pasted text, and images to the course/exam notebook.
- Select some or all notebook sources.
- Enter instructions, including requested topics or tutor-emphasized material.
- Ask NotebookLM to generate a quiz.
- Review images, accuracy findings, and quiz content before publishing.

The existing source removal, select-all, drag-and-drop, and prompt-resizing behavior remains part of this workflow.

#### Import Practice Questions

This creates a native quiz from existing questions:

- Add one or more local files or HTTP/HTTPS URLs.
- Assign each source one of four roles: Questions, Answer Key, Supporting Reference, or Combined Questions and Answers.
- Select the destination course, exam, label, and content kind.
- Parse the sources and inspect extracted questions in review.
- Resolve warnings and verify generated answers.
- Publish to Practice Questions by default.

The user may override the destination content kind before publication.

### 5.2 Review page

The review page uses the current quiz-review foundation and adds question-level provenance. Each question displays:

- Original question location and, where feasible, a source preview.
- Parsed stem, choices, answer, rationale, topic, and learning objective.
- Related source image and alternative image candidates.
- Answer provenance badge.
- Extraction confidence and unresolved warnings.
- An explicit **Verify generated answer** control when required.

Answer provenance values are:

- **Provided by source**
- **Answered by NotebookLM**
- **Generated by AI**
- **Manually corrected**

Editing an AI-generated answer does not silently verify it. The user must explicitly verify the final answer after editing. There is no bulk approval action for AI-generated answers.

Blocking issues appear at the top of review and beside the affected question. Publication remains disabled until all blocking issues are resolved.

### 5.3 Library organization

The quiz domain gains a content-kind discriminator rather than a second quiz implementation:

- `lecture_quiz`
- `exam_review`
- `practice_questions`

The navigation exposes **Quizzes** and **Practice Questions** as separate views. Both reuse the same player, answer handling, flagging, progress summaries, resets, media delivery, and tokenized publication contract.

## 6. Architectural overview

```text
                         Quiz Builder
                              |
               +--------------+--------------+
               |                             |
        Generate Quiz               Import Practice Questions
               |                             |
       NotebookLM sources           immutable source snapshots
       + generation prompt                    |
               |                    document processor router
               |                     /        |         \
               |                Anydoc      PDF       web/text
               |                     \        |         /
               |                    canonical documents
               |                             |
               |                 structured question extraction
               |                             |
               |                  answer and image resolution
               |                             |
               +-------------+---------------+
                             |
                    native quiz validation
                             |
          image review + provenance review + accuracy gate
                             |
                    explicit publication
                             |
               Quizzes or Practice Questions
```

The two workflows converge only after they produce a validated native quiz draft. This keeps acquisition and generation concerns separate from review and publishing concerns.

## 7. Source acquisition and roles

Every uploaded file, pasted source, or imported web page becomes an immutable snapshot containing:

- Stable source identifier.
- User-visible title.
- Source role.
- Original filename or URL.
- Media type and byte size.
- SHA-256 checksum.
- Acquisition timestamp.
- Stored payload path.
- Processing state and diagnostic details.

Source roles are explicit because filename inference alone cannot reliably distinguish a question file from an answer key:

- `questions`
- `answer_key`
- `supporting_reference`
- `combined_questions_answers`
- `generation_material` for the NotebookLM workflow

URLs are fetched and snapshotted for direct import. Anydoc does not fetch URLs. The URL acquisition adapter retrieves public HTTP/HTTPS content, validates redirects and destination addresses, enforces time and byte limits, records the final URL, and stores the returned content before parsing. Question and answer URLs remain separate snapshots even when they come from the same site.

## 8. Canonical document model

All direct-import parsers produce an internal `ParsedDocument` contract instead of quiz JSON. The model contains:

- Document identity, format, parser name, and parser version.
- Ordered content segments.
- Stable segment identifiers.
- Text, list, table, heading, note, and image-reference segment types.
- Best available locator: page, slide, sheet, section, block, or source image.
- Embedded and rendered assets with content hashes, media types, dimensions, and origin locations.
- Parent/child and adjacency relationships needed for local image association.
- Extraction warnings and degraded-capability markers.

The canonical model is deliberately richer than Markdown. Markdown is useful for prompts and previews but cannot carry embedded bytes, stable relationships, or reliable page/slide provenance by itself.

### 8.1 Processor interface

A `DocumentProcessor` interface accepts a stored source snapshot and returns a `ParsedDocument`. A router chooses the processor by validated content type and format signature, not only by file extension.

Initial adapters are:

- **Anydoc adapter:** Office, OpenDocument, RTF, EPUB, CSV, and other supported structured files.
- **PPTX locator enricher:** Restores explicit slide boundaries and slide numbers when Anydoc's flattened model is insufficient.
- **PDF adapter:** Keeps PDF-Inspector classification, page-aware text extraction, page-image extraction, and OCR routing.
- **Web adapter:** Converts stored HTML into ordered content while retaining relevant image URLs and document structure.
- **Plain-text adapter:** Text, Markdown, JSON, XML, YAML, and CSV when structured treatment is unnecessary.
- **Existing Office fallback:** Used when Anydoc is unavailable or fails on a supported Office source.

The processor result records which parser and fallback path were used so the review page and diagnostics can explain degraded output.

### 8.2 Images and rendered visual content

Embedded Office images are retained through Anydoc when possible. Format-specific enrichment maps each asset to its slide or document location. PDF images remain page-aware through the existing PDF image path.

Some educational content is represented as charts, grouped shapes, SmartArt, equations, or vector objects rather than standalone image files. The processor may create a rendered page or slide image as a fallback asset. The review system distinguishes embedded assets from full-page or full-slide renders.

Automatic image binding is conservative:

- Exact source and locator matches are preferred.
- Block adjacency may rank candidates but cannot silently resolve multiple plausible images.
- Decorative, repeated, or tiny assets are excluded where deterministic rules allow.
- Ambiguous candidates remain in review.
- A question that explicitly requires an unresolved image cannot be published.

## 9. Direct practice-question import pipeline

Direct import is a durable staged job:

1. **Acquire:** Validate and snapshot every source.
2. **Parse:** Produce canonical documents and asset inventories.
3. **Segment:** Identify question boundaries, choices, answer-key entries, explanations, and candidate images.
4. **Pair:** Match questions to supplied answers and explanations.
5. **Resolve missing answers:** Ask NotebookLM, then conditionally invoke the fallback answer model.
6. **Normalize:** Produce a native quiz draft with provenance metadata.
7. **Validate:** Enforce the native quiz contract and answer consistency.
8. **Review:** Resolve warnings, images, and generated-answer verification.
9. **Accuracy gate:** Run the configured medical-accuracy check.
10. **Publish:** Atomically publish quiz content and media.

Completed stage artifacts are persisted. A retry resumes from the failed stage unless an input, prompt, model setting, or source snapshot changed.

## 10. Structured extraction

A configurable extraction model converts canonical documents into `QuestionDraft` records. This is an appropriate role for a low-cost model available through the existing provider and model-selection infrastructure. No vendor or model family is hardcoded.

The extraction contract requires:

- Source question identifier or inferred sequence number.
- Stem and choices, preserving supplied wording by default.
- Supplied correct answer and explanation when present.
- Source references for every extracted field.
- Image references and confidence.
- Ambiguity and completeness flags.

The model receives bounded, locality-preserving document segments rather than an undifferentiated full-document prompt. Outputs must satisfy a strict structured schema. Invalid output is retried once with schema feedback, then moves to review or fails the stage without losing parsed artifacts.

Deterministic matching runs before semantic matching:

- Exact question numbers and answer labels.
- Normalized numbering variants.
- Stable source order where both documents are complete and aligned.
- Explicit headings or table row identifiers.

Semantic matching may suggest a relationship but cannot finalize a low-confidence or conflicting match. No question receives a silently guessed supplied answer.

## 11. Missing-answer resolution

The answer-resolution order is mandatory:

1. Use an unambiguous answer supplied by an answer key or combined source.
2. For a missing answer, send the individual question to NotebookLM with the selected course/exam notebook and supporting sources.
3. Accept a NotebookLM answer only when the response identifies an answer and source support.
4. If NotebookLM successfully reports that the selected sources do not contain enough information, send the question and available evidence to the configured fallback answer model.
5. Mark the resulting answer as `generated_by_ai` and `verification_required`.

A NotebookLM timeout, authentication problem, rate limit, or provider outage is not equivalent to "no answer." Those conditions pause or retry the NotebookLM stage. They do not silently activate fallback generation.

Missing-answer resolution requires a reachable NotebookLM notebook for the selected course and exam. If the mapping or connection is unavailable, the question remains blocked with a setup diagnostic; the Hub does not skip directly to AI answer generation. Supporting-reference files may be attached for this resolution step when the user selects them, while the imported question set itself remains local unless explicitly selected as a NotebookLM source.

Fallback generation returns a proposed answer, rationale, supporting references when available, and uncertainty notes. It may use broader model knowledge when the supplied sources are insufficient, but that condition is visible in review. Every fallback-generated answer is a hard publication blocker until explicitly verified by the user.

Conflicting supplied answer keys also block publication. NotebookLM or another model may provide review evidence, but cannot silently choose between conflicting professor-provided answers.

## 12. Quiz and review data changes

The existing native quiz fields remain valid. New fields are additive so previously published quizzes continue to parse and render.

At the quiz level:

- Content kind.
- Import or generation workflow kind.
- Source snapshot references.
- Extraction provider/model metadata when applicable.

At the question-review level:

- Answer provenance.
- Verification state and verifier timestamp.
- Question, answer, explanation, and image source references.
- Extraction confidence.
- Blocking and non-blocking diagnostics.
- Original imported identifier.

Published quiz payloads need only retain provenance fields required for user-visible attribution and future audit. Private model diagnostics and internal source paths remain outside public quiz responses.

## 13. NotebookLM generation workflow

The Generate Quiz path preserves the current Studio behavior and source controls. Files used to request a newly authored quiz are attached to the selected NotebookLM notebook. User instructions are combined with the existing validated quiz prompt, image rules, subject-specific OMM rules, and native JSON contract.

This path remains suitable when the input contains topics, tutor priorities, learning objectives, or source material rather than completed questions. It does not invoke the direct question extraction pipeline unless the user explicitly chooses Import Practice Questions.

Both paths converge at native quiz validation and review, so medical-accuracy checks, image resolution, previews, and publication behavior stay consistent.

## 14. Publication gates

Publication is disabled while any of the following exists:

- Native quiz schema failure.
- Missing or conflicting correct answer.
- AI-generated answer not explicitly verified.
- Ambiguous question-answer pairing.
- Required image missing or ambiguously bound.
- Accuracy-gate failure or unresolved review verdict when the gate is enabled.
- Source snapshot or parser artifact required by the draft is missing.

NotebookLM answers and supplied answer keys pass through the normal quiz-level review and medical-accuracy gate. They do not require the special generated-answer verification control unless manually converted to AI-generated provenance.

Publication writes the quiz, content kind, version, source audit data, and media bindings atomically. A failed replacement never removes or alters the current published version.

## 15. Error handling and operational behavior

### 15.1 Document failures

- Empty, corrupt, encrypted, unsupported, or oversized documents fail validation with a specific user-facing reason.
- Anydoc failures invoke a known safe fallback where one exists and record degraded processing.
- Scanned or mixed PDFs route to configured OCR or review; missing extracted text is not treated as a valid empty result.
- Partial parse results may be retained for diagnostics but cannot proceed as complete without an explicit completeness decision.

### 15.2 Provider failures

- Transient model and NotebookLM failures retry with bounded backoff.
- Authentication and configuration failures stop immediately with an actionable diagnostic.
- Each provider call records stage, provider, model, attempt count, request identifier when available, and sanitized error details.
- Completed acquisition and parsing work is reused across provider retries.

### 15.3 URL safety

- Only HTTP and HTTPS are accepted.
- Every initial and redirected destination is checked against private, loopback, link-local, and otherwise disallowed addresses.
- Redirect count, response time, download bytes, and supported content types are bounded.
- The fetched snapshot, not the mutable live page, is the input to downstream parsing and review.
- Active content is never executed by the parser or preview.

### 15.4 Idempotency

Source checksum, role, workflow configuration, prompt version, and model settings form the run signature. Repeating an unchanged request does not duplicate snapshots, drafts, assets, or publications. Changing a source or a material setting creates a new version without mutating prior review evidence.

## 16. Anydoc adoption and rollback

Anydoc is pinned to an exact validated release. Windows Python 3.12 wheel installation is tested in CI and in the deployment sandbox before instructions are given to the production host.

Rollout proceeds in phases:

1. **Interface introduction:** Add canonical document contracts and adapters without changing production routing.
2. **Shadow evaluation:** Run Anydoc and current parsers against representative real documents and store comparison metrics without affecting user output.
3. **Practice-question enablement:** Use the new pipeline for direct imports, with current fallbacks available.
4. **Office preference:** Make Anydoc primary for each Office format only after that format meets acceptance criteria.
5. **Lecture semantic extraction:** Allow the main non-Anki lecture workflow to prefer Anydoc plus locator enrichment after PPTX corpus approval.

The parser router supports a configuration rollback to the previous adapter without database rollback. Existing immutable snapshots can be reprocessed with either parser.

PDF-Inspector remains directly available during the transition because the Hub needs its classification contract and Anydoc itself relies on it for PDFs. PowerPoint-to-PDF conversion remains in place for NotebookLM, Goodnotes, and canonical PDF artifacts.

The Anki source extractor remains unchanged. Any future Anki migration requires its own design, implementation, and tests against exact slide locators, speaker notes, source evidence, and curation outputs.

## 17. Testing strategy

### 17.1 Unit and contract tests

- Processor routing by content signature and extension.
- Canonical segment, locator, asset, and warning contracts.
- Anydoc-to-canonical mapping.
- PPTX slide-locator enrichment.
- PDF classification, page text, page images, and OCR routing.
- HTML snapshot parsing and URL safety.
- Deterministic question-answer matching.
- Structured model output validation.
- Answer provenance and verification state transitions.
- Publication gate enforcement.
- Backward compatibility for quiz JSON without new fields.

### 17.2 Golden fixtures

Fixtures cover:

- PPTX questions with answers on later slides.
- PPTX questions containing embedded images, charts, and speaker notes.
- Separate question and answer-key DOCX files.
- Text-based, mixed, and scanned PDFs.
- PDF questions with page images.
- Combined questions and answers in tables.
- Web pages with separate question and answer URLs.
- Missing answers that NotebookLM can resolve.
- Missing answers that require AI fallback.
- Conflicting answer keys and ambiguous image candidates.

Expected outputs include exact question counts, supplied wording, answer mappings, page/slide locators, image bindings, provenance, and blockers.

### 17.3 Provider and recovery tests

NotebookLM and model calls are mocked for deterministic automated tests. Scenarios include timeouts, rate limits, invalid credentials, malformed structured output, provider refusal, successful no-answer responses, interrupted stages, and resumed jobs. Automated tests never call live providers.

### 17.4 End-to-end tests

- Generate Quiz from selected NotebookLM sources through publication.
- Import a complete practice-question file without contacting NotebookLM.
- Import paired question and answer sources.
- Resolve a missing answer through NotebookLM.
- Generate a fallback answer, verify it, and publish.
- Confirm publication is blocked before generated-answer verification.
- Publish required media and render it in the shared quiz player.
- Display content in the correct Quizzes or Practice Questions library.

### 17.5 Anydoc corpus gate

Representative real lecture decks and practice-question documents are processed by both current and candidate adapters. The report compares:

- Extracted question and answer counts.
- Text completeness and ordering.
- Tables, choices, notes, and explanations.
- Page and slide attribution.
- Embedded and rendered image inventory.
- Parse failures, warnings, and processing time.

A format cannot switch to Anydoc-primary routing if the comparison shows a known question was silently dropped, an answer was incorrectly paired, a required image was incorrectly bound, or required page/slide provenance was lost. Ambiguous material must become an explicit review item.

### 17.6 Regression suites

The full main Hub suite verifies existing ingestion, source management, quizzes, images, medical-accuracy checks, progress, and publication. The separately maintained Anki suite verifies no behavior or output changed. Anydoc-related implementation changes do not alter Anki code merely to satisfy main Hub tests.

## 18. Acceptance criteria

The feature is complete when:

- The UI is named Quiz Builder and exposes Generate Quiz and Import Practice Questions.
- The existing NotebookLM generation workflow remains usable.
- Files and public URLs can be assigned question, answer-key, supporting-reference, or combined roles.
- Supported documents produce canonical text, table, locator, and asset records.
- Existing questions can become validated native quiz drafts without NotebookLM reimport.
- Missing answers are sent to NotebookLM before fallback answer generation.
- Provider failure is never mistaken for NotebookLM lacking an answer.
- Every AI-generated answer is labeled and blocks publication until individually verified.
- Required unresolved images and ambiguous answer matches block publication.
- Published content appears in the selected Quizzes or Practice Questions view using the shared player.
- Existing quiz JSON remains compatible.
- Anydoc can be disabled or rolled back without data loss.
- Office formats become Anydoc-primary only after passing the corpus gate.
- Existing PDF classification and PowerPoint-to-PDF artifact workflows continue to work.
- The Anki implementation remains unchanged and its independent tests pass.

## 19. Decisions

- **Adopt Anydoc incrementally, not as an immediate wholesale replacement.**
- **Use a shared Quiz Builder with two workflows.**
- **Use one quiz domain and player with separate library views.**
- **Keep document normalization separate from question extraction.**
- **Allow configurable low-cost models for structured extraction.**
- **Ask NotebookLM before generating any missing answer.**
- **Treat NotebookLM unavailability differently from a supported no-answer result.**
- **Require explicit question-level verification for AI-generated answers.**
- **Preserve PDF-specific and slide-specific provenance adapters.**
- **Keep Anki code and migration decisions isolated.**
