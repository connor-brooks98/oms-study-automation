const test = require("node:test");
const assert = require("node:assert/strict");

const library = require("../../src/oms_hub/web/static/public_quiz_library.js");

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
    rows = [],
    resetButtons = [],
    removeButtons = [],
    titleForms = [],
    libraryMoveButtons = [],
    orderMoveButtons = [],
    resetMessage,
  }) {
    this.rows = rows;
    this.resetButtons = resetButtons;
    this.removeButtons = removeButtons;
    this.titleForms = titleForms;
    this.libraryMoveButtons = libraryMoveButtons;
    this.orderMoveButtons = orderMoveButtons;
    this.resetMessage = resetMessage || new FakeLibraryElement();
  }

  querySelectorAll(selector) {
    if (selector === ".disclosure") return [];
    if (selector === "[data-quiz-row]") return this.rows;
    if (selector === "[data-reset-quiz]") return this.resetButtons;
    if (selector === "[data-remove-quiz]") return this.removeButtons;
    if (selector === "[data-title-form]") return this.titleForms;
    if (selector === "[data-move-quiz-library]") return this.libraryMoveButtons;
    if (selector === "[data-move-quiz-order]") return this.orderMoveButtons;
    return [];
  }

  querySelector(selector) {
    if (selector === "[data-reset-message]") return this.resetMessage;
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

test("per-quiz reset asks for confirmation and leaves progress untouched when cancelled", () => {
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
    handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.equal(storage.getItem(storageKey) !== null, true);
  assert.equal(documentRef.resetMessage.textContent, "");
});

test("per-quiz reset clears progress once confirmed", () => {
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
    handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.equal(storage.getItem(storageKey), null);
  assert.match(documentRef.resetMessage.textContent, /reset/i);
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

test("title edit sends a trimmed PATCH and reloads only after success", async () => {
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
    return { ok: true, async json() { return { title: "Revised title" }; } };
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
  assert.equal(reloads, 1);
});

test("library controls and direction sequences preserve management payloads", async () => {
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
  assert.deepEqual(library.directionSequence(0, 3), ["down", "down", "down"]);
  assert.deepEqual(library.directionSequence(3, 1), ["up", "up"]);
  assert.deepEqual(library.directionSequence(2, 2), []);
});

test("failed drag reorder keeps the control usable and reports the server detail", async () => {
  const control = new FakeLibraryElement();
  const row = { dataset: { orderUrl: "/api/published-quizzes/tok1/order" } };
  const documentRef = new FakeLibraryDocument({});
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const originalLocation = global.location;
  let reloads = 0;
  global.location = { reload: () => { reloads += 1; } };
  let result;
  try {
    result = await library.reorderRequest(
      documentRef,
      control,
      row,
      ["down", "down"],
      async () => ({ ok: false, async json() { return { detail: "Order is no longer available" }; } }),
    );
  } finally {
    global.location = originalLocation;
  }

  assert.equal(result, false);
  assert.equal(reloads, 0);
  assert.equal(control.disabled, false);
  assert.equal(documentRef.resetMessage.textContent, "Order is no longer available");
});

test("successful multi-step reorder sends every direction and reloads once", async () => {
  const control = new FakeLibraryElement();
  const row = { dataset: { orderUrl: "/api/published-quizzes/tok1/order" } };
  const documentRef = new FakeLibraryDocument({});
  const originalLocation = global.location;
  const requests = [];
  let reloads = 0;
  global.location = { reload: () => { reloads += 1; } };
  try {
    const result = await library.reorderRequest(
      documentRef,
      control,
      row,
      ["down", "down"],
      async (_url, options) => {
        requests.push(JSON.parse(options.body).direction);
        return { ok: true, async json() { return {}; } };
      },
    );
    assert.equal(result, true);
  } finally {
    global.location = originalLocation;
  }
  assert.deepEqual(requests, ["down", "down"]);
  assert.equal(reloads, 1);
});

test("partial reorder failure reloads authoritative state and restores its message", async () => {
  const control = new FakeLibraryElement();
  const row = { dataset: { orderUrl: "/api/published-quizzes/tok1/order" } };
  const documentRef = new FakeLibraryDocument({});
  const storage = makeMemoryStorage();
  const originalLocation = global.location;
  let reloads = 0;
  let requests = 0;
  global.location = { reload: () => { reloads += 1; } };
  try {
    const result = await library.reorderRequest(
      documentRef,
      control,
      row,
      ["down", "down"],
      async () => {
        requests += 1;
        return requests === 1
          ? { ok: true, async json() { return {}; } }
          : { ok: false, async json() { return { detail: "Order changed elsewhere" }; } };
      },
      storage,
    );
    assert.equal(result, false);
  } finally {
    global.location = originalLocation;
  }
  assert.equal(reloads, 1);
  assert.equal(storage.getItem(library.reorderFailureStorageKey), "Order changed elsewhere");
  library.consumeReorderFailure(documentRef, storage);
  assert.equal(documentRef.resetMessage.textContent, "Order changed elsewhere");
  assert.equal(storage.getItem(library.reorderFailureStorageKey), null);
});

test("keyboard reorder only permits an in-bounds arrow direction", () => {
  assert.equal(library.keyboardReorderDirection("ArrowUp", 0, 3), null);
  assert.equal(library.keyboardReorderDirection("ArrowDown", 0, 3), "down");
  assert.equal(library.keyboardReorderDirection("ArrowUp", 2, 3), "up");
  assert.equal(library.keyboardReorderDirection("ArrowDown", 2, 3), null);
  assert.equal(library.keyboardReorderDirection("Enter", 1, 3), null);
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
