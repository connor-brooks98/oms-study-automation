const test = require("node:test");
const assert = require("node:assert/strict");

const shell = require("../../src/oms_hub/web/static/study_hub_shell.js");

test("command queries are normalized and match both labels and supporting text", () => {
  assert.equal(shell.normalizeQuery("  Quiz BUILDER "), "quiz builder");
  assert.equal(shell.matchesCommand("Upload slides Convert PowerPoint files", "powerpoint"), true);
  assert.equal(shell.matchesCommand("Review Resolve stopped workflows", "anki"), false);
});

test("command selection wraps in both directions", () => {
  assert.equal(shell.nextIndex(-1, 1, 4), 0);
  assert.equal(shell.nextIndex(3, 1, 4), 0);
  assert.equal(shell.nextIndex(0, -1, 4), 3);
  assert.equal(shell.nextIndex(0, 1, 0), -1);
});

test("transition timing honors CSS units and reduced-motion preferences", () => {
  const windowRef = {
    document: { documentElement: {} },
    matchMedia: () => ({ matches: false }),
    getComputedStyle: () => ({ getPropertyValue: () => "0.15s" }),
  };
  assert.equal(shell.transitionDelay(windowRef, "--modal-duration"), 150);
  windowRef.matchMedia = () => ({ matches: true });
  assert.equal(shell.transitionDelay(windowRef, "--modal-duration"), 0);
});

test("button semantics reserve stateful motion for submissions and map action icons", () => {
  const submission = {
    matches: (selector) => selector === "button.sh-btn",
    getAttribute: () => "submit",
    textContent: "Save changes",
  };
  const download = { ...submission, getAttribute: () => "button", textContent: "Download lecture" };
  const tab = {
    ...submission,
    matches: (selector) => selector === "button.sh-btn" || selector === '[role="tab"], .sh-seg__btn',
    getAttribute: () => "button",
    textContent: "Generate Quiz",
  };
  assert.equal(shell.isStatefulAction(submission), true);
  assert.equal(shell.isStatefulAction(download), false);
  assert.equal(shell.isStatefulAction(tab), false);
  assert.equal(shell.buttonIcon(download.textContent), "download");
  assert.equal(shell.buttonIcon("Remove source"), "trash");
  assert.equal(shell.buttonIcon("Continue"), "continue");
  assert.equal(shell.buttonIcon("Test connection"), "");
});
