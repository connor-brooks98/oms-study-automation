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

test("submission freezes the picker manifest before mutable selection changes", () => {
  const first = { name: "first.txt", size: 10 };
  const second = { name: "second.txt", size: 20 };
  const frozen = uploads.freezeManifest([first, second], (() => {
    let next = 0;
    return () => `slot-${next++}`;
  })());

  first.name = "mutated.txt";

  assert.equal(Object.isFrozen(frozen), true);
  assert.equal(frozen.length, 2);
  assert.equal(frozen[0].slotId, "slot-0");
  assert.equal(frozen[0].filename, "first.txt");
  assert.equal(frozen[1].filename, "second.txt");
});

test("only authoritative lifecycle terminal state stops polling", () => {
  assert.equal(
    uploads.batchIsTerminal({ lifecycle: "active", outcome: "failed" }),
    false,
  );
  assert.equal(
    uploads.batchIsTerminal({ lifecycle: "terminal", outcome: "failed" }),
    true,
  );
});

test("stalled polling/request has an explicit timeout while failed outcome stays active", async () => {
  assert.equal(
    uploads.batchIsTerminal({ lifecycle: "active", outcome: "failed" }),
    false,
  );
  const stalled = async (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      reject(new DOMException("Aborted", "AbortError"));
    });
  });

  await assert.rejects(
    uploads.requestWithTimeout(stalled, "/api/upload-batches/test", {}, 1),
    /Upload request timed out\./,
  );
});

test("cancellation aborts the in-flight request without becoming a timeout", async () => {
  const controller = new AbortController();
  const pending = uploads.requestWithTimeout(
    async (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        reject(new DOMException("Cancelled", "AbortError"));
      });
    }),
    "/api/upload-manifests/test",
    { signal: controller.signal },
    1000,
  );
  controller.abort();

  await assert.rejects(pending, (error) => error.name === "AbortError");
});

test("cancellation while waiting for duplicate confirmation rejects promptly", async () => {
  const controller = new AbortController();
  const decision = uploads.createDecisionWait(controller.signal);
  controller.abort();

  await assert.rejects(
    decision.promise,
    (error) => error.name === "AbortError",
  );
});

test("selection locks and serialized errors are rendered as safe text inputs", () => {
  assert.equal(uploads.selectionIsLocked(null), false);
  assert.equal(uploads.selectionIsLocked({ controller: {} }), true);
  assert.equal(
    uploads.itemErrorText({ error: "transcript is not UTF-8" }),
    "transcript is not UTF-8",
  );
  assert.equal(uploads.itemErrorText({ error: null }), "");
  assert.equal(
    uploads.rejectionDetail({
      errors: [
        { filename: "one.txt", detail: "bad encoding" },
        { filename: "two.txt", detail: "too large" },
      ],
    }, "fallback"),
    "one.txt: bad encoding two.txt: too large",
  );
});
