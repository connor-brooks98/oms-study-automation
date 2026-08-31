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
    this.hidden = false;
    this.focused = false;
    this.options = [];
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
    if (node.tagName === "option") this.options.push(node);
  }

  insertBefore(node, before) {
    const index = this.options.indexOf(before);
    if (index === -1) this.options.push(node);
    else this.options.splice(index, 0, node);
  }

  focus() {
    this.focused = true;
  }
}

class FakeLectureDocument {
  constructor(cards, {
    passRows = [], passCount = null, addPass = null, feedback = null,
  } = {}) {
    this.cards = cards;
    this.passRows = passRows;
    this.passCount = passCount;
    this.addPass = addPass;
    this.feedback = feedback;
    this.cookie = "";
  }

  createElement(tagName) {
    const element = new FakeLectureElement();
    element.tagName = tagName;
    return element;
  }

  addEventListener() {}

  querySelectorAll(selector) {
    if (selector === "[data-generation-card]") return this.cards;
    if (selector === "[data-pass-row]") return this.passRows;
    if (selector === "[data-pass-complete]") {
      return this.passRows.map((row) => row.querySelector("[data-pass-complete]"));
    }
    if (selector === "[data-pass-date]") {
      return this.passRows.map((row) => row.querySelector("[data-pass-date]"));
    }
    if (selector === "[data-pass-resource]") {
      return this.passRows.map((row) => row.querySelector("[data-pass-resource]"));
    }
    return [];
  }

  querySelector(selector) {
    if (selector === "[data-pass-count]") return this.passCount;
    if (selector === "[data-add-pass]") return this.addPass;
    if (selector === "[data-pass-feedback]") return this.feedback;
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

const buildPassRow = ({ position, completed = false, completedOn = "", resource = "", options = [] }) => {
  const row = new FakeLectureElement();
  row.dataset.passPosition = String(position);
  const checkbox = new FakeLectureElement();
  checkbox.dataset.passPosition = String(position);
  checkbox.checked = completed;
  const date = new FakeLectureElement();
  date.value = completedOn;
  date.disabled = !completed;
  const select = new FakeLectureElement();
  select.dataset.passPosition = String(position);
  select.value = resource;
  select.options = options.map((value) => ({ tagName: "option", value, textContent: value }));
  const custom = new FakeLectureElement();
  custom.hidden = true;
  const customInput = new FakeLectureElement();
  const addResource = new FakeLectureElement();
  row._children["[data-pass-complete]"] = checkbox;
  row._children["[data-pass-date]"] = date;
  row._children["[data-pass-resource]"] = select;
  row._children["[data-pass-resource-custom]"] = custom;
  row._children["[data-pass-resource-name]"] = customInput;
  row._children["[data-add-pass-resource]"] = addResource;
  checkbox.closest = () => row;
  select.closest = () => row;
  return { row, checkbox, date, select, custom, customInput, addResource };
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

test("pass dates format local ISO values and leave unknown values readable", () => {
  assert.equal(lecture.formatCompletedOn(null), "Not completed");
  assert.equal(lecture.formatCompletedOn("not-a-date"), "not-a-date");
  assert.equal(lecture.formatCompletedOn("2026-08-30"), "Aug 30, 2026");
});

test("local date values preserve the browser calendar day", () => {
  assert.equal(lecture.localDateValue(new Date(2026, 7, 31, 23, 30)), "2026-08-31");
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

test("pass completion, date edits, and resource changes PATCH independently with CSRF", async () => {
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
        if (body.completed_on) {
          return { position: 1, completed_on: body.completed_on, resource: "" };
        }
        if (body.completed === false) {
          return { position: 1, completed_on: null, resource: "Anki" };
        }
        return { position: 1, completed_on: "2026-08-29", resource: body.resource };
      },
    };
  };
  const originalLocation = global.location;
  const OriginalDate = global.Date;
  global.location = { pathname: "/lectures/42" };
  global.Date = class extends OriginalDate {
    constructor(...args) {
      return args.length ? new OriginalDate(...args) : new OriginalDate(2026, 7, 31, 23, 30);
    }
  };

  try {
    lecture.initialize(documentRef, fetchImpl);
    passes[0].checkbox.checked = true;
    await passes[0].checkbox.dispatch("change");
    await flushMicrotasks();

    passes[0].date.value = "2026-08-29";
    await passes[0].date.dispatch("change");
    await flushMicrotasks();

    passes[0].select.value = "Anki";
    await passes[0].select.dispatch("change");
    await flushMicrotasks();

    passes[0].checkbox.checked = false;
    await passes[0].checkbox.dispatch("change");
    await flushMicrotasks();

    const mutations = requests.filter(({ options }) => options.method === "PATCH");
    assert.equal(mutations.length, 4);
    assert.deepEqual(
      mutations.map(({ url, options }) => ({
        url,
        body: JSON.parse(options.body),
        csrf: options.headers["X-CSRF-Token"],
      })),
      [
        {
          url: "/api/lectures/42/passes/1",
          body: { completed_on: "2026-08-31" },
          csrf: "csrf-token",
        },
        {
          url: "/api/lectures/42/passes/1",
          body: { completed_on: "2026-08-29" },
          csrf: "csrf-token",
        },
        {
          url: "/api/lectures/42/passes/1",
          body: { resource: "Anki" },
          csrf: "csrf-token",
        },
        {
          url: "/api/lectures/42/passes/1",
          body: { completed: false },
          csrf: "csrf-token",
        },
      ],
    );
    assert.equal(passes[0].date.value, "");
    assert.equal(passes[0].date.disabled, true);
    assert.equal(passCount.textContent, "0/5");
  } finally {
    global.Date = OriginalDate;
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("failed pass updates restore the prior completion, editable date, count, and resource", async () => {
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
    assert.equal(passes[0].date.value, "");
    assert.equal(passes[0].date.disabled, true);
    assert.equal(passCount.textContent, "0/5");

    passes[0].select.value = "Anki";
    await passes[0].select.dispatch("change");
    await flushMicrotasks();
    assert.equal(passes[0].select.value, "Lecture");

    const completed = buildPassRow({ position: 6, completed: true, completedOn: "2026-08-30" });
    const completedDocument = new FakeLectureDocument([], {
      passRows: [completed.row],
      passCount: new FakeLectureElement(),
      addPass: new FakeLectureElement(),
    });
    completedDocument.cookie = "study_hub_csrf=csrf-token";
    lecture.initialize(completedDocument, fetchImpl);
    completed.date.value = "2026-08-29";
    await completed.date.dispatch("change");
    await flushMicrotasks();
    assert.equal(completed.date.value, "2026-08-30");
    assert.equal(completed.date.disabled, false);
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("overlapping pass responses update only their owned fields", async () => {
  const pass = buildPassRow({ position: 1, resource: "Lecture" });
  const passCount = new FakeLectureElement();
  const documentRef = new FakeLectureDocument([], {
    passRows: [pass.row],
    passCount,
    addPass: new FakeLectureElement(),
  });
  const pending = [];
  const fetchImpl = async (_url, options = {}) => {
    if (!options.method) return { ok: true, async json() { return {}; } };
    return new Promise((resolve) => pending.push({ options, resolve }));
  };
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, fetchImpl);
    pass.checkbox.checked = true;
    const completion = pass.checkbox.dispatch("change");
    await flushMicrotasks();
    pass.select.value = "Pathoma";
    const resource = pass.select.dispatch("change");
    await flushMicrotasks();

    pending.find(({ options }) => JSON.parse(options.body).resource).resolve({
      ok: true,
      async json() { return { position: 1, completed_on: null, resource: "Pathoma" }; },
    });
    await resource;
    pending.find(({ options }) => JSON.parse(options.body).completed_on).resolve({
      ok: true,
      async json() { return { position: 1, completed_on: "2026-08-30", resource: "Lecture" }; },
    });
    await completion;

    assert.equal(pass.checkbox.checked, true);
    assert.equal(pass.date.value, "2026-08-30");
    assert.equal(passCount.textContent, "1/1");
    assert.equal(pass.select.value, "Pathoma");
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("a pending completion keeps its date input from starting a competing save", async () => {
  const pass = buildPassRow({ position: 1 });
  const pending = [];
  const documentRef = new FakeLectureDocument([], {
    passRows: [pass.row],
    passCount: new FakeLectureElement(),
    addPass: new FakeLectureElement(),
  });
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, async (_url, options = {}) => {
      if (!options.method) return { ok: true, async json() { return {}; } };
      return new Promise((resolve) => pending.push({ options, resolve }));
    });
    pass.checkbox.checked = true;
    const completion = pass.checkbox.dispatch("change");
    await flushMicrotasks();

    assert.equal(pass.date.disabled, true);
    pass.date.value = "2026-08-29";
    await pass.date.dispatch("change");
    assert.equal(pending.length, 1);

    pending[0].resolve({
      ok: true,
      async json() { return { position: 1, completed_on: "2026-08-31", resource: null }; },
    });
    await completion;
    assert.equal(pass.date.disabled, false);
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("a pending date save gates adding a pass until it settles", async () => {
  const passes = Array.from({ length: 5 }, (_, index) => buildPassRow({
    position: index + 1,
    completed: true,
    completedOn: "2026-08-30",
  }));
  const addPass = new FakeLectureElement();
  const pending = [];
  const documentRef = new FakeLectureDocument([], {
    passRows: passes.map(({ row }) => row),
    passCount: new FakeLectureElement(),
    addPass,
  });
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, async (_url, options = {}) => {
      if (!options.method) return { ok: true, async json() { return {}; } };
      return new Promise((resolve) => pending.push({ options, resolve }));
    });
    assert.equal(addPass.disabled, false);

    passes[0].date.value = "2026-08-29";
    const saved = passes[0].date.dispatch("change");
    await flushMicrotasks();
    assert.equal(passes[0].date.disabled, true);
    assert.equal(addPass.disabled, true);

    pending.shift().resolve({
      ok: true,
      async json() { return { position: 1, completed_on: "2026-08-29", resource: null }; },
    });
    await saved;
    assert.equal(addPass.disabled, false);

    passes[0].date.value = "2026-08-28";
    const failed = passes[0].date.dispatch("change");
    await flushMicrotasks();
    assert.equal(addPass.disabled, true);
    pending.shift().resolve({
      ok: false,
      async json() { return { detail: "Date rejected." }; },
    });
    await failed;
    assert.equal(passes[0].date.value, "2026-08-29");
    assert.equal(addPass.disabled, false);
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("selecting Other reveals the inline editor without saving a sentinel", async () => {
  const pass = buildPassRow({ position: 1, resource: "Lecture" });
  const requests = [];
  const documentRef = new FakeLectureDocument([], {
    passRows: [pass.row],
    passCount: new FakeLectureElement(),
    addPass: new FakeLectureElement(),
  });
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, async (_url, options = {}) => {
      requests.push(options);
      return { ok: true, async json() { return {}; } };
    });
    pass.select.value = "Other";
    await pass.select.dispatch("change");

    assert.equal(pass.custom.hidden, false);
    assert.equal(pass.customInput.focused, true);
    assert.equal(requests.filter(({ method }) => method === "PATCH").length, 0);
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("a legacy Other resource reveals the inline editor during initialization", () => {
  const pass = buildPassRow({ position: 1, resource: "Other" });
  const documentRef = new FakeLectureDocument([], {
    passRows: [pass.row],
    passCount: new FakeLectureElement(),
    addPass: new FakeLectureElement(),
  });
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, async () => ({ ok: true, async json() { return {}; } }));

    assert.equal(pass.custom.hidden, false);
    assert.equal(pass.customInput.focused, true);
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("adding a custom resource trims, saves, and shares a text-safe option", async () => {
  const options = ["Lecture", "Anki", "Other"];
  const passes = [
    buildPassRow({ position: 1, resource: "Lecture", options }),
    buildPassRow({ position: 2, options }),
  ];
  const requests = [];
  const documentRef = new FakeLectureDocument([], {
    passRows: passes.map(({ row }) => row),
    passCount: new FakeLectureElement(),
    addPass: new FakeLectureElement(),
  });
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, async (_url, options = {}) => {
      requests.push(options);
      if (!options.method) return { ok: true, async json() { return {}; } };
      return {
        ok: true,
        async json() { return { position: 1, completed_on: null, resource: "Pathoma" }; },
      };
    });
    passes[0].select.value = "Other";
    await passes[0].select.dispatch("change");
    passes[0].customInput.value = "  Pathoma  ";
    await passes[0].addResource.dispatch("click");

    const patches = requests.filter(({ method }) => method === "PATCH");
    assert.deepEqual(patches.map(({ body }) => JSON.parse(body)), [{ resource: "Pathoma" }]);
    for (const { select } of passes) {
      assert.equal(select.options.filter(({ value }) => value === "Pathoma").length, 1);
      assert.equal(select.options.find(({ value }) => value === "Pathoma").textContent, "Pathoma");
      assert.deepEqual(select.options.map(({ value }) => value), ["Lecture", "Anki", "Pathoma", "Other"]);
    }
    assert.equal(passes[0].select.value, "Pathoma");
    assert.equal(passes[0].custom.hidden, true);
    assert.equal(passes[0].customInput.value, "");
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});

test("custom resource validation and save failures keep the editor available", async () => {
  const pass = buildPassRow({ position: 1, resource: "Lecture" });
  const feedback = new FakeLectureElement();
  const requests = [];
  const documentRef = new FakeLectureDocument([], {
    passRows: [pass.row],
    passCount: new FakeLectureElement(),
    addPass: new FakeLectureElement(),
    feedback,
  });
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42" };

  try {
    lecture.initialize(documentRef, async (_url, options = {}) => {
      requests.push(options);
      if (!options.method) return { ok: true, async json() { return {}; } };
      return { ok: false, async json() { return { detail: "Resource rejected." }; } };
    });
    pass.select.value = "Other";
    await pass.select.dispatch("change");
    pass.customInput.value = "   ";
    await pass.addResource.dispatch("click");
    assert.equal(requests.filter(({ method }) => method === "PATCH").length, 0);
    assert.match(feedback.textContent, /resource name/i);

    pass.customInput.value = "Pathoma";
    await pass.addResource.dispatch("click");
    assert.equal(pass.select.value, "Lecture");
    assert.equal(pass.custom.hidden, false);
    assert.equal(pass.customInput.value, "Pathoma");
    assert.equal(feedback.textContent, "Pass 1 resource update failed: Resource rejected.");
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

test("failed add pass restores the control and announces the server detail", async () => {
  const passes = Array.from({ length: 5 }, (_, index) => buildPassRow({
    position: index + 1,
    completed: true,
  }));
  const addPass = new FakeLectureElement();
  const feedback = new FakeLectureElement();
  const documentRef = new FakeLectureDocument([], {
    passRows: passes.map(({ row }) => row),
    passCount: new FakeLectureElement(),
    addPass,
    feedback,
  });
  documentRef.cookie = "study_hub_csrf=csrf-token";
  const fetchImpl = async (_url, options = {}) => (
    options.method === "POST"
      ? { ok: false, async json() { return { detail: "Finish current passes." }; } }
      : { ok: true, async json() { return {}; } }
  );
  const originalLocation = global.location;
  global.location = { pathname: "/lectures/42", reload() {} };

  try {
    lecture.initialize(documentRef, fetchImpl);
    assert.equal(addPass.disabled, false);
    await addPass.dispatch("click");
    await flushMicrotasks();
    assert.equal(addPass.disabled, false);
    assert.equal(feedback.textContent, "Add pass failed: Finish current passes.");
  } finally {
    if (originalLocation === undefined) delete global.location;
    else global.location = originalLocation;
  }
});
