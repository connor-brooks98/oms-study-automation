import {postDownloadComplete} from "./lib/hub-client.js";
import {completedDownload} from "./lib/downloads.js";
import {runScan} from "./lib/scanner.js";

const ALARM = "canvas_scan";
const COMMAND_ALARM = "canvas_commands";

async function ensureAlarm() {
  if (!await chrome.alarms.get(ALARM)) {
    await chrome.alarms.create(ALARM, {delayInMinutes: 1, periodInMinutes: 30});
  }
  if (!await chrome.alarms.get(COMMAND_ALARM)) {
    await chrome.alarms.create(COMMAND_ALARM, {delayInMinutes: 1, periodInMinutes: 1});
  }
}

chrome.runtime.onInstalled.addListener(ensureAlarm);
chrome.runtime.onStartup.addListener(ensureAlarm);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) runScan().catch(() => {});
  if (alarm.name === COMMAND_ALARM) {
    import("./lib/hub-client.js").then(async ({getConfig}) => {
      const config = await getConfig();
      if (config.scan_requested) await runScan();
    }).catch(() => {});
  }
});
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "scan-now") {
    runScan().then(sendResponse).catch((error) => sendResponse({status: "error", error: String(error)}));
    return true;
  }
  return false;
});
chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state?.current === "complete") {
    completedDownload(delta.id, postDownloadComplete).catch(() => {});
  }
});
ensureAlarm();
