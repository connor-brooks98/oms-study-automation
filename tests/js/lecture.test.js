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

  async dispatch(type) {
    await Promise.all(
      (this._listeners[type] || []).map((handler) => handler({
        target: this,
        preventDefault() {},
      })),
    );
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
  constructor(cards, { passRows = [], passCount = null, addPass = null } = {}) {
    this.cards = cards;
    this.passRows = passRows;
    this.passCount = passCount;
    this.addPass = addPass;
    this.cookie = "";
  }

  addEventListener() {}

  querySelectorAll(selector) {
    if (selector === "[data-generation-card]") return this.cards;
    if (selector === "[data-pass-row]") return this.passRows;
    if (selector === "[data-pass-complete]") {
      return this.passRows.map((row) => row.querySelector("[data-pass-complete]"));
    }
    if (selector === "[data-pass-resource]") {
      return this.passRows.map((row) => row.querySelector("[data-pass-resource]"));
    }
    return [];
  }

  querySelector(selector) {
    if (selector === "[data-pass-count]") return this.passCount;
    if (selector === "[data-add-pass]") return this.addPass;
    return null;
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

const buildPassRow = ({ position, completed = false, resource = "" }) => {
  const row = new FakeLectureElement();
  row.dataset.passPosition = String(position);
  const checkbox = new FakeLectureElement();
  checkbox.dataset.passPosition = String(position);
  checkbox.checked = completed;
  const date = new FakeLectureElement();
  date.textContent = completed ? "Aug 29, 2026" : "Not completed";
  const select = new FakeLectureElement();
  select.dataset.passPosition = String(position);
  select.value = resource;
  row._children["[data-pass-complete]"] = checkbox;
  row._children["[data-pass-date]"] = date;
  row._children["[data-pass-resource]"] = select;
  checkbox.closest = () => row;
  select.closest = () => row;
  return { row, checkbox, date, select };
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

test("pass completion and resource changes PATCH independently with CSRF and refresh the ledger", async () => {
  const passes = Array.from({ length: 5 }, (_, index) => buildPassRow({ position: index + 1 }));
  const passCount = new FakeLectureElement();
  passCount.textContent = "0/5";
  const addPass = new FakeLectureElement();
  addPass.disabled = true;
  const documentRef = new FakeLectureDocument([], {
    passRows: passes.map(({ row }) => row),
    passCount,
    addPass,
  });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const requests = [];
  const fetchImpl = async (url, options = {}) => {
    requests.push({ url, options });
    if (!options.method) return { ok: true, async json() { return {}; } };
    const body = JSON.parse(options.body);
    return {
      ok: true,
      async json() {
        return body.completed === true
          ? { position: 1, completed_on: "2026-08-30", resource: "" }
          : { position: 1, completed_on: "2026-08-30", resource: body.resource };
      },
    };
  };
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, fetchImpl);
    passes[0].checkbox.checked = true;
    await passes[0].checkbox.dispatch("change");
    await flushMicrotasks();

    passes[0].select.value = "Anki";
    await passes[0].select.dispatch("change");
    await flushMicrotasks();

    const mutations = requests.filter(({ options }) => options.method === "PATCH");
    assert.equal(mutations.length, 2);
    assert.deepEqual(
      mutations.map(({ url, options }) => ({
        url,
        body: JSON.parse(options.body),
        csrf: options.headers["X-CSRF-Token"],
      })),
      [
        {
          url: "/api/lectures/42/passes/1",
          body: { completed: true },
          csrf: "csrf-token",
        },
        {
          url: "/api/lectures/42/passes/1",
          body: { resource: "Anki" },
          csrf: "csrf-token",
        },
      ],
    );
    assert.equal(passes[0].date.textContent, "Aug 30, 2026");
    assert.equal(passCount.textContent, "1/5");
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("failed pass updates restore the prior completion, date, count, and resource", async () => {
  const passes = Array.from({ length: 5 }, (_, index) => buildPassRow({
    position: index + 1,
    resource: index === 0 ? "Lecture" : "",
  }));
  const passCount = new FakeLectureElement();
  passCount.textContent = "0/5";
  const documentRef = new FakeLectureDocument([], {
    passRows: passes.map(({ row }) => row),
    passCount,
    addPass: new FakeLectureElement(),
  });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const fetchImpl = async (_url, options = {}) => {
    if (!options.method) return { ok: true, async json() { return {}; } };
    return { ok: false, async json() { return { detail: "Pass update failed." }; } };
  };
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, fetchImpl);

    passes[0].checkbox.checked = true;
    await passes[0].checkbox.dispatch("change");
    await flushMicrotasks();
    assert.equal(passes[0].checkbox.checked, false);
    assert.equal(passes[0].date.textContent, "Not completed");
    assert.equal(passCount.textContent, "0/5");

    passes[0].select.value = "Anki";
    await passes[0].select.dispatch("change");
    await flushMicrotasks();
    assert.equal(passes[0].select.value, "Lecture");
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("add pass stays inert until every current pass is complete, then POSTs with CSRF", async () => {
  const passes = Array.from({ length: 5 }, (_, index) => buildPassRow({
    position: index + 1,
    completed: index < 4,
  }));
  const passCount = new FakeLectureElement();
  passCount.textContent = "4/5";
  const addPass = new FakeLectureElement();
  addPass.disabled = true;
  const documentRef = new FakeLectureDocument([], {
    passRows: passes.map(({ row }) => row),
    passCount,
    addPass,
  });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const requests = [];
  const fetchImpl = async (url, options = {}) => {
    requests.push({ url, options });
    if (!options.method) return { ok: true, async json() { return {}; } };
    if (options.method === "POST") {
      return {
        ok: true,
        async json() { return { position: 6, completed_on: null, resource: null }; },
      };
    }
    return {
      ok: true,
      async json() { return { position: 5, completed_on: "2026-08-30", resource: null }; },
    };
  };
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42", reload() {} };

  try {
    lecture.initialize(documentRef, fetchImpl);
    await addPass.dispatch("click");
    await flushMicrotasks();
    assert.equal(requests.filter(({ options }) => options.method === "POST").length, 0);

    passes[4].checkbox.checked = true;
    await passes[4].checkbox.dispatch("change");
    await flushMicrotasks();
    assert.equal(passCount.textContent, "5/5");
    assert.equal(addPass.disabled, false);

    await addPass.dispatch("click");
    await flushMicrotasks();
    const posts = requests.filter(({ options }) => options.method === "POST");
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, "/api/lectures/42/passes");
    assert.equal(posts[0].options.headers["X-CSRF-Token"], "csrf-token");
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});
