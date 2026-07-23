import assert from "node:assert/strict";
import test from "node:test";

import {runPanoptoCommand} from "../lib/panopto-runner.js";

const COMMAND = {id: "command-id", kind: "scan", payload: {manual: true}};
const SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156";
const VIEWER = `https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=${SESSION_ID}`;

function fakeTabs(messages = []) {
  return {
    created: [],
    updated: [],
    removed: [],
    async create(value) {
      const tab = {id: 42, ...value};
      this.created.push(tab);
      return tab;
    },
    async update(id, value) { this.updated.push({id, ...value}); },
    async remove(id) { this.removed.push(id); },
    async sendMessage() { return messages.shift(); },
  };
}

function fakeHub(dispositions = []) {
  return {
    discoveries: [],
    transcripts: [],
    results: [],
    heartbeats: [],
    async heartbeat(state) { this.heartbeats.push(state); },
    async postDiscover(value) {
      this.discoveries.push(value);
      return {dispositions};
    },
    async postTranscript(value) { this.transcripts.push(value); },
    async postResult(value) { this.results.push(value); },
  };
}

test("runner creates and removes only its own inactive tab", async () => {
  const tabs = fakeTabs([{recordings: []}]);
  const hub = fakeHub();

  await runPanoptoCommand(COMMAND, {
    tabs,
    hub,
    waitForReady: async () => {},
  });

  assert.equal(tabs.created[0].active, false);
  assert.deepEqual(tabs.removed, [42]);
  assert.deepEqual(tabs.updated, []);
  assert.equal(hub.results[0].status, "complete");
});

test("matched recording is extracted and posted without page HTML", async () => {
  const tabs = fakeTabs([
    {recordings: [{
      session_id: SESSION_ID,
      name: "Shoulder",
      created_utc: "2026-07-23T13:05:00.000Z",
      duration_seconds: 3600,
      folder_name: "Shared with Me",
      viewer_url: VIEWER,
    }]},
    {
      language: "English_USA",
      line_count: 2,
      complete: true,
      text: "00:01 First\n00:03 Second",
    },
  ]);
  const hub = fakeHub([{
    recording_id: 7,
    session_id: SESSION_ID,
    action: "extract_transcript",
    viewer_url: VIEWER,
    reason: "matched",
  }]);

  await runPanoptoCommand(COMMAND, {
    tabs,
    hub,
    waitForReady: async () => {},
  });

  assert.deepEqual(tabs.updated, [{id: 42, url: VIEWER}]);
  assert.equal(hub.transcripts[0].recording_id, 7);
  assert.equal(hub.transcripts[0].text, "00:01 First\n00:03 Second");
  assert.equal("html" in hub.transcripts[0], false);
});

test("login requirement is reported once and tab is still removed", async () => {
  const tabs = fakeTabs([]);
  tabs.sendMessage = async () => {
    const error = new Error("Sign in required");
    error.code = "panopto_login_required";
    throw error;
  };
  const hub = fakeHub();

  const result = await runPanoptoCommand(COMMAND, {
    tabs,
    hub,
    waitForReady: async () => {},
  });

  assert.equal(result.reason_code, "panopto_login_required");
  assert.deepEqual(tabs.removed, [42]);
  assert.deepEqual(hub.results, [{
    command_id: "command-id",
    status: "failed",
    reason_code: "panopto_login_required",
  }]);
});

test("content-script reason codes are preserved without raw errors", async () => {
  const tabs = fakeTabs([{
    error: true,
    code: "transcript_processing",
  }]);
  const hub = fakeHub();

  const result = await runPanoptoCommand(COMMAND, {
    tabs,
    hub,
    waitForReady: async () => {},
  });

  assert.equal(result.reason_code, "transcript_processing");
});

test("SSO redirect without a Panopto content script requests sign-in", async () => {
  const tabs = fakeTabs();
  tabs.sendMessage = async () => {
    throw new Error("Could not establish connection. Receiving end does not exist.");
  };

  const result = await runPanoptoCommand(COMMAND, {
    tabs,
    hub: fakeHub(),
    waitForReady: async () => {},
  });

  assert.equal(result.reason_code, "panopto_login_required");
});
