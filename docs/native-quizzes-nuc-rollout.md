# Public Quiz Library and Gemini Notebook NUC Rollout

This rollout uses branch `codex/native-study-hub-quizzes`. It keeps the native
quiz player and formatted outline PDFs, replaces Google Docs indexes with one
shared Study Hub library, and reduces Google setup to Gemini Notebook login.

## 1. Update the NUC

Open PowerShell as Administrator. Study Hub must be stopped before reinstalling
so Windows releases `oms-hub.exe`.

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"
$Branch = "codex/native-study-hub-quizzes"

Set-Location $ProjectRoot
if (-not (Test-Path ".git")) {
    throw "$ProjectRoot is not the Git checkout."
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

git fetch origin
git switch $Branch
git pull --ff-only origin $Branch

.\.venv\Scripts\python.exe -m pip install -e .

Start-ScheduledTask -TaskName $TaskName

$Deadline = (Get-Date).AddSeconds(60)
$Health = $null
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
git rev-parse HEAD
```

The upgrade keeps existing lectures, prompts, Notebook mappings, Notebook
browser state, outlines, published quiz tokens, and quiz content. On startup it
removes only the retired Google Docs OAuth client file and Docs refresh-token
keys. Legacy database columns remain in place for rollback compatibility.

If installation reports that `oms-hub.exe` is in use, confirm the scheduled
task is stopped and close any PowerShell window running `oms-hub serve` before
running the install command again.

## 2. Confirm prompts and Gemini Notebook

Open **Settings → Notebook prompts** and select **Test file** for both the
outline and quiz prompts.

Open **Settings → Gemini Notebook**:

1. Select **Connect Notebook**.
2. Complete Google sign-in in the Chrome window on the NUC.
3. Return to Settings and select **Test connection**.
4. Confirm the card shows **Connected**.

There is no OAuth JSON upload and no Google Docs connection. Study Hub performs
a live Gemini Notebook check before queueing generation. If that login expires,
the job pauses until **Connect Notebook** succeeds again.

Notebook sources retain the canonical filed lecture names. Every outline and
quiz request sends exactly two source IDs: that lecture's current PDF and
current cleaned transcript.

## 3. Configure Cloudflare Access

Use one more-specific Cloudflare Access application for the shared area:

1. Create a Self-hosted application for the existing Study Hub hostname.
2. Set the path to `/public/quizzes*` so both `/public/quizzes` and its child
   quiz/outline routes are covered.
3. Add an Allow policy whose include rule is **Emails ending in** with
   `@lmunet.edu`.
4. Enable **One-time PIN** as the login method.
5. Keep the existing owner-only application on the rest of the hostname.

Do not create an Everyone or Bypass policy. The application itself exposes only
the quiz library, quiz player endpoints, and token-scoped current outline PDFs
below `/public/quizzes`; Cloudflare remains the classmate authentication gate.

## 4. Acceptance test

1. Generate an outline and quiz for one lecture.
2. Open `/public/quizzes`.
3. Confirm the lecture appears under the correct Course → Exam accordion.
4. Select **Lecture Outline** and confirm the same formatted PDF shown on the
   private lecture page opens.
5. Take the quiz, submit one answer, and return to the library.
6. Confirm its status changes to **In progress**.
7. Finish the quiz and confirm the status changes to **Completed**.
8. Regenerate the quiz and confirm the same public token remains while its
   status returns to **Not started** for the new version.
9. Select **Reset quiz progress** and confirm only Study Hub quiz progress in
   that browser is cleared.
10. Test the shared library in a private window with an `@lmunet.edu` one-time
    PIN, then confirm the private dashboard remains blocked.

Clearing browser cookies/site data also clears progress. No classmate identity,
completion record, answer, or analytics row is stored by Study Hub.

## 5. Roll back

Stop Study Hub, switch to the earlier known-good commit, reinstall, and restart.
The additive database schema and existing Notebook storage remain available.
The older build may require its previous Google Docs OAuth setup again if that
workflow is restored.
