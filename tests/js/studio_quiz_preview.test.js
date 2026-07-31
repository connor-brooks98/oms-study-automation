const test = require("node:test");
const assert = require("node:assert/strict");

const preview = require("../../src/oms_hub/web/static/studio_quiz_preview.js");

test("publish request uses POST and CSRF then returns only trusted public path", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      json: async () => ({
        token: "a".repeat(64),
        published_url: `/public/quizzes/${"a".repeat(64)}`,
      }),
    };
  };

  const url = await preview.publishQuiz(fetchImpl, "/studio/runs/run-1/publication", "csrf-1");

  assert.equal(url, `/public/quizzes/${"a".repeat(64)}`);
  assert.equal(calls[0].url, "/studio/runs/run-1/publication");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-1");
});

test("publish request rejects malformed success response", async () => {
  const fetchImpl = async () => ({
    ok: true,
    json: async () => ({ published_url: "https://evil.example/quiz" }),
  });

  await assert.rejects(
    preview.publishQuiz(fetchImpl, "/studio/runs/run-1/publication", "csrf-1"),
    /could not be published/,
  );
});
