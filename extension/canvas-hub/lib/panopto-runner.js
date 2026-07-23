import * as defaultHub from "./hub-client.js";

const HOME = "https://lmunet.hosted.panopto.com/Panopto/Pages/Home.aspx";
const SAFE_REASONS = new Set([
  "panopto_login_required",
  "transcript_processing",
  "english_captions_missing",
  "transcript_incomplete",
  "page_structure_changed",
  "browser_command_failed",
]);

function safeReason(error) {
  if (String(error?.message || "").includes("Receiving end does not exist")) {
    return "panopto_login_required";
  }
  return SAFE_REASONS.has(error?.code) ? error.code : "browser_command_failed";
}

async function pageMessage(tabs, tabId, type) {
  const response = await tabs.sendMessage(tabId, {type});
  if (response?.error) {
    throw Object.assign(new Error("Panopto page command failed"), {
      code: response.code,
    });
  }
  return response;
}

async function defaultWaitForReady(tabs, tabId) {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Panopto tab timed out"));
    }, 30000);
    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

export async function runPanoptoCommand(command, dependencies = {}) {
  const tabs = dependencies.tabs || chrome.tabs;
  const hub = dependencies.hub || defaultHub;
  const waitForReady = dependencies.waitForReady
    || ((tabId) => defaultWaitForReady(tabs, tabId));
  let tab;
  try {
    await hub.heartbeat("scanning");
    tab = await tabs.create({url: HOME, active: false});
    await waitForReady(tab.id);
    if (command.kind === "connection_check") {
      await pageMessage(tabs, tab.id, "panopto:connection-check");
    } else if (command.kind === "scan") {
      const discovery = await pageMessage(tabs, tab.id, "panopto:discover");
      const response = await hub.postDiscover({
        command_id: command.id,
        recordings: discovery.recordings,
      });
      for (const disposition of response.dispositions) {
        if (disposition.action !== "extract_transcript" || !disposition.viewer_url) {
          continue;
        }
        await tabs.update(tab.id, {url: disposition.viewer_url});
        await waitForReady(tab.id);
        const transcript = await pageMessage(tabs, tab.id, "panopto:extract");
        await hub.postTranscript({
          command_id: command.id,
          recording_id: disposition.recording_id,
          session_id: disposition.session_id,
          viewer_url: disposition.viewer_url,
          ...transcript,
        });
      }
    } else {
      throw Object.assign(new Error("Unsupported Panopto command"), {
        code: "browser_command_failed",
      });
    }
    await hub.postResult({
      command_id: command.id,
      status: "complete",
      reason_code: null,
    });
    await hub.heartbeat("connected");
    return {status: "complete"};
  } catch (error) {
    const reason = safeReason(error);
    await hub.postResult({
      command_id: command.id,
      status: "failed",
      reason_code: reason,
    }).catch(() => {});
    return {status: "failed", reason_code: reason};
  } finally {
    if (tab?.id !== undefined) await tabs.remove(tab.id).catch(() => {});
  }
}
