const test = require("node:test");
const assert = require("node:assert/strict");

const studio = require("../../src/oms_hub/web/static/notebook_studio.js");

test("course keys normalize whitespace and case", () => {
  assert.equal(studio.normalizeSubject("  Neuro   Science "), "neuro science");
});

test("source polling remains active only while work is pending", () => {
  assert.equal(studio.hasActiveSources([{ state: "pending" }]), true);
  assert.equal(studio.hasActiveSources([{ state: "attaching" }]), true);
  assert.equal(studio.hasActiveSources([{ state: "attached" }, { state: "failed" }]), false);
});

test("run payload keeps an intentional empty source selection explicit", () => {
  const form = {
    elements: {
      prompt: { value: "Create a quiz" },
      label: { value: "Exam review" },
    },
    ownerDocument: { querySelectorAll: () => [] },
  };

  const payload = studio.buildRunPayload(
    form,
    { value: "Neuro" },
    { value: "1" },
    { value: "Cardiology" },
    { value: "2" },
  );

  assert.deepEqual(payload.source_ids, []);
  assert.equal(payload.exam_number, 1);
  assert.equal(payload.destination_exam_number, 2);
});

test("retry status exposes a countdown without HTML", () => {
  assert.equal(
    studio.retryStatus(
      { state: "retrying", next_attempt_at: "2026-08-01T00:00:10.000Z" },
      Date.parse("2026-08-01T00:00:00.000Z"),
    ),
    "retrying in 10s",
  );
});

test("source picker filtering is case-insensitive", () => {
  const labels = [
    { textContent: "Professor URL", hidden: false },
    { textContent: "Review PDF", hidden: false },
  ];
  studio.filterSourcePicker({ querySelectorAll: () => labels }, "PROFESSOR");

  assert.equal(labels[0].hidden, false);
  assert.equal(labels[1].hidden, true);
});

test("source renderer writes untrusted content through textContent", () => {
  const created = [];
  const documentRef = {
    createElement: () => {
      const node = { textContent: "", dataset: {}, append: () => {} };
      created.push(node);
      return node;
    },
  };
  const list = {
    replaceChildren: () => {},
    append: () => {},
  };

  studio.renderSources(documentRef, list, [{
    title: "<img src=x onerror=alert(1)>",
    type: "url",
    state: "failed",
    error: "<script>bad()</script>",
  }]);

  assert.match(created[0].textContent, /<img/);
  assert.match(created[0].textContent, /<script>/);
  assert.equal("innerHTML" in created[0], false);
});

test("awaiting image runs expose an Add images action", () => {
  const created = [];
  const documentRef = {
    createElement: (tagName) => {
      const node = {
        tagName,
        textContent: "",
        dataset: {},
        children: [],
        append(child) { this.children.push(child); },
      };
      created.push(node);
      return node;
    },
  };
  const container = {
    children: [],
    replaceChildren() { this.children = []; },
    append(child) { this.children.push(child); },
  };

  studio.renderRuns(documentRef, container, [{
    id: "run-1",
    label: "Professor questions",
    state: "awaiting_images",
    stage: "image_review",
    attempts: 1,
    error: null,
    next_attempt_at: null,
    published_url: null,
    image_review_url: "/studio/runs/run-1/images",
    attempt_history: [],
  }]);

  const link = created.find((node) => node.tagName === "a");
  assert.equal(link.textContent, "Add images");
  assert.equal(link.href, "/studio/runs/run-1/images");
  assert.match(created.find((node) => node.tagName === "p").textContent, /Images needed/);
});
