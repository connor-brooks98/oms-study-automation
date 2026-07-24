const PREFIX = "OMSStudyHub/PanoptoInbox/";
const SESSION_KEY = "managedPanoptoDownloads";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function managed() {
  return (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY] || {};
}

function lmuUrl(value, label) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`unsafe Panopto ${label} URL`);
  }
  if (
    url.protocol !== "https:"
    || url.hostname !== "lmunet.hosted.panopto.com"
  ) {
    throw new Error(`unsafe Panopto ${label} URL`);
  }
  return url;
}

function validateMetadata(metadata) {
  if (!UUID.test(metadata.request_id || "") || !UUID.test(metadata.session_id || "")) {
    throw new Error("invalid Panopto download metadata");
  }
  if (
    metadata.recording_id !== null
    && (!Number.isSafeInteger(metadata.recording_id) || metadata.recording_id <= 0)
  ) {
    throw new Error("invalid Panopto recording ID");
  }
  const viewer = lmuUrl(metadata.viewer_url, "viewer");
  if (
    viewer.pathname !== "/Panopto/Pages/Viewer.aspx"
    || viewer.searchParams.get("id") !== metadata.session_id
  ) {
    throw new Error("invalid Panopto viewer URL");
  }
}

export async function downloadPanoptoCaption(descriptor, metadata) {
  validateMetadata(metadata);
  if (descriptor.status !== "ready" || descriptor.language !== "English_USA") {
    throw new Error("English (United States) captions are not ready");
  }
  const downloadUrl = lmuUrl(descriptor.download_url, "caption").toString();
  const filename = `${PREFIX}${metadata.request_id}/${metadata.session_id}-captions.txt`;
  const id = await chrome.downloads.download({
    url: downloadUrl,
    filename,
    conflictAction: "uniquify",
    saveAs: false,
  });
  const records = await managed();
  records[String(id)] = {
    request_id: metadata.request_id,
    recording_id: metadata.recording_id,
    session_id: metadata.session_id,
    viewer_url: metadata.viewer_url,
    language: "English_USA",
  };
  await chrome.storage.session.set({[SESSION_KEY]: records});
  return id;
}

export async function completePanoptoDownload(downloadId, report) {
  const records = await managed();
  const record = records[String(downloadId)];
  if (!record) return false;
  const results = await chrome.downloads.search({id: downloadId});
  if (
    results.length !== 1
    || results[0].state !== "complete"
    || !results[0].filename
  ) {
    return false;
  }
  await report(record.request_id, {
    recording_id: record.recording_id,
    session_id: record.session_id,
    viewer_url: record.viewer_url,
    language: record.language,
    chrome_download_id: downloadId,
    path: results[0].filename,
  });
  delete records[String(downloadId)];
  await chrome.storage.session.set({[SESSION_KEY]: records});
  return true;
}

export async function waitForPanoptoDownloadReport(
  downloadId,
  options = {},
) {
  const maxAttempts = options.maxAttempts ?? 300;
  const settle = options.settle
    ?? (() => new Promise((resolve) => setTimeout(resolve, 100)));
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const records = await managed();
    if (!records[String(downloadId)]) return true;
    await settle();
  }
  throw new Error("Panopto caption report timed out");
}
