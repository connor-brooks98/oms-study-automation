# NotebookLM and Gemini Quiz NUC Rollout

This rollout uses branch `codex/notebooklm-gemini-workflow`. It keeps existing
Study Hub data in place and adds only database tables/columns and local Google
session files.

## 1. Update the NUC to the test branch

Open PowerShell as Administrator:

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"

Set-Location $ProjectRoot
git fetch origin
git switch codex/notebooklm-gemini-workflow
git pull --ff-only origin codex/notebooklm-gemini-workflow

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\playwright.exe install chromium
Start-ScheduledTask -TaskName $TaskName

$Deadline = (Get-Date).AddSeconds(60)
do {
    try {
        $Health = Invoke-RestMethod "http://127.0.0.1:8765/health"
    }
    catch {
        Start-Sleep -Seconds 2
    }
} until ($Health -or (Get-Date) -ge $Deadline)

if (-not $Health) {
    throw "Study Hub did not become healthy within 60 seconds."
}
$Health
```

## 2. Configure the Quiz Gem and prompts

Add the stable URL for the existing Gemini Quiz Gem to `.env`:

```text
OMS_HUB_GEMINI_QUIZ_GEM_URL=https://gemini.google.com/gem/YOUR-GEM-ID
OMS_HUB_GENERATION_TIMEOUT_SECONDS=180
```

Restart the scheduled task after editing `.env`.

Open Study Hub **Settings → Notebook prompts**. For each prompt:

1. Select **Select Path**.
2. Choose the matching Obsidian Markdown or text file in the NUC file browser.
3. Select **Save Path**.
4. Select **Test file**.

Study Hub reads the latest file content when a job starts, so later prompt
edits require no code change.

## 3. Connect Google once

In Google Cloud Console:

1. Enable the Google Docs API and Google Drive API.
2. Configure the OAuth consent screen for the Google account that owns the
   NotebookLM notebooks, Gemini Quiz Gem, and quiz documents.
3. While the OAuth app is in testing mode, add that email under **Test users**.
4. Create an OAuth client with application type **Desktop app**.
5. Download its JSON file to the NUC.

On the NUC itself, or through Remote Desktop, open **Settings → Google
workspace**:

1. Choose the downloaded **OAuth client JSON** and select **Save client file**.
2. Select **Connect Google**.
3. Complete OAuth consent in the browser.
4. In the Google Chrome window opened by Study Hub, sign in to the same
   account in Gemini and NotebookLM.
5. Leave the browser windows open until Study Hub finishes checking all three
   services.
6. Return to Settings and select **Test connection**. NotebookLM, Gemini, and
   Google Docs should each show **connected**.

If a service fails, Settings now names the affected surface and the next action.
Select **Connect Google** again after correcting the issue; each attempt requests
fresh consent and an abandoned sign-in attempt expires after five minutes.

The OAuth refresh token is kept in Windows Credential Manager. The OAuth client
file, browser profile, and NotebookLM storage state are owner-only files below
`C:\ProgramData\OMSStudyHub\google`; they must not be copied into Git or a
release archive.

## 4. One-lecture acceptance test

Use a lecture whose lecture PDF and cleaned transcript already open normally.

1. Select **Generate Outline**.
2. Wait for the card to show ready, then select **Open Lecture Outline**.
3. Confirm one PDF exists in:
   `Course\Exam #\Lecture Outlines`.
4. Select **Generate Quiz**.
5. Wait for **Take Lecture Quiz**, open it, and complete one question.
6. Open the course quiz Google Doc. Confirm the exam tab contains:
   `Lecture 1: https://gemini.google.com/share/...`
7. Open the quiz link in a signed-out/private browser and confirm classmates
   can access it.

Then test the boundaries:

1. Generate a second lecture in the same exam. Confirm the same NotebookLM
   notebook and Google Doc exam tab are used, with links in lecture order.
2. Generate a lecture in another exam. Confirm a different NotebookLM notebook
   and a different tab in the same course Google Doc are used.
3. Regenerate the first outline and quiz. Confirm the lecture page and Google
   Doc show only the new current result.
4. Start a job, restart the scheduled task, and confirm the job resumes without
   submitting a second Gemini quiz.

## Roll back to main

This keeps the database, uploaded lectures, generated outlines, and Google
session files:

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"

Set-Location $ProjectRoot
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
git fetch origin
git switch main
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Start-ScheduledTask -TaskName $TaskName
```

Do not delete `C:\ProgramData\OMSStudyHub` during rollback. The main branch
ignores the additive generation records and files.
