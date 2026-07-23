# Phase 3 Panopto browser-session rollout

## Safety model

Phase 3 uses the existing paired OMS Study Hub Chrome companion and the user's
normal LMU Panopto session. It does not use a Panopto API client, client secret,
OAuth token, exported cookie, or separate browser profile. The extension scans
only recordings rendered in **Shared with Me**, extracts a transcript only for
a Hub-selected recording, and cannot edit or delete Panopto content.

Raw and cleaned revisions remain immutable under
`C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions`. Never delete that
folder or `hub.db` during installation or troubleshooting. Canonical
transcripts are filed only after validation under
`%USERPROFILE%\Documents\OMS II\<Subject>\Exam <number>\Transcripts`.

## Safe NUC update

Use an elevated PowerShell window. Stop the task and every Hub process first so
Windows does not lock `oms-hub.exe` or the SQLite database:

```powershell
Disable-ScheduledTask -TaskName "OMS Study Automation Hub"
Stop-ScheduledTask -TaskName "OMS Study Automation Hub" -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process |
    Where-Object {
        $_.ExecutablePath -like 'C:\Services\oms-study-automation\.venv\Scripts\*' -and
        $_.Name -in @('python.exe', 'pythonw.exe', 'oms-hub.exe')
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Set-Location C:\Services\oms-study-automation
git fetch origin
git switch feat/panopto-browser-companion
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1

Enable-ScheduledTask -TaskName "OMS Study Automation Hub"
Start-ScheduledTask -TaskName "OMS Study Automation Hub"
```

The installer preserves `.env`, `hub.db`, ProgramData revisions, Canvas
artifacts, the OMS II hierarchy, and the Obsidian prompt.

## Refresh the existing extension

1. Open `chrome://extensions` in the same Chrome profile used for Canvas.
2. Find **OMS Study Hub Browser Companion** and choose **Reload**.
3. Approve access to `lmunet.hosted.panopto.com` if Chrome asks.
4. Confirm the extension remains paired on the Canvas setup page.

Do not install a second extension or create a Panopto API client. The companion
has exact access only to LMU Canvas, LMU Panopto, and the local Hub; it has no
Chrome cookie permission. Chrome must remain running for browser commands.

## OpenAI prompt readiness

The OpenAI key already stored in Windows Credential Manager remains valid. If
needed, store or rotate it interactively:

```powershell
.\.venv\Scripts\oms-hub.exe openai-set-key
.\.venv\Scripts\oms-hub.exe panopto-init-prompt
```

Edit:

```text
C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md
```

Then approve the exact revision:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-approve-prompt
```

Editing the prompt later changes its hash and pauses cleaning until the new
revision is approved.

## Connect and run live acceptance

Open `http://127.0.0.1:8765/panopto/setup`.

1. Choose **Sign in to Panopto** and complete the Microsoft school login in the
   Chrome tab.
2. Return to the setup page and choose **Check connection**.
3. Run acceptance against approved recording
   `8796399e-393c-4256-b6e4-b48f0150d156`.
4. Confirm the recording metadata, MSK lecture match, transcript preview,
   destination preview, and immutable ProgramData path are correct.
5. Trigger the representative workflow and confirm the checklist completes in
   order: recording found, transcript downloaded, transcript cleaned,
   transcript filed.
6. Confirm `raw.txt` and `cleaned.txt` exist in one immutable revision folder
   and one canonical transcript exists in the MSK exam `Transcripts` folder.
7. Run the same scan again and confirm no new revision, OpenAI request, or
   canonical file is created.
8. Verify the established Canvas Neuro and Heme/Lymph scans still preserve
   originals, conversions, quarantine/replacement review, local filing, and
   Goodnotes staging.
9. Enable Panopto automation only after all checks pass.

## Daily operation and statuses

On weekdays with an Outlook-scheduled lecture, the Hub queues a scan every
15 minutes from 9:20 AM through 7:00 PM Eastern. A complete, confidently
matched transcript is cleaned automatically with `gpt-5.6-terra`; no dashboard
approval is required. No scheduled lecture means no automatic scan.

The main operational states are:

| Status | Meaning and action |
|---|---|
| `companion_unavailable` | Chrome is closed, the extension is not paired, or its heartbeat is stale. Start Chrome, reload the extension, and confirm pairing. |
| `panopto_login_required` | The temporary tab reached LMU/Microsoft sign-in instead of Panopto. Use **Sign in to Panopto**, finish login, then check the connection again. |
| `waiting_for_transcript` | The recording exists but Panopto has not exposed a complete English transcript. The next eligible scan retries it without consuming a failed attempt. |
| `needs_review` | Matching, language, prompt, or cleaning validation was not safe enough for automatic filing. Review and remap or retry in the Hub. |

Useful diagnostics:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-status
.\.venv\Scripts\oms-hub.exe panopto-scan-once
.\.venv\Scripts\oms-hub.exe panopto-worker-once
.\.venv\Scripts\oms-hub.exe panopto-recover
```

## Legacy credential cleanup

Only after browser-session acceptance passes, explicitly remove credentials
left by the abandoned API-client attempt:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-clear-legacy-credentials
```

This removes only the known legacy Panopto secret, refresh-token, and OAuth
state entries from Windows Credential Manager. It does not delete the OpenAI
key, database records, transcripts, jobs, or immutable revisions.

## Recovery and rollback

- Keep the Hub paused while diagnosing a browser or login problem.
- Run `panopto-recover` after an unexpected stop; it verifies immutable hashes
  before requeueing and recognizes an already-filed canonical copy.
- If the database reports read-only, stop all Hub processes and confirm the
  scheduled task runs as the intended user with write access to
  `C:\ProgramData\OMSStudyHub`.
- For rollback, pause Panopto, stop the scheduled task and Hub processes, and
  deploy the prior application revision.

Rollback must preserve `C:\ProgramData\OMSStudyHub\hub.db`, all Canvas and
Panopto revision roots, Canvas artifacts, and every canonical transcript in the
OMS II hierarchy. Do not delete or recreate the database to roll back code.
