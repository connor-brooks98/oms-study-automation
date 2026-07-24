import assert from "node:assert/strict";
import test from "node:test";

import {
  runPanoptoRequest,
  waitForTabReady,
} from "../lib/panopto-runner.js";

const REQUEST_ID = "84729a54-a9a7-4835-9535-e44f8bbcb375";
const SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156";
const VIEWER = `https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=${SESSION_ID}`;
const SHARED_WITH_ME = "https://lmunet.hosted.panopto.com/Panopto/Pages/Sessions/List.aspx#isSharedWithMe=true";
const RECORDING = {
  session_id: SESSION_ID,
  name: "Shoulder",
  created_utc: "2026-07-23T13:05:00.000Z",
  duration_seconds: 3600,
  folder_name: "Shared with Me",
  viewer_url: VIEWER,
};

function fakeTabs(messages = []) {
  return {
    created: [],
    updated: [],
    removed: [],
    current: {id: 42, status: "complete", url: SHARED_WITH_ME},
    async create(value) {
      const tab = {id: 42, status: "complete", ...value};
      this.created.push(tab);
      this.current = tab;
      return tab;
    },
    async update(id, value) {
      this.updated.push({id, ...value});
      this.current = {...this.current, ...value};
      return this.current;
    },
    async remove(id) { this.removed.push(id); },
    async sendMessage() { return messages.shift(); },
    async get() { return this.current; },
  };
}

function fakeHub(dispositions = []) {
  return {
    progress: [],
    discoveries: [],
    results: [],
    heartbeats: [],
    async heartbeat(state) { this.heartbeats.push(state); },
    async postProgress(id, state, progress) {
      this.progress.push({id, state, progress});
    },
    async postDiscovery(id, recordings) {
      this.discoveries.push({id, recordings});
      return {dispositions};
    },
    async postResult(id, status, reasonCode) {
      this.results.push({id, status, reasonCode});
    },
  };
}

function fakeDownloads() {
  return {
    started: [],
    async start(descriptor, metadata) {
      this.started.push({descriptor, metadata});
      return 17;
    },
    async waitAndComplete() { return true; },
  };
}

test("tab readiness recognizes a viewer that already finished loading", async () => {
  const listeners = new Set();
  const tabs = {
    onUpdated: {
      addListener(listener) { listeners.add(listener); },
      removeListener(listener) { listeners.delete(listener); },
    },
    async get(id) {
      return {id, status: "complete"};
    },
  };

  await waitForTabReady(tabs, 42);

  assert.equal(listeners.size, 0);
});

test("connection test opens visibly, selects newest recording, and downloads", async () => {
  const older = {...RECORDING, session_id: "old", created_utc: "2026-07-23T12:00:00Z"};
  const tabs = fakeTabs([
    {recordings: [older, RECORDING]},
    {
      status: "ready",
      language: "English_USA",
      download_url: "https://lmunet.hosted.panopto.com/Panopto/captions.txt",
      filename: "captions.txt",
    },
  ]);
  const hub = fakeHub();
  const downloads = fakeDownloads();

  const result = await runPanoptoRequest(
    {id: REQUEST_ID, kind: "connection_test", state: "requested", payload: {}},
    {tabs, hub, downloads, waitForReady: async () => {}},
  );

  assert.equal(result.status, "complete");
  assert.deepEqual(tabs.created[0], {
    id: 42,
    status: "complete",
    url: SHARED_WITH_ME,
    active: true,
  });
  assert.deepEqual(tabs.updated[0], {id: 42, url: VIEWER, active: true});
  assert.equal(downloads.started[0].metadata.recording_id, null);
  assert.equal(downloads.started[0].metadata.session_id, SESSION_ID);
  assert.deepEqual(tabs.removed, [42]);
});

test("scheduled scan uses an inactive tab and managed caption downloads", async () => {
  const tabs = fakeTabs([
    {recordings: [RECORDING]},
    {
      status: "ready",
      language: "English_USA",
      download_url: "https://lmunet.hosted.panopto.com/Panopto/captions.txt",
    },
  ]);
  const hub = fakeHub([{
    recording_id: 7,
    session_id: SESSION_ID,
    action: "download_caption",
    viewer_url: VIEWER,
    reason: "matched",
  }]);
  const downloads = fakeDownloads();

  await runPanoptoRequest(
    {id: REQUEST_ID, kind: "scan", state: "requested", payload: {manual: false}},
    {tabs, hub, downloads, waitForReady: async () => {}},
  );

  assert.equal(tabs.created[0].active, false);
  assert.equal(hub.discoveries[0].recordings.length, 1);
  assert.equal(downloads.started[0].metadata.recording_id, 7);
  assert.deepEqual(hub.results, [{
    id: REQUEST_ID,
    status: "complete",
    reasonCode: null,
  }]);
});

test("manual scan opens visibly for immediate Hub feedback", async () => {
  const tabs = fakeTabs([{recordings: [RECORDING]}]);
  const hub = fakeHub([]);

  await runPanoptoRequest(
    {id: REQUEST_ID, kind: "scan", state: "requested", payload: {manual: true}},
    {tabs, hub, downloads: fakeDownloads(), waitForReady: async () => {}},
  );

  assert.equal(tabs.created[0].active, true);
});

test("missing captions return the request to the polling lineup", async () => {
  const tabs = fakeTabs([
    {recordings: [RECORDING]},
    {status: "captions_pending"},
  ]);
  const hub = fakeHub([{
    recording_id: 7,
    session_id: SESSION_ID,
    action: "download_caption",
    viewer_url: VIEWER,
    reason: "matched",
  }]);

  const result = await runPanoptoRequest(
    {id: REQUEST_ID, kind: "scan", state: "requested", payload: {manual: false}},
    {tabs, hub, downloads: fakeDownloads(), waitForReady: async () => {}},
  );

  assert.equal(result.status, "waiting_for_captions");
  assert.deepEqual(hub.results, [{
    id: REQUEST_ID,
    status: "waiting_for_captions",
    reasonCode: "captions_pending",
  }]);
});

test("login in the same visible tab continues the connection test", async () => {
  const tabs = fakeTabs();
  let messages = 0;
  tabs.sendMessage = async () => {
    messages += 1;
    if (messages === 1) {
      const error = new Error("Receiving end does not exist");
      error.code = "panopto_login_required";
      throw error;
    }
    if (messages === 2) return {recordings: [RECORDING]};
    return {
      status: "ready",
      language: "English_USA",
      download_url: "https://lmunet.hosted.panopto.com/Panopto/captions.txt",
    };
  };
  const hub = fakeHub();
  let continued = false;

  const result = await runPanoptoRequest(
    {id: REQUEST_ID, kind: "connection_test", state: "requested", payload: {}},
    {
      tabs,
      hub,
      downloads: fakeDownloads(),
      waitForReady: async () => {},
      messageRetryDelay: async () => {},
      waitForLogin: async () => { continued = true; },
    },
  );

  assert.equal(result.status, "complete");
  assert.equal(continued, true);
  assert.equal(tabs.updated.some((value) => value.active === true), true);
  assert.equal(
    hub.progress.some((value) => value.state === "awaiting_login"),
    true,
  );
});
