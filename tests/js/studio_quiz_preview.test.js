const test = require("node:test");
const assert = require("node:assert/strict");

const preview = require("../../src/oms_hub/web/static/studio_quiz_preview.js");

test("publishing previews exposes loading and success before navigation", async () => {
  const listeners = {};
  const button = {
    dataset: { state: "idle" }, disabled: false,
    addEventListener(type, handler) { listeners[type] = handler; },
    setAttribute() {}, removeAttribute() {},
  };
  const message = { textContent: "" };
  const panel = {
    dataset: { publishUrl: "/studio/runs/run-1/publication" },
    querySelector(selector) { return selector === "[data-publish-quiz]" ? button : message; },
  };
  const documentRef = { cookie: "study_hub_csrf=token", querySelector: () => panel };
  const priorLocation = global.location;
  const priorTimeout = global.setTimeout;
  let assigned = null;
  global.location = { assign(url) { assigned = url; } };
  global.setTimeout = (callback) => { callback(); return 1; };
  try {
    preview.initialize(documentRef, async () => ({
      ok: true,
      async json() { return { published_url: "/public/quizzes/" + "a".repeat(64) }; },
    }));
    await listeners.click();
  } finally {
    global.location = priorLocation;
    global.setTimeout = priorTimeout;
  }
  assert.equal(button.dataset.state, "success");
  assert.match(message.textContent, /published/i);
  assert.equal(assigned, "/public/quizzes/" + "a".repeat(64));
});
