const adapterPromise = import(chrome.runtime.getURL("lib/panopto-page.js"));

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function sharedWithMe(adapter) {
  if (adapter.isLoginRequired(document, location)) {
    throw Object.assign(new Error("Sign in to Panopto"), {
      code: "panopto_login_required",
    });
  }
  try {
    return adapter.readSharedRecordings(document);
  } catch (error) {
    if (error.code !== "page_structure_changed") throw error;
  }
  const control = [...document.querySelectorAll("a,button")].find(
    (item) => (item.textContent || "").trim().toLowerCase() === "shared with me",
  );
  if (!control) {
    throw Object.assign(new Error("Shared with Me was not found"), {
      code: "page_structure_changed",
    });
  }
  control.click();
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await wait(250);
    try {
      return adapter.readSharedRecordings(document);
    } catch (error) {
      if (error.code !== "page_structure_changed") throw error;
    }
  }
  throw Object.assign(new Error("Shared with Me did not load"), {
    code: "page_structure_changed",
  });
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
    if (message?.type === "panopto:extract") {
      const transcript = await adapter.readTranscript(document);
      return {language: "English_USA", ...transcript};
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
