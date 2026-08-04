const test = require("node:test");
const assert = require("node:assert/strict");

const quiz = require("../../src/oms_hub/web/static/public_quiz.js");

const content = {
  token: "a".repeat(64),
  version: 3,
  questions: [
    {
      id: "q1",
      stem: "Which mechanism is most likely?",
      choices: [
        { id: "c1", text: "First" },
        { id: "c2", text: "Second" },
        { id: "c3", text: "Third" },
      ],
    },
    {
      id: "q2",
      stem: "Which finding is expected?",
      choices: [
        { id: "c1", text: "Alpha" },
        { id: "c2", text: "Beta" },
      ],
    },
  ],
};

test("answer selection stays editable until submission", () => {
  let state = quiz.createQuizState(content);

  state = quiz.selectChoice(state, "q1", "c1");
  state = quiz.selectChoice(state, "q1", "c2");

  assert.equal(state.questions.q1.selectedChoiceId, "c2");
  assert.equal(state.questions.q1.submitted, false);
});

test("eliminating an answer clears its selection and can be restored", () => {
  let state = quiz.selectChoice(quiz.createQuizState(content), "q1", "c2");

  state = quiz.toggleEliminated(state, "q1", "c2");
  assert.equal(state.questions.q1.selectedChoiceId, null);
  assert.deepEqual(state.questions.q1.eliminatedChoiceIds, ["c2"]);

  state = quiz.toggleEliminated(state, "q1", "c2");
  assert.deepEqual(state.questions.q1.eliminatedChoiceIds, []);
});

test("submitted feedback locks the question and changes score only once", () => {
  let state = quiz.selectChoice(quiz.createQuizState(content), "q1", "c1");
  const feedback = {
    correct: true,
    correct_choice_id: "c1",
    rationale: "First is correct.",
  };

  state = quiz.recordFeedback(state, "q1", feedback);
  const repeated = quiz.recordFeedback(state, "q1", feedback);
  const changed = quiz.selectChoice(repeated, "q1", "c2");

  assert.equal(state.questions.q1.submitted, true);
  assert.equal(state.score, 1);
  assert.equal(repeated.score, 1);
  assert.equal(changed.questions.q1.selectedChoiceId, "c1");
});

test("highlight ranges merge and can be cleared", () => {
  let state = quiz.createQuizState(content);

  state = quiz.addHighlight(state, "q1", 5, 12);
  state = quiz.addHighlight(state, "q1", 10, 18);
  assert.deepEqual(state.questions.q1.highlights, [{ start: 5, end: 18 }]);

  state = quiz.clearHighlights(state, "q1");
  assert.deepEqual(state.questions.q1.highlights, []);
});

test("progress restores only for the same quiz token and version", () => {
  let state = quiz.selectChoice(quiz.createQuizState(content), "q1", "c2");
  state = quiz.toggleEliminated(state, "q1", "c3");
  state = quiz.addHighlight(state, "q1", 0, 5);
  const serialized = quiz.serializeProgress(state);

  const restored = quiz.restoreProgress(content, serialized);
  const newVersion = quiz.restoreProgress(
    { ...content, version: 4 },
    serialized,
  );

  assert.equal(restored.questions.q1.selectedChoiceId, "c2");
  assert.deepEqual(restored.questions.q1.eliminatedChoiceIds, ["c3"]);
  assert.deepEqual(restored.questions.q1.highlights, [{ start: 0, end: 5 }]);
  assert.equal(newVersion.questions.q1.selectedChoiceId, null);
});

test("corrupted submitted feedback is discarded instead of breaking the quiz", () => {
  const corrupted = quiz.createQuizState(content);
  corrupted.questions.q1.selectedChoiceId = "c1";
  corrupted.questions.q1.submitted = true;
  corrupted.questions.q1.feedback = null;

  const restored = quiz.restoreProgress(
    content,
    quiz.serializeProgress(corrupted),
  );

  assert.equal(restored.questions.q1.submitted, false);
  assert.equal(restored.questions.q1.selectedChoiceId, null);
});

test("answer request sends CSRF protection and keeps answers out of URL", async () => {
  let captured;
  const fakeFetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      json: async () => ({
        correct: false,
        correct_choice_id: "c1",
        rationale: "Explanation",
      }),
    };
  };

  const feedback = await quiz.answerRequest(
    fakeFetch,
    "/public/quizzes/token/answer",
    "q1",
    "c2",
    "csrf-token",
  );

  assert.equal(captured.url, "/public/quizzes/token/answer");
  assert.equal(captured.url.includes("q1"), false);
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers["X-CSRF-Token"], "csrf-token");
  assert.deepEqual(JSON.parse(captured.options.body), {
    question_id: "q1",
    choice_id: "c2",
  });
  assert.equal(feedback.correct, false);
});

test("question navigation and flags persist in quiz state", () => {
  let state = quiz.createQuizState({ ...content, questions: content.questions });

  state = quiz.setFlagReason(state, "q1", "inaccurate_question");
  state = quiz.navigateQuestion(state, 1, content.questions.length);

  assert.equal(state.currentIndex, 1);
  assert.equal(state.questions.q1.flagReason, "inaccurate_question");
  assert.throws(
    () => quiz.setFlagReason(state, "q1", "not-a-reason"),
    /Unknown flag reason/,
  );
});

test("performance summary groups right and need-review counts", () => {
  const tagged = {
    ...content,
    topic: "Course topic",
    questions: [
      { ...content.questions[0], area: "Neuro", learning_objective: "Recognize", topic: "Stroke" },
      { ...content.questions[1], area: "Neuro", learning_objective: "Recognize", topic: "Seizure" },
    ],
  };
  let state = quiz.createQuizState(tagged);
  state = quiz.selectChoice(state, "q1", "c1");
  state = quiz.recordFeedback(state, "q1", {
    correct: true,
    correct_choice_id: "c1",
    rationale: "Correct.",
  });
  state = quiz.selectChoice(state, "q2", "c1");
  state = quiz.recordFeedback(state, "q2", {
    correct: false,
    correct_choice_id: "c2",
    rationale: "Review.",
  });

  const summary = quiz.performanceSummary(tagged, state);
  assert.equal(summary.correct, 1);
  assert.equal(summary.percentage, 50);
  assert.deepEqual(summary.areas[0], {
    label: "Neuro",
    total: 2,
    answered: 2,
    correct: 1,
    incorrect: 1,
    unanswered: 0,
    needReview: 1,
    flagged: 0,
  });
});
