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
