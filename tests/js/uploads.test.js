const test = require("node:test");
const assert = require("node:assert/strict");

const uploads = require("../../src/oms_hub/web/static/uploads.js");

test("lecture warning uses course, padded lecture number, and topic", () => {
  assert.equal(
    uploads.formatLecture({
      subject: "Cardiology",
      lecture_number: 7,
      topic: "Heart Failure",
    }),
    "Cardiology · Lecture 07 · Heart Failure",
  );
});

test("next confirmation returns only a paused upload with metadata", () => {
  const selected = uploads.nextConfirmation({
    items: [
      { id: "queued", state: "queued" },
      {
        id: "paused-item",
        state: "awaiting_confirmation",
        duplicate_warning: {
          subject: "Cardiology",
          lecture_number: 7,
          topic: "Heart Failure",
        },
      },
    ],
  });

  assert.equal(selected.id, "paused-item");
  assert.equal(uploads.nextConfirmation({ items: [] }), null);
});

test("decision request posts with CSRF and keeps item data out of URL", async () => {
  let captured;
  const fakeFetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      json: async () => ({ item_id: "paused-item", state: "queued" }),
    };
  };

  await uploads.postDecision(
    fakeFetch,
    "paused-item",
    "confirm",
    "csrf-token",
  );

  assert.equal(
    captured.url,
    "/api/upload-items/paused-item/confirm",
  );
  assert.equal(captured.options.method, "POST");
  assert.equal(
    captured.options.headers["X-CSRF-Token"],
    "csrf-token",
  );
  assert.equal(captured.options.body, undefined);
  assert.equal(captured.url.includes("?"), false);
});

test("decision request reports a friendly message for an HTML error body", async () => {
  const fakeFetch = async () => ({
    ok: false,
    json: async () => { throw new SyntaxError("HTML"); },
  });

  await assert.rejects(
    uploads.postDecision(fakeFetch, "item", "confirm", "csrf"),
    /Study Hub rejected the decision/,
  );
});

test("large lecture uploads retain their selected lecture at finalize", () => {
  assert.equal(
    uploads.chunkFinalizeUrl("session-1", "42"),
    "/api/upload-chunks/session-1/finalize?lecture_id=42",
  );
  assert.equal(
    uploads.chunkFinalizeUrl("session-1", ""),
    "/api/upload-chunks/session-1/finalize",
  );
});
