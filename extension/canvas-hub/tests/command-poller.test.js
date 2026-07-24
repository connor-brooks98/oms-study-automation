import assert from "node:assert/strict";
import test from "node:test";

import {createCommandPoller} from "../lib/command-poller.js";

test("a long Canvas scan does not block the Panopto command request", async () => {
  let finishCanvas;
  const canvasScan = new Promise((resolve) => {
    finishCanvas = resolve;
  });
  let panoptoRequests = 0;
  const poll = createCommandPoller({
    getConfig: async () => ({scan_requested: true}),
    runScan: async () => canvasScan,
    getPanoptoCommand: async () => {
      panoptoRequests += 1;
      return null;
    },
    runPanoptoCommand: async () => ({status: "complete"}),
    panoptoHub: {},
  });

  const result = await poll();

  assert.equal(result, null);
  assert.equal(panoptoRequests, 1);
  finishCanvas();
});
