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
function lecturePage(status) {
  const vm = require("node:vm");
  const fs = require("node:fs");
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.value = ""; this.hidden = false; }
    get options() { return this.children; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; this.value = ""; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
  }
  const elements = Object.fromEntries(["message", "notebook", "more", "jobs", "lecture-upload", "setup-needed"].map(name => [name, new Element()]));
  const root = {
    dataset: { csrf: "csrf-test", lectureId: "42" },
    querySelector: selector => elements[selector.slice(6, -1)] || null,
    querySelectorAll: () => [elements.more, elements["lecture-upload"]],
  };
  const calls = [];
  vm.runInNewContext(fs.readFileSync(require.resolve("../../src/oms_hub/web/static/extendlm.js"), "utf8"), {
    document: { querySelector: () => root, createElement: () => new Element() },
    FormData, clearTimeout, setTimeout,
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const data = url.endsWith("/status") ? status : url.endsWith("/notebooks") ? {
        items: [{ id: "exam-notebook", title: "Cardio Exam 1" }], next_cursor: "",
      } : { job_id: "upload-1" };
      return { ok: true, json: async () => data };
    },
  });
  return { elements, calls };
}

test("saved setup loads exam notebooks and sends the lecture without account reselection", async () => {
  const { elements, calls } = lecturePage({ signed_in: true, selected: true, jobs: [] });
  await new Promise(setImmediate);
  assert.deepEqual(calls.map(call => call.url), ["/settings/extendlm/status", "/settings/extendlm/notebooks"]);
  assert.equal(elements.notebook.options[1].textContent, "Cardio Exam 1");
  assert.equal(elements["setup-needed"].hidden, true);
  assert.equal(elements["lecture-upload"].disabled, true);
  elements.notebook.value = "exam-notebook";
  elements.notebook.handlers.change();
  assert.equal(elements["lecture-upload"].disabled, false);
  await elements["lecture-upload"].handlers.click();
  const upload = calls.find(call => call.url.endsWith("/lectures/42/upload"));
  assert.equal(upload.options.body.get("notebook_id"), "exam-notebook");
  assert.equal(upload.options.body.get("csrf_token"), "csrf-test");
  assert.equal(calls.filter(call => call.url.endsWith("/notebooks")).length, 1);
});

test("missing setup directs the lecture page to Settings without calling provider tools", async () => {
  for (const status of [{ signed_in: false }, { signed_in: true, selected: false }]) {
    const { elements, calls } = lecturePage(status);
    await new Promise(setImmediate);
    assert.equal(elements["setup-needed"].hidden, false);
    assert.equal(elements["lecture-upload"].disabled, true);
    assert.deepEqual(calls.map(call => call.url), ["/settings/extendlm/status"]);
  }
});
