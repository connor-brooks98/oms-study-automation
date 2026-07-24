# Phase 3 Panopto browser-session rollout

## Safety model

Phase 3 uses the existing paired OMS Study Hub Chrome companion and the user's
normal LMU Panopto session. It does not use a Panopto API client, client secret,
OAuth token, exported cookie, or separate browser profile. The extension scans
only recordings rendered in **Shared with Me**, uses Panopto's built-in
**Download Captions** control for English (United States), and cannot edit or
delete Panopto content. It does not play lectures or scrape transcript-panel
lines.

Raw and cleaned revisions remain immutable under
`C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions`. Never delete that
folder or `hub.db` during installation or troubleshooting. Canonical
transcripts are filed only after validation under
`%USERPROFILE%\Documents\OMS II\<Subject>\Exam <number>\Transcripts`.
Chrome downloads first enter
`%USERPROFILE%\Downloads\OMSStudyHub\PanoptoInbox`; production files are
removed only after the immutable ProgramData original is verified. Invalid
managed files move to `C:\ProgramData\OMSStudyHub\quarantine\panopto`.

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
2. Find **OMS Study Hub Companion** and choose **Reload**.
3. Approve access to `lmunet.hosted.panopto.com` if Chrome asks.
4. Open `http://127.0.0.1:8765/setup` and confirm Canvas still shows paired.

Do not install a second extension or create a Panopto API client. The companion
has exact access only to LMU Canvas, LMU Panopto, and the local Hub; it has no
Chrome cookie permission. Chrome must remain running for browser requests. The
extension popup is only for pairing/repair diagnostics; it is not part of the
normal Panopto test flow.

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

Open `http://127.0.0.1:8765/setup`. The Setup Center must open on its overview;
do not click the extension popup during these checks.

1. **Logged-in one-click test:** choose **Sign in to Panopto**, finish the
   Microsoft school login, then choose **Test Panopto Connection** once in the
   Hub. Confirm an active tab immediately opens the newest recording under
   Shared with Me, downloads its English (United States) captions, closes, and
   reports a successful test. The temporary test caption must be deleted with
   no revision, cleaning request, canonical file, or checklist change.
2. **Logged-out continuation:** sign out of Panopto and run the same Hub test.
   Confirm the active tab remains open for sign-in and the same request
   continues automatically after login without an extension click.
3. **Captions-pending retry:** use a newly released recording whose captions
   are still processing. Confirm the Hub shows `waiting_for_captions`, creates
   no revision/OpenAI call/review item, and retries after 15 minutes within the
   weekday 9:20 AM–7:00 PM Eastern window.
4. **Real ingestion:** run a scan for a confidently matched lecture with
   captions. Confirm the checklist completes in order—recording found,
   transcript downloaded, transcript cleaned, transcript filed—and confirm
   immutable `raw.txt`/`cleaned.txt` plus one canonical transcript under the
   correct exam's `Transcripts` folder. Repeat unchanged captions to prove no
   duplicate revision or OpenAI request is created.
5. **Live overview:** while the test and scan run, leave the Setup Center open
   and confirm Canvas, Panopto, OpenAI, prompt, and Panopto progress update
   without refreshing the page.
6. **Restart recovery:** start a test or scan, restart the Hub/extension before
   it finishes, and confirm the persisted request resumes rather than
   disappearing or duplicating the download.

Finally, verify the established Canvas Neuro and Heme/Lymph scans still
preserve originals, conversions, quarantine/replacement review, local filing,
and Goodnotes staging. Enable Panopto automation only after all checks pass.

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
| `waiting_for_captions` | The recording exists but Panopto has not exposed the English (United States) caption download. The request returns to the eligible 15-minute polling lineup without a revision, OpenAI call, or review item. |
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
