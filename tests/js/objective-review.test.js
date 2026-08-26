const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const review = require("../../src/oms_hub/web/static/js/objectives/objective-review.js");

test("review client uses scoped routes, JSON bodies, and CSRF headers", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push([url, options]);
    return { ok: true, json: async () => ({ status: "pending" }) };
  };
  const client = new review.ObjectiveReviewClient(fetchImpl, "csrf-token");

  await client.extract(["revision-1"]);
  await client.approve("objective-1");
  await client.merge("objective-1", "objective-2");
  await client.retire("objective-1");
  await client.previewEvidence("evidence-1");

  assert.deepEqual(calls.map(([url]) => url), [
    "/api/v1/objectives/extract",
    "/api/v1/objectives/objective-1/approve",
    "/api/v1/objectives/objective-1/merge",
    "/api/v1/objectives/objective-1/retire",
    "/api/v1/knowledge/evidence/evidence-1",
  ]);
  assert.equal(calls[1][1].headers["X-CSRF-Token"], "csrf-token");
  assert.deepEqual(JSON.parse(calls[2][1].body), { target_objective_id: "objective-2" });
});

test("pending actions reject duplicates and always recover after failure", async () => {
  const pending = new review.PendingActions();
  let release;
  const first = pending.run("approve:objective-1", () => new Promise((resolve) => { release = resolve; }));

  assert.equal(pending.has("approve:objective-1"), true);
  await assert.rejects(
    pending.run("approve:objective-1", async () => undefined),
    /already pending/,
  );
  release();
  await first;
  assert.equal(pending.has("approve:objective-1"), false);

  await assert.rejects(
    pending.run("retire:objective-1", async () => { throw new Error("offline"); }),
    /offline/,
  );
  assert.equal(pending.has("retire:objective-1"), false);
});

test("review cards reuse semantic disclosure, status, form, and evidence primitives", () => {
  const html = review.objectiveCard({
    objective_id: "objective-1",
    observable_verb: "differentiate",
    concept: "<unsafe>",
    description: "Distinguish HIT.",
    evidence_ids: ["evidence-1"],
    status: "pending",
  });

  assert.match(html, /<details/);
  assert.match(html, /<form/);
  assert.match(html, /sh-pill sh-pill--warn/);
  assert.match(html, /data-preview-evidence="evidence-1"/);
  assert.doesNotMatch(html, /<unsafe>/);
  assert.equal(review.statusTone("approved"), "sh-pill--ok");
  assert.equal(review.statusTone("retired"), "sh-pill--bare");
});

test("scoped stylesheet imports frozen design tokens without central wiring", () => {
  const css = fs.readFileSync(
    "src/oms_hub/web/static/css/objectives.css",
    "utf8",
  );

  assert.match(css, /@import url\("\.\.\/tokens\.css"\)/);
  assert.match(css, /\.objective-review/);
  assert.match(css, /var\(--/);
});
