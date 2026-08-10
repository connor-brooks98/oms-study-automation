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
    this.id = "";
    this.htmlFor = "";
    this.open = false;
    this.parentElement = null;
  }

  append(...items) {
    items.forEach((item) => { if (item && typeof item === "object") item.parentElement = this; });
    this.children.push(...items);
  }
  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }
  addEventListener() {}
  setAttribute(name, value) { this[name] = value; }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
  }
  focus() { documentRef.activeElement = this; }
  querySelectorAll(selector) {
    const matches = (element) => {
      if (!element?.dataset) return false;
      if (selector === "[data-import-source-row]") return element.dataset.importSourceRow === "true";
      if (selector === "details[data-state-key]") return element.tagName === "details" && Boolean(element.dataset.stateKey);
      if (selector === "[data-state-key]") return Boolean(element.dataset.stateKey);
      if (selector === "[data-focus-key]") return Boolean(element.dataset.focusKey);
      return false;
    };
    const found = [];
    const visit = (element) => {
      if (matches(element)) found.push(element);
      element?.children?.forEach(visit);
    };
    this.children.forEach(visit);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const documentRef = {
  activeElement: null,
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => ({ textContent: text }),
};

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

  assert.deepEqual(Object.keys(payload).sort(), [
    "content_kind", "destination_exam_number", "destination_subject", "exam_number",
    "label", "sources", "subject",
  ]);
  assert.equal(payload.content_kind, "practice_questions");
  assert.deepEqual(payload.sources, [
    { source_id: "questions-id", role: "questions", attach_to_notebook: false },
    { source_id: "answers-id", role: "answer_key", attach_to_notebook: false },
  ]);
});

test("dynamic imported-source role select has a visible associated label", () => {
  const list = new Element("ul");
  list.querySelector = () => null;

  studio.appendImportSource(documentRef, list, { id: "source-42", title: "Exam PDF" }, "questions", false);

  const row = list.children[0];
  const roleLabel = row.children[1];
  const roleSelect = row.children[2];
  assert.equal(roleLabel.tagName, "label");
  assert.equal(roleLabel.textContent, "Role");
  assert.equal(roleLabel.htmlFor, roleSelect.id);
  assert.equal(roleSelect.id, "import-source-role-source-42");
  assert.match(roleSelect.className, /sh-select/);
  assert.match(row.children[3].className, /sh-check/);
  assert.match(row.children[4].className, /sh-btn--danger/);
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

test("durable source deletion remains active until a terminal source response", () => {
  assert.equal(studio.hasActiveSources([{ state: "deleting" }]), true);
  assert.equal(studio.hasActiveSources([{ state: "deleted" }]), false);
});

test("workflow tabs receive the locked segmented active state", () => {
  const generate = new Element("button");
  generate.className = "button sh-btn";
  generate.dataset.workflowTab = "generate";
  const imported = new Element("button");
  imported.className = "button sh-btn";
  imported.dataset.workflowTab = "import";
  const generatePanel = new Element("section");
  generatePanel.dataset.workflowPanel = "generate";
  const importPanel = new Element("section");
  importPanel.dataset.workflowPanel = "import";
  const page = {
    querySelectorAll: (selector) => selector === "[data-workflow-tab]"
      ? [generate, imported]
      : [generatePanel, importPanel],
  };

  studio.setWorkflowState(page, "import");

  assert.match(imported.className, /sh-seg__btn--active/);
  assert.match(imported.className, /sh-btn--primary/);
  assert.doesNotMatch(imported.className, /sh-btn--secondary/);
  assert.match(generate.className, /sh-btn--secondary/);
  assert.equal(imported["aria-pressed"], "true");
  assert.equal(generatePanel.hidden, true);
  assert.equal(importPanel.hidden, false);
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

test("terminal runs expose compact rerun and history-only remove actions", () => {
  const container = new Element("div");
  studio.renderRuns(documentRef, container, [{
    id: "run-1", label: "Imported set", state: "complete", stage: "complete",
    attempts: 1, error: null, workflow_kind: "direct_import", review_url: null,
    image_review_url: null, published_url: "/quizzes/token", attempt_history: [],
  }]);

  const actions = container.children[0].children.at(-1);
  assert.equal(actions.className, "studio-run-actions");
  assert.equal(actions.children[0].dataset.rerun, "run-1");
  assert.equal(actions.children[1].dataset.removeRun, "run-1");
  assert.equal(actions.children[1].ariaLabel, "Remove run from history");
});

test("run history rerender moves focus to a surviving control when the focused action disappears", () => {
  const container = new Element("div");
  const originalRuns = [{
    id: "run-1", label: "First", state: "complete", stage: "complete",
    attempts: 1, error: null, workflow_kind: "generate", review_url: null,
    image_review_url: null, published_url: "/quizzes/token", attempt_history: [],
  }, {
    id: "run-2", label: "Second", state: "complete", stage: "complete",
    attempts: 1, error: null, workflow_kind: "generate", review_url: null,
    image_review_url: null, published_url: null, attempt_history: [],
  }];
  studio.renderRuns(documentRef, container, originalRuns);
  const unpublish = container.querySelectorAll("[data-focus-key]")
    .find((element) => element.dataset.focusKey === "run:run-1:unpublish");
  unpublish.focus();

  studio.renderRuns(documentRef, container, [originalRuns[1]]);

  assert.equal(documentRef.activeElement.dataset.focusKey, "run:run-2:rerun");
});

test("empty run history receives focus after its focused run is removed", () => {
  const container = new Element("div");
  studio.renderRuns(documentRef, container, [{
    id: "run-1", label: "First", state: "complete", stage: "complete",
    attempts: 1, error: null, workflow_kind: "generate", review_url: null,
    image_review_url: null, published_url: null, attempt_history: [],
  }]);
  container.querySelectorAll("[data-focus-key]")[0].focus();

  studio.renderRuns(documentRef, container, []);

  assert.equal(documentRef.activeElement, container);
});

test("ready local-import rows hydrate on refresh and deduplicate by source id", () => {
  const list = new Element("ul");
  const sources = [{
    id: "source-42", title: "Exam PDF", state: "ready", purpose: "local_import",
    import_defaults: { role: "supporting_reference", attach_to_notebook: true },
  }];

  studio.hydrateImportSources(documentRef, list, sources);
  studio.hydrateImportSources(documentRef, list, sources);

  assert.equal(list.querySelectorAll("[data-import-source-row]").length, 1);
  const row = list.children[0];
  assert.equal(row.dataset.sourceId, "source-42");
  assert.equal(row.children[2].children.find((option) => option.selected).value, "supporting_reference");
  assert.equal(row.children[3].children[0].checked, true);
});

test("ready local-import defaults reconstruct after a fresh page instance", () => {
  const sources = [{
    id: "source-1", title: "Questions", state: "ready", purpose: "local_import",
    import_defaults: { role: "questions", attach_to_notebook: true },
  }];
  const first = new Element("ul");
  const backForward = new Element("ul");

  studio.hydrateImportSources(documentRef, first, sources);
  studio.hydrateImportSources(documentRef, backForward, sources);

  [first, backForward].forEach((list) => {
    const notebook = list.children[0].children[3].children[0];
    assert.equal(notebook.checked, false);
    assert.equal(notebook.disabled, true);
  });
});

test("only ready local-import sources hydrate into import rows", () => {
  const list = new Element("ul");
  studio.hydrateImportSources(documentRef, list, [
    { id: "pending", title: "Pending", state: "pending", purpose: "local_import", import_defaults: { role: "questions", attach_to_notebook: false } },
    { id: "deleted", title: "Deleted", state: "deleted", purpose: "local_import", import_defaults: { role: "questions", attach_to_notebook: false } },
    { id: "generic", title: "Generic", state: "ready", purpose: "notebook_source", import_defaults: { role: "questions", attach_to_notebook: false } },
    { id: "ready", title: "Ready", state: "ready", purpose: "local_import", import_defaults: { role: "answer_key", attach_to_notebook: false } },
  ]);

  assert.deepEqual(list.querySelectorAll("[data-import-source-row]").map((row) => row.dataset.sourceId), ["ready"]);
});

test("import hydration prunes stale scoped rows while retaining authoritative rows", () => {
  const list = new Element("ul");
  const source = (id) => ({
    id, title: id, state: "ready", purpose: "local_import",
    import_defaults: { role: "questions", attach_to_notebook: false },
  });
  studio.hydrateImportSources(documentRef, list, [source("exam-1"), source("shared")]);
  const shared = list.querySelectorAll("[data-import-source-row]")
    .find((row) => row.dataset.sourceId === "shared");

  studio.hydrateImportSources(documentRef, list, [source("shared")]);

  const rows = list.querySelectorAll("[data-import-source-row]");
  assert.deepEqual(rows.map((row) => row.dataset.sourceId), ["shared"]);
  assert.equal(rows[0], shared);
});

test("forbidden import roles never serialize a programmatically checked NotebookLM attachment", () => {
  const row = {
    dataset: { sourceId: "questions-id", role: "questions" },
    querySelector: (selector) => selector === "[data-import-row-role]"
      ? { value: "questions" }
      : { checked: true },
  };
  const form = { elements: { label: { value: "Imported set" } }, ownerDocument: { querySelectorAll: () => [row] } };

  const payload = studio.buildImportRunPayload(
    form, { value: "Neuro" }, { value: "1" }, { value: "Neuro" }, { value: "1" }, [row],
  );

  assert.equal(payload.sources[0].attach_to_notebook, false);
});

test("run attempt disclosure and keyed focus survive polling rerender", () => {
  const container = new Element("div");
  const run = {
    id: "run-1", label: "Imported set", state: "failed", stage: "review", attempts: 1,
    error: null, workflow_kind: "direct_import", review_url: null, image_review_url: null,
    published_url: null, attempt_history: [{ attempt_number: 1, diagnostic_source: "contract", error: "bad" }],
  };
  studio.renderRuns(documentRef, container, [run]);
  const details = container.querySelectorAll("details[data-state-key]")[0];
  const summary = details.children[0];
  details.open = true;
  summary.focus();

  studio.renderRuns(documentRef, container, [run]);

  const restored = container.querySelectorAll("details[data-state-key]")[0];
  assert.equal(restored.open, true);
  assert.equal(documentRef.activeElement, restored.children[0]);
});
