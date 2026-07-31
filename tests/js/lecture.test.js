const test = require("node:test");
const assert = require("node:assert/strict");

const lecture = require("../../src/oms_hub/web/static/lecture.js");

test("bracket shortcuts navigate only outside form controls", () => {
  assert.equal(lecture.shortcutTarget("[", "/previous", "/next"), "/previous");
  assert.equal(lecture.shortcutTarget("]", "/previous", "/next"), "/next");
  assert.equal(lecture.shortcutTarget("]", "/previous", "/next", "input"), null);
  assert.equal(lecture.shortcutTarget("x", "/previous", "/next"), null);
});
