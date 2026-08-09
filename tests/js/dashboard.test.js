const test = require("node:test");
const assert = require("node:assert/strict");

const dashboard = require("../../src/oms_hub/web/static/dashboard.js");

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
