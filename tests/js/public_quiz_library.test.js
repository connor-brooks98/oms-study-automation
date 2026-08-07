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
  assert.equal(library.progressLabel(complete, 2), "Completed");
  assert.equal(library.progressLabel(complete, 3), "Not started");
});

test("corrupt browser progress is treated as not started", () => {
  const storage = {
    getItem: () => "{not-json",
  };

  assert.equal(library.readProgress(storage, "token", 1), "Not started");
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

class FakeLibraryDocument {
  constructor({
    rows = [],
    resetButtons = [],
    removeButtons = [],
    titleButtons = [],
    libraryMoveButtons = [],
    orderMoveButtons = [],
    resetMessage,
    resetProgressButton,
  }) {
    this.rows = rows;
    this.resetButtons = resetButtons;
    this.removeButtons = removeButtons;
    this.titleButtons = titleButtons;
    this.libraryMoveButtons = libraryMoveButtons;
    this.orderMoveButtons = orderMoveButtons;
    this.resetMessage = resetMessage || new FakeLibraryElement();
    this.resetProgressButton = resetProgressButton || null;
  }

  querySelectorAll(selector) {
    if (selector === ".disclosure") return [];
    if (selector === "[data-quiz-row]") return this.rows;
    if (selector === "[data-reset-quiz]") return this.resetButtons;
    if (selector === "[data-remove-quiz]") return this.removeButtons;
    if (selector === "[data-edit-quiz-title]") return this.titleButtons;
    if (selector === "[data-move-quiz-library]") return this.libraryMoveButtons;
    if (selector === "[data-move-quiz-order]") return this.orderMoveButtons;
    return [];
  }

  querySelector(selector) {
    if (selector === "[data-reset-message]") return this.resetMessage;
    if (selector === "[data-reset-progress]") return this.resetProgressButton;
    return null;
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

test("global reset-all asks for confirmation and leaves progress untouched when cancelled", () => {
  const storage = makeMemoryStorage();
  storage.setItem("oms-study-hub-quiz:tok1:v1", "{}");
  const resetProgressButton = new FakeLibraryElement();
  const documentRef = new FakeLibraryDocument({ resetProgressButton });

  const originalConfirm = global.confirm;
  global.confirm = () => false;
  try {
    library.initialize(documentRef, storage);
    const [handler] = resetProgressButton._listeners.click;
    handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.equal(storage.getItem("oms-study-hub-quiz:tok1:v1"), "{}");
});

test("global reset-all clears every quiz's progress once confirmed", () => {
  const storage = makeMemoryStorage();
  storage.setItem("oms-study-hub-quiz:tok1:v1", "{}");
  storage.setItem("oms-study-hub-quiz:tok2:v1", "{}");
  storage.setItem("unrelated-key", "keep me");
  const resetProgressButton = new FakeLibraryElement();
  const documentRef = new FakeLibraryDocument({ resetProgressButton });

  const originalConfirm = global.confirm;
  global.confirm = () => true;
  try {
    library.initialize(documentRef, storage);
    const [handler] = resetProgressButton._listeners.click;
    handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.equal(storage.getItem("oms-study-hub-quiz:tok1:v1"), null);
  assert.equal(storage.getItem("oms-study-hub-quiz:tok2:v1"), null);
  assert.equal(storage.getItem("unrelated-key"), "keep me");
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
  const titleButton = new FakeLibraryElement();
  titleButton.dataset = {
    title: "Old title",
    titleUrl: "/api/published-quizzes/tok1/title",
  };
  const documentRef = new FakeLibraryDocument({ titleButtons: [titleButton] });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const originalFetch = global.fetch;
  const originalPrompt = global.prompt;
  const originalLocation = global.location;
  let request;
  let reloads = 0;
  global.prompt = () => "  Revised title  ";
  global.location = { reload: () => { reloads += 1; } };
  global.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, async json() { return { title: "Revised title" }; } };
  };
  try {
    library.initialize(documentRef, makeMemoryStorage());
    await titleButton._listeners.click[0]();
  } finally {
    global.fetch = originalFetch;
    global.prompt = originalPrompt;
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

test("library and order controls send their PATCH payloads", async () => {
  const libraryButton = new FakeLibraryElement();
  libraryButton.dataset = {
    libraryUrl: "/api/published-quizzes/tok1/library",
    targetSection: "practice_questions",
  };
  const upButton = new FakeLibraryElement();
  upButton.dataset = {
    orderUrl: "/api/published-quizzes/tok1/order",
    direction: "up",
  };
  const downButton = new FakeLibraryElement();
  downButton.dataset = {
    orderUrl: "/api/published-quizzes/tok1/order",
    direction: "down",
  };
  const documentRef = new FakeLibraryDocument({
    libraryMoveButtons: [libraryButton],
    orderMoveButtons: [upButton, downButton],
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
    await upButton._listeners.click[0]();
    await downButton._listeners.click[0]();
  } finally {
    global.fetch = originalFetch;
    global.location = originalLocation;
  }

  assert.deepEqual(requests, [
    {
      url: "/api/published-quizzes/tok1/library",
      body: JSON.stringify({ section: "practice_questions" }),
    },
    {
      url: "/api/published-quizzes/tok1/order",
      body: JSON.stringify({ direction: "up" }),
    },
    {
      url: "/api/published-quizzes/tok1/order",
      body: JSON.stringify({ direction: "down" }),
    },
  ]);
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
