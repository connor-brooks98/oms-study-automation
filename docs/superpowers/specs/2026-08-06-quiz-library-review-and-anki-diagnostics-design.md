# Quiz Library Review and Anki Diagnostics Design

## Goal

Make private quiz review easier to navigate, make released Studio/practice-question sets read naturally in the library, let the owner unpublish any released quiz without deleting its source data, and make card-centric Anki reconciliation failures explain themselves.

## Settled behavior

- In the private Studio preview only, Back and Next remain available before a question is answered. The released public player continues requiring submission before advancing.
- A Studio or imported practice-question row uses the user-supplied quiz title as its primary heading. It does not render the generic label `Studio quiz`. Lecture rows retain `Lecture N` as the primary heading and the lecture topic beneath it.
- Every released lecture quiz, Studio quiz, exam review, and practice-question set has a red **Remove** control next to **Reset**.
- Remove means unpublish, never permanent deletion. It preserves Studio runs, reviewed questions, images, source URLs, lecture jobs, and historical publication rows. A later generation or Studio publication may publish the material again.
- Unpublish is one token-based operation for both lecture and Studio publications.
- Card-centric reconciliation failures show the exact failed assertion identifiers and messages in the persisted job error, the Anki status UI, and the worker log.

## Navigation mode

The shared player remains the only quiz-player implementation. The Studio preview template opts into `data-allow-unanswered-navigation="true"` on the player root. `public_quiz.js` reads that explicit capability once during initialization and uses it only for the forward-button disabled state and click guard.

No behavior is inferred from token prefixes or URLs. A released quiz page omits the capability and therefore preserves the current answer-before-advance behavior. Back remains bounded only by the first question in both modes.

## Library presentation

Library rows receive one display model from `_quiz_library`:

- Lecture publication: `primary_label = "Lecture N"`, `secondary_label = lecture.topic`.
- Studio/import publication: `primary_label = published.label or published.title`, with no duplicate secondary label when it would repeat the primary label.

The template renders those fields without branching on the generic phrase `Studio quiz`.

## Unpublish lifecycle and security

`GenerationRepository.unpublish_quiz(token)` is the single persistence operation. It requires an active publication, marks it inactive, and clears `StudioRunModel.published_token` when the publication belongs to a Studio run. It does not delete the publication row, payload, media, run, job, or source records.

The browser calls `DELETE /api/published-quizzes/{token}`. This route deliberately lives outside `/public`, so the existing application middleware enforces:

- local-host access rules on the NUC; or
- a valid Cloudflare Access identity on the configured public hostname;
- same-origin mutation checks; and
- the existing CSRF cookie/header check.

The public quiz bypass remains GET-only in effect for this feature. Anonymous visitors may see the rendered management control, but cannot successfully call the private endpoint. The UI confirms the action, submits the CSRF header, removes browser-local progress for that token/version, removes the row after success, and displays a clear failure message without changing the page when the server rejects the request.

The existing Studio-run unpublish route delegates to the generic repository operation so both entry points share lifecycle rules.

## Anki reconciliation diagnostics

The reconciliation artifact already contains structured `failed` findings. The card-centric stage builds a bounded error message from those findings, for example:

`Card-centric reconciliation failed: A6: YES plus generated cards must total at least 10`

The structured artifact remains authoritative and unchanged. The detailed message is stored in `job.error`. After a stage returns a failed state without raising an exception, `AnkiCurationWorker` logs the persisted error once, matching exception-driven terminal failures. The existing job API and processing view already consume `job.error`, so they display the same actionable reason.

## Verification

Tests must prove:

- private preview can move forward while unanswered;
- released player still cannot move forward while unanswered;
- Studio/import rows show only the supplied title while lecture rows retain lecture numbering;
- both lecture and Studio publications can be unpublished by token;
- unpublish preserves underlying records and permits a later publication;
- the management endpoint requires CSRF and remains outside the public-host bypass;
- successful library removal clears local progress and removes the row;
- rejected removal leaves the row present and reports the error;
- card-centric error text includes all failed assertion IDs/messages; and
- the worker logs a terminal stage failure that was returned rather than raised.

Focused tests run before the complete Python, JavaScript, Ruff, and mypy gates.
