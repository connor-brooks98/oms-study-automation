const test = require("node:test");
const assert = require("node:assert/strict");

const quiz = require("../../src/oms_hub/web/static/public_quiz.js");

const content = {
  token: "a".repeat(64),
  version: 3,
  questions: [
    {
      id: "q1",
      stem: "Which mechanism is most likely?",
      choices: [
        { id: "c1", text: "First" },
        { id: "c2", text: "Second" },
        { id: "c3", text: "Third" },
      ],
    },
    {
      id: "q2",
      stem: "Which finding is expected?",
      choices: [
        { id: "c1", text: "Alpha" },
        { id: "c2", text: "Beta" },
      ],
    },
  ],
};

test("answer selection stays editable until submission", () => {
  let state = quiz.createQuizState(content);

  state = quiz.selectChoice(state, "q1", "c1");
  state = quiz.selectChoice(state, "q1", "c2");

  assert.equal(state.questions.q1.selectedChoiceId, "c2");
  assert.equal(state.questions.q1.submitted, false);
});

test("eliminating an answer clears its selection and can be restored", () => {
  let state = quiz.selectChoice(quiz.createQuizState(content), "q1", "c2");

  state = quiz.toggleEliminated(state, "q1", "c2");
  assert.equal(state.questions.q1.selectedChoiceId, null);
  assert.deepEqual(state.questions.q1.eliminatedChoiceIds, ["c2"]);

  state = quiz.toggleEliminated(state, "q1", "c2");
  assert.deepEqual(state.questions.q1.eliminatedChoiceIds, []);
});

test("submitted feedback locks the question and changes score only once", () => {
  let state = quiz.selectChoice(quiz.createQuizState(content), "q1", "c1");
  const feedback = {
    correct: true,
    correct_choice_id: "c1",
    rationale: "First is correct.",
  };

  state = quiz.recordFeedback(state, "q1", feedback);
  const repeated = quiz.recordFeedback(state, "q1", feedback);
  const changed = quiz.selectChoice(repeated, "q1", "c2");

  assert.equal(state.questions.q1.submitted, true);
  assert.equal(state.score, 1);
  assert.equal(repeated.score, 1);
  assert.equal(changed.questions.q1.selectedChoiceId, "c1");
});

test("highlight ranges merge and can be cleared", () => {
  let state = quiz.createQuizState(content);

  state = quiz.addHighlight(state, "q1", 5, 12);
  state = quiz.addHighlight(state, "q1", 10, 18);
  assert.deepEqual(state.questions.q1.highlights, [{ start: 5, end: 18 }]);

  state = quiz.clearHighlights(state, "q1");
  assert.deepEqual(state.questions.q1.highlights, []);
});

test("progress restores only for the same quiz token and version", () => {
  let state = quiz.selectChoice(quiz.createQuizState(content), "q1", "c2");
  state = quiz.toggleEliminated(state, "q1", "c3");
  state = quiz.addHighlight(state, "q1", 0, 5);
  const serialized = quiz.serializeProgress(state);

  const restored = quiz.restoreProgress(content, serialized);
  const newVersion = quiz.restoreProgress(
    { ...content, version: 4 },
    serialized,
  );

  assert.equal(restored.questions.q1.selectedChoiceId, "c2");
  assert.deepEqual(restored.questions.q1.eliminatedChoiceIds, ["c3"]);
  assert.deepEqual(restored.questions.q1.highlights, [{ start: 0, end: 5 }]);
  assert.equal(newVersion.questions.q1.selectedChoiceId, null);
});

test("corrupted submitted feedback is discarded instead of breaking the quiz", () => {
  const corrupted = quiz.createQuizState(content);
  corrupted.questions.q1.selectedChoiceId = "c1";
  corrupted.questions.q1.submitted = true;
  corrupted.questions.q1.feedback = null;

  const restored = quiz.restoreProgress(
    content,
    quiz.serializeProgress(corrupted),
  );

  assert.equal(restored.questions.q1.submitted, false);
  assert.equal(restored.questions.q1.selectedChoiceId, null);
});

test("answer request sends CSRF protection and keeps answers out of URL", async () => {
  let captured;
  const fakeFetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      json: async () => ({
        correct: false,
        correct_choice_id: "c1",
        rationale: "Explanation",
      }),
    };
  };

  const feedback = await quiz.answerRequest(
    fakeFetch,
    "/public/quizzes/token/answer",
    "q1",
    "c2",
    "csrf-token",
  );

  assert.equal(captured.url, "/public/quizzes/token/answer");
  assert.equal(captured.url.includes("q1"), false);
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers["X-CSRF-Token"], "csrf-token");
  assert.deepEqual(JSON.parse(captured.options.body), {
    question_id: "q1",
    choice_id: "c2",
  });
  assert.equal(feedback.correct, false);
});

test("question navigation and flags persist in quiz state", () => {
  let state = quiz.createQuizState({ ...content, questions: content.questions });

  state = quiz.setFlagReason(state, "q1", "inaccurate_question");
  state = quiz.navigateQuestion(state, 1, content.questions.length);

  assert.equal(state.currentIndex, 1);
  assert.equal(state.questions.q1.flagReason, "inaccurate_question");
  assert.throws(
    () => quiz.setFlagReason(state, "q1", "not-a-reason"),
    /Unknown flag reason/,
  );
});

// -- Minimal fake DOM sufficient to drive initialize()/render() --

class FakeQuizNode {
  constructor(tag, documentRef) {
    this.tagName = tag;
    this.documentRef = documentRef;
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.style = {};
    this.className = "";
    this._classSet = new Set();
    this.classList = {
      add: (name) => this._classSet.add(name),
      contains: (name) => this._classSet.has(name),
    };
    this._text = "";
    this._listeners = {};
    this.disabled = false;
    this.type = "";
    this.value = "";
  }

  set textContent(value) {
    this._text = value;
    this.children = [];
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((child) => child.textContent || "").join("");
  }

  append(...nodes) {
    nodes.forEach((node) => {
      if (node && typeof node === "object") node.parent = this;
      this.children.push(node);
    });
  }

  replaceChildren() {
    this.children = [];
  }

  addEventListener(type, handler) {
    (this._listeners[type] ||= []).push(handler);
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  focus() {
    // Mirrors real <button>/<select> behavior: focusing a disabled control
    // is a silent no-op, it does not become the active element.
    if (this.disabled) return;
    this.documentRef.activeElement = this;
  }

  contains(node) {
    let current = node;
    while (current) {
      if (current === this) return true;
      current = current.parent;
    }
    return false;
  }

  querySelector(selector) {
    const match = /^\[data-focus-key="([^"]+)"\]$/.exec(selector);
    if (!match) return null;
    const [, key] = match;
    const stack = [...this.children];
    while (stack.length) {
      const node = stack.shift();
      if (node?.dataset?.focusKey === key) return node;
      if (node?.children) stack.push(...node.children);
    }
    return null;
  }
}

class FakeQuizDocument {
  constructor() {
    this.activeElement = null;
    this.defaultView = undefined;
    this.cookie = "";
  }

  createElement(tag) {
    return new FakeQuizNode(tag, this);
  }

  createTextNode(text) {
    return { tagName: "#text", textContent: text };
  }

  querySelector(selector) {
    if (selector === "[data-quiz-token]") return this.app;
    return null;
  }
}

const buildQuizApp = () => {
  const documentRef = new FakeQuizDocument();
  const app = documentRef.createElement("main");
  app.dataset.contentUrl = "/mock/content";
  app.dataset.answerUrl = "/mock/answer";
  documentRef.app = app;
  return { documentRef, app };
};

const makeQuizStorage = () => {
  const map = new Map();
  const removed = [];
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, value),
    removeItem: (key) => {
      map.delete(key);
      removed.push(key);
    },
    removed,
  };
};

test("initialize renders the could-not-load state when the fetch rejects", async () => {
  const { documentRef, app } = buildQuizApp();
  const fetchImpl = async () => {
    throw new Error("network down");
  };

  await quiz.initialize(documentRef, fetchImpl);

  assert.equal(app.textContent, "This quiz could not be loaded.");
});

test("initialize renders the could-not-load state when the response body is not valid JSON", async () => {
  const { documentRef, app } = buildQuizApp();
  const fetchImpl = async () => ({
    ok: true,
    async json() {
      throw new SyntaxError("Unexpected token");
    },
  });

  await quiz.initialize(documentRef, fetchImpl);

  assert.equal(app.textContent, "This quiz could not be loaded.");
});

test("restoreFocus falls back to the container when the equivalent control renders disabled", async () => {
  const { documentRef, app } = buildQuizApp();
  const content = {
    token: "tok",
    version: 1,
    questions: [
      {
        id: "q1",
        stem: "Q1?",
        choices: [{ id: "c1", text: "A" }, { id: "c2", text: "B" }],
      },
      {
        id: "q2",
        stem: "Q2?",
        choices: [{ id: "c1", text: "A" }, { id: "c2", text: "B" }],
      },
    ],
  };
  const fetchImpl = async (url) => {
    if (url === "/mock/content") {
      return { ok: true, async json() { return content; } };
    }
    if (url === "/mock/answer") {
      return {
        ok: true,
        async json() {
          return { correct: true, correct_choice_id: "c1", rationale: "Because." };
        },
      };
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  await quiz.initialize(documentRef, fetchImpl);

  // Select and submit an answer on q1 so "Next →" becomes enabled.
  const answerButton = app.querySelector('[data-focus-key="answer-c1"]');
  assert.ok(answerButton, "expected an answer choice button");
  answerButton._listeners.click[0]();

  const submitButton = app.querySelector('[data-focus-key="submit"]');
  assert.ok(submitButton, "expected a submit button once an answer is selected");
  await submitButton._listeners.click[0]();

  const forwardButton = app.querySelector('[data-focus-key="forward"]');
  assert.ok(forwardButton, "expected a forward/next button");
  assert.equal(forwardButton.disabled, false, "forward should be enabled once submitted");
  forwardButton.focus();
  assert.equal(documentRef.activeElement, forwardButton);

  // Advance to q2 - the freshly rendered "forward" button starts out
  // disabled again (q2 has not been submitted yet). restoreFocus must not
  // silently no-op by calling .focus() on that disabled control; it should
  // fall back to the tabindex="-1" player container instead.
  forwardButton._listeners.click[0]();

  const nextForwardButton = app.querySelector('[data-focus-key="forward"]');
  assert.ok(nextForwardButton);
  assert.equal(
    nextForwardButton.disabled,
    true,
    "the new question's forward button should start disabled",
  );
  assert.equal(
    documentRef.activeElement,
    app,
    "focus should fall back to the tabindex=-1 container, not silently stay put",
  );
});

test("results-screen Start Over is a no-op when the confirmation is cancelled", async () => {
  const { documentRef, app } = buildQuizApp();
  documentRef.defaultView = { localStorage: makeQuizStorage() };
  const fetchImpl = async () => ({
    ok: true,
    async json() {
      return { token: "tok", version: 1, questions: [] };
    },
  });

  const originalConfirm = global.confirm;
  global.confirm = () => false;
  try {
    await quiz.initialize(documentRef, fetchImpl);
    const restart = app.querySelector('[data-focus-key="result-restart"]');
    assert.ok(restart, "expected a Start Over button on the results screen");
    const [handler] = restart._listeners.click;
    handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.deepEqual(documentRef.defaultView.localStorage.removed, []);
});

test("results-screen Start Over clears progress once confirmed", async () => {
  const { documentRef, app } = buildQuizApp();
  documentRef.defaultView = { localStorage: makeQuizStorage() };
  const fetchImpl = async () => ({
    ok: true,
    async json() {
      return { token: "tok", version: 1, questions: [] };
    },
  });

  const originalConfirm = global.confirm;
  global.confirm = () => true;
  try {
    await quiz.initialize(documentRef, fetchImpl);
    const restart = app.querySelector('[data-focus-key="result-restart"]');
    const [handler] = restart._listeners.click;
    handler();
  } finally {
    global.confirm = originalConfirm;
  }

  assert.deepEqual(documentRef.defaultView.localStorage.removed, [
    "oms-study-hub-quiz:tok:v1",
  ]);
});

test("captureFocusKey only restores focus when a tracked control already had it", () => {
  const documentRef = new FakeQuizDocument();
  const container = documentRef.createElement("main");
  const button = documentRef.createElement("button");
  button.dataset.focusKey = "submit";
  container.append(button);

  assert.equal(quiz.captureFocusKey(documentRef, container), undefined);

  documentRef.activeElement = button;
  assert.equal(quiz.captureFocusKey(documentRef, container), "submit");
});

test("restoreFocus prefers the matching control and falls back to the container", () => {
  const documentRef = new FakeQuizDocument();
  const container = documentRef.createElement("main");
  const back = documentRef.createElement("button");
  back.dataset.focusKey = "back";
  container.append(back);

  quiz.restoreFocus(container, "back");
  assert.equal(documentRef.activeElement, back);

  quiz.restoreFocus(container, "does-not-exist");
  assert.equal(documentRef.activeElement, container);
});

test("performance summary groups right and need-review counts", () => {
  const tagged = {
    ...content,
    topic: "Course topic",
    questions: [
      { ...content.questions[0], area: "Neuro", learning_objective: "Recognize", topic: "Stroke" },
      { ...content.questions[1], area: "Neuro", learning_objective: "Recognize", topic: "Seizure" },
    ],
  };
  let state = quiz.createQuizState(tagged);
  state = quiz.selectChoice(state, "q1", "c1");
  state = quiz.recordFeedback(state, "q1", {
    correct: true,
    correct_choice_id: "c1",
    rationale: "Correct.",
  });
  state = quiz.selectChoice(state, "q2", "c1");
  state = quiz.recordFeedback(state, "q2", {
    correct: false,
    correct_choice_id: "c2",
    rationale: "Review.",
  });

  const summary = quiz.performanceSummary(tagged, state);
  assert.equal(summary.correct, 1);
  assert.equal(summary.percentage, 50);
  assert.deepEqual(summary.areas[0], {
    label: "Neuro",
    total: 2,
    answered: 2,
    correct: 1,
    incorrect: 1,
    unanswered: 0,
    needReview: 1,
    flagged: 0,
  });
});
