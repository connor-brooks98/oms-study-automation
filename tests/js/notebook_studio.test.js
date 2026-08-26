const test = require("node:test");
const assert = require("node:assert/strict");

const studio = require("../../src/oms_hub/web/static/notebook_studio.js");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.disabled = false;
    this.checked = false;
    this.type = "";
    this.href = "";
    this.id = "";
    this.htmlFor = "";
    this.style = {};
    this.offsetLeft = 0;
    this.offsetWidth = 120;
    this.tabIndex = 0;
    this.open = false;
    this.parentElement = null;
    this._value = "";
  }

  get value() { return this._value; }
  set value(next) {
    this._value = String(next);
    if (this.tagName === "select") {
      this.children.forEach((option) => { option.selected = option.value === this._value; });
    }
  }
  get options() { return this.children; }
  get selectedIndex() {
    const index = this.children.findIndex((option) => option.value === this.value);
    return index < 0 ? 0 : index;
  }

  append(...items) {
    items.forEach((item) => { if (item && typeof item === "object") item.parentElement = this; });
    this.children.push(...items);
    if (this.tagName === "select") {
      const selected = items.find((item) => item?.selected);
      if (selected) this._value = selected.value;
    }
  }
  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async dispatch(type) {
    for (const listener of this.listeners.get(type) || []) await listener({ target: this });
  }
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

test("file text and URL import forms submit selected role and Notebook defaults", () => {
  class CapturingFormData {
    constructor(form) { this.values = new Map(Object.entries(form.namedValues)); }
    set(name, value) { this.values.set(name, value); }
    get(name) { return this.values.get(name); }
  }

  [
    ["file", { title: "File source", file: "questions.pdf" }],
    ["text", { title: "Text source", text: "Question text" }],
    ["url", { title: "URL source", url: "https://example.test/questions" }],
  ].forEach(([sourceType, namedValues]) => {
    const role = { value: "supporting_reference" };
    const notebook = { checked: true, disabled: false };
    const form = {
      dataset: { importSourceType: sourceType }, namedValues,
      querySelector: (selector) => selector === "[data-import-role]" ? role : notebook,
    };

    const { body, roleState } = studio.buildImportSourceFormData(
      form, { value: "Neuro" }, { value: "1" }, "csrf-token", CapturingFormData,
    );

    assert.deepEqual(roleState, {
      role: "supporting_reference", attach_to_notebook: true,
    });
    assert.equal(body.get("role"), "supporting_reference");
    assert.equal(body.get("attach_to_notebook"), "true");
    assert.equal(body.get("subject"), "Neuro");
    assert.equal(body.get("exam_number"), "1");
    assert.equal(body.get("csrf_token"), "csrf-token");
    Object.entries(namedValues).forEach(([name, value]) => {
      assert.equal(body.get(name), value);
    });
  });
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
    querySelector: () => null,
  };

  studio.setWorkflowState(page, "import");

  assert.match(imported.className, /sh-seg__btn--active/);
  assert.match(imported.className, /sh-btn--primary/);
  assert.doesNotMatch(imported.className, /sh-btn--secondary/);
  assert.match(generate.className, /sh-btn--secondary/);
  assert.equal(imported["aria-selected"], "true");
  assert.equal(imported.tabIndex, 0);
  assert.equal(generate.tabIndex, -1);
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

test("a failed run action preserves history and restores its action focus", () => {
  const container = new Element("div");
  studio.renderRuns(documentRef, container, [{
    id: "run-1", label: "First", state: "complete", stage: "complete",
    attempts: 1, error: null, workflow_kind: "generate", review_url: null,
    image_review_url: null, published_url: null, attempt_history: [],
  }]);
  const originalCard = container.children[0];
  const rerun = container.querySelectorAll("[data-focus-key]")
    .find((element) => element.dataset.focusKey === "run:run-1:rerun");
  const status = new Element("p");
  rerun.disabled = true;

  studio.restoreFailedAction(rerun, status, "Remote action failed.");

  assert.equal(container.children.length, 1);
  assert.equal(container.children[0], originalCard);
  assert.equal(status.textContent, "Remote action failed.");
  assert.equal(rerun.disabled, false);
  assert.equal(documentRef.activeElement, rerun);
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

test("initialize restores durable URL scope and fresh-page import rows", async () => {
  const select = (values) => {
    const element = new Element("select");
    values.forEach(([value, exams = ""]) => {
      const option = new Element("option");
      option.value = value;
      option.dataset.exams = exams;
      element.append(option);
    });
    element.value = "";
    return element;
  };
  const course = select([[""], ["Neuro", "1,2"], ["Cardio", "1"]]);
  course.dataset.studioCourse = "true";
  const exam = select([[""]]);
  exam.dataset.studioExam = "true";
  exam.disabled = true;
  const workflowTabs = ["generate", "import"].map((workflow) => {
    const tab = new Element("button");
    tab.dataset.workflowTab = workflow;
    return tab;
  });
  const workflowPanels = ["generate", "import"].map((workflow) => {
    const panel = new Element("section");
    panel.dataset.workflowPanel = workflow;
    return panel;
  });
  const elements = {
    "[data-studio-course]": course,
    "[data-studio-exam]": exam,
    "[data-source-list]": new Element("ul"),
    "[data-source-status]": new Element("p"),
    "[data-source-picker]": new Element("div"),
    "[data-source-filter]": new Element("input"),
    "[data-select-all-sources]": null,
    "[data-image-dropzone]": null,
    "[data-image-drop-message]": null,
    "[data-run-list]": new Element("div"),
    "[data-run-form]": new Element("form"),
    "[data-destination-course]": select([[""]]),
    "[data-destination-exam]": select([[""]]),
    "[data-import-run-form]": new Element("form"),
    "[data-import-destination-course]": select([[""]]),
    "[data-import-destination-exam]": select([[""]]),
    "[data-import-source-list]": new Element("ul"),
    "[data-poll-status]": new Element("p"),
  };
  const page = new Element("section");
  page.querySelector = (selector) => elements[selector] || null;
  page.querySelectorAll = (selector) => {
    if (selector === "[data-workflow-tab]") return workflowTabs;
    if (selector === "[data-workflow-panel]") return workflowPanels;
    if (selector === "[data-source-form]" || selector === "[data-import-source-form]") return [];
    return [];
  };
  const freshDocument = {
    ...documentRef,
    cookie: "",
    querySelector: (selector) => selector === "[data-studio-page]" ? page : null,
  };
  const requests = [];
  let delayNextNeuroSources = false;
  let resolveDelayedNeuroSources = null;
  const fetchImpl = async (url) => {
    requests.push(url);
    if (
      delayNextNeuroSources
      && url.startsWith("/studio/sources?")
      && url.includes("subject_key=neuro")
    ) {
      delayNextNeuroSources = false;
      return new Promise((resolve) => { resolveDelayedNeuroSources = resolve; });
    }
    return {
      ok: true,
      json: async () => url.startsWith("/studio/sources?") ? {
        sources: url.includes("subject_key=neuro") ? [{
          id: "source-1", title: "Questions", type: "text", state: "ready",
          purpose: "local_import",
          import_defaults: { role: "questions", attach_to_notebook: true },
        }] : [],
      } : { runs: [] },
    };
  };
  const replacedUrls = [];
  const navigation = {
    location: { href: "https://study.test/studio?subject=neuro&exam=1&workflow=import" },
    history: {
      state: null,
      replaceState: (_state, _title, url) => replacedUrls.push(String(url)),
    },
  };

  await studio.initialize(freshDocument, fetchImpl, navigation);

  assert.equal(course.value, "Neuro");
  assert.equal(exam.value, "1");
  assert.equal(exam.disabled, false);
  assert.equal(workflowPanels.find((panel) => panel.dataset.workflowPanel === "import").hidden, false);
  assert.deepEqual(requests, [
    "/studio/sources?subject_key=neuro&exam_number=1",
    "/studio/runs?subject_key=neuro&exam_number=1",
  ]);
  const row = elements["[data-import-source-list]"].children[0];
  assert.equal(row.dataset.sourceId, "source-1");
  assert.equal(row.children[2].value, "questions");
  assert.equal(row.children[3].children[0].checked, false);
  assert.equal(row.children[3].children[0].disabled, true);
  assert.match(replacedUrls.at(-1), /subject=neuro&exam=1&workflow=import/);

  course.value = "Cardio";
  await course.dispatch("change");
  assert.equal(exam.value, "");
  assert.equal(elements["[data-import-source-list]"].querySelectorAll(
    "[data-import-source-row]",
  ).length, 0);
  assert.match(replacedUrls.at(-1), /subject=cardio/);
  assert.doesNotMatch(replacedUrls.at(-1), /[?&]exam=/);
  exam.value = "1";
  await exam.dispatch("change");
  const changedScope = new URL(replacedUrls.at(-1));
  assert.equal(changedScope.searchParams.get("subject"), "cardio");
  assert.equal(changedScope.searchParams.get("exam"), "1");
  assert.equal(changedScope.searchParams.get("workflow"), "import");
  assert.equal(elements["[data-import-source-list]"].querySelectorAll(
    "[data-import-source-row]",
  ).length, 0);

  course.value = "Neuro";
  await course.dispatch("change");
  exam.value = "1";
  delayNextNeuroSources = true;
  const staleNeuroRefresh = exam.dispatch("change");
  assert.equal(typeof resolveDelayedNeuroSources, "function");

  course.value = "Cardio";
  await course.dispatch("change");
  exam.value = "1";
  await exam.dispatch("change");
  resolveDelayedNeuroSources({
    ok: true,
    json: async () => ({
      sources: [{
        id: "stale-neuro-source", title: "Stale Questions", type: "text", state: "ready",
        purpose: "local_import",
        import_defaults: { role: "questions", attach_to_notebook: false },
      }],
    }),
  });
  await staleNeuroRefresh;
  assert.equal(elements["[data-import-source-list]"].querySelectorAll(
    "[data-import-source-row]",
  ).length, 0);
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
