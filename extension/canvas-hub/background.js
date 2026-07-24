import {postDownloadComplete} from "./lib/hub-client.js";
import {completedDownload} from "./lib/downloads.js";
import {createCommandPoller} from "./lib/command-poller.js";
import {runScan} from "./lib/scanner.js";
import {runPanoptoCommand} from "./lib/panopto-runner.js";
import {
  getConfig,
  getPanoptoCommand,
  panoptoHub,
} from "./lib/hub-client.js";

const ALARM = "canvas_scan";
const COMMAND_ALARM = "canvas_commands";
const pollCommands = createCommandPoller({
  getConfig,
  runScan,
  getPanoptoCommand,
  runPanoptoCommand,
  panoptoHub,
});

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
  if (alarm.name === COMMAND_ALARM) pollCommands().catch(() => {});
});
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "scan-now") {
    runScan().then(sendResponse).catch((error) => sendResponse({status: "error", error: String(error)}));
    return true;
  }
  if (message?.type === "panopto-scan-now") {
    pollCommands().then(sendResponse)
      .catch((error) => sendResponse({status: "error", error: String(error)}));
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
