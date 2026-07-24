const adapterPromise = import(chrome.runtime.getURL("lib/panopto-page.js"));

async function sharedWithMe(adapter) {
  return adapter.waitForSharedRecordings(document, location);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    const adapter = await adapterPromise;
    if (message?.type === "panopto:connection-check") {
      await sharedWithMe(adapter);
      return {status: "connected"};
    }
    if (message?.type === "panopto:discover") {
      return {recordings: await sharedWithMe(adapter)};
    }
    if (message?.type === "panopto:caption-download") {
      return adapter.waitForCaptionDownload(document, location);
    }
    throw Object.assign(new Error("Unsupported Panopto page command"), {
      code: "page_structure_changed",
    });
  })().then(sendResponse).catch((error) => {
    sendResponse({
      error: true,
      code: error.code || "page_structure_changed",
    });
  });
  return true;
});
