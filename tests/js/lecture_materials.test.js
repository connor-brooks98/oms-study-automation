const test = require("node:test");
const assert = require("node:assert/strict");
const panels = require("../../src/oms_hub/web/static/lecture_materials.js");

const file = (name, text = "fixture") => new File([text], name);
const ok = (payload) => ({ ok: true, status: 200, json: async () => payload });

test("each panel validates one file and requires an exact lecture", () => {
  assert.equal(panels.validateSelection([file("slides.PPTX")], "slides", "200").name, "slides.PPTX");
  assert.equal(panels.validateSelection([file("slides.pdf")], "slides", "200").name, "slides.pdf");
  for (const [files, kind, lecture] of [
    [[file("x.txt")], "slides", "200"], [[file("x.pdf")], "transcripts", "200"],
    [[file("x.txt"), file("y.txt")], "transcripts", "200"], [[file("x.txt")], "transcripts", ""],
  ]) assert.throws(() => panels.validateSelection(files, kind, lecture));
});

test("concurrent slide and transcript sends retain their own target and bytes", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => { calls.push([url, options]); return ok({ batch_id: url }); };
  await Promise.all([
    panels.uploadFile({ file: file("a.pdf"), kind: "slides", lectureId: "200", csrf: "token", fetchImpl }),
    panels.uploadFile({ file: file("b.txt"), kind: "transcripts", lectureId: "201", csrf: "token", fetchImpl }),
  ]);
  assert.deepEqual(calls.map(([url, options]) => [url, options.body.get("lecture_id"), options.body.get("files").name]), [
    ["/uploads/slides", "200", "a.pdf"], ["/uploads/transcripts", "201", "b.txt"],
  ]);
  assert.ok(calls.every(([, options]) => options.headers["X-CSRF-Token"] === "token"));
});

test("removed duplicate response keeps explicit restore guidance", async () => {
  await assert.rejects(panels.uploadFile({
    file: file("a.txt"), kind: "transcripts", lectureId: "200", csrf: "token",
    fetchImpl: async () => ({ ok: false, status: 409, json: async () => ({ detail: "Restore the retained material" }) }),
  }), /Restore the retained material/);
});

test("large file manifest and chunk completion keep lecture and kind", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push([url, options]);
    if (url === "/api/upload-manifests") return ok({ manifest_id: "manifest" });
    if (url === "/api/upload-chunks") return ok({ session_id: "chunk" });
    if (options.method === "PUT") return ok({ received: Number(new URL(url, "http://fixture").searchParams.get("offset")) + options.body.size });
    return ok({ batch_id: "batch" });
  };
  const result = await panels.uploadFile({
    file: new File([new Uint8Array(9 * 1024 * 1024)], "a.pptx"), kind: "slides", lectureId: "200", csrf: "token", fetchImpl,
  });
  assert.equal(result.batch_id, "batch");
  const manifest = JSON.parse(calls[0][1].body);
  assert.equal(manifest.lecture_id, 200);
  assert.equal(manifest.kind, "slides");
  assert.equal(manifest.files.length, 1);
  assert.match(manifest.files[0].sha256, /^[a-f0-9]{64}$/);
  assert.ok(calls.some(([url]) => url === "/api/upload-chunks/chunk/finalize?lecture_id=200"));
});

test("polling distinguishes complete, failure and replacement approval without reloading", async () => {
  for (const state of ["complete", "failed", "awaiting_confirmation"]) {
    let count = 0;
    const result = await panels.pollBatch("batch", {
      fetchImpl: async () => ok({ lifecycle: count++ ? (state === "awaiting_confirmation" ? "active" : "terminal") : "active", items: [{ state: count === 1 ? "processing" : state }] }),
      sleep: async () => {}, onStatus: () => {},
    });
    assert.equal(result.items[0].state, state);
    assert.equal(count, 2);
  }
});

test("polling has a bound and sends users to the existing processes view", async () => {
  await assert.rejects(panels.pollBatch("batch", {
    fetchImpl: async () => ok({ lifecycle: "active", items: [{ state: "queued" }] }),
    sleep: async () => {}, onStatus: () => {}, attempts: 2,
  }), /still processing/);
});

test("panel refresh reconciles processing and replacement reviews without replacing the tracker", () => {
  const checklist = { open: true, replaceWith(next) { this.replacement = next; } };
  const reviews = { replaceWith(next) { this.replacement = next; } };
  const current = { querySelector: (selector) => ({
    "[data-processing-checklist]": checklist, "[data-material-reviews]": reviews,
  })[selector] || null };
  const nextChecklist = { open: false };
  const nextReviews = {};
  const updated = { querySelector: (selector) => ({
    "[data-processing-checklist]": nextChecklist, "[data-material-reviews]": nextReviews,
  })[selector] || null };
  panels.syncSharedMaterialState(current, updated);
  assert.equal(checklist.replacement, nextChecklist);
  assert.equal(nextChecklist.open, true);
  assert.equal(reviews.replacement, nextReviews);
});

class Node {
  constructor() { this.listeners = {}; this.disabled = false; this.hidden = false; this.textContent = ""; this.dataset = {}; this.classList = { add() {}, remove() {} }; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  dispatch(name, extra = {}) { return this.listeners[name]?.({ preventDefault() {}, ...extra }); }
  setAttribute(name, value) { this[name] = value; }
}
const uiPanel = (kind, lectureId) => {
  const selectors = ["upload", "file", "dropzone", "submit", "status", "recovery", "decision", "confirm", "discard", "refresh", "removed"];
  const nodes = Object.fromEntries(selectors.map((name) => [name, new Node()]));
  nodes.decision.hidden = nodes.recovery.hidden = true;
  nodes.decision.querySelectorAll = () => [nodes.confirm, nodes.discard];
  const panel = new Node();
  panel.dataset = { kind, lectureId, csrf: "fixture-csrf" };
  panel.querySelector = (selector) => nodes[selector.replace(/^\[data-material-|\]$/g, "")];
  panel.querySelectorAll = () => [];
  return { panel, ...nodes };
};

test("drag/drop and picker keep concurrent panel uploads and failures independent", async () => {
  const slides = uiPanel("slides", "200");
  const transcript = uiPanel("transcripts", "200");
  let releaseSlides;
  const calls = [];
  panels.initialize({ querySelectorAll: () => [slides.panel, transcript.panel] }, async (url, options) => {
    calls.push([url, options]);
    if (url === "/uploads/slides") await new Promise((resolve) => { releaseSlides = resolve; });
    if (url.startsWith("/uploads/")) return ok({ batch_id: url.endsWith("slides") ? "slides" : "transcripts" });
    return ok({ lifecycle: "terminal", items: [{ state: "failed", error: "Fixture processing failed" }] });
  });
  await slides.dropzone.dispatch("drop", { dataTransfer: { files: [file("slides.pdf")] } });
  transcript.file.files = [file("captions.txt")];
  await transcript.file.dispatch("change");
  const activeSlides = slides.upload.dispatch("submit");
  assert.equal(slides.file.disabled, true);
  assert.equal(transcript.file.disabled, false);
  await transcript.upload.dispatch("submit");
  assert.equal(transcript.status.textContent, "Fixture processing failed");
  assert.equal(slides.status.textContent, "Uploading to this lecture…");
  releaseSlides();
  await activeSlides;
  assert.equal(slides.status.textContent, "Fixture processing failed");
  assert.deepEqual(calls.filter(([url]) => url.startsWith("/uploads/")).map(([url, options]) => [url, options.body.get("lecture_id"), options.body.get("files").name]), [
    ["/uploads/slides", "200", "slides.pdf"], ["/uploads/transcripts", "200", "captions.txt"],
  ]);
});

test("removed response opens only that panel's retained file recovery", async () => {
  const transcript = uiPanel("transcripts", "200");
  panels.initialize({ querySelectorAll: () => [transcript.panel] }, async () => ({ ok: false, status: 409, json: async () => ({ detail: "Restore the retained material" }) }));
  await transcript.dropzone.dispatch("drop", { dataTransfer: { files: [file("captions.txt")] } });
  await transcript.upload.dispatch("submit");
  assert.equal(transcript.removed.open, true);
  assert.equal(transcript.recovery.hidden, false);
  assert.equal(transcript.file.disabled, false);
});

test("replacement confirmation blocks new uploads until the decision is accepted", async () => {
  const transcript = uiPanel("transcripts", "200");
  const calls = [];
  let confirmed = false;
  panels.initialize({ querySelectorAll: () => [transcript.panel] }, async (url) => {
    calls.push(url);
    if (url === "/uploads/transcripts") return ok({ batch_id: "batch" });
    if (url.endsWith("/confirm")) { confirmed = true; return ok({ state: "queued" }); }
    return ok({ lifecycle: confirmed ? "terminal" : "active", items: [{ id: "item", state: confirmed ? "failed" : "awaiting_confirmation", error: confirmed ? "Fixture failure" : null }] });
  });
  transcript.file.files = [file("replacement.txt")];
  await transcript.file.dispatch("change");
  await transcript.upload.dispatch("submit");
  assert.equal(transcript.decision.hidden, false);
  assert.equal(transcript.file.disabled, true);
  await transcript.upload.dispatch("submit");
  assert.equal(calls.filter((url) => url.startsWith("/uploads/")).length, 1);
  await transcript.confirm.dispatch("click");
  assert.ok(calls.includes("/api/upload-items/item/confirm"));
  assert.equal(transcript.decision.hidden, true);
  assert.equal(transcript.file.disabled, false);
  assert.equal(transcript.status.textContent, "Fixture failure");
});

test("a rejected discard keeps the existing transcript decision recoverable", async () => {
  const transcript = uiPanel("transcripts", "200");
  panels.initialize({ querySelectorAll: () => [transcript.panel] }, async (url) => {
    if (url === "/uploads/transcripts") return ok({ batch_id: "batch" });
    if (url.endsWith("/discard")) return { ok: false, status: 409, json: async () => ({ detail: "Decision could not be completed" }) };
    return ok({ lifecycle: "active", items: [{ id: "item", state: "awaiting_confirmation" }] });
  });
  transcript.file.files = [file("replacement.txt")];
  await transcript.file.dispatch("change");
  await transcript.upload.dispatch("submit");
  await transcript.discard.dispatch("click");
  assert.equal(transcript.decision.hidden, false);
  assert.equal(transcript.file.disabled, true);
  assert.equal(transcript.confirm.disabled, false);
  assert.equal(transcript.status.textContent, "Decision could not be completed");
});

test("empty slides uses its bottom button to pick and auto-submits a chosen or dropped file", async () => {
  const slides = uiPanel("slides", "200");
  const find = slides.panel.querySelector;
  slides.panel.querySelector = (selector) => selector === "[data-material-picker]" ? slides.submit : find(selector);
  let picks = 0, submissions = 0;
  slides.file.click = () => { picks += 1; };
  slides.upload.requestSubmit = () => { submissions += 1; };
  panels.initialize({ querySelectorAll: () => [slides.panel] }, async () => { throw new Error("No upload without a file"); });
  await slides.submit.dispatch("click");
  assert.equal(picks, 1);
  slides.file.files = [file("slides.pdf")];
  await slides.file.dispatch("change");
  assert.equal(submissions, 1);
  await slides.dropzone.dispatch("drop", { dataTransfer: { files: [file("new.pptx")] } });
  assert.equal(submissions, 2);
  await slides.dropzone.dispatch("drop", { dataTransfer: { files: [file("wrong.txt")] } });
  assert.equal(submissions, 2);
});

for (const kind of ["slides", "transcripts"]) test(`compact ready ${kind} initializes without an uploader and cancellation never removes`, async () => {
  const transcript = uiPanel(kind, "200");
  const find = transcript.panel.querySelector;
  transcript.panel.querySelector = (selector) => ["upload", "file", "dropzone", "submit"].some((name) => selector === `[data-material-${name}]`) ? null : find(selector);
  const remove = new Node();
  remove.dataset.materialRemove = kind === "slides" ? "slides" : "transcript";
  const invoker = { focus() {} };
  transcript.panel.querySelectorAll = (selector) => selector === "form:not([data-material-upload])" ? [remove] : [];
  let requests = 0, confirmations = 0, confirmationMessage;
  const previous = global.StudyHubShell;
  global.StudyHubShell = { confirmAction: async (_doc, options) => {
    confirmations += 1;
    confirmationMessage = options.message;
    return false;
  } };
  try {
    panels.initialize({ querySelectorAll: () => [transcript.panel] }, async () => { requests += 1; });
    await remove.dispatch("submit", { submitter: invoker });
    assert.equal(confirmations, 1);
    assert.match(confirmationMessage, kind === "slides" ? /retained.*Upload Slides/ : /retained.*Restore/);
    assert.equal(requests, 0);
  } finally { global.StudyHubShell = previous; }
});
