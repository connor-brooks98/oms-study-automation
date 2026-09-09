const test = require("node:test");
const assert = require("node:assert/strict");

const library = require("../../src/oms_hub/web/static/public_quiz_library.js");

test("right click opens the row overflow menu without replacing the three-dot trigger", () => {
  const menu = { open: false, querySelector: () => ({ setAttribute() {} }) };
  const row = { querySelector: () => menu };
  let prevented = false;
  const documentRef = { querySelectorAll: () => [] };
  assert.equal(library.openContextMenu(documentRef, row, { preventDefault: () => { prevented = true; } }), true);
  assert.equal(menu.open, true);
  assert.equal(prevented, true);
});

test("progress key matches the quiz player's versioned key", () => {
  assert.equal(
    library.progressKey("token", 4),
    "oms-study-hub-quiz:token:v4",
  );
});

test("progress is classified from the quiz player's saved state", () => {
  const untouched = {
    version: 2,
    currentIndex: 0,
    questions: {
      q1: { submitted: false },
      q2: { submitted: false },
    },
  };
  const started = {
    ...untouched,
    questions: {
      q1: {
        submitted: false,
        selectedChoiceId: "c1",
        eliminatedChoiceIds: [],
        highlights: [],
      },
      q2: { submitted: false },
    },
  };
  const complete = {
    ...started,
    currentIndex: 2,
    questions: {
      q1: { submitted: true },
      q2: { submitted: true },
    },
  };

  assert.equal(library.progressLabel(untouched, 2), "Not started");
  assert.equal(library.progressLabel(started, 2), "In progress");
  assert.equal(library.progressLabel(complete, 2), "Complete");
  assert.equal(library.progressClass("Complete"), "sh-pill--ok");
  assert.equal(library.progressLabel(complete, 3), "Not started");
});

test("corrupt browser progress is treated as not started", () => {
  const storage = {
    getItem: () => "{not-json",
  };

  assert.equal(library.readProgress(storage, "token", 1), "Not started");
});

test("course disclosures keep aria and the shared glyph state in sync", () => {
  const glyph = {
    states: [],
    classList: { toggle(_name, active) { glyph.states.push(active); } },
  };
  const panel = { hidden: false };
  const button = {
    attributes: { "aria-controls": "course-1" },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] || null; },
    querySelector(selector) { return selector === ".sh-disclose" ? glyph : null; },
    ownerDocument: { getElementById(id) { return id === "course-1" ? panel : null; } },
  };

  library.setExpanded(button, false);

  assert.equal(button.attributes["aria-expanded"], "false");
  assert.equal(panel.hidden, true);
  assert.deepEqual(glyph.states, [false]);
});

test("closing a course semantically collapses its open descendant exams", () => {
  const examPanel = { hidden: false, querySelectorAll: () => [] };
  const panels = new Map();
  const makeButton = (controls, expanded) => ({
    attributes: { "aria-controls": controls, "aria-expanded": String(expanded) },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] || null; },
    querySelector() { return null; },
    ownerDocument: { getElementById(id) { return panels.get(id) || null; } },
  });
  const exam = makeButton("exam-1", true);
  const coursePanel = {
    hidden: false,
    querySelectorAll(selector) {
      return selector === ".disclosure[aria-expanded='true']" ? [exam] : [];
    },
  };
  const course = makeButton("course-1", true);
  panels.set("course-1", coursePanel);
  panels.set("exam-1", examPanel);

  library.setExpanded(course, false);

  assert.equal(course.attributes["aria-expanded"], "false");
  assert.equal(coursePanel.hidden, true);
  assert.equal(exam.attributes["aria-expanded"], "false");
  assert.equal(examPanel.hidden, true);

  library.setExpanded(course, true);
  assert.equal(exam.attributes["aria-expanded"], "false");
  assert.equal(examPanel.hidden, true);
});

// -- Minimal fake DOM sufficient to drive initialize()'s reset controls --

class FakeLibraryElement {
  constructor() {
    this.dataset = {};
    this.textContent = "";
    this._listeners = {};
    this.disabled = false;
  }

  addEventListener(type, handler) {
    (this._listeners[type] ||= []).push(handler);
  }

  getAttribute() {
    return null;
  }

  setAttribute() {}

  closest() { return null; }
}

class FakeQuizRow {
  constructor(token, version) {
    this.dataset = { quizToken: token, quizVersion: String(version) };
    this.progress = new FakeLibraryElement();
    this.removed = false;
  }

  querySelector(selector) {
    return selector === "[data-quiz-progress]" ? this.progress : null;
  }

  remove() {
    this.removed = true;
  }
}

class FakeRemoveButton extends FakeLibraryElement {
  constructor(token, version, row) {
    super();
    this.dataset = {
      quizToken: token,
      quizVersion: String(version),
      removeUrl: `/api/published-quizzes/${token}`,
    };
    this.row = row;
  }

  closest(selector) {
    return selector === ".lecture-row" ? this.row : null;
  }
}

class FakeTitleForm extends FakeLibraryElement {
  constructor(title) {
    super();
    this.dataset.titleUrl = "/api/published-quizzes/tok1/title";
    this.input = new FakeLibraryElement();
    this.input.value = title;
    this.saveButton = new FakeLibraryElement();
  }

  querySelector(selector) {
    if (selector === "[data-title-input]") return this.input;
    if (selector === "[data-save-title]") return this.saveButton;
    return null;
  }
}

class FakeLibraryDocument {
  constructor({
    disclosures = [],
    rows = [],
    resetButtons = [],
    removeButtons = [],
    titleForms = [],
    editButtons = [],
    libraryMoveButtons = [],
    orderMoveButtons = [],
    resetMessage,
    libraryRoot,
  }) {
    this.disclosures = disclosures;
    this.rows = rows;
    this.resetButtons = resetButtons;
    this.removeButtons = removeButtons;
    this.titleForms = titleForms;
    this.editButtons = editButtons;
    this.libraryMoveButtons = libraryMoveButtons;
    this.orderMoveButtons = orderMoveButtons;
    this.resetMessage = resetMessage || new FakeLibraryElement();
    this.libraryRoot = libraryRoot || null;
  }

  querySelectorAll(selector) {
    if (selector === ".disclosure") return this.disclosures;
    if (selector === "[data-quiz-row]") return this.rows;
    if (selector === "[data-reset-quiz]") return this.resetButtons;
    if (selector === "[data-remove-quiz]") return this.removeButtons;
    if (selector === "[data-title-form]") return this.titleForms;
    if (selector === "[data-edit-questions]") return this.editButtons;
    if (selector === "[data-move-quiz-library]") return this.libraryMoveButtons;
    if (selector === "[data-move-quiz-order]") return this.orderMoveButtons;
    return [];
  }

  querySelector(selector) {
    if (selector === "[data-reset-message]") return this.resetMessage;
    if (selector === "[data-quiz-library]") return this.libraryRoot;
    return null;
  }

  addEventListener(type, handler) {
    (this._listeners ||= {})[type] = handler;
  }
}

const makeMemoryStorage = () => {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, value),
    removeItem: (key) => map.delete(key),
    key: (index) => [...map.keys()][index] ?? null,
    get length() {
      return map.size;
    },
  };
};

test("an already-loaded library initializes immediately", () => {
  const disclosure = new FakeLibraryElement();
  const documentRef = new FakeLibraryDocument({ disclosures: [disclosure] });
  documentRef.readyState = "complete";

  library.bootstrap(documentRef, makeMemoryStorage());

  assert.equal(disclosure._listeners.click.length, 1);
});

test("library initialization is idempotent", () => {
  const disclosure = new FakeLibraryElement();
  const documentRef = new FakeLibraryDocument({
    disclosures: [disclosure],
    libraryRoot: new FakeLibraryElement(),
  });

  library.initialize(documentRef, makeMemoryStorage());
  library.initialize(documentRef, makeMemoryStorage());

  assert.equal(disclosure._listeners.click.length, 1);
});

test("per-quiz reset asks for confirmation and leaves progress untouched when cancelled", async () => {
  const storage = makeMemoryStorage();
  const storageKey = library.progressKey("tok1", 1);
  storage.setItem(storageKey, JSON.stringify({ version: 1, currentIndex: 1, questions: {} }));
  const row = new FakeQuizRow("tok1", 1);
  const resetButton = new FakeLibraryElement();
  resetButton.dataset.quizToken = "tok1";
  resetButton.dataset.quizVersion = "1";
  const documentRef = new FakeLibraryDocument({ rows: [row], resetButtons: [resetButton] });

  const originalConfirm = global.confirm;
  global.confirm = () => false;
  try {
    library.initialize(documentRef, storage);
    const [handler] = resetButton._listeners.click;
    await handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.equal(storage.getItem(storageKey) !== null, true);
  assert.equal(documentRef.resetMessage.textContent, "");
});

test("per-quiz reset clears progress once confirmed", async () => {
  const storage = makeMemoryStorage();
  const storageKey = library.progressKey("tok1", 1);
  storage.setItem(storageKey, JSON.stringify({ version: 1, currentIndex: 1, questions: {} }));
  const row = new FakeQuizRow("tok1", 1);
  const resetButton = new FakeLibraryElement();
  resetButton.dataset.quizToken = "tok1";
  resetButton.dataset.quizVersion = "1";
  const documentRef = new FakeLibraryDocument({ rows: [row], resetButtons: [resetButton] });

  const originalConfirm = global.confirm;
  global.confirm = () => true;
  try {
    library.initialize(documentRef, storage);
    const [handler] = resetButton._listeners.click;
    await handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.equal(storage.getItem(storageKey), null);
  assert.match(documentRef.resetMessage.textContent, /reset/i);
});

test("edit questions opens the full private review workspace", async () => {
  const button = new FakeLibraryElement();
  button.dataset.editUrl = "/api/published-quizzes/token/edit-run";
  const documentRef = new FakeLibraryDocument({ editButtons: [button] });
  const originalFetch = global.fetch;
  const originalLocation = global.location;
  let assigned = null;
  global.fetch = async () => ({ ok: true, json: async () => ({ review_url: "/studio/runs/edit/review" }) });
  global.location = { assign: (url) => { assigned = url; } };
  try {
    library.initialize(documentRef, makeMemoryStorage());
    await button._listeners.click[0]();
  } finally {
    global.fetch = originalFetch;
    global.location = originalLocation;
  }
  assert.equal(assigned, "/studio/runs/edit/review");
});

test("remove leaves a released quiz alone when confirmation is cancelled", async () => {
  const storage = makeMemoryStorage();
  const row = new FakeQuizRow("tok1", 1);
  const removeButton = new FakeRemoveButton("tok1", 1, row);
  const documentRef = new FakeLibraryDocument({ rows: [row], removeButtons: [removeButton] });
  const originalConfirm = global.confirm;
  const originalFetch = global.fetch;
  let called = false;
  global.confirm = () => false;
  global.fetch = async () => { called = true; };
  try {
    library.initialize(documentRef, storage);
    await removeButton._listeners.click[0]();
  } finally {
    global.confirm = originalConfirm;
    global.fetch = originalFetch;
  }

  assert.equal(called, false);
  assert.equal(row.removed, false);
  assert.equal(removeButton.disabled, false);
});

test("remove clears local progress and removes a row only after successful unpublish", async () => {
  const storage = makeMemoryStorage();
  const row = new FakeQuizRow("tok1", 1);
  const removeButton = new FakeRemoveButton("tok1", 1, row);
  const documentRef = new FakeLibraryDocument({ rows: [row], removeButtons: [removeButton] });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  storage.setItem(library.progressKey("tok1", 1), "{}");
  const originalConfirm = global.confirm;
  const originalFetch = global.fetch;
  let request;
  global.confirm = () => true;
  global.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, async json() { return { state: "unpublished" }; } };
  };
  try {
    library.initialize(documentRef, storage);
    await removeButton._listeners.click[0]();
  } finally {
    global.confirm = originalConfirm;
    global.fetch = originalFetch;
  }

  assert.deepEqual(request, {
    url: "/api/published-quizzes/tok1",
    options: { method: "DELETE", headers: { "X-CSRF-Token": "csrf-token" } },
  });
  assert.equal(storage.getItem(library.progressKey("tok1", 1)), null);
  assert.equal(row.removed, true);
});

test("successful unpublish updates the UI when browser progress cleanup is denied", async () => {
  const storage = {
    getItem: () => null,
    removeItem() { throw new Error("storage denied"); },
    key: () => null,
    get length() { return 0; },
  };
  const row = new FakeQuizRow("tok1", 1);
  const removeButton = new FakeRemoveButton("tok1", 1, row);
  const documentRef = new FakeLibraryDocument({ rows: [row], removeButtons: [removeButton] });
  const originalConfirm = global.confirm;
  const originalFetch = global.fetch;
  global.confirm = () => true;
  global.fetch = async () => ({
    ok: true,
    async json() {
      return {
        state: "unpublished",
        exam_quiz_count: 0,
        course_quiz_count: 0,
      };
    },
  });
  try {
    library.initialize(documentRef, storage);
    await removeButton._listeners.click[0]();
  } finally {
    global.confirm = originalConfirm;
    global.fetch = originalFetch;
  }

  assert.equal(row.removed, true);
  assert.match(documentRef.resetMessage.textContent, /released quiz was removed/i);
  assert.match(documentRef.resetMessage.textContent, /progress could not be cleared/i);
});

test("storage-denied unpublish still applies authoritative counts, pruning, disclosure, and focus", async () => {
  const storage = {
    getItem: () => null,
    removeItem() { throw new Error("storage denied"); },
    key: () => null,
    get length() { return 0; },
  };
  const examCount = { textContent: "1 quiz" };
  const courseCount = { textContent: "2 quizzes" };
  const courseDisclosure = { attributes: { "aria-expanded": "true" } };
  const exam = {
    removed: false,
    querySelector(selector) { return selector === "[data-exam-count]" ? examCount : null; },
    remove() { this.removed = true; },
  };
  const course = {
    removed: false,
    querySelector(selector) {
      if (selector === "[data-course-count]") return courseCount;
      if (selector === ".disclosure") return courseDisclosure;
      return null;
    },
    remove() { this.removed = true; },
  };
  const survivingControl = {
    focused: false, isConnected: true, disabled: false,
    focus() { this.focused = true; },
  };
  const survivingRow = { querySelector: () => survivingControl };
  const row = new FakeQuizRow("tok1", 1);
  row.nextElementSibling = survivingRow;
  row.closest = (selector) => selector === "[data-exam-key]" ? exam : course;
  const removeButton = new FakeRemoveButton("tok1", 1, row);
  const documentRef = new FakeLibraryDocument({ rows: [row], removeButtons: [removeButton] });
  const originalConfirm = global.confirm;
  const originalFetch = global.fetch;
  global.confirm = () => true;
  global.fetch = async () => ({
    ok: true,
    async json() {
      return {
        state: "unpublished", exam_key: "neuro:1", course_key: "neuro",
        exam_quiz_count: 0, course_quiz_count: 1,
      };
    },
  });
  try {
    library.initialize(documentRef, storage);
    await removeButton._listeners.click[0]();
  } finally {
    global.confirm = originalConfirm;
    global.fetch = originalFetch;
  }

  assert.equal(row.removed, true);
  assert.equal(examCount.textContent, "0 quizzes");
  assert.equal(courseCount.textContent, "1 quiz");
  assert.equal(exam.removed, true);
  assert.equal(course.removed, false);
  assert.equal(courseDisclosure.attributes["aria-expanded"], "true");
  assert.equal(survivingControl.focused, true);
  assert.match(documentRef.resetMessage.textContent, /released quiz was removed/i);
  assert.match(documentRef.resetMessage.textContent, /progress could not be cleared/i);
});

test("failed remove keeps the row and reports the server detail", async () => {
  const storage = makeMemoryStorage();
  const row = new FakeQuizRow("tok1", 1);
  const removeButton = new FakeRemoveButton("tok1", 1, row);
  const documentRef = new FakeLibraryDocument({ rows: [row], removeButtons: [removeButton] });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const originalConfirm = global.confirm;
  const originalFetch = global.fetch;
  global.confirm = () => true;
  global.fetch = async () => ({
    ok: false,
    async json() { return { detail: "Cloudflare Access identity is required" }; },
  });
  try {
    library.initialize(documentRef, storage);
    await removeButton._listeners.click[0]();
  } finally {
    global.confirm = originalConfirm;
    global.fetch = originalFetch;
  }

  assert.equal(row.removed, false);
  assert.equal(removeButton.disabled, false);
  assert.equal(documentRef.resetMessage.textContent, "Cloudflare Access identity is required");
});

test("title edit sends a trimmed PATCH and preserves the page after success", async () => {
  const titleForm = new FakeTitleForm("  Revised title  ");
  const documentRef = new FakeLibraryDocument({ titleForms: [titleForm] });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const originalFetch = global.fetch;
  const originalLocation = global.location;
  let request;
  let reloads = 0;
  global.location = { reload: () => { reloads += 1; } };
  global.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, async json() { return { token: "tok1", title: "Revised title" }; } };
  };
  try {
    library.initialize(documentRef, makeMemoryStorage());
    await titleForm._listeners.submit[0]({ preventDefault() {} });
  } finally {
    global.fetch = originalFetch;
    global.location = originalLocation;
  }

  assert.deepEqual(request, {
    url: "/api/published-quizzes/tok1/title",
    options: {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": "csrf-token",
      },
      body: JSON.stringify({ title: "Revised title" }),
    },
  });
  assert.equal(reloads, 0);
  assert.equal(titleForm.saveButton.disabled, false);
  assert.equal(documentRef.resetMessage.textContent, "Quiz title updated.");
});

test("rename applies the authoritative title in place and focuses an enabled title input", () => {
  const display = new FakeLibraryElement();
  const input = new FakeLibraryElement();
  input.value = "Old title";
  input.dataset.titleInput = "";
  const namedSurface = (dataset, tagName) => ({
    dataset,
    tagName,
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
  });
  const reset = namedSurface({ resetQuiz: "" });
  const overflow = namedSurface({}, "SUMMARY");
  overflow.attributes.title = "More actions";
  const dragHandle = namedSurface({ quizDragHandle: "" });
  dragHandle.attributes.title = "Reorder quiz";
  dragHandle.textContent = "⠿";
  const save = new FakeLibraryElement();
  save.disabled = true;
  input.disabled = false;
  let focused = false;
  input.focus = () => { focused = true; };
  const documentRef = {
    querySelectorAll(selector) {
      assert.equal(selector, '[data-quiz-title-for="tok1"]');
      return [display, input, reset, overflow, dragHandle];
    },
  };

  library.applyRenamedTitle(documentRef, "tok1", "Authoritative title", input);

  assert.equal(display.textContent, "Authoritative title");
  assert.equal(input.value, "Authoritative title");
  assert.equal(reset.attributes["aria-label"], "Restart Authoritative title");
  assert.equal(reset.attributes.title, "Restart Authoritative title");
  assert.equal(overflow.attributes["aria-label"], "More actions for Authoritative title");
  assert.equal(dragHandle.attributes["aria-label"], "Reorder Authoritative title. Use Arrow Up or Arrow Down.");
  assert.equal(dragHandle.attributes.title, "Reorder quiz");
  assert.equal(dragHandle.textContent, "⠿");
  assert.equal(input.disabled, false);
  assert.equal(focused, true);
});

test("unpublish uses authoritative counts, prunes empty shells, and focuses a surviving row control", () => {
  const count = { textContent: "2 quizzes" };
  const exam = { removed: false, querySelector: () => count, remove() { this.removed = true; } };
  const course = { removed: false, querySelector: () => count, remove() { this.removed = true; } };
  const neighborLink = { focused: false, focus() { this.focused = true; } };
  const neighbor = { querySelector() { return neighborLink; } };
  const row = {
    removed: false,
    nextElementSibling: neighbor,
    remove() { this.removed = true; },
    closest() { return null; },
  };
  const documentRef = {
    querySelector(selector) {
      if (selector === '[data-exam-key="neuro:1"]') return exam;
      if (selector === '[data-course-key="neuro"]') return course;
      return null;
    },
  };

  library.applyUnpublish(documentRef, row, {
    exam_key: "neuro:1",
    course_key: "neuro",
    exam_quiz_count: 0,
    course_quiz_count: 0,
  });

  assert.equal(row.removed, true);
  assert.equal(exam.removed, true);
  assert.equal(course.removed, true);
  assert.equal(neighborLink.focused, true);
});

test("unpublish of the final row ignores pruned controls and focuses the library main", () => {
  const main = { focused: false, focus() { this.focused = true; } };
  const examControl = { isConnected: true, focus() { throw new Error("removed control must not receive focus"); } };
  const courseControl = { isConnected: true, focus() { throw new Error("removed control must not receive focus"); } };
  const examCount = { textContent: "1 quiz" };
  const courseCount = { textContent: "1 quiz" };
  const exam = {
    isConnected: true,
    querySelector(selector) { return selector === "[data-exam-count]" ? examCount : examControl; },
    remove() { this.isConnected = false; examControl.isConnected = false; },
  };
  const course = {
    isConnected: true,
    querySelector(selector) { return selector === "[data-course-count]" ? courseCount : courseControl; },
    remove() { this.isConnected = false; courseControl.isConnected = false; },
  };
  const row = {
    remove() {},
    closest(selector) { return selector === "[data-exam-key]" ? exam : course; },
  };
  const documentRef = {
    querySelector(selector) {
      if (selector === "[data-quiz-library]") return main;
      return null;
    },
  };

  library.applyUnpublish(documentRef, row, {
    exam_key: "neuro:1",
    course_key: "neuro",
    exam_quiz_count: 0,
    course_quiz_count: 0,
  });

  assert.equal(main.focused, true);
});

test("library controls preserve management payloads", async () => {
  const libraryButton = new FakeLibraryElement();
  libraryButton.dataset = {
    libraryUrl: "/api/published-quizzes/tok1/library",
    targetSection: "practice_questions",
  };
  const documentRef = new FakeLibraryDocument({
    libraryMoveButtons: [libraryButton],
  });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const originalFetch = global.fetch;
  const originalLocation = global.location;
  const requests = [];
  global.location = { reload() {} };
  global.fetch = async (url, options) => {
    requests.push({ url, body: options.body });
    return { ok: true, async json() { return {}; } };
  };
  try {
    library.initialize(documentRef, makeMemoryStorage());
    await libraryButton._listeners.click[0]();
  } finally {
    global.fetch = originalFetch;
    global.location = originalLocation;
  }

  assert.deepEqual(requests, [
    {
      url: "/api/published-quizzes/tok1/library",
      body: JSON.stringify({ section: "practice_questions" }),
    },
  ]);
});

const makeOrderList = (tokens, { reducedMotion = false, extraControls = [] } = {}) => {
  const list = {
    children: [],
    extraControls,
    classList: { add() {}, remove() {} },
    querySelectorAll(selector) {
      if (selector === "[data-quiz-order-row]") return this.children;
      if (selector === "[data-quiz-drag-handle]") {
        return this.children.map((row) => row.handle);
      }
      if (selector === "button, input, select, textarea") {
        return [...this.children.map((row) => row.handle), ...this.extraControls];
      }
      return [];
    },
    appendChild(row) {
      this.children = this.children.filter((candidate) => candidate !== row);
      this.children.push(row);
      row.parentElement = this;
    },
    insertBefore(row, before) {
      this.children = this.children.filter((candidate) => candidate !== row);
      const index = this.children.indexOf(before);
      this.children.splice(index < 0 ? this.children.length : index, 0, row);
      row.parentElement = this;
    },
  };
  for (const token of tokens) {
    const handle = new FakeLibraryElement();
    handle.focus = () => { handle.focused = true; };
    const row = {
      dataset: {
        quizToken: token,
        orderUrl: `/api/published-quizzes/${token}/order`,
      },
      handle,
      parentElement: list,
      querySelector(selector) {
        return selector === "[data-quiz-drag-handle]" ? handle : null;
      },
    };
    handle.closest = (selector) => selector === "[data-quiz-order-row]" ? row : null;
    list.children.push(row);
  }
  const documentRef = new FakeLibraryDocument({});
  documentRef.cookie = "study_hub_csrf=csrf-token";
  documentRef.defaultView = {
    matchMedia: () => ({ matches: reducedMotion }),
  };
  return { list, documentRef };
};

const fakeSortable = () => {
  const created = [];
  return {
    created,
    create(list, options) {
      const instance = {
        list,
        options,
        optionCalls: [],
        option(name, value) { this.optionCalls.push([name, value]); },
        sort(tokens) { library.applyTokenOrder(list, tokens); },
      };
      created.push(instance);
      return instance;
    },
  };
};

test("a long drag saves the full order in one request and keeps the server order", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c", "d"]);
  const Sortable = fakeSortable();
  const requests = [];
  const binding = library.bindSortableReorder(
    documentRef,
    list,
    Sortable,
    async (url, options) => {
      requests.push({ url, options });
      return {
        ok: true,
        async json() { return { token: "a", ordered_tokens: ["b", "d", "c", "a"] }; },
      };
    },
  );

  binding.options.onStart({ item: list.children[0] });
  list.appendChild(list.children[0]);
  await binding.options.onEnd({ item: list.children[3], oldIndex: 0, newIndex: 3 });

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/published-quizzes/a/order");
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    ordered_tokens: ["b", "c", "d", "a"],
  });
  assert.deepEqual(library.tokenOrder(list), ["b", "d", "c", "a"]);
  assert.equal(documentRef.resetMessage.textContent, "Quiz order saved.");
});

test("a no-op drag sends no request", async () => {
  const { list, documentRef } = makeOrderList(["a", "b"]);
  const Sortable = fakeSortable();
  let requests = 0;
  const binding = library.bindSortableReorder(
    documentRef,
    list,
    Sortable,
    async () => { requests += 1; },
  );

  binding.options.onStart({ item: list.children[0] });
  await binding.options.onEnd({ item: list.children[0], oldIndex: 0, newIndex: 0 });

  assert.equal(requests, 0);
});

test("a cancelled pointer drag restores its snapshot without saving", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const Sortable = fakeSortable();
  let requests = 0;
  const binding = library.bindSortableReorder(
    documentRef,
    list,
    Sortable,
    async () => { requests += 1; },
  );

  binding.options.onStart({ item: list.children[0] });
  list.appendChild(list.children[0]);
  await binding.options.onEnd({
    item: list.children[2],
    oldIndex: 0,
    newIndex: 2,
    originalEvent: { type: "pointercancel" },
  });

  assert.equal(requests, 0);
  assert.deepEqual(library.tokenOrder(list), ["a", "b", "c"]);
});

test("a cancelled legacy touch drag restores its snapshot without saving", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const Sortable = fakeSortable();
  let requests = 0;
  const binding = library.bindSortableReorder(
    documentRef,
    list,
    Sortable,
    async () => { requests += 1; },
  );

  binding.options.onStart({ item: list.children[0] });
  list.appendChild(list.children[0]);
  await binding.options.onEnd({
    item: list.children[2],
    oldIndex: 0,
    newIndex: 2,
    originalEvent: { type: "touchcancel" },
  });

  assert.equal(requests, 0);
  assert.deepEqual(library.tokenOrder(list), ["a", "b", "c"]);
});

test("Escape cancels an active Sortable drag without saving on release", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const Sortable = fakeSortable();
  let requests = 0;
  const binding = library.bindSortableReorder(
    documentRef,
    list,
    Sortable,
    async () => { requests += 1; },
  );

  binding.options.onStart({ item: list.children[0] });
  list.appendChild(list.children[0]);
  assert.equal(library.cancelActiveReorder(documentRef), true);
  await binding.options.onEnd({ item: list.children[0], oldIndex: 0, newIndex: 0 });

  assert.equal(requests, 0);
  assert.deepEqual(library.tokenOrder(list), ["a", "b", "c"]);
});

test("a pending save locks every reorder handle and prevents another save", async () => {
  const mutationControl = new FakeLibraryElement();
  const alreadyDisabled = new FakeLibraryElement();
  alreadyDisabled.disabled = true;
  const { list, documentRef } = makeOrderList(
    ["a", "b", "c"],
    { extraControls: [mutationControl, alreadyDisabled] },
  );
  list.children[2].handle.disabled = true;
  const Sortable = fakeSortable();
  let release;
  let requests = 0;
  const response = new Promise((resolve) => { release = resolve; });
  const binding = library.bindSortableReorder(
    documentRef,
    list,
    Sortable,
    async () => { requests += 1; return response; },
  );
  const original = library.tokenOrder(list);
  list.appendChild(list.children[0]);

  const first = library.saveQuizOrder(
    documentRef,
    list,
    list.children[2],
    original,
    library.tokenOrder(list),
    async () => { requests += 1; return response; },
  );
  const second = library.saveQuizOrder(
    documentRef,
    list,
    list.children[0],
    original,
    library.tokenOrder(list),
    async () => { requests += 1; return response; },
  );

  assert.equal(requests, 1);
  assert.deepEqual(list.children.map((row) => row.handle.disabled), [true, true, true]);
  assert.equal(mutationControl.disabled, true);
  assert.equal(alreadyDisabled.disabled, true);
  release({
    ok: true,
    async json() { return { token: "a", ordered_tokens: ["b", "c", "a"] }; },
  });
  assert.equal(await first, true);
  assert.equal(await second, false);
  assert.deepEqual(list.children.map((row) => row.handle.disabled), [false, true, false]);
  assert.equal(mutationControl.disabled, false);
  assert.equal(alreadyDisabled.disabled, true);
  assert.deepEqual(binding.optionCalls, [["disabled", true], ["disabled", false]]);
});

test("Arrow Down uses the same atomic save path and retains focus", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const handle = list.children[0].handle;
  const requests = [];
  library.bindKeyboardReorder(documentRef, handle, async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return {
      ok: true,
      async json() { return { token: "a", ordered_tokens: ["b", "a", "c"] }; },
    };
  });
  let prevented = false;

  await handle._listeners.keydown[0]({
    key: "ArrowDown",
    preventDefault() { prevented = true; },
  });

  assert.equal(prevented, true);
  assert.deepEqual(requests, [{ ordered_tokens: ["b", "a", "c"] }]);
  assert.deepEqual(library.tokenOrder(list), ["b", "a", "c"]);
  assert.equal(handle.focused, true);
});

test("keyboard input during an active pointer drag waits for the final drop save", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const Sortable = fakeSortable();
  const requests = [];
  const fetchImpl = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return {
      ok: true,
      async json() { return { token: "a", ordered_tokens: ["b", "c", "a"] }; },
    };
  };
  const binding = library.bindSortableReorder(documentRef, list, Sortable, fetchImpl);
  const handle = list.children[0].handle;
  library.bindKeyboardReorder(documentRef, handle, fetchImpl);
  let prevented = false;

  binding.options.onStart({ item: list.children[0] });
  await handle._listeners.keydown[0]({
    key: "ArrowDown",
    preventDefault() { prevented = true; },
  });
  assert.deepEqual(library.tokenOrder(list), ["a", "b", "c"]);
  list.appendChild(list.children[0]);
  await binding.options.onEnd({ item: list.children[2], oldIndex: 0, newIndex: 2 });

  assert.equal(prevented, true);
  assert.deepEqual(requests, [{ ordered_tokens: ["b", "c", "a"] }]);
});

test("an unconfirmed save restores the snapshot and tells the user to refresh", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const original = library.tokenOrder(list);
  const row = list.children[0];
  list.appendChild(row);

  const saved = await library.saveQuizOrder(
    documentRef,
    list,
    row,
    original,
    library.tokenOrder(list),
    async () => ({
      ok: true,
      async json() { return { token: "a", ordered_tokens: ["a", "not-in-this-list", "c"] }; },
    }),
  );

  assert.equal(saved, false);
  assert.deepEqual(library.tokenOrder(list), original);
  assert.match(documentRef.resetMessage.textContent, /could not be confirmed/i);
  assert.match(documentRef.resetMessage.textContent, /refresh/i);
});

test("a changed list cannot report saved when the authoritative order cannot be applied", async () => {
  const { list, documentRef } = makeOrderList(["a", "b", "c"]);
  const original = library.tokenOrder(list);
  const row = list.children[0];
  list.appendChild(row);

  const saved = await library.saveQuizOrder(
    documentRef,
    list,
    row,
    original,
    library.tokenOrder(list),
    async () => {
      list.children = list.children.filter((candidate) => candidate.dataset.quizToken !== "c");
      return {
        ok: true,
        async json() { return { token: "a", ordered_tokens: ["b", "c", "a"] }; },
      };
    },
  );

  assert.equal(saved, false);
  assert.notEqual(documentRef.resetMessage.textContent, "Quiz order saved.");
  assert.match(documentRef.resetMessage.textContent, /could not be confirmed/i);
  assert.match(documentRef.resetMessage.textContent, /refresh/i);
  assert.doesNotMatch(documentRef.resetMessage.textContent, /previous order is shown/i);
});

test("Sortable is vertical, list-local, fluid, touch-friendly, and honors reduced motion", () => {
  const { list, documentRef } = makeOrderList(["a", "b"], { reducedMotion: true });
  const Sortable = fakeSortable();

  const binding = library.bindSortableReorder(documentRef, list, Sortable, async () => {});

  assert.equal(binding.options.direction, "vertical");
  assert.deepEqual(binding.options.group, { pull: false, put: false });
  assert.equal(binding.options.handle, "[data-quiz-drag-handle]");
  assert.equal(binding.options.draggable, "[data-quiz-order-row]");
  assert.equal(binding.options.animation, 0);
  assert.equal(binding.options.forceFallback, true);
  assert.equal(binding.options.fallbackOnBody, true);
  assert.ok(binding.options.fallbackTolerance >= 4);
  assert.equal(binding.options.scroll, true);
});

test("failed management updates keep the button enabled and do not reload", async () => {
  const button = new FakeLibraryElement();
  button.dataset = {
    libraryUrl: "/api/published-quizzes/tok1/library",
    targetSection: "practice_questions",
  };
  const documentRef = new FakeLibraryDocument({ libraryMoveButtons: [button] });
  const originalFetch = global.fetch;
  const originalLocation = global.location;
  let reloads = 0;
  global.location = { reload: () => { reloads += 1; } };
  global.fetch = async () => ({
    ok: false,
    async json() { return { detail: "Quiz was not found" }; },
  });
  try {
    library.initialize(documentRef, makeMemoryStorage());
    await button._listeners.click[0]();
  } finally {
    global.fetch = originalFetch;
    global.location = originalLocation;
  }

  assert.equal(reloads, 0);
  assert.equal(button.disabled, false);
  assert.equal(documentRef.resetMessage.textContent, "Quiz was not found");
});
