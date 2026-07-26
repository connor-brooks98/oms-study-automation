# Study Hub V2 Multi-Provider NUC Rollout

This update adds secure dashboard credential entry, model selection, connection
testing, and transcript cleaning through OpenAI, Google Gemini, and Anthropic
Claude. It also adds a pre-LLM duplicate transcript confirmation and a
validated `.txt` download from the cleaned-transcript review page.

The hotfix does not contain credentials, `.env`, the Study Hub database,
transcripts, or user documents.

## Before installing

1. Generate a replacement OpenAI credential. Revoke the credential stored in
   the plaintext `GPT Key.pdf`; do not reuse it.
2. Copy these two files to the NUC Downloads folder:
   - `Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip`
   - `Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip.sha256`
3. Open PowerShell as Administrator.

## Verify, back up, and install

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$Hotfix = "$env:USERPROFILE\Downloads\Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip"
$ChecksumFile = "$Hotfix.sha256"
$TaskName = "OMS Study Hub V2"
$BackupRoot = Join-Path `
    $ProjectRoot `
    ("backups\multi-provider-" + (Get-Date -Format "yyyyMMdd-HHmmss"))

$ExpectedHash = ((Get-Content $ChecksumFile -Raw).Trim() -split "\s+")[0]
$ActualHash = (Get-FileHash $Hotfix -Algorithm SHA256).Hash.ToLower()

if ($ActualHash -ne $ExpectedHash.ToLower()) {
    throw "Hotfix checksum does not match. Do not install it."
}

New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
Copy-Item "$ProjectRoot\src" "$BackupRoot\src" -Recurse

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$Listener = Get-NetTCPConnection `
    -LocalPort 8765 `
    -State Listen `
    -ErrorAction SilentlyContinue

if ($Listener) {
    $Listener |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
}

Expand-Archive `
    -Path $Hotfix `
    -DestinationPath $ProjectRoot `
    -Force

Start-ScheduledTask -TaskName $TaskName

$Deadline = (Get-Date).AddSeconds(45)
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
    throw "Study Hub did not become healthy within 45 seconds."
}

$Health
Write-Host "Rollback backup: $BackupRoot"
```

Expected health output includes:

```text
status : ok
```

## Configure and test providers

1. Open `https://studyhub.perch-bird.com/settings`.
2. For OpenAI:
   - Paste the replacement credential.
   - Click **Save credential**.
   - Confirm the card says **Configured**.
   - Confirm the model is `gpt-5.2`, or enter another model available to the
     account.
   - Click **Test connection**.
   - Confirm the button turns green and says **Connected**.
3. Repeat for Google Gemini. The default model is `gemini-3.6-flash`.
4. Repeat for Anthropic Claude. The default model is `claude-sonnet-5`.
5. Select the desired **Active provider** and click **Use provider**.

If a test fails, the card turns red and identifies whether the problem is:

- Study Hub
- NUC networking
- Provider authentication
- Provider model access
- Provider quota or billing
- Provider service availability

Keep the displayed Study Hub reference and provider request ID when reviewing
the server log. Do not send or screenshot the credential.

## Acceptance test

Upload one short non-sensitive sample transcript with each provider active.
Confirm each job completes and the cleaned transcript opens normally. Switch
providers only between jobs.

Then verify the transcript safeguards:

1. Upload the exact same transcript again. Confirm the upload says it was
   already processed and that no API request was made.
2. Upload a different transcript that matches the same lecture. Confirm the
   Hub pauses and displays the detected course, lecture number, and topic.
3. Choose **Discard upload**. Confirm the new upload is discarded and the
   existing cleaned transcript still opens.
4. Repeat with another different file and choose **Process anyway**. Confirm
   one replacement job is queued and the normal replacement-review workflow
   follows.
5. Open the cleaned transcript and click **Download transcript**. Confirm the
   `.txt` filename contains the course, lecture number, topic, and
   `Transcript`, and that its content matches the reviewed transcript.

## Rollback

Use the backup path printed during installation:

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"
$BackupRoot = "PASTE-THE-PRINTED-BACKUP-PATH-HERE"
$FailedRoot = Join-Path `
    $ProjectRoot `
    ("failed-multi-provider-" + (Get-Date -Format "yyyyMMdd-HHmmss"))

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$Listener = Get-NetTCPConnection `
    -LocalPort 8765 `
    -State Listen `
    -ErrorAction SilentlyContinue

if ($Listener) {
    $Listener |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
}

Move-Item "$ProjectRoot\src" $FailedRoot
Copy-Item "$BackupRoot\src" "$ProjectRoot\src" -Recurse
Start-ScheduledTask -TaskName $TaskName
```

The database migration is additive. The prior V2 code ignores the new provider
settings table and provider attribution column.
