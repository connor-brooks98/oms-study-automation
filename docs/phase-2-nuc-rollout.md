# Phase 2 NUC rollout

## Update and install

```powershell
cd C:\Services\oms-study-automation
git pull
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\scripts\install-windows.ps1
```

The installer preserves an existing `.env`, creates the Canvas inbox, study root, and immutable revision root, and starts the Hub in the signed-in interactive user session. Word and PowerPoint conversion require that interactive session; the Hub never closes an Office window it did not start.

## Safe enablement order

1. Install and pair the unpacked extension.
2. Map only Neuro and run discovery-only first.
3. Confirm a representative lecture PPTX, duplicate professor PDF, PQ document, and ignored reading.
4. Complete all eight mappings, confirm the local and iCloud Drive roots, and enable Neuro automatic processing.
5. Run the same scan again and confirm it creates no duplicate artifacts or review cards.
6. Expand to the other seven courses.
7. Exercise a controlled lecture revision and confirm it waits for approval.
8. Restart the NUC user session and confirm Chrome, the dashboard, extension heartbeat, and worker recovery.

## Daily operation

The extension scans every 30 minutes while Chrome is running. If LockDown Browser clears the Canvas session, the dashboard shows **Canvas login required**; sign back into LMU Canvas and choose **Scan now**. Goodnotes delivery means the PDF was checksum-verified into `iCloud Drive\OMS II Goodnotes Inbox`; import that visible staging file into Goodnotes when convenient.

Changed lecture files never replace the current version automatically. Use **Canvas review** to approve the replacement, keep the current version, or remap it. All originals and prior revisions remain under `C:\ProgramData\OMSStudyHub\artifacts\revisions`.

## Diagnostics and rollback

```powershell
.\.venv\Scripts\oms-hub.exe canvas-status
.\.venv\Scripts\oms-hub.exe canvas-worker-once
.\.venv\Scripts\oms-hub.exe canvas-recover
```

To pause Canvas safely, disable the unpacked extension and set `OMS_HUB_CANVAS_AUTO_PROCESS=false`. Do not delete the database or revision folder. Re-enable the extension after inspection.
