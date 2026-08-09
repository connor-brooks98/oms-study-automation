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

test("duplicate confirmation wait rejects an already-aborted submission", async () => {
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    uploads.createDecisionWait(controller.signal).promise,
    (error) => error.name === "AbortError",
  );
});

test("duplicate confirmation wait respects the overall polling deadline", async () => {
  const controller = new AbortController();
  const decision = uploads.createDecisionWait(controller.signal, Date.now() + 1);

  await assert.rejects(
    decision.promise,
    /Upload status timed out before a terminal result\./,
  );
});

test("decision deadline clears the active modal state before upload cleanup", async () => {
  const controller = new AbortController();
  let resume = undefined;
  let cleared = 0;

  await assert.rejects(
    uploads.waitForDecision(
      controller.signal,
      Date.now() + 1,
      (value) => { resume = value; },
      () => { cleared += 1; },
    ),
    /Upload status timed out before a terminal result\./,
  );

  assert.equal(cleared, 1);
  assert.equal(resume, null);
});

test("decision POST shares the active abort signal and bounded timeout", async () => {
  let captured;
  const stalled = async (_url, options) => {
    captured = options;
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        reject(new DOMException("Timed out", "AbortError"));
      });
    });
  };

  await assert.rejects(
    uploads.postDecision(stalled, "paused", "confirm", "csrf", undefined, 1),
    /Upload request timed out\./,
  );
  assert.equal(captured.method, "POST");
  assert.equal(captured.headers["X-CSRF-Token"], "csrf");
});

test("manifest cancellation reconciles a committed batch instead of claiming success", async () => {
  const finalized = await uploads.cancelManifest(
    async () => ({
      status: 409,
      ok: false,
      json: async () => ({ batch_id: "batch-1" }),
    }),
    "manifest-1",
    { "X-CSRF-Token": "csrf" },
  );
  const cancelled = await uploads.cancelManifest(
    async () => ({
      status: 204,
      ok: true,
      json: async () => { throw new Error("204 response has no JSON body"); },
    }),
    "manifest-2",
    { "X-CSRF-Token": "csrf" },
  );

  assert.deepEqual(finalized, { finalized: true, batchId: "batch-1" });
  assert.deepEqual(cancelled, { finalized: false, batchId: null });
});

test("finalized cancellation fetches and renders authoritative batch state", async () => {
  let captured;
  let rendered;
  const batch = { lifecycle: "terminal", outcome: "complete", items: [] };

  const result = await uploads.reconcileFinalizedBatch(
    async (url, options) => {
      captured = { url, options };
      return { ok: true, json: async () => batch };
    },
    "batch-1",
    { Accept: "application/json" },
    (value) => { rendered = value; },
  );

  assert.equal(captured.url, "/api/upload-batches/batch-1");
  assert.equal(captured.options.cache, "no-store");
  assert.deepEqual(rendered, batch);
  assert.deepEqual(result, batch);
});

test("duplicate-dialog Escape aborts an active submission and keeps modal dismissals explicit", () => {
  const controller = new AbortController();
  let prevented = false;
  uploads.handleDecisionDialogCancel(
    { preventDefault: () => { prevented = true; } },
    { controller },
  );
  assert.equal(prevented, true);
  assert.equal(controller.signal.aborted, true);

  let inactivePrevented = false;
  uploads.handleDecisionDialogCancel(
    { preventDefault: () => { inactivePrevented = true; } },
    null,
  );
  assert.equal(inactivePrevented, true);
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
