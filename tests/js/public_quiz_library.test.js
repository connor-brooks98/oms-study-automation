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
  }

  querySelector(selector) {
    return selector === "[data-quiz-progress]" ? this.progress : null;
  }
}

class FakeLibraryDocument {
  constructor({ rows = [], resetButtons = [], resetMessage, resetProgressButton }) {
    this.rows = rows;
    this.resetButtons = resetButtons;
    this.resetMessage = resetMessage || new FakeLibraryElement();
    this.resetProgressButton = resetProgressButton || null;
  }

  querySelectorAll(selector) {
    if (selector === ".disclosure") return [];
    if (selector === "[data-quiz-row]") return this.rows;
    if (selector === "[data-reset-quiz]") return this.resetButtons;
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
