import assert from "node:assert/strict";
import test from "node:test";

import {
  completePanoptoDownload,
  downloadPanoptoCaption,
} from "../lib/panopto-downloads.js";

const REQUEST_ID = "84729a54-a9a7-4835-9535-e44f8bbcb375";
const SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156";
const VIEWER = `https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=${SESSION_ID}`;

function chromeHarness() {
  let stored = {};
  const calls = [];
  global.chrome = {
    downloads: {
      download: async (value) => { calls.push(value); return 17; },
      search: async () => [{
        id: 17,
        state: "complete",
        filename: `C:\\Users\\conbr\\Downloads\\OMSStudyHub\\PanoptoInbox\\${REQUEST_ID}\\captions.txt`,
      }],
    },
    storage: {
      session: {
        get: async () => stored,
        set: async (value) => { stored = value; },
      },
    },
  };
  return {calls, stored: () => stored};
}

test("downloads captions into a request-confined managed filename", async () => {
  const harness = chromeHarness();
  const id = await downloadPanoptoCaption(
    {
      status: "ready",
      language: "English_USA",
      download_url: "https://lmunet.hosted.panopto.com/Panopto/captions.txt",
      filename: "../../unsafe.txt",
    },
    {
      request_id: REQUEST_ID,
      recording_id: 3,
      session_id: SESSION_ID,
      viewer_url: VIEWER,
    },
  );

  assert.equal(id, 17);
  assert.deepEqual(harness.calls[0], {
    url: "https://lmunet.hosted.panopto.com/Panopto/captions.txt",
    filename: `OMSStudyHub/PanoptoInbox/${REQUEST_ID}/${SESSION_ID}-captions.txt`,
    conflictAction: "uniquify",
    saveAs: false,
  });
  assert.equal(
    harness.stored().managedPanoptoDownloads["17"].request_id,
    REQUEST_ID,
  );
});

test("reports a completed managed caption and clears recovery state", async () => {
  const harness = chromeHarness();
  await downloadPanoptoCaption(
    {
      status: "ready",
      language: "English_USA",
      download_url: "https://lmunet.hosted.panopto.com/Panopto/captions.txt",
    },
    {
      request_id: REQUEST_ID,
      recording_id: null,
      session_id: SESSION_ID,
      viewer_url: VIEWER,
    },
  );
  const reports = [];

  const completed = await completePanoptoDownload(
    17,
    async (requestId, payload) => reports.push({requestId, payload}),
  );

  assert.equal(completed, true);
  assert.equal(reports[0].requestId, REQUEST_ID);
  assert.equal(reports[0].payload.path.includes("PanoptoInbox"), true);
  assert.equal(reports[0].payload.language, "English_USA");
  assert.deepEqual(harness.stored().managedPanoptoDownloads, {});
});

test("rejects caption URLs outside the LMU tenant", async () => {
  chromeHarness();
  await assert.rejects(
    downloadPanoptoCaption(
      {
        status: "ready",
        language: "English_USA",
        download_url: "https://evil.example/captions.txt",
      },
      {
        request_id: REQUEST_ID,
        recording_id: null,
        session_id: SESSION_ID,
        viewer_url: VIEWER,
      },
    ),
    /unsafe Panopto caption URL/,
  );
});
