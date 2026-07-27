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

### Read-only Anki bridge

The first Anki milestone exports and indexes `Anking Step Deck`; it does not
tag notes, create notes, store media, or sync. On the NUC, expose only the
loopback Hub port to the tailnet:

```text
tailscale serve --bg 8765
tailscale serve status
```

Use the Tailscale hostname as the Mac agent's Hub URL. Store the shared bearer
value in Windows Credential Manager and macOS Keychain under service
`OMSStudyHub` and account `anki-agent-token`. Do not put it in the LaunchAgent,
shell history, `.env`, or a command-line flag.

On the Mac, install the package, confirm AnkiConnect v6 is listening only on
`127.0.0.1:8765`, then run:

```text
oms-anki-agent doctor
scripts/macos/install-anki-agent.sh \
  --hub-url https://study-hub.example.ts.net
launchctl print gui/$(id -u)/com.omsstudy.anki-agent
```

Rotate the bearer value in both Windows Credential Manager and macOS Keychain,
then restart the Hub and agent. Disable the private route with
`tailscale serve reset`. Confirm the public Cloudflare hostname does not expose
the agent family:

```text
curl -i https://study.example.com/agent/v1/heartbeat
# Expected: HTTP 404
```

The agent logs beneath `~/Library/Logs/OMSStudyHub`. A manual read-only export
is available with `oms-anki-agent snapshot --full`.

The multi-provider hotfix procedure is in
[docs/v2-multi-provider-nuc-rollout.md](docs/v2-multi-provider-nuc-rollout.md).
