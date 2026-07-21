# OMS II Study Automation Hub

Phase 1 provides the authoritative lecture catalog, Outlook calendar matching,
the 21-step lecture checklist, and the local daily approval dashboard. It is
designed to run on a Windows 11 Pro NUC and bind only to the local computer.

## Prerequisites

- Windows 11 Pro
- Python 3.12
- The Fall 2026 tracker workbook
- A Microsoft Entra public-client application ID with delegated
  `Calendars.Read` permission

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

## Backup and recovery

Stop the hub before copying `C:\ProgramData\OMSStudyHub\hub.db`, or use SQLite's
online backup API. Restart failed or reviewed steps from the dashboard. Never
edit the SQLite database manually.

## Phase 1 limitations

Phase 1 does not yet download Canvas files, call Panopto, clean transcripts,
operate NotebookLM or Gemini, publish Google Docs links, import into Goodnotes,
or create Anki cards. Those connectors build on the catalog and checklist
delivered here.
