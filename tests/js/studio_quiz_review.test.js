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

test("issue helpers retain roles, groups, and stable question anchors", () => {
  const issues = [
    { question_id: "question/1", type: "answer", message: "missing", role: "err" },
    { question_id: "question-2", type: "draft_diagnostic", message: "uncertain", role: "warn" },
    { question_id: "question-3", type: "answer", message: "outside range", role: "err" },
  ];
  assert.equal(review.questionAnchor("question/1"), "question-10-7175657374696f6e2f31");
  assert.equal(review.issueSummary(issues), "3 issues · 2 blocking issues · 1 warning");
  assert.deepEqual(review.groupIssues(issues).map((group) => [group.type, group.issues.length]), [
    ["answer", 2],
    ["draft_diagnostic", 1],
  ]);
});

test("question anchors are stable, fragment-safe, and injective for allowed identifiers", () => {
  const identifiers = ["question/1", "question%2F1", "question 1", "質問 1"];
  const anchors = identifiers.map((identifier) => review.questionAnchor(identifier));
  assert.equal(new Set(anchors).size, identifiers.length);
  assert.ok(anchors.every((anchor) => /^question-[0-9]+-[0-9a-f]+$/.test(anchor)));
  assert.equal(review.questionAnchor("質問 1"), review.questionAnchor("質問 1"));
});

test("no-candidate empty state is limited to unresolved image review", () => {
  assert.equal(review.hasImageReviewIssues([]), false);
  assert.equal(review.hasImageReviewIssues([{ type: "answer", code: "missing_answer" }]), false);
  assert.equal(review.hasImageReviewIssues([{ type: "image", code: "required_image_unresolved" }]), true);
  assert.equal(review.hasImageReviewIssues([{ type: "draft_diagnostic", code: "required_image_unresolved" }]), true);
  assert.equal(review.shouldRenderNoCandidateEmpty({
    issues: [{ type: "answer", code: "missing_answer" }],
    questions: [{ candidates: [] }],
  }), false);
  assert.equal(review.shouldRenderNoCandidateEmpty({
    issues: [{ type: "image", code: "required_image_unresolved" }],
    questions: [{ candidates: [] }],
  }), true);
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
