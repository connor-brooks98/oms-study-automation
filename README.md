# Medical School Study Hub V2

Study Hub V2 is a private, NUC-hosted library for manually supplied lecture
PowerPoints and Panopto transcript downloads. It imports an Excel exam tracker,
groups lectures by course and Exam, matches uploads, quarantines uncertain
matches, converts PowerPoints to PDF, cleans transcripts with the approved
Obsidian prompt, and files the resulting artifacts on the NUC and in iCloud.

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
.\.venv\Scripts\oms-hub.exe openai-set-key
.\.venv\Scripts\oms-hub.exe prompt-init
.\.venv\Scripts\oms-hub.exe prompt-fingerprint
```

Edit the prompt in Obsidian, copy the printed fingerprint into
`OMS_HUB_TRANSCRIPT_PROMPT_SHA256`, and restart the Hub. A prompt change pauses
cleaning until its new fingerprint is explicitly configured.

## NUC and remote access

The complete side-by-side installation, Cloudflare Access, backup, acceptance,
and rollback procedure is in [docs/v2-nuc-rollout.md](docs/v2-nuc-rollout.md).
The Hub remains bound to `127.0.0.1`; Cloudflare Tunnel provides outbound-only
remote connectivity, and the application independently verifies the Access
JWT and allowed email.

NotebookLM/Gemini automation and Anki automation are later milestones. Anki
already has a dedicated placeholder page in the interface.
