const test = require("node:test");
const assert = require("node:assert/strict");
const { queueExtendLMFiles } = require("../../src/oms_hub/web/static/extendlm.js");

test("bulk files use separate requests with the fixed destination", async () => {
  const files = [new File(["slides"], "lecture.pdf"), new File(["transcript"], "lecture.txt")];
  const seen = [];
  let receipts = 0;
  await queueExtendLMFiles(async (path, body) => {
    assert.equal(path, "/uploads");
    assert.equal(body.get("notebook_id"), "selected-notebook");
    assert.equal(body.getAll("files").length, 1);
    seen.push(body.get("files").name);
  }, files, "selected-notebook", async () => { receipts++; });
  assert.deepEqual(seen, ["lecture.pdf", "lecture.txt"]);
  assert.equal(receipts, 2);
});

test("an interrupted batch stops without silently retrying a submitted file", async () => {
  const files = [new File(["a"], "a.txt"), new File(["b"], "b.txt"), new File(["c"], "c.txt")];
  let sent = 0, receipts = 0;
  await assert.rejects(queueExtendLMFiles(async () => {
    if (++sent === 2) throw new Error("connection interrupted");
  }, files, "notebook", async () => { receipts++; }), /connection interrupted/);
  assert.equal(sent, 2);
  assert.equal(receipts, 1);
});

// Exercise the actual browser initializer with the lecture page's smaller DOM.
function lecturePage(status, {settings = false, replies = {}} = {}) {
  const vm = require("node:vm");
  const fs = require("node:fs");
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.value = ""; this.hidden = false; }
    get options() { return this.children; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; this.value = ""; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
  }
  const elements = Object.fromEntries(["message", "notebook", "more", "jobs", "lecture-upload", "setup-needed", "destination", "destination-picker", "save-destination", "change-destination", ...(settings ? ["account", "accounts"] : [])].map(name => [name, new Element()]));
  const root = {
    dataset: { csrf: "csrf-test", ...(settings ? {} : { lectureId: "42" }) },
    querySelector: selector => elements[selector.slice(6, -1)] || null,
    querySelectorAll: () => [elements.more, elements["lecture-upload"]],
  };
  const calls = [], timers = [];
  vm.runInNewContext(fs.readFileSync(require.resolve("../../src/oms_hub/web/static/extendlm.js"), "utf8"), {
    document: { querySelector: () => root, createElement: () => new Element() },
    FormData, clearTimeout: () => {}, setTimeout: (fn) => { timers.push(fn); return timers.length; },
    window: { addEventListener: () => {} },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const data = replies[url] || (url.includes("/status") ? status : url.endsWith("/notebooks") ? {
        items: [{ id: "exam-notebook", title: "Cardio Exam 1" }], next_cursor: "",
      } : url.endsWith("/accounts") ? { accounts: [{id: "account-1", label: "Browser 1 · Account 0"}] } : { job_id: "upload-1" });
      return { ok: true, json: async () => data };
    },
  });
  return { elements, calls, timers };
}

test("first lecture visit saves an exam destination before its explicit send", async () => {
  const status = { signed_in: true, selected: true, jobs: [] };
  const destination = {notebook_id: "exam-notebook", notebook_title: "Cardio Exam 1", account_label: "Browser 1 · Account 0"};
  const { elements, calls } = lecturePage(status, {replies: {"/settings/extendlm/lectures/42/destination": {destination}}});
  await new Promise(setImmediate);
  assert.deepEqual(calls.map(call => call.url), ["/settings/extendlm/status?lecture_id=42", "/settings/extendlm/notebooks"]);
  assert.equal(elements.notebook.options[1].textContent, "Cardio Exam 1");
  assert.equal(elements["setup-needed"].hidden, true);
  assert.equal(elements["lecture-upload"].disabled, true);
  elements.notebook.value = "exam-notebook";
  elements.notebook.handlers.change();
  assert.equal(elements["save-destination"].disabled, false);
  status.destination = destination;
  await elements["save-destination"].handlers.click();
  assert.equal(elements["lecture-upload"].disabled, false);
  await elements["lecture-upload"].handlers.click();
  const upload = calls.find(call => call.url.endsWith("/lectures/42/upload"));
  assert.equal(upload.options.body.get("notebook_id"), null);
  assert.equal(upload.options.body.get("csrf_token"), "csrf-test");
  assert.equal(calls.filter(call => call.url.endsWith("/notebooks")).length, 1);
});

test("missing setup directs the lecture page to Settings without calling provider tools", async () => {
  for (const status of [{ signed_in: false }, { signed_in: true, selected: false }]) {
    const { elements, calls } = lecturePage(status);
    await new Promise(setImmediate);
    assert.equal(elements["setup-needed"].hidden, false);
    assert.equal(elements["lecture-upload"].disabled, true);
    assert.deepEqual(calls.map(call => call.url), ["/settings/extendlm/status?lecture_id=42"]);
  }
});


test("saved destination enables one-click send and shows the selected account", async () => {
  const { elements, calls } = lecturePage({signed_in: true, selected: true, jobs: [], destination: {
    notebook_id: "exam-notebook", notebook_title: "Cardio Exam 1", account_label: "Browser 1 · Account 0",
  }});
  await new Promise(setImmediate);
  assert.equal(elements["lecture-upload"].disabled, false);
  assert.equal(elements["lecture-upload"].textContent, "Send to Cardio Exam 1");
  assert.match(elements.destination.textContent, /Browser 1 · Account 0/);
  assert.equal(elements["destination-picker"].hidden, true);
  assert.equal(calls.length, 1);
  await elements["lecture-upload"].handlers.click();
  assert.equal(calls.filter(c => c.url.endsWith("/lectures/42/upload")).length, 1);
  assert.equal(calls.filter(c => c.url.endsWith("/notebooks")).length, 0);
});

test("OAuth return loads connection choices without selecting an account", async () => {
  const { elements, calls } = lecturePage({signed_in: true, selected: false, jobs: []}, {settings: true});
  await new Promise(setImmediate);
  assert.equal(elements.account.options[1].textContent, "Browser 1 · Account 0");
  assert.equal(calls.filter(c => c.url.endsWith("/accounts")).length, 1);
  assert.equal(calls.filter(c => c.url.endsWith("/select")).length, 0);
});

test("accepted sources get bounded automatic indexing checks and never resume or upload", async () => {
  const job = {id: "job-1", notebook_title: "Exam 1", status: "accepted", message: "Accepted", files: [{filename: "lecture.txt", stage: "accepted", source_status: "processing"}]};
  const { elements, calls, timers } = lecturePage({signed_in: true, selected: true, jobs: [job], destination: {notebook_id: "exam-notebook", notebook_title: "Exam 1"}});
  await new Promise(setImmediate);
  for (let i = 0; i < 25 && timers.length; i++) {
    await timers.shift()();
    await new Promise(setImmediate);
  }
  const checks = calls.filter(c => c.url.endsWith("/check"));
  assert.ok(checks.length > 0 && checks.length <= 20);
  assert.equal(timers.length, 0);
  assert.equal(calls.filter(c => /\/(resume|upload|uploads)$/.test(c.url)).length, 0);
  assert.equal(elements.jobs.children[0].children.at(-1).textContent, "Refresh indexing");
});

test("indexing polling stops when ready or failed and partial receipts stay partial", async () => {
  for (const status of ["ready", "indexing_failed", "partial"]) {
    const job = {id: "job-1", notebook_title: "Exam 1", status, message: "Partial delivery", files: [{filename: "lecture.txt", stage: "accepted", source_status: "ready"}]};
    const { calls, timers, elements } = lecturePage({signed_in: true, selected: true, jobs: [job], destination: {notebook_id: "exam-notebook", notebook_title: "Exam 1"}});
    await new Promise(setImmediate);
    assert.equal(calls.filter(c => c.url.endsWith("/check")).length, 0);
    assert.equal(timers.length, 0);
    assert.match(elements.jobs.children[0].children[0].textContent, new RegExp(status.replaceAll("_", " ")));
  }
});
