const test = require("node:test");
const assert = require("node:assert/strict");

const studio = require("../../src/oms_hub/web/static/notebook_studio.js");

test("course keys normalize whitespace and case", () => {
  assert.equal(studio.normalizeSubject("  Neuro   Science "), "neuro science");
});

test("source polling remains active only while work is pending", () => {
  assert.equal(studio.hasActiveSources([{ state: "pending" }]), true);
  assert.equal(studio.hasActiveSources([{ state: "attaching" }]), true);
  assert.equal(studio.hasActiveSources([{ state: "attached" }, { state: "failed" }]), false);
});

test("source renderer writes untrusted content through textContent", () => {
  const created = [];
  const documentRef = {
    createElement: () => {
      const node = { textContent: "" };
      created.push(node);
      return node;
    },
  };
  const list = {
    replaceChildren: () => {},
    append: () => {},
  };

  studio.renderSources(documentRef, list, [{
    title: "<img src=x onerror=alert(1)>",
    type: "url",
    state: "failed",
    error: "<script>bad()</script>",
  }]);

  assert.match(created[0].textContent, /<img/);
  assert.match(created[0].textContent, /<script>/);
  assert.equal("innerHTML" in created[0], false);
});
