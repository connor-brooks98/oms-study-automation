import * as defaultHub from "./hub-client.js";
import {
  downloadPanoptoCaption,
  waitForPanoptoDownloadReport,
} from "./panopto-downloads.js";
import {newestSharedRecording} from "./panopto-page.js";

const SHARED_WITH_ME = "https://lmunet.hosted.panopto.com/Panopto/Pages/Sessions/List.aspx#isSharedWithMe=true";
const SAFE_REASONS = new Set([
  "panopto_login_required",
  "captions_pending",
  "no_shared_recordings",
  "page_structure_changed",
  "unsafe_caption_download",
  "panopto_tab_open_failed",
  "panopto_tab_load_failed",
  "panopto_page_message_failed",
  "panopto_hub_request_failed",
  "browser_request_failed",
]);

function safeReason(error, stage) {
  if (String(error?.message || "").includes("Receiving end does not exist")) {
    return "panopto_login_required";
  }
  if (SAFE_REASONS.has(error?.code)) return error.code;
  return {
    tab_open: "panopto_tab_open_failed",
    tab_load: "panopto_tab_load_failed",
    page_message: "panopto_page_message_failed",
    hub_request: "panopto_hub_request_failed",
  }[stage] || "browser_request_failed";
}

async function pageMessage(tabs, tabId, type, retryDelay) {
  let response;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      response = await tabs.sendMessage(tabId, {type});
      break;
    } catch (error) {
      if (error?.code === "panopto_login_required" || attempt === 19) throw error;
      await retryDelay();
    }
  }
  if (response?.error) {
    throw Object.assign(new Error("Panopto page command failed"), {
      code: response.code,
    });
  }
  return response;
}

export function waitForTabReady(tabs, tabId) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timeout;
    function finish(error) {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      tabs.onUpdated.removeListener(listener);
      if (error) reject(error);
      else resolve();
    }
    function listener(id, info) {
      if (id === tabId && info.status === "complete") finish();
    }
    tabs.onUpdated.addListener(listener);
    timeout = setTimeout(
      () => finish(new Error("Panopto tab timed out")),
      30000,
    );
    tabs.get(tabId)
      .then((tab) => {
        if (tab.status === "complete") finish();
      })
      .catch(finish);
  });
}

export function waitForPanoptoLogin(tabs, tabId) {
  return new Promise((resolve, reject) => {
    let timeout;
    function finish(error) {
      clearTimeout(timeout);
      tabs.onUpdated.removeListener(listener);
      if (error) reject(error);
      else resolve();
    }
    function listener(id, info, tab) {
      if (id !== tabId || info.status !== "complete") return;
      try {
        const url = new URL(tab?.url || info.url || "");
        if (url.hostname === "lmunet.hosted.panopto.com") finish();
      } catch {
        // Continue waiting while Microsoft redirects.
      }
    }
    tabs.onUpdated.addListener(listener);
    timeout = setTimeout(
      () => finish(Object.assign(new Error("Panopto sign-in timed out"), {
        code: "panopto_login_required",
      })),
      5 * 60 * 1000,
    );
  });
}

function waitForChromeDownload(downloadId) {
  return new Promise((resolve, reject) => {
    let timeout;
    function finish(error) {
      clearTimeout(timeout);
      chrome.downloads.onChanged.removeListener(listener);
      if (error) reject(error);
      else resolve();
    }
    function listener(delta) {
      if (delta.id !== downloadId) return;
      if (delta.error?.current) {
        finish(new Error("Panopto caption download failed"));
      } else if (delta.state?.current === "complete") {
        finish();
      }
    }
    chrome.downloads.onChanged.addListener(listener);
    timeout = setTimeout(
      () => finish(new Error("Panopto caption download timed out")),
      30000,
    );
    chrome.downloads.search({id: downloadId}).then((results) => {
      if (results[0]?.state === "complete") finish();
    }).catch(finish);
  });
}

const defaultDownloads = {
  start: downloadPanoptoCaption,
  async waitAndComplete(downloadId) {
    await waitForChromeDownload(downloadId);
    await waitForPanoptoDownloadReport(downloadId);
  },
};

async function messageWithLogin({
  tabs,
  tab,
  type,
  request,
  hub,
  retryDelay,
  waitForLogin,
}) {
  try {
    return await pageMessage(tabs, tab.id, type, retryDelay);
  } catch (error) {
    if (safeReason(error, "page_message") !== "panopto_login_required") throw error;
    await hub.postProgress(request.id, "awaiting_login", "sign_in_required");
    await tabs.update(tab.id, {active: true});
    await waitForLogin(tab.id);
    await hub.postProgress(request.id, "running", "sign_in_complete");
    return pageMessage(tabs, tab.id, type, retryDelay);
  }
}

async function downloadAndAcknowledge({
  descriptor,
  metadata,
  downloads,
  hub,
}) {
  const downloadId = await downloads.start(descriptor, metadata);
  await downloads.waitAndComplete(
    downloadId,
    (requestId, payload) => hub.postDownloadComplete(requestId, payload),
  );
}

export async function runPanoptoRequest(request, dependencies = {}) {
  const tabs = dependencies.tabs || chrome.tabs;
  const hub = dependencies.hub || defaultHub.panoptoRequestHub;
  const downloads = dependencies.downloads || defaultDownloads;
  const waitForReady = dependencies.waitForReady
    || ((tabId) => waitForTabReady(tabs, tabId));
  const waitForLogin = dependencies.waitForLogin
    || ((tabId) => waitForPanoptoLogin(tabs, tabId));
  const messageRetryDelay = dependencies.messageRetryDelay
    || (() => new Promise((resolve) => setTimeout(resolve, 250)));
  let tab;
  let stage = "hub_request";
  let keepOpen = false;
  try {
    await hub.postProgress(request.id, "running", "opening_shared");
    await hub.heartbeat("scanning");
    stage = "tab_open";
    tab = await tabs.create({
      url: SHARED_WITH_ME,
      active: (
        request.kind === "connection_test"
        || (request.kind === "scan" && request.payload?.manual === true)
      ),
    });
    stage = "tab_load";
    await waitForReady(tab.id);
    stage = "page_message";
    const discovery = await messageWithLogin({
      tabs,
      tab,
      type: "panopto:discover",
      request,
      hub,
      retryDelay: messageRetryDelay,
      waitForLogin,
    });

    if (request.kind === "connection_test") {
      const newest = newestSharedRecording(discovery.recordings);
      await hub.postProgress(request.id, "running", "opening_latest_recording");
      stage = "tab_load";
      await tabs.update(tab.id, {url: newest.viewer_url, active: true});
      await waitForReady(tab.id);
      stage = "page_message";
      const descriptor = await messageWithLogin({
        tabs,
        tab,
        type: "panopto:caption-download",
        request,
        hub,
        retryDelay: messageRetryDelay,
        waitForLogin,
      });
      if (descriptor.status === "captions_pending") {
        await hub.postResult(
          request.id,
          "waiting_for_captions",
          "captions_pending",
        );
        return {status: "waiting_for_captions", reason_code: "captions_pending"};
      }
      stage = "hub_request";
      await hub.postProgress(request.id, "running", "downloading_captions");
      await downloadAndAcknowledge({
        descriptor,
        metadata: {
          request_id: request.id,
          recording_id: null,
          session_id: newest.session_id,
          viewer_url: newest.viewer_url,
        },
        downloads,
        hub,
      });
      return {status: "complete"};
    }

    if (request.kind !== "scan") {
      throw Object.assign(new Error("Unsupported Panopto request"), {
        code: "browser_request_failed",
      });
    }
    stage = "hub_request";
    const response = await hub.postDiscovery(request.id, discovery.recordings);
    let captionsPending = false;
    for (const disposition of response.dispositions) {
      if (disposition.action !== "download_caption" || !disposition.viewer_url) {
        continue;
      }
      stage = "tab_load";
      await tabs.update(tab.id, {url: disposition.viewer_url, active: false});
      await waitForReady(tab.id);
      stage = "page_message";
      const descriptor = await messageWithLogin({
        tabs,
        tab,
        type: "panopto:caption-download",
        request,
        hub,
        retryDelay: messageRetryDelay,
        waitForLogin,
      });
      if (descriptor.status === "captions_pending") {
        captionsPending = true;
        continue;
      }
      stage = "hub_request";
      await downloadAndAcknowledge({
        descriptor,
        metadata: {
          request_id: request.id,
          recording_id: disposition.recording_id,
          session_id: disposition.session_id,
          viewer_url: disposition.viewer_url,
        },
        downloads,
        hub,
      });
    }
    if (captionsPending) {
      await hub.postResult(
        request.id,
        "waiting_for_captions",
        "captions_pending",
      );
      return {status: "waiting_for_captions", reason_code: "captions_pending"};
    }
    await hub.postResult(request.id, "complete", null);
    await hub.heartbeat("connected");
    return {status: "complete"};
  } catch (error) {
    const reason = safeReason(error, stage);
    keepOpen = reason === "panopto_login_required";
    await hub.postResult(request.id, "failed", reason).catch(() => {});
    return {status: "failed", reason_code: reason};
  } finally {
    if (!keepOpen && tab?.id !== undefined) {
      await tabs.remove(tab.id).catch(() => {});
    }
  }
}
