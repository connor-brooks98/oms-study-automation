# OMS II Study Automation Hub

The Hub provides the authoritative lecture catalog, Outlook calendar matching,
the 21-step lecture checklist, Canvas lecture/PQ processing, Panopto transcript
cleaning, and the local daily review dashboard. It is designed to run on a
Windows 11 Pro NUC and bind only to the local computer.

## Prerequisites

- Windows 11 Pro
- Python 3.12
- The Fall 2026 tracker workbook
- A Microsoft Entra public-client application ID with delegated
  `Calendars.Read` permission
- Google Chrome, Microsoft PowerPoint, Microsoft Word, and iCloud for Windows
- A Panopto Server-side Web Application client ID and secret
- An OpenAI API key

## Local install

```powershell
cd C:\Services\oms-study-automation
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set `OMS_HUB_OUTLOOK_CLIENT_ID` in `.env`. Do not put tokens or passwords in
`.env`.

## One-time tracker import

```powershell
.\.venv\Scripts\oms-hub.exe import-tracker "C:\path\Fall 2026 Grades & Study Tracker Sheet.xlsx"
```

Review the imported counts and every ambiguous row in the dashboard. An
identical workbook cannot be imported twice, and the source workbook is never
modified.

## Outlook device login

```powershell
.\.venv\Scripts\oms-hub.exe outlook-login
```

Follow the displayed Microsoft device-login instructions. The token cache is
stored through Windows Credential Manager. The application requests only
delegated `Calendars.Read` access.

## Preview calendar matches

```powershell
.\.venv\Scripts\oms-hub.exe dry-run --date 2026-07-01
```

The preview prints proposed matches and does not change external-event or
checklist records. Once the matches look right, persist a 14-day window:

```powershell
.\.venv\Scripts\oms-hub.exe sync-outlook --days 14
```

## Start the dashboard

```powershell
.\.venv\Scripts\oms-hub.exe serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). If Outlook is configured,
the running service synchronizes at 5:00 AM and 5:00 PM Eastern. A failed sync
is logged and does not stop the dashboard.

## Install scheduled startup

From an elevated PowerShell window:

```powershell
.\scripts\install-windows.ps1
```

The task starts the hub when the NUC user signs in and restarts it after a
failure. No password or token is stored in either PowerShell script.

## Canvas setup

Follow [the Canvas companion installation guide](docs/canvas-extension-install.md), then complete `http://127.0.0.1:8765/canvas/setup`. The setup stays in discovery-only mode until the extension, eight course mappings, local/iCloud roots, and representative results are confirmed.

The extension scans mapped Canvas modules every 30 minutes using the existing Chrome session. New high-confidence lectures and professor practice questions are converted and filed automatically. Changed lectures wait in Canvas review and never replace current files without approval. See the [NUC rollout and recovery guide](docs/phase-2-nuc-rollout.md).

## Panopto transcript setup

Create a Panopto **Server-side Web Application** client with:

- CORS Origin URL: `https://localhost`
- Redirect URL: `http://127.0.0.1:8765/panopto/oauth/callback`

Put only its client ID in `.env` as `OMS_HUB_PANOPTO_CLIENT_ID`. Store the new
client secret and OpenAI key interactively:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-set-secret
.\.venv\Scripts\oms-hub.exe openai-set-key
.\.venv\Scripts\oms-hub.exe panopto-init-prompt
```

Start the Hub, open `http://127.0.0.1:8765/panopto/setup`, and choose
**Connect Panopto**. The initial LMU SSO sign-in grants read access as your
Panopto user. The refresh credential is stored in Windows Credential Manager;
the Hub never stores your Panopto password.

Edit `C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md`,
then approve that exact revision:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-approve-prompt
```

After connecting, complete read-only acceptance and enablement on the same
setup page. On Outlook-scheduled lecture days the
Hub polls every 15 minutes from 9:20 AM through 7:00 PM Eastern. It keeps
immutable raw and cleaned revisions in ProgramData, runs approved transcripts
through `gpt-5.6-terra` automatically, and files validated text under the
lecture's `Transcripts` folder. See the
[Phase 3 NUC rollout and recovery guide](docs/phase-3-nuc-rollout.md).

## Backup and recovery

Stop the hub before copying `C:\ProgramData\OMSStudyHub\hub.db`, or use SQLite's
online backup API. Restart failed or reviewed steps from the dashboard. Never
edit the SQLite database manually.

## Current limitations

The Hub does not yet operate NotebookLM or Gemini, publish Google Docs links,
control the Goodnotes UI, or create Anki cards. Canvas PDFs are staged in
ordinary iCloud Drive for manual Goodnotes import.
