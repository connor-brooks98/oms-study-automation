const PREFIX = "OMSStudyHub/CanvasInbox/";
const SESSION_KEY = "managedCanvasDownloads";

async function managed() {
  return (await chrome.storage.session.get(SESSION_KEY))[SESSION_KEY] || {};
}

export async function downloadDisposition(disposition, metadata) {
  if (disposition.action !== "download") return null;
  if (!disposition.relative_filename || disposition.relative_filename.includes("..")) {
    throw new Error("Hub returned an unsafe managed filename");
  }
  const relative = disposition.relative_filename.replaceAll("\\", "-").replace(/^\/+/, "");
  const id = await chrome.downloads.download({
    url: metadata.download_url,
    filename: `${PREFIX}${relative}`,
    conflictAction: "uniquify",
    saveAs: false,
  });
  const records = await managed();
  records[String(id)] = {source_item_id: disposition.source_item_id};
  await chrome.storage.session.set({[SESSION_KEY]: records});
  return id;
}

export async function completedDownload(downloadId, report) {
  const records = await managed();
  const record = records[String(downloadId)];
  if (!record) return false;
  const results = await chrome.downloads.search({id: downloadId});
  if (results.length !== 1 || results[0].state !== "complete" || !results[0].filename) return false;
  await report({source_item_id: record.source_item_id, download_id: downloadId, path: results[0].filename});
  delete records[String(downloadId)];
  await chrome.storage.session.set({[SESSION_KEY]: records});
  return true;
}
