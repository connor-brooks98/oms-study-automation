# Medical School Study Hub V2

Study Hub V2 is a private, NUC-hosted library for manually supplied lecture
PowerPoints and Panopto transcript downloads. It imports an Excel exam tracker,
groups lectures by course and Exam, matches uploads, quarantines uncertain
matches, converts PowerPoints to PDF, cleans transcripts with the approved
Obsidian prompt, and files the resulting artifacts on the NUC and in iCloud.
It can also build lecture-specific NotebookLM outlines and native Study Hub
quizzes from exactly the current lecture PDF and cleaned transcript.

Canvas polling, Panopto polling, Outlook synchronization, and the browser
extension are intentionally not part of V2. The NUC must be online for remote
uploads and artifact access.

## Development

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\pytest
.\.venv\Scripts\oms-hub.exe serve
```

Open `http://127.0.0.1:8765`. Upload the tracker on Settings, then use the
Slides and Transcripts pages.

## Transcript setup

```powershell
.\.venv\Scripts\oms-hub.exe prompt-init
.\.venv\Scripts\oms-hub.exe prompt-fingerprint
```

Edit the prompt in Obsidian, copy the printed fingerprint into
`OMS_HUB_TRANSCRIPT_PROMPT_SHA256`, and restart the Hub. A prompt change pauses
cleaning until its new fingerprint is explicitly configured.

Open **Settings → AI providers** to save OpenAI, Google Gemini, and Anthropic
Claude credentials in Windows Credential Manager. Select a model, run
**Test connection**, and choose the active provider. Stored credentials are
never returned to the browser or written to `.env` or SQLite.

## Transcript cost safeguard and download

Study Hub checks a matched transcript before creating an LLM processing job.
Uploading the exact same source again finishes without an API request. A
different transcript matched to a lecture that already has a cleaned version
pauses and displays the detected course, lecture number, and topic. Choose
**Process anyway** to authorize the new request or **Discard upload** to remove
only the newly staged file.

Open a lecture, select **Open Cleaned Transcript**, and use **Download
transcript** to save the already-validated text. The downloaded filename
contains the course, lecture number, topic, and `Transcript`; downloading does
not call an LLM.

## NUC and remote access

The complete side-by-side installation, Cloudflare Access, backup, acceptance,
and rollback procedure is in [docs/v2-nuc-rollout.md](docs/v2-nuc-rollout.md).
The Hub remains bound to `127.0.0.1`; Cloudflare Tunnel provides outbound-only
remote connectivity, and the application independently verifies the Access
JWT and allowed email.

## NotebookLM outlines and native quizzes

The lecture page has separate **Generate Outline** and **Generate Quiz**
actions. Outline and quiz prompts remain editable in Obsidian and are linked
from **Settings → Notebook prompts**. Gemini Notebook is connected from
**Settings → Gemini Notebook** using its system-Chrome login. No Google Docs
API, OAuth client JSON, or copied authorization code is required. The saved
Notebook authorization stays on the Study Hub device and is checked live
before generation.

Each course exam receives its own NotebookLM notebook. The current lecture PDF
and cleaned transcript are the only two source IDs submitted for that lecture,
even if the NotebookLM website visually shows other checked sources. Uploaded
sources retain their canonical filed lecture names. Outlines are stored under
the exam's `Lecture Outlines` folder and render headings, emphasis, and nested
lists as formatted PDF content. Published quizzes are organized at the single
shareable `/public/quizzes` library by Course → Exam → Lecture and remain
available through **Take Lecture Quiz** on the private lecture page. Lectures
with an outline also show a **Lecture Outline** button in the shared library.
The native quiz
player gives feedback after every submitted question and supports highlighting
question text, crossing out answer choices, changing a choice before
submission, and restoring progress after a refresh. The shared library shows
Not started, In progress, or Completed using only that browser's local storage.

The branch setup, Google Cloud setup, Cloudflare quiz-sharing rule, acceptance
test, and rollback procedure are in
[docs/native-quizzes-nuc-rollout.md](docs/native-quizzes-nuc-rollout.md).
Anki automation remains a later milestone.

The multi-provider hotfix procedure is in
[docs/v2-multi-provider-nuc-rollout.md](docs/v2-multi-provider-nuc-rollout.md).
