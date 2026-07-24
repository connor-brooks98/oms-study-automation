import assert from "node:assert/strict";
import test from "node:test";

import {createCommandPoller} from "../lib/command-poller.js";

test("a long Canvas scan does not block the Panopto request", async () => {
  let finishCanvas;
  const canvasScan = new Promise((resolve) => {
    finishCanvas = resolve;
  });
  let panoptoRequests = 0;
  const poll = createCommandPoller({
    getConfig: async () => ({scan_requested: true}),
    runScan: async () => canvasScan,
    getPanoptoRequest: async () => {
      panoptoRequests += 1;
      return null;
    },
    runPanoptoRequest: async () => ({status: "complete"}),
    panoptoHub: {},
  });

  const result = await poll();

  assert.equal(result, null);
  assert.equal(panoptoRequests, 1);
  finishCanvas();
});

test("duplicate polls share one active recoverable request", async () => {
  let finish;
  const running = new Promise((resolve) => { finish = resolve; });
  let executions = 0;
  const poll = createCommandPoller({
    getConfig: async () => ({scan_requested: false}),
    runScan: async () => {},
    getPanoptoRequest: async () => ({id: "request", kind: "connection_test"}),
    runPanoptoRequest: async () => {
      executions += 1;
      return running;
    },
    panoptoHub: {},
  });

  const first = poll();
  const second = poll();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(executions, 1);
  finish({status: "complete"});
  await Promise.all([first, second]);
});
