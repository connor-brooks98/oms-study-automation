const test = require("node:test");
const assert = require("node:assert/strict");

const dashboard = require("../../src/oms_hub/web/static/dashboard.js");

test("a stored open exam stays semantically collapsed below a closed course", () => {
  assert.equal(dashboard.nestedExpanded(false, "true"), false);
  assert.equal(dashboard.nestedExpanded(true, "true"), true);
  assert.equal(dashboard.nestedExpanded(true, "false"), false);
});
