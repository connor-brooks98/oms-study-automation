import test from "node:test";
import assert from "node:assert/strict";
import {downloadDisposition} from "../lib/downloads.js";

test("downloads only approved dispositions into the managed inbox", async () => {
  const calls = [];
  global.chrome = {
    downloads: {download: async (value) => { calls.push(value); return 7; }},
    storage: {session: {get: async () => ({}), set: async () => {}}},
  };
  await downloadDisposition({action: "review", source_item_id: 1}, {download_url: "https://example"});
  await downloadDisposition({action: "download", source_item_id: 1, relative_filename: "1/2/file.pptx"}, {download_url: "https://lmunet.instructure.com/files/1/download"});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].filename, "OMSStudyHub/CanvasInbox/1/2/file.pptx");
});
