# Studio Quiz Images and Library Navigation Design

## Goal

Let NotebookLM Studio identify quiz questions that depend on a specific source
image, hold those quizzes for private media review, reuse one uploaded image
across multiple questions, and publish only after a final preview. Add a
persistent **Quiz Library** link to the private Study Hub navigation.

## Scope

This workflow applies to quizzes generated from NotebookLM Studio prompts.
Lecture automation keeps its existing text-only output contract and automatic
publication behavior. Existing published quizzes and stored payloads remain
valid without migration or manual editing.

Each question supports at most one displayed image. A single uploaded asset may
be referenced by any number of questions. A multi-panel figure must be supplied
as one composite screenshot.

## NotebookLM Studio output contract

Study Hub appends an image-aware contract only to Studio quiz prompts. Every
question must include an `image_ref` field:

```json
{
  "title": "Lecture quiz title",
  "questions": [
    {
      "stem": "Question text",
      "choices": ["Choice A", "Choice B", "Choice C", "Choice D"],
      "correct_index": 0,
      "rationale": "Why the correct answer is correct and the others are not.",
      "image_ref": null
    },
    {
      "stem": "Question that depends on a source image",
      "choices": ["Choice A", "Choice B", "Choice C", "Choice D"],
      "correct_index": 1,
      "rationale": "Why the correct answer is correct and the others are not.",
      "image_ref": {
        "key": "image-1",
        "source_title": "Dr. Wang's website",
        "locator": "Image immediately before question 4",
        "description": "Reference image used for questions 4-7"
      }
    }
  ]
}
```

The appended instructions tell NotebookLM to:

- use `null` when the question can be answered without a specific image;
- create an image reference when the question or its source instructions depend
  on a particular diagram, photograph, scan, graph, table, or other image;
- identify where the user can find the source image instead of attempting to
  reproduce or embed it;
- use lowercase keys containing letters, numbers, and hyphens;
- repeat the exact same key and metadata for every question that uses the same
  source image; and
- never invent image contents, URLs, or source locations.

The parser accepts a missing `image_ref` as `null` for backward compatibility.
It rejects malformed references and rejects a response when the same key is
reused with conflicting source title, locator, or description. The existing
question, choice, answer-index, and rationale validation remains unchanged.

## Durable draft and review state

After NotebookLM responds, the Studio worker validates and serializes a native
quiz draft.

- If every `image_ref` is `null`, the worker publishes and completes the run
  exactly as it does today.
- If any question has an image reference, the worker stores the validated draft
  and its unique image requirements, then moves the run to the durable
  `awaiting_images` state and `image_review` stage.
- An awaiting-images run has no active public token and does not appear in the
  public quiz library.
- Restart recovery leaves awaiting-images runs untouched because they are
  waiting for a user decision, not worker execution.

The Studio run stores the validated draft payload separately from the raw
NotebookLM response. One requirement row is stored per `(run_id, image_key)`.
Per-question overrides are stored independently so one question may be marked
**No image needed** without affecting other questions that share the key.

For replacement runs, the currently published quiz remains active while the new
run is awaiting images. The existing public quiz changes only after the user
publishes the completed replacement preview.

## Image review interface

An awaiting-images run displays an **Images needed** status and an **Add
images** action in NotebookLM Studio. The private review page groups questions
by unique image key. Each group shows:

- source title;
- source locator;
- NotebookLM's description;
- every question number using the image; and
- upload status and image dimensions when present.

The user may upload or replace one PNG, JPEG, or WebP file for the group. The
uploaded image is automatically used by every non-overridden question carrying
that key. The user may mark an individual question **No image needed**, with a
confirmation prompt, and may reverse that decision. If every question using a
key is overridden, that key no longer requires an upload.

The publish action remains disabled until every non-overridden image reference
has an uploaded asset. Upload and override errors leave the validated draft and
all prior successful work intact.

## Image validation and private storage

Uploads are limited to 10 MiB and 40 million decoded pixels. Study Hub verifies
the decoded file instead of trusting the filename or browser content type. It
rejects unsupported, truncated, animated, or decompression-bomb images.

Accepted images are corrected for EXIF orientation, stripped of metadata, and
re-encoded as a lossless PNG. The sanitized image is written atomically under
the configured Study Hub data directory, scoped by Studio run and image key.
Database rows store only the sanitized path, SHA-256 digest, width, height, and
media type. Replacing an upload first commits the new safe file and database
binding; an older unreferenced file may then be removed.

Image keys never become filesystem path components without server-side
validation and normalization. Upload, replace, override, preview, and publish
routes are private Studio routes protected by the existing Cloudflare Access,
host validation, CSRF checks, and same-origin policy.

## Final preview and publication

When every requirement is resolved, **Preview quiz** opens a private preview
using the real quiz player. It uses the same question order, answer behavior,
rationales, responsive image rendering, and CSS as the public experience.

Each resolved image appears above the question stem, preserves its aspect
ratio, scales down to fit mobile and desktop layouts, includes the reference
description as alternative text, and can be clicked or tapped to view a larger
version. The review page remains available from the preview so the user can
replace an image or reverse an override.

The user must explicitly select **Publish quiz** from the final preview. The
server rechecks that all requirements are resolved, removes overridden image
references from the publishable quiz, and publishes the quiz and its image
bindings in one transaction. A failed publish exposes neither a partial quiz
nor partial media.

New Studio quizzes receive their normal stable random token. Replacement runs
keep the existing token and increment the published quiz version only when the
new preview is published. Once publication succeeds, the Studio run becomes
complete and links to the published quiz.

## Public media delivery

Public quiz content includes an `image_url` and `image_alt` only for questions
with a resolved image. It never exposes local paths, source files, raw NotebookLM
responses, image-review metadata, answer keys, or unpublished drafts.

Sanitized images are served from:

```text
GET /public/quizzes/{token}/media/{image_key}
```

The route returns an image only when the token names an active quiz and the key
is bound to that published quiz. Unknown, inactive, unbound, or malformed
token/key combinations return 404. Unpublishing a Studio quiz therefore removes
access to both its player and its images. Public media requests use the existing
quiz rate limiter and safe response headers.

## Navigation

The shared private top navigation adds a **Quiz Library** link to
`/public/quizzes`. It appears on Dashboard, Uploads, Quarantine, Review, Anki,
NotebookLM Studio, and Settings pages and opens in the current tab. The public
library and player retain their existing public-only layout and back links.

## Data model and migration

The schema version increases and adds:

- `studio_runs.draft_payload_json`, nullable, containing only a validated quiz
  draft;
- `studio_quiz_image_requirements`, unique by run and image key, containing
  source metadata plus sanitized asset metadata; and
- `studio_quiz_image_overrides`, unique by run and question ID, recording an
  explicit no-image decision for that question and key.

The published quiz JSON stores optional image-reference metadata. Published
media bindings are copied into a dedicated table keyed by quiz token and image
key, so later Studio draft edits cannot change an already-published quiz. The
binding stores the sanitized path, digest, dimensions, media type, and public
alternative text.

All new columns and tables are additive. Existing quiz JSON without image
references parses as before, and existing publication rows require no backfill.

## Error handling and recovery

- Invalid NotebookLM image metadata is a contract failure and follows the
  existing single automatic contract retry before the run fails.
- A missing, invalid, or oversized upload returns a specific validation error
  and does not alter the current requirement binding.
- Preview and publish return a conflict response if unresolved image keys
  remain.
- Publishing is idempotent for the same completed Studio run.
- Restart recovery requeues only interrupted worker states; it never
  auto-publishes an awaiting-images run.
- Unpublishing preserves private Studio history, draft metadata, and sanitized
  files so the result remains auditable and may be regenerated safely.

## Verification

Automated coverage must prove:

- the Studio prompt contains the image-aware contract while the lecture prompt
  remains text-only;
- image references parse, serialize, and remain optional for legacy payloads;
- conflicting metadata for a shared key is rejected;
- image-free Studio output still publishes automatically;
- image-dependent output enters `awaiting_images` without a public token;
- one uploaded asset fulfills a key reused by multiple questions;
- individual no-image overrides are reversible and affect only that question;
- unresolved requirements block preview publication;
- PNG, JPEG, and WebP uploads are decoded, orientation-corrected,
  metadata-stripped, and sanitized as PNG;
- oversized, over-pixel, animated, malformed, and unsupported uploads are
  rejected without replacing a valid asset;
- final preview uses the same player behavior and media URLs as publication;
- publishing is atomic, explicit, and stable across replacement runs;
- public content exposes image URLs but no answer keys or private metadata;
- inactive or unbound public media requests return 404;
- one image renders above each linked question and supports responsive
  enlargement;
- the private top navigation contains the Quiz Library link; and
- release packaging includes the image-processing dependency and all new
  templates, scripts, styles, migrations, and runtime modules.
