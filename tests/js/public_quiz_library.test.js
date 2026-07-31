const test = require("node:test");
const assert = require("node:assert/strict");

const library = require("../../src/oms_hub/web/static/public_quiz_library.js");

test("progress key matches the quiz player's versioned key", () => {
  assert.equal(
    library.progressKey("token", 4),
    "oms-study-hub-quiz:token:v4",
  );
});

test("progress is classified from the quiz player's saved state", () => {
  const untouched = {
    version: 2,
    currentIndex: 0,
    questions: {
      q1: { submitted: false },
      q2: { submitted: false },
    },
  };
  const started = {
    ...untouched,
    questions: {
      q1: {
        submitted: false,
        selectedChoiceId: "c1",
        eliminatedChoiceIds: [],
        highlights: [],
      },
      q2: { submitted: false },
    },
  };
  const complete = {
    ...started,
    currentIndex: 2,
    questions: {
      q1: { submitted: true },
      q2: { submitted: true },
    },
  };

  assert.equal(library.progressLabel(untouched, 2), "Not started");
  assert.equal(library.progressLabel(started, 2), "In progress");
  assert.equal(library.progressLabel(complete, 2), "Completed");
  assert.equal(library.progressLabel(complete, 3), "Not started");
});

test("corrupt browser progress is treated as not started", () => {
  const storage = {
    getItem: () => "{not-json",
  };

  assert.equal(library.readProgress(storage, "token", 1), "Not started");
});

test("blocked localStorage access is treated as unavailable", () => {
  const view = {};
  Object.defineProperty(view, "localStorage", {
    get() { throw new Error("blocked"); },
  });

  assert.equal(library.acquireStorage(view), null);
  assert.equal(library.readProgress(null, "token", 1), "Not started");
});

test("resetting progress requires confirmation", () => {
  const removed = [];
  const storage = {
    length: 2,
    key(index) {
      return ["oms-study-hub-quiz:first:v1", "unrelated"][index];
    },
    removeItem(key) { removed.push(key); },
  };

  assert.equal(library.resetProgress(storage, () => false), "cancelled");
  assert.deepEqual(removed, []);
  assert.equal(library.resetProgress(storage, () => true), "reset");
  assert.deepEqual(removed, ["oms-study-hub-quiz:first:v1"]);
});
