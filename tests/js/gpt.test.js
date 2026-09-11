const test = require("node:test");
const assert = require("node:assert/strict");
const gpt = require("../../src/oms_hub/web/static/gpt.js");

test("objectives omit blank lines and preserve exact learner text in ordered JSON", () => {
  assert.deepEqual(gpt.objectives("  Explain anemia\r\n\n Compare <Hb> & iron  "), [
    { id: "lo-1", text: "Explain anemia" }, { id: "lo-2", text: "Compare <Hb> & iron" },
  ]);
  assert.deepEqual(gpt.objectives(" \n\r\n"), []);
});

test("account connection never implies unverified generation is ready", () => {
  assert.equal(gpt.statusText({ account_connected: true, state: "unavailable", error_code: "capability_unverified" }),
    "Account: connected. Generation readiness: capabilities not verified.");
  assert.match(gpt.statusText({ state: "limited", reset_at: "2026-09-12T00:00:00Z", error_code: "rate_limited" }),
    /not connected.*paused at usage limit.*2026-09-12T00:00:00Z.*rate_limited/);
  assert.match(gpt.statusText({ state: "connected", account_connected: true }), /readiness: ready/);
});

test("API errors preserve plain detail and validation messages", () => {
  assert.equal(gpt.errorMessage({ detail: "<b>Approval required</b>" }), "<b>Approval required</b>");
  assert.equal(gpt.errorMessage({ detail: [{ msg: "Missing title" }, { msg: "Missing objective" }] }), "Missing title; Missing objective");
  assert.equal(gpt.errorMessage(null, "Unavailable"), "Unavailable");
});

test("POST includes JSON and existing CSRF cookie; non-JSON errors stay readable", async () => {
  let request;
  const documentRef = { cookie: "other=x; study_hub_csrf=a%20b" };
  await gpt.post(documentRef, async (...args) => { request = args; return { ok: true, json: async () => ({}) }; }, "/settings/generation/codex/model", { model: "sample" });
  assert.equal(request[1].headers["X-CSRF-Token"], "a b");
  assert.equal(request[1].credentials, "same-origin");
  assert.deepEqual(JSON.parse(request[1].body), { model: "sample" });
  await assert.rejects(gpt.post(documentRef, async () => ({ ok: false, status: 503, json: async () => { throw Error(); } }), "/status"), /503/);
});

test("login links require HTTPS and review links stay on this Hub", () => {
  const base = "http://localhost:8765/lectures/1";
  assert.equal(gpt.safeLink("https://auth.openai.com/example?code=actual", base, true), "https://auth.openai.com/example?code=actual");
  assert.equal(gpt.safeLink("/studio/runs/example", base), "http://localhost:8765/studio/runs/example");
  for (const value of ["javascript:alert(1)", "http://auth.openai.com", "https://user:pass@auth.openai.com"]) {
    assert.throws(() => gpt.safeLink(value, base, true), /invalid link/);
  }
  assert.throws(() => gpt.safeLink("https://example.com/review", base), /invalid link/);
});

test("initializing an empty page makes no connection or login calls", () => {
  gpt.initialize({ querySelector: () => null }, () => { throw Error("Unexpected page-load request"); });
});
