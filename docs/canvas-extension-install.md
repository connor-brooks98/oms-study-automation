# Install the Canvas companion extension

1. On the NUC, update the project in `C:\Services\oms-study-automation` and rerun `scripts\install-windows.ps1`.
2. Open `chrome://extensions` in the same Chrome profile you use for LMU Canvas.
3. Turn on **Developer mode**, choose **Load unpacked**, and select `C:\Services\oms-study-automation\extension\canvas-hub`.
4. Open `http://127.0.0.1:8765/canvas/setup`, generate a pairing code, and enter it in the extension.
5. The extension sends the Hub only the active course IDs, names, and codes. Select the eight Fall 2026 courses on the setup page.

The extension can access only `https://lmunet.instructure.com/*` and the Hub at `http://127.0.0.1:8765/*`. It does not request cookie access and does not inspect grades, quizzes, submissions, assignments, discussions, announcements, or unrelated websites.

After an extension update, open `chrome://extensions` and choose **Reload** on OMS Study Hub Canvas Companion.
