import assert from "node:assert/strict";
import test from "node:test";

import {
  PanoptoPageError,
  isLoginRequired,
  readSharedRecordings,
  readTranscript,
  waitForSharedRecordings,
} from "../lib/panopto-page.js";

const SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156";
const VIEWER = `https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=${SESSION_ID}`;

function node({text = "", attrs = {}, one = {}, many = {}} = {}) {
  return {
    textContent: text,
    scrollTop: 0,
    scrollHeight: 100,
    clientHeight: 20,
    getAttribute(name) { return attrs[name] ?? null; },
    querySelector(selector) { return one[selector] ?? null; },
    querySelectorAll(selector) { return many[selector] ?? []; },
  };
}

function sharedDocument() {
  const link = node({text: "6H. MSK Shoulder Disease Injury", attrs: {href: VIEWER}});
  const time = node({attrs: {datetime: "2026-07-23T13:05:00Z"}});
  const duration = node({text: "1:00:00"});
  const folder = node({text: "Shared with Me"});
  const row = node({
    one: {
      ".item-title.title-link a.detail-title": link,
      "time": time,
      "[data-duration]": duration,
      ".folder-name": folder,
    },
  });
  const container = node({many: {"tbody tr.list-view-row": [row]}});
  return node({one: {"#listViewContainer": container}});
}

test("detects Microsoft or Panopto login pages", () => {
  assert.equal(isLoginRequired(node(), {hostname: "login.microsoftonline.com"}), true);
  assert.equal(
    isLoginRequired(node({one: {"form[action*='Login']": node()}}), {
      hostname: "lmunet.hosted.panopto.com",
    }),
    true,
  );
});

test("missing Shared with Me list fails closed", () => {
  assert.throws(
    () => readSharedRecordings(node()),
    (error) => error instanceof PanoptoPageError
      && error.code === "page_structure_changed",
  );
});

test("normalizes only bounded recording metadata", () => {
  const result = readSharedRecordings(sharedDocument());

  assert.deepEqual(result, [{
    session_id: SESSION_ID,
    name: "6H. MSK Shoulder Disease Injury",
    created_utc: "2026-07-23T13:05:00.000Z",
    duration_seconds: 3600,
    folder_name: "Shared with Me",
    viewer_url: VIEWER,
  }]);
});

test("waits for the Shared with Me list to render", async () => {
  const ready = sharedDocument();
  let attempts = 0;
  const delayed = node();
  delayed.querySelector = (selector) => {
    if (selector === "#listViewContainer") {
      attempts += 1;
      return attempts >= 3 ? ready.querySelector(selector) : null;
    }
    return null;
  };

  const result = await waitForSharedRecordings(
    delayed,
    {hostname: "lmunet.hosted.panopto.com"},
    {maxAttempts: 5, settle: async () => {}},
  );

  assert.equal(result.length, 1);
  assert.equal(attempts, 3);
});

test("rejects a recording link outside LMU Panopto", () => {
  const document = sharedDocument();
  document.querySelector("#listViewContainer")
    .querySelectorAll("tbody tr.list-view-row")[0]
    .querySelector(".item-title.title-link a.detail-title")
    .getAttribute = () => "https://evil.example/viewer";

  assert.throws(
    () => readSharedRecordings(document),
    (error) => error.code === "page_structure_changed",
  );
});

test("loads a virtualized transcript until the ordered line set is stable", async () => {
  let pass = 0;
  const pane = node();
  pane.scrollHeight = 120;
  pane.clientHeight = 20;
  pane.querySelectorAll = () => {
    pass += 1;
    const texts = pass < 2
      ? ["00:01 First line"]
      : ["00:01 First line", "00:04 Second line", "00:08 Third line"];
    return texts.map((text) => node({text}));
  };
  const document = node({one: {"div.event-tab-scroll-pane": pane}});

  const result = await readTranscript(document, {
    maxScrolls: 20,
    stablePasses: 3,
    settle: async () => {},
  });

  assert.equal(result.complete, true);
  assert.equal(result.line_count, 3);
  assert.equal(
    result.text,
    "00:01 First line\n00:04 Second line\n00:08 Third line",
  );
});

test("empty transcript is reported as still processing", async () => {
  const pane = node({many: {"li.index-event": []}});
  const document = node({one: {"div.event-tab-scroll-pane": pane}});

  await assert.rejects(
    readTranscript(document, {settle: async () => {}}),
    (error) => error.code === "transcript_processing",
  );
});
