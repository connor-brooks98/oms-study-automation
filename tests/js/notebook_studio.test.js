const test = require("node:test");
const assert = require("node:assert/strict");

const studio = require("../../src/oms_hub/web/static/notebook_studio.js");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.disabled = false;
    this.checked = false;
    this.type = "";
    this.href = "";
  }

  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}

const documentRef = { createElement: (tag) => new Element(tag) };

test("import payload preserves explicit question and answer roles", () => {
  const rows = [
    { dataset: { sourceId: "questions-id", role: "questions" }, querySelector: () => null },
    { dataset: { sourceId: "answers-id", role: "answer_key" }, querySelector: () => null },
  ];
  const form = { elements: { label: { value: "Imported set" } }, ownerDocument: { querySelectorAll: () => rows } };
  const payload = studio.buildImportRunPayload(
    form,
    { value: "Neuro" }, { value: "1" }, { value: "Neuro" }, { value: "1" }, rows,
  );

  assert.equal(payload.workflow_kind, "direct_import");
  assert.equal(payload.content_kind, "practice_questions");
  assert.deepEqual(payload.sources, [
    { source_id: "questions-id", role: "questions", attach_to_notebook: false },
    { source_id: "answers-id", role: "answer_key", attach_to_notebook: false },
  ]);
});

test("role changes clear and disable NotebookLM use for question and answer-key sources", () => {
  const role = { value: "supporting_reference" };
  const checkbox = { checked: true, disabled: false };
  const form = { querySelector: (selector) => (selector === "[data-import-role]" ? role : checkbox) };

  assert.deepEqual(studio.applyImportRoleState(form), {
    role: "supporting_reference", attach_to_notebook: true,
  });
  role.value = "questions";
  assert.deepEqual(studio.applyImportRoleState(form), {
    role: "questions", attach_to_notebook: false,
  });
  assert.equal(checkbox.disabled, true);
  assert.equal(checkbox.checked, false);
});

test("workflow panel state remains deterministic", () => {
  assert.deepEqual(studio.workflowPanelState("generate"), { generate: true, import: false });
  assert.deepEqual(studio.workflowPanelState("import"), { generate: false, import: true });
});

test("direct run history labels review stages and links to question review", () => {
  const container = new Element("div");
  studio.renderRuns(documentRef, container, [{
    id: "run-1", label: "Imported set", state: "awaiting_review", stage: "review",
    attempts: 1, error: null, workflow_kind: "direct_import", review_url: "/studio/runs/run-1/review",
    image_review_url: null, published_url: null, attempt_history: [],
  }]);

  const card = container.children[0];
  assert.match(card.children[1].textContent, /review ready/);
  assert.equal(card.children[2].textContent, "Review questions");
  assert.equal(card.children[2].href, "/studio/runs/run-1/review");
});
