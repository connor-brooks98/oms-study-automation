# Phase 3 Panopto transcript rollout

## Safety model

Phase 3 uses a Panopto **Server-side Web Application** only for read
operations. A one-time LMU SSO sign-in connects the Hub as the signed-in
Panopto user. The Hub can search sessions, read session metadata, and download
captions; it has no recording, upload, edit, delete, sharing, or publishing
operation. The client secret, refresh credential, and OpenAI key live only in
Windows Credential Manager. OAuth access credentials remain in memory and are
refreshed automatically. The Hub never stores the Panopto password.

Raw and cleaned revisions are immutable under
`C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions`. Never delete that
folder or `hub.db` during troubleshooting. A cleaned transcript is copied into
`%USERPROFILE%\Documents\OMS II\<Subject>\Exam <number>\Transcripts` only after
UTF-8, checksum, approved-prompt, and cleaned-length validation pass.

## Update and install

Open an elevated PowerShell window:

```powershell
cd C:\Services\oms-study-automation
git pull
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\scripts\install-windows.ps1
```

The installer preserves an existing `.env`, creates the Panopto revision root,
and does not create a secret file or modify the Obsidian prompt.

In Panopto, create a new API client with these exact settings:

| Setting | Value |
|---|---|
| Client name | `OMS Study Hub NUC` |
| Client type | `Server-side Web Application` |
| CORS Origin URL | `https://localhost` |
| Redirect URL | `http://127.0.0.1:8765/panopto/oauth/callback` |

Put only its non-secret client ID in `.env`:

```dotenv
OMS_HUB_PANOPTO_CLIENT_ID=<Panopto Server-side Web Application client ID>
```

Do not add the client secret or OpenAI key to `.env`.

## Store credentials and approve the prompt

```powershell
.\.venv\Scripts\oms-hub.exe panopto-set-secret
.\.venv\Scripts\oms-hub.exe openai-set-key
.\.venv\Scripts\oms-hub.exe panopto-init-prompt
```

The `panopto-set-secret` command clears any prior Panopto user connection,
because a refresh credential belongs to a specific client ID and secret.

Edit:

```text
C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md
```

Then approve its exact SHA-256:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-approve-prompt
```

Any later edit changes the hash and pauses automatic cleaning until the new
prompt is reviewed and approved again.

## Connect the Panopto user

Start or restart the Hub, open
`http://127.0.0.1:8765/panopto/setup`, and choose **Connect Panopto**. Complete
the normal LMU Panopto SSO flow in the browser. Panopto redirects back to the
local Hub, which stores the refresh credential in Windows Credential Manager.

The setup page must show **Connected as Panopto user** before acceptance can
run. If the callback reports a redirect error, confirm the Panopto client uses
the exact Redirect URL above, including `http`, `127.0.0.1`, port `8765`, and
the complete path.

## Read-only acceptance while paused

Keep automatic discovery paused. Open
`http://127.0.0.1:8765/panopto/setup` and choose **Validate acceptance
session**. The check uses session
`8796399e-393c-4256-b6e4-b48f0150d156`.

Confirm:

1. Panopto refresh authentication succeeds as the connected Panopto user.
2. The session exposes `English_USA` captions.
3. The caption response is plain UTF-8 text, not an authentication page.
4. The corresponding MSK lecture match is correct.
5. The destination preview is the MSK exam transcript folder.
6. The immutable raw path is below the ProgramData Panopto revision root.

The acceptance action downloads only for validation and does not change
Panopto.

## Controlled automatic validation

Choose **Enable automatic discovery** only after every setup status is ready.
On weekdays with an Outlook-scheduled lecture, polling runs every 15 minutes
from 9:20 AM through 7:00 PM Eastern. Cleaning starts automatically without a
dashboard approval.

For the first representative lecture:

1. Confirm the four checklist steps complete in order: recording found,
   transcript downloaded, transcript cleaned, transcript filed.
2. Confirm `raw.txt` and `cleaned.txt` exist in one immutable revision folder.
3. Confirm one canonical `Lecture ## - Topic - Transcript.txt` exists under
   the subject and exam `Transcripts` folder.
4. Confirm the dashboard reports Terra input/output tokens and cost.
5. Run the same scan again and confirm there is no new revision, OpenAI
   request, or canonical file.
6. Use a controlled corrected-caption fixture and confirm it creates a new
   revision while the prior raw and cleaned files remain unchanged.
7. Re-run the verified Canvas Neuro and Heme/Lymph cases and confirm their
   originals, conversion, quarantine/replacement review, local filing, and
   Goodnotes staging behavior are unchanged.

## Daily operation

The Outlook schedule is the automatic polling gate. No scheduled lecture means
no automatic Panopto search. The first eligible poll includes the prior day's
missing transcript backfill. A caption that is not ready waits for a later
poll; it does not consume a failed attempt. Ambiguous matches, a changed
prompt, a non-US-English caption, or an unsafe cleaning result appears in
Panopto review.

Useful diagnostics:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-status
.\.venv\Scripts\oms-hub.exe panopto-scan-once
.\.venv\Scripts\oms-hub.exe panopto-worker-once
.\.venv\Scripts\oms-hub.exe panopto-recover
```

## Pause, retry, recovery, and rotation

- Pause discovery at `/panopto/setup`; queued durable work can still be
  inspected.
- Remap ambiguous recordings or retry reviewed/failed jobs at
  `/panopto/review`.
- Run `panopto-recover` after an unexpected stop. It verifies immutable hashes
  before requeueing and recognizes an already-filed canonical copy.
- To rotate the Panopto client secret, rerun `panopto-set-secret`, restart the
  Hub, and choose **Connect Panopto** again. To rotate the OpenAI key, rerun
  `openai-set-key`. Do not put replacements in command arguments, logs, or
  `.env`.
- **Disconnect Panopto** removes only the stored Panopto refresh credential,
  pauses discovery, and requires acceptance validation again. It does not
  delete transcripts, jobs, or immutable revisions.
- To rotate the prompt, edit the Obsidian note and explicitly approve the new
  hash.

## Rollback

Pause Panopto in the dashboard, stop the scheduled task, and deploy the prior
application revision. Preserve `C:\ProgramData\OMSStudyHub\hub.db`, both Canvas
and Panopto revision roots, and the canonical OMS II hierarchy. Rollback never
requires deleting artifacts. Resume only after diagnostics and read-only
acceptance pass.
