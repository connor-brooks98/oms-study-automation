# Native Study Hub Quizzes Design

## Goal

Replace the fragile NotebookLM-to-Gemini browser handoff with a native Study
Hub quiz player. NotebookLM remains responsible for generating
lecture-specific questions from exactly the selected lecture sources. Study Hub
validates, publishes, serves, and links each quiz.

## Approved user experience

The quiz player uses the approved **Study Focus** layout and the existing Study
Hub visual language:

- one question is shown at a time;
- the header shows course, exam, lecture, question count, and progress;
- selecting an answer is reversible until **Submit Answer** is pressed;
- each answer has an independent strike-through control that eliminates or
  restores that choice without submitting it;
- selected question text can be highlighted and all highlights can be cleared;
- submitting locks the question, shows the selected answer in red or green,
  reveals the correct answer, and displays the expert rationale;
- **Continue** moves to the next question;
- the final screen reports the score and supports starting over; and
- current progress is restored from browser storage after a refresh.

The browser stores progress only on the reader's device. Study Hub does not
collect student names, answers, scores, or analytics.

## Generation architecture

Each course exam keeps one NotebookLM notebook. The worker continues to upload
the current lecture PDF and cleaned transcript and passes only those two remote
source IDs to NotebookLM.

For quiz jobs, Study Hub appends a non-editable output contract to the
user-managed Obsidian quiz prompt. The contract requires one JSON object:

```json
{
  "title": "Lecture quiz title",
  "questions": [
    {
      "stem": "Question text",
      "choices": ["Choice A", "Choice B", "Choice C", "Choice D"],
      "correct_index": 0,
      "rationale": "Why the correct answer is correct and the others are not."
    }
  ]
}
```

The Obsidian prompt remains the source of the quiz-writing instructions and can
be edited without a deployment. The appended contract controls only the
machine-readable response format.

Study Hub accepts a raw JSON object or a JSON object inside a Markdown code
fence. Validation requires:

- a non-empty title;
- between 1 and 100 questions;
- a non-empty stem and rationale for every question;
- between 2 and 8 distinct, non-empty choices per question; and
- a zero-based `correct_index` within the choice list.

Malformed output fails at the quiz-validation stage. No incomplete quiz is
published or linked.

## Durable storage and stable links

A `published_quizzes` table stores one row per lecture:

- an unguessable URL token;
- the current generation job ID;
- quiz title and serialized validated payload;
- a monotonically increasing content version; and
- created and updated timestamps.

Publishing is an idempotent upsert by lecture and job. Regenerating a lecture
replaces its payload and increments its version while preserving the same token
and public URL. Retrying the same job does not increment the version.

The existing `quiz_outputs` history continues to record which generation job
produced the current link. Existing Gemini URLs remain readable in historical
rows, but the next successful generation replaces the current output with the
native link.

## Public routes and security

The public player uses:

- `GET /public/quizzes/{token}` for the HTML shell;
- `GET /public/quizzes/{token}/content` for stems and choices without answer
  keys;
- `POST /public/quizzes/{token}/answer` for correctness and rationale; and
- `GET /public/quizzes/assets/player.js` for the CSP-compatible player.

Tokens use at least 256 bits of randomness. Unknown tokens return 404. Public
payloads expose no lecture files, transcripts, prompts, NotebookLM identifiers,
Google credentials, private application navigation, or other Study Hub data.

When accessed through the configured public hostname, only the
`/public/quizzes/` path bypasses Cloudflare Access identity verification. The
rest of Study Hub remains protected. Public answer submissions remain
same-origin and CSRF protected. The Cloudflare dashboard must have a matching,
more-specific Bypass application for `/public/quizzes/*`.

The public URL is:

- `https://{OMS_HUB_PUBLIC_HOSTNAME}/public/quizzes/{token}` when a public
  hostname is configured; or
- `http://127.0.0.1:{OMS_HUB_DASHBOARD_PORT}/public/quizzes/{token}` for local
  development.

Google Docs accepts only URLs matching that exact configured origin and native
quiz path.

## Worker stages and recovery

Quiz jobs use these durable stages:

1. source validation;
2. NotebookLM notebook and source setup;
3. NotebookLM prompt;
4. quiz validation;
5. native publication;
6. Google Docs synchronization; and
7. complete.

The worker records the public URL before Google Docs synchronization. A retry
after publication reuses the same published quiz and link. The lecture pipeline
continues to use the existing `quiz_published` completion step, with status
messages that identify validation, publication, or Docs synchronization errors.

## Google integration changes

Native quiz generation no longer uses the consumer Gemini website or Gemini
Quiz Gem. The Google connection card and connection probe cover NotebookLM and
Google Docs only. Interactive connection opens NotebookLM and completes the
Google Docs OAuth flow; it no longer opens Gemini.

The independent Gemini API provider under AI provider settings is outside this
change and remains available.

For every course, Google Docs still maintains one master quiz document. Each
exam has its own tab, and each lecture has one entry:

```text
Lecture 1: LINK TO NATIVE QUIZ
Lecture 2: LINK TO NATIVE QUIZ
```

Regeneration updates the existing named range instead of adding a duplicate.

## Lecture page

The lecture quiz card uses native wording:

- ready: “Current Study Hub quiz is ready.”
- not generated: “Built from this lecture’s PDF and cleaned transcript only.”
- action: **Take Lecture Quiz**
- generation action: **Generate Quiz**

The generated quiz link opens in a new browser tab. Status polling continues to
receive the current quiz URL from the existing lecture generation endpoint.

## Deployment

The release archive includes the native quiz domain/parser, routes, template,
player JavaScript, styles, migrations, worker changes, and updated NUC rollout
documentation.

The NUC update requires:

1. pulling the implementation branch;
2. reinstalling the package while the Study Hub process is stopped;
3. restarting Study Hub so schema migration runs;
4. adding the Cloudflare Access path bypass for `/public/quizzes/*`; and
5. generating one lecture quiz and opening its public link in a private browser
   window.

No Gemini Quiz Gem URL is required after this rollout.

## Verification

Automated coverage must prove:

- valid fenced and unfenced NotebookLM JSON parses;
- malformed, duplicate-choice, and out-of-range answers are rejected;
- publishing is stable across regeneration and idempotent for retries;
- public content omits answer keys and public answer submission returns only
  the requested question's feedback;
- public quiz routes bypass Access while dashboard routes do not;
- answer submission still requires same-origin CSRF verification;
- the worker publishes natively and does not call Gemini;
- Google Docs rejects untrusted quiz URLs;
- lecture status and page links point to the native quiz;
- the browser player supports selection, elimination, submission, feedback,
  continuation, and local progress; and
- release archives contain the new runtime files and no browser credentials.

