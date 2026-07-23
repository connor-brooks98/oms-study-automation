# Install the OMS Study Hub browser companion

The same extension handles Canvas and Panopto. It reuses one Hub pairing but
keeps the two workflows isolated.

1. On the NUC, update the project in `C:\Services\oms-study-automation` and rerun `scripts\install-windows.ps1` with PowerShell's one-process execution-policy bypass if required.
2. Open `chrome://extensions` in the same Chrome profile you use for LMU Canvas.
3. Turn on **Developer mode**, choose **Load unpacked**, and select `C:\Services\oms-study-automation\extension\canvas-hub`.
4. Open `http://127.0.0.1:8765/canvas/setup`, generate a pairing code, and enter it in the extension.
5. The extension sends the Hub only the active course IDs, names, and codes. Select the eight Fall 2026 courses on the setup page.

The extension can access only LMU Canvas, LMU Panopto, and the local Hub. It
does not request Chrome cookie access, export session data, or inspect unrelated
websites. Panopto work occurs in a temporary inactive tab created and closed by
the extension; it scans only recordings rendered in **Shared with Me** and
transcripts for recordings selected by the Hub.

After an extension update, open `chrome://extensions`, choose **Reload** on OMS
Study Hub Browser Companion, and approve LMU Panopto access if Chrome asks.
Chrome must remain running for Canvas and Panopto browser commands.
