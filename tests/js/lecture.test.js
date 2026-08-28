const test = require("node:test");
const assert = require("node:assert/strict");

const lecture = require("../../src/oms_hub/web/static/lecture.js");

// -- Minimal fake DOM sufficient to drive initialize()'s polling loop --

class FakeLectureElement {
  constructor() {
    this.dataset = {};
    this.className = "";
    this.textContent = "";
    this.disabled = false;
    this.attributes = {};
    this._listeners = {};
    this._children = {};
    this.classList = {
      add: (name) => {
        if (!this.className.split(" ").includes(name)) {
          this.className = `${this.className} ${name}`.trim();
        }
      },
      remove: (name) => {
        this.className = this.className
          .split(" ")
          .filter((item) => item && item !== name)
          .join(" ");
      },
    };
  }

  addEventListener(type, handler) {
    (this._listeners[type] ||= []).push(handler);
  }

  querySelector(selector) {
    return this._children[selector] || null;
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  append(node) {
    this.appended = node;
  }
}

class FakeLectureDocument {
  constructor(cards) {
    this.cards = cards;
    this.cookie = "";
  }

  addEventListener() {}

  querySelectorAll(selector) {
    if (selector === "[data-generation-card]") return this.cards;
    return [];
  }
}

const buildCard = (kind) => {
  const card = new FakeLectureElement();
  card.dataset.kind = kind;
  const message = new FakeLectureElement();
  const generateButton = new FakeLectureElement();
  card._children["[data-generation-message]"] = message;
  card._children["[data-generate]"] = generateButton;
  return { card, message, generateButton };
};

test("completed generation refresh names the ready artifact", () => {
  const quiz = buildCard("quiz");
  const outline = buildCard("outline");

  lecture.render(quiz.card, { state: "complete" });
  lecture.render(outline.card, { state: "complete" });

  assert.equal(quiz.message.textContent, "1 Study Hub quiz is ready.");
  assert.equal(outline.message.textContent, "Lecture outline PDF is ready.");
});

test("runtime generation links use the shared primary button classes", () => {
  const { card } = buildCard("quiz");
  const actions = {
    prepend(node) { this.node = node; },
    append(node) { this.appended = node; },
    classList: { remove() {} },
  };
  card._children[".file-actions"] = actions;
  card.ownerDocument = {
    createElement() {
      return { dataset: {} };
    },
  };

  lecture.render(card, { state: "complete", url: "/public/quizzes/a1" });

  assert.equal(actions.node.className, "button primary sh-btn sh-btn--primary");
  assert.equal(actions.node.dataset.generationLink, "");
  assert.equal(actions.node.textContent, "Take Lecture Quiz");
  assert.equal(card.appended.className, "lecture-regenerate sh-iconbtn");
  assert.equal(card.appended.attributes["aria-label"], "Regenerate lecture quiz");
});

test("completed outline adds white open, blue download, and regenerate controls", () => {
  const { card } = buildCard("outline");
  const actions = {
    prepend(node) { this.node = node; },
    append(node) { this.appended = node; },
    classList: {
      remove(name) { this.removed = name; },
    },
  };
  card._children[".file-actions"] = actions;
  card.ownerDocument = {
    createElement() {
      return { dataset: {} };
    },
  };

  lecture.render(card, { state: "complete", url: "/artifacts/outlines/7" });

  assert.equal(actions.node.className, "button secondary sh-btn sh-btn--secondary");
  assert.equal(actions.node.textContent, "Open Lecture Outline");
  assert.equal(actions.appended.className, "button primary sh-btn sh-btn--primary");
  assert.equal(actions.appended.href, "/artifacts/outlines/7/download");
  assert.equal(actions.classList.removed, "lecture-card-actions--single");
  assert.equal(card.appended.attributes["aria-label"], "Regenerate lecture outline");
});

// The real setTimeout, saved before any monkeypatching below, used only to
// yield to pending microtasks between async steps.
const realSetTimeout = global.setTimeout;
const flushMicrotasks = async (times = 6) => {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => realSetTimeout(resolve, 0));
  }
};

test("generation-status polling surfaces failures, doubles the retry delay up to a 30s cap, and resets after success", async () => {
  const { card, message } = buildCard("quiz");
  const documentRef = new FakeLectureDocument([card]);

  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  const scheduled = [];
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  global.setTimeout = (fn, delay) => {
    const entry = { fn, delay };
    scheduled.push(entry);
    return entry;
  };
  global.clearTimeout = (entry) => {
    const index = scheduled.indexOf(entry);
    if (index !== -1) scheduled.splice(index, 1);
  };

  let callCount = 0;
  const runningStatus = () => ({
    ok: true,
    async json() {
      return { quiz: { state: "running", stage: "generating_cards" } };
    },
  });
  const fetchImpl = async () => {
    callCount += 1;
    if (callCount === 1) return runningStatus();
    // Calls 2 through 6 fail; call 7 recovers.
    if (callCount >= 2 && callCount <= 6) {
      throw new Error("Network hiccup");
    }
    return runningStatus();
  };

  try {
    lecture.initialize(documentRef, fetchImpl);
    await flushMicrotasks();

    // Initial poll succeeds and the card is "active" (running), so the
    // next refresh is scheduled at the base 2500ms delay.
    assert.equal(scheduled.length, 1, "expected the healthy poll to be scheduled");
    assert.equal(scheduled[0].delay, 2500);

    // Failure #1: delay doubles to 5000, error surfaced on the card.
    scheduled.shift().fn();
    await flushMicrotasks();
    assert.match(message.textContent, /Network hiccup/);
    assert.match(message.textContent, /Retrying/);
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 5000);

    // Failure #2: delay doubles to 10000.
    scheduled.shift().fn();
    await flushMicrotasks();
    assert.equal(scheduled[0].delay, 10000);

    // Failure #3: delay doubles to 20000.
    scheduled.shift().fn();
    await flushMicrotasks();
    assert.equal(scheduled[0].delay, 20000);

    // Failure #4: 20000 * 2 = 40000, capped at 30000.
    scheduled.shift().fn();
    await flushMicrotasks();
    assert.equal(scheduled[0].delay, 30000, "delay should cap at 30s");

    // Failure #5: still capped at 30000 (not 60000).
    scheduled.shift().fn();
    await flushMicrotasks();
    assert.equal(scheduled[0].delay, 30000, "delay should stay capped at 30s");

    // Recovery: the next poll succeeds, so the delay resets to the base.
    scheduled.shift().fn();
    await flushMicrotasks();
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 2500, "delay should reset to the base after a success");
  } finally {
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});
