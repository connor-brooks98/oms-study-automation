const test = require("node:test");
const assert = require("node:assert/strict");

const dashboard = require("../../src/oms_hub/web/static/dashboard.js");

class FakeClassList {
  constructor(...names) { this.names = new Set(names); }
  contains(name) { return this.names.has(name); }
  toggle() {}
}

class FakeButton {
  constructor(kind, key, controls, expanded = false) {
    this.dataset = { storageKey: key };
    this.attributes = { "aria-controls": controls, "aria-expanded": String(expanded) };
    this.classList = new FakeClassList(kind);
    this.listeners = {};
  }
  getAttribute(name) { return this.attributes[name]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector() { return { classList: { toggle() {} } }; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
}

const dashboardFixture = ({ firstCourseOpen = true, firstExamOpen = true } = {}) => {
  const firstCourse = new FakeButton("course-toggle", "course:MSK", "course-1", firstCourseOpen);
  const secondCourse = new FakeButton("course-toggle", "course:Neuro", "course-2", false);
  const firstExam = new FakeButton("exam-toggle", "exam:MSK:1", "exam-1", firstExamOpen);
  const secondExam = new FakeButton("exam-toggle", "exam:Neuro:1", "exam-2", false);
  const panels = new Map([
    ["course-1", { hidden: !firstCourseOpen, querySelectorAll() { return [firstExam]; } }],
    ["course-2", { hidden: true, querySelectorAll() { return [secondExam]; } }],
    ["exam-1", { hidden: !firstExamOpen }],
    ["exam-2", { hidden: true }],
  ]);
  firstExam.closest = () => ({ querySelector() { return firstCourse; } });
  secondExam.closest = () => ({ querySelector() { return secondCourse; } });
  const documentRef = {
    querySelectorAll() { return [firstCourse, firstExam, secondCourse, secondExam]; },
    getElementById(id) { return panels.get(id); },
  };
  return { documentRef, firstCourse, secondCourse, firstExam, secondExam, panels };
};

test("a stored open exam stays semantically collapsed below a closed course", () => {
  assert.equal(dashboard.nestedExpanded(false, "true"), false);
  assert.equal(dashboard.nestedExpanded(true, "true"), true);
  assert.equal(dashboard.nestedExpanded(true, "false"), false);
});

test("closing a course collapses every descendant exam and records false aria state", () => {
  const changes = [];
  const exam = {
    dataset: { storageKey: "exam:MSK:1" },
    getAttribute() { return "exam-panel"; },
    setAttribute(name, value) { changes.push([name, value]); },
    querySelector() { return { classList: { toggle() {} } }; },
  };
  const panel = { querySelectorAll() { return [exam]; } };
  const documentRef = { getElementById(id) { return id === "course-panel" ? panel : { hidden: false }; } };
  const course = { getAttribute() { return "course-panel"; } };
  const writes = [];

  dashboard.collapseDescendants(documentRef, { setItem(...args) { writes.push(args); } }, course);

  assert.deepEqual(changes, [["aria-expanded", "false"]]);
  assert.deepEqual(writes, [["study-hub:disclosure:exam:MSK:1", "false"]]);
});

test("initialize restores a stored-open non-first course before its exam", () => {
  const fixture = dashboardFixture();
  const storage = {
    getItem(key) {
      return ({ "study-hub:disclosure:course:Neuro": "true", "study-hub:disclosure:exam:Neuro:1": "true" })[key] ?? null;
    },
  };

  dashboard.initialize(fixture.documentRef, storage);

  assert.equal(fixture.secondCourse.getAttribute("aria-expanded"), "true");
  assert.equal(fixture.secondExam.getAttribute("aria-expanded"), "true");
  assert.equal(fixture.panels.get("exam-2").hidden, false);
});

test("initialize closes a default-open exam under a stored-closed first course", () => {
  const fixture = dashboardFixture({ firstCourseOpen: true, firstExamOpen: true });
  dashboard.initialize(fixture.documentRef, {
    getItem(key) { return key === "study-hub:disclosure:course:MSK" ? "false" : null; },
  });

  assert.equal(fixture.firstCourse.getAttribute("aria-expanded"), "false");
  assert.equal(fixture.firstExam.getAttribute("aria-expanded"), "false");
  assert.equal(fixture.panels.get("exam-1").hidden, true);
});

test("a denied sessionStorage getter leaves dashboard server defaults usable", () => {
  const fixture = dashboardFixture({ firstCourseOpen: true, firstExamOpen: true });
  const descriptor = Object.getOwnPropertyDescriptor(global, "sessionStorage");
  Object.defineProperty(global, "sessionStorage", { configurable: true, get() { throw new Error("denied"); } });
  try {
    dashboard.initialize(fixture.documentRef);
  } finally {
    if (descriptor) Object.defineProperty(global, "sessionStorage", descriptor);
    else delete global.sessionStorage;
  }

  assert.equal(fixture.firstCourse.getAttribute("aria-expanded"), "true");
  assert.equal(fixture.firstExam.getAttribute("aria-expanded"), "true");
});

test("the exam overview link stays navigation while its adjacent disclosure button expands the tree", () => {
  const fixture = dashboardFixture({ firstCourseOpen: true, firstExamOpen: true });
  const overviewLink = {
    listeners: {},
    addEventListener(type, callback) { this.listeners[type] = callback; },
  };
  fixture.documentRef.querySelectorAll = (selector) => {
    if (selector === "[data-disclosure]") return [fixture.firstCourse, fixture.firstExam];
    if (selector === ".exam-overview-link") return [overviewLink];
    return [];
  };

  dashboard.initialize(fixture.documentRef, { getItem() { return null; } });

  assert.equal(overviewLink.listeners.click, undefined);
  fixture.firstExam.listeners.click();
  assert.equal(fixture.firstExam.getAttribute("aria-expanded"), "false");
  assert.equal(fixture.panels.get("exam-1").hidden, true);
});
