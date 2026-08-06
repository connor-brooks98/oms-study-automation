const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const review = require("../../src/oms_hub/web/static/studio_quiz_review.js");

test("publish gating follows authoritative blocker state", () => {
  assert.equal(review.canPublish([]), true);
  assert.equal(review.canPublish(["q1: answer is missing"]), false);
  assert.equal(review.blockersText([]), "Ready for preview and publication.");
  assert.match(review.blockersText(["first", "second"]), /first\nsecond/);
});

test("edit and candidate payload helpers normalize only supported values", () => {
  assert.deepEqual(
    review.normalizedEditPayload({
      stem: "  Stem  ",
      choices: [" A ", "", "B"],
      correct_index: "1",
      rationale: " because ",
      topic: " ",
      area: "Neuro",
      learning_objective: "Recall",
    }),
    {
      stem: "Stem",
      choices: ["A", "B"],
      correct_index: 1,
      rationale: "because",
      topic: null,
      area: "Neuro",
      learning_objective: "Recall",
    },
  );
  assert.deepEqual(review.candidateSelectionPayload("candidate-1"), {
    image_candidate_id: "candidate-1",
  });
  assert.equal(
    review.candidateSelectionUrl("run / 1", "question/1"),
    "/studio/runs/run%20%2F%201/questions/question%2F1/image-selection",
  );
});

test("review UI uses DOM text nodes rather than untrusted HTML injection", () => {
  const source = fs.readFileSync(
    "src/oms_hub/web/static/studio_quiz_review.js",
    "utf8",
  );
  assert.equal(source.includes("innerHTML"), false);
  assert.match(source, /textContent/);
  assert.match(source, /X-CSRF-Token/);
  assert.match(source, /"PATCH"/);
});
