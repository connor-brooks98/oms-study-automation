# Native Study Hub Quizzes NUC Rollout

This rollout uses branch `codex/native-study-hub-quizzes`. It replaces the
Gemini Quiz Gem browser handoff with a native, publicly shareable Study Hub
quiz while preserving the existing NotebookLM notebooks, prompt paths, lecture
files, outlines, Google Docs, and database.

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

The first start upgrades the SQLite schema to version 6. It retains the
additive native quiz tables and adds the durable NotebookLM source-title
registry. The migration does not delete or rewrite existing lecture data.

If installation again reports that `oms-hub.exe` is in use, confirm the
scheduled task is stopped, close any PowerShell window running `oms-hub serve`,
and then run the install command again.

## 2. Confirm prompts and Google

No Gemini Quiz Gem URL is needed. An old
`OMS_HUB_GEMINI_QUIZ_GEM_URL` line may be removed from `.env`; if left in place,
it is ignored.

Open **Settings → Notebook prompts** and use **Test file** for both the outline
and quiz prompts. The quiz prompt remains editable in Obsidian. Study Hub adds
the machine-readable JSON requirement automatically when it submits a quiz
request to NotebookLM.

Open **Settings → Google workspace**:

1. Select the Google Desktop app OAuth JSON once and choose **Save client
   file**. The page should show **Client file saved** after saving and after a
   reload. The file is required for private Google Docs; it is never displayed
   back in the browser.
2. Select **Connect Google**. If the saved Google Docs authorization is still
   valid, Study Hub reuses it without showing consent again. Otherwise,
   complete the Google Docs consent window.
3. Complete the NotebookLM login in the system Chrome window opened by the
   supported NotebookLM login command.
4. Return to Settings and select **Test connection**. This performs a live
   NotebookLM token fetch and Google account request; it does not rely only on
   the last saved green status.
5. Confirm NotebookLM and Google Docs both show **connected**.

Generation performs the same live check before adding a job. If either
authorization expires during a job, only that service is marked failed and
the job pauses so it can be retried after **Connect Google**.

NotebookLM sources use the canonical filed lecture names. For example, the
uploaded source title should read `Lecture 02 - Demyelinating Disease -
Transcript`, not an internal name such as
`OMS-1-cleaned_transcript-2ddfc6dcf8905f73`.

For every outline and quiz request, Study Hub sends exactly two source IDs:
the selected lecture's current PDF and its current cleaned transcript. The
checkboxes visible when a person opens NotebookLM are browser-interface state;
they do not control the explicit source IDs sent by Study Hub.

Gemini is no longer a required connection surface. The separate Gemini API
provider under **AI providers** is unchanged and may still be used for other
Study Hub processing.

## 3. Restrict shared quiz links to LMU organizational email

The Study Hub application accepts unauthenticated requests only below
`/public/quizzes/`, but Cloudflare Access remains the sharing gate. Give that
path its own, more-specific Access application so classmates can authenticate
without gaining access to the owner-only Study Hub.

In Cloudflare Zero Trust:

1. Open **Access controls → Applications**.
2. Add a **Self-hosted** application for the existing Study Hub hostname.
3. Set its path to `/public/quizzes/*`.
4. Add an **Allow** policy whose include rule is **Emails ending in** and enter
   `@lmunet.edu`.
5. Enable **One-time PIN** as the login method. This works with the
   organizational Microsoft mailbox without requiring Microsoft tenant admin
   access.
6. Save the application and confirm it is more specific than the existing
   whole-host Study Hub application.

Do not create an Everyone or Bypass policy. Do not apply the classmate rule to
`/`, `/static/*`, `/lectures/*`, `/settings/*`, or the entire hostname. The
main Study Hub application must continue requiring the owner's Cloudflare
Access identity.

## 4. One-lecture acceptance test

Use a lecture whose PDF and cleaned transcript already open normally.

1. Open the lecture and select **Generate Quiz**.
2. Watch the card move through NotebookLM, quiz validation, publication, and
   Google Docs synchronization.
3. Select **Take Lecture Quiz**.
4. Confirm the page shows one question at a time in the Study Hub style.
5. Select one answer, change to another, cross out and restore a different
   answer, and highlight part of the question.
6. Select **Submit Answer**.
7. Confirm the selection locks, correct/incorrect colors appear, and the expert
   rationale is shown.
8. Select **Continue**, refresh the browser, and confirm progress is retained.
9. Finish the quiz and confirm the score screen appears.
10. Select **Open Lecture Outline** and confirm headings are bold, nested
    bullets are indented, and Markdown stars are not printed as literal
    formatting characters.

Then confirm sharing:

1. Copy the quiz URL.
2. Open it in a private browser window not signed in to Cloudflare.
3. Confirm Cloudflare requests an email address. Verify a non-`@lmunet.edu`
   address is refused, then authenticate using an `@lmunet.edu` address and
   its emailed one-time PIN.
4. Confirm the quiz opens after the organizational login.
5. In that same private window, open the Study Hub hostname without the quiz
   path and confirm Cloudflare Access still blocks it.
6. Open the course quiz Google Doc and confirm the correct exam tab contains a
   hyperlink whose visible text is:

   ```text
   Lecture N Quiz
   ```

Regenerate the lecture quiz once. The public URL and Google Doc entry should
stay the same while the quiz content version changes.

## 5. Roll back

Rollback keeps the database, lecture files, generated outlines, native quiz
records, and Google session files. The older branch ignores the additive native
quiz table.

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"

Set-Location $ProjectRoot
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
git fetch origin
git switch codex/notebooklm-gemini-workflow
git pull --ff-only origin codex/notebooklm-gemini-workflow
.\.venv\Scripts\python.exe -m pip install -e .
Start-ScheduledTask -TaskName $TaskName
```

Do not delete `C:\ProgramData\OMSStudyHub` during rollback.
