# Study Hub Updates Architecture

**Date:** 2026-08-04  
**Target branch:** `codex/anki-v4-implementation`  
**Source context:** current private worktree plus the remote
`codex/notebooklm-studio-main-hardening` branch.

## Scope and boundaries

The Study Hub updates are implemented in the main Hub layers:

- `oms_hub/study_generation`: NotebookLM Studio, quiz contracts, quiz review,
  source intake, image resolution, and medical-accuracy gating.
- `oms_hub/files`: bounded Office conversion and PDF inspection.
- `oms_hub/llm`: existing transcript providers remain unchanged; OpenRouter is
  a separate study-generation service so it does not alter Anki provider
  contracts.
- `oms_hub/web`: private Studio routes, Settings, public quiz delivery, and
  browser-only quiz progress.
- `models.py` and `migrations.py`: additive durable state only.

The Anki implementation boundary is explicitly frozen. No file under
`src/oms_hub/anki`, `src/oms_anki_agent`, `tests/anki`, or `tests/agent` is
changed. Shared navigation may affect the rendered Anki page through
`base.html`, but it does not alter Anki behavior or its contracts.

## Existing system shape

Study Hub is a FastAPI application backed by SQLite/SQLAlchemy. Upload and
generation workers claim durable jobs, NotebookLM is accessed through the
stored authenticated gateway, and native quizzes are parsed into validated
domain objects before publication. Public quiz answers are graded by the
server; progress, highlighting, and interaction state currently live in the
browser.

The remote Studio branch supplies the durable Studio source/run/image-review
model, source deletion, source selection, bounded PPTX conversion, encrypted
NotebookLM storage, image-safe upload handling, preview/public media routes,
and the Quiz Library navigation. Those capabilities are integrated without
discarding the current Anki branch.

## Target architecture

```text
file / text / URL / pasted image
              │
              ▼
       Studio source store ──► source worker ──► NotebookLM notebook
              │                         │
              │                         ├─ PDF inspector classification
              │                         └─ embedded-image inventory
              ▼                         │
       selected source IDs ─────────────┘
              │
              ▼
       Studio/lecture chat run
              │
              ├─ subject-aware prompt + image-aware JSON contract
              ├─ native quiz parser (metadata, image refs, safe IDs)
              ├─ OpenRouter medical-accuracy gate when enabled
              └─ deterministic image resolver
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
  automatic publication   durable image review
                              │
                 upload/auto-bind/override → preview → explicit publish
                              │
                              ▼
                    tokenized public quiz + media
```

### Source intake and NotebookLM Studio

`StudioService` accepts PDF, PPTX, plain text/Markdown, supported Office/text
files, URLs, and image drops. The payload is stored atomically under the
configured data directory. PPTX/Office conversion runs through the existing
bounded single-process converter; partial outputs are deleted on timeout or
failure.

`PdfInspector` is a small adapter around the pinned Git dependency
`firecrawl/pdf-inspector`. It classifies PDFs as text-based, scanned,
image-based, or mixed and reports confidence/pages requiring OCR. The existing
`pypdf` validation remains the structural safety check and is the fallback for
test environments where native bindings are unavailable.

For PDFs and PPTX files, the source worker builds an image inventory from
embedded page/slide images. A quiz image reference is matched by source title
and a conservative page/slide/image locator. Only unambiguous matches are
auto-bound; everything else remains in the existing private image-review flow.
This prevents a wrong medical figure from being silently attached.

The Studio page has one mixed-source dropzone, a source list that is refreshed
after every attach/delete, a select-all control scoped to the selected
NotebookLM course/exam folder, and a vertical-only resizable prompt textarea.
Pasted image data and dropped image URLs are normalized into the same image
source path, so an image can be dragged from Google Images without first being
saved manually.

### Quiz contract and generation

`QuizQuestion` gains optional `topic`, `learning_objective`, and `image_ref`
metadata. Existing quiz JSON without these fields remains valid. The prompt
builder adds:

- the image contract to lecture and Studio quiz prompts;
- explicit OMM-only rules when the subject is OMM; and
- an explicit prohibition on OMM-specific questions, including thoracic spine
  level questions, for every non-OMM subject.

The parser rejects malformed image metadata, conflicting metadata for one
shared image key, duplicate choices, invalid answer indexes, and invalid
metadata lengths. It serializes only validated data.

Before automatic publication, `MedicalAccuracyService` can send the validated
quiz to OpenRouter using a key stored only in the OS keyring. Its structured
report has one verdict per question (`pass`, `review`, or `fail`) and a safe
reason. When the gate is enabled, missing credentials, malformed reports, or
any non-pass verdict stop publication and leave the run in review. Automated
tests mock the endpoint; no test calls a live model.

### Durable review and publication

Studio image-dependent runs remain `awaiting_images` until each non-overridden
image reference has an asset. Automatically matched images count as resolved.
Manual upload accepts PNG/JPEG/WebP, verifies decoded content, rejects
animated/truncated/oversized/decompression-bomb files, corrects EXIF
orientation, strips metadata, and writes sanitized PNG atomically.

Preview uses the same player and grading contract as the public quiz. Publish
rechecks resolution and accuracy status, then commits quiz payload and media
bindings atomically. Replacement runs do not disturb the current public quiz
until explicit publication.

### Quiz player and progress

The browser state machine gains:

- previous/next navigation after a question has been answered, including a
  review path from the result screen;
- per-question flag state with a reason dropdown;
- per-quiz reset, in addition to the existing reset-all action;
- grouped result summaries by learning objective/topic, showing correct and
  review counts; and
- optional question images with safe relative URLs and accessible alt text.

These interactions remain local to the browser. The server never receives
answer keys or flag content through the public content endpoint.

### Settings and model selection

Existing transcript-provider settings remain intact. Settings gains a separate
OpenRouter card with keyring-backed API-key entry, connection test, accuracy
gate toggle, and a curated model dropdown with a custom-model fallback. The
OpenRouter model and gate preference are non-secret SQLite settings. Existing
provider settings and Anki provider selection are not repurposed.

## Data changes

Migrations are additive and preserve existing quiz tokens/payloads:

- Studio source/run/image-review/public-media tables from the Studio branch;
- optional quiz metadata/image-reference fields in serialized payloads;
- accuracy status/report fields on generation and Studio runs;
- OpenRouter model/gate preference row; and
- PDF inspection metadata for Studio file sources.

No credential, raw NotebookLM response, local media path, or answer key is
returned through public APIs.

## Risks and controls

| Risk | Control |
|---|---|
| Wrong image matched to a question | Require unambiguous locator match; otherwise use private review. |
| Native Office process hangs | Run conversion in a bounded child process and remove partial output. |
| Malicious image/PDF upload | Size/type/structure checks, decoded-pixel limits, atomic sanitized output, PDF inspector plus `pypdf`. |
| Medical misinformation | Optional-but-explicit OpenRouter gate; enabled runs cannot auto-publish review/fail results. |
| Public data leakage | Public content/media serializers whitelist fields; answer grading remains server-side. |
| Anki regression | No Anki implementation/test files changed; run Anki tests unchanged. |

## Verification strategy

Run focused Python tests for source/quiz/accuracy/migration behavior, Node
tests for quiz navigation/flags/reset/summary and Studio controls, then the
full existing Python and JavaScript suites. Run Ruff and strict mypy. Perform
the real PPTX/NotebookLM/OpenRouter checks only on the Windows NUC with fresh
credentials and test data.
