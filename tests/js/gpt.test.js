const test = require("node:test");
const assert = require("node:assert/strict");
const gpt = require("../../src/oms_hub/web/static/study_generation.js");

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

test("run controls and polling follow only their allowed states", () => {
  for (const state of ["queued", "running"]) assert.deepEqual(
    [gpt.runPresentation(state).active, gpt.runPresentation(state).resume, gpt.runPresentation(state).review], [true, false, false]);
  for (const state of ["paused", "interrupted", "failed"]) assert.deepEqual(
    [gpt.runPresentation(state).active, gpt.runPresentation(state).resume, gpt.runPresentation(state).review], [false, true, false]);
  for (const state of ["awaiting_review", "complete"]) assert.deepEqual(
    [gpt.runPresentation(state).active, gpt.runPresentation(state).resume, gpt.runPresentation(state).review], [false, false, true]);
  assert.deepEqual(gpt.runPresentation("unexpected"), { label: "Status unavailable", active: false, resume: false, review: false });
});

function runFixture() {
  const nodes = Object.fromEntries(["state", "refresh", "cancel", "resume", "review", "message", "error", "diagnostic"].map((name) => [name, {
    hidden: true, textContent: "", addEventListener(type, fn) { this[type] = fn; },
  }]));
  const page = { dataset: { gptRun: "run-1" }, setAttribute() {}, querySelector: (selector) => nodes[selector.slice(14, -1)] };
  const documentRef = { cookie: "study_hub_csrf=token", baseURI: "http://localhost:8765/lectures/gpt-runs/run-1" };
  const runtime = { setTimeout(fn, delay) { this.next = fn; this.delay = delay; return 1; }, clearTimeout() { this.next = null; }, addEventListener(_, fn) { this.unload = fn; } };
  return { nodes, page, documentRef, runtime };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("run poll is serialized, stops on terminal state, and never publishes", async () => {
  const { nodes, page, documentRef, runtime } = runFixture();
  const calls = [];
  let resolve;
  const fetchImpl = (...args) => { calls.push(args); return new Promise((done) => { resolve = done; }); };
  gpt.initializeRun(page, documentRef, fetchImpl, runtime);
  nodes.refresh.click();
  nodes.cancel.click();
  assert.equal(calls.length, 1);
  resolve({ ok: true, json: async () => ({ run_id: "run-1", state: "running" }) });
  await flush();
  assert.equal(runtime.delay, 3000);
  assert.equal(nodes.cancel.hidden, false);
  runtime.next();
  resolve({ ok: true, json: async () => ({ run_id: "run-1", state: "awaiting_review", review_url: "/studio/runs/run-1" }) });
  await flush();
  assert.equal(runtime.next, null);
  assert.equal(nodes.review.href, "http://localhost:8765/studio/runs/run-1");
  assert.equal(nodes.review.hidden, false);
  assert.equal(nodes.cancel.hidden, true);
  assert.ok(calls.every(([path, options]) => path.endsWith("/runs/run-1") && !options.method));
});

test("explicit resume uses CSRF then refreshes; unload ignores late responses", async () => {
  const { nodes, page, documentRef, runtime } = runFixture();
  const calls = [];
  let finish;
  const fetchImpl = async (path, options) => {
    calls.push([path, options]);
    if (calls.length === 3) return new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => ({ run_id: "run-1", state: "paused" }) };
  };
  gpt.initializeRun(page, documentRef, fetchImpl, runtime);
  await flush();
  assert.equal(nodes.resume.hidden, false);
  nodes.resume.click();
  await flush();
  assert.equal(calls[1][0], "/settings/generation/codex/runs/run-1/resume");
  assert.equal(calls[1][1].headers["X-CSRF-Token"], "token");
  runtime.unload();
  finish({ ok: true, json: async () => ({ run_id: "run-1", state: "running" }) });
  await flush();
  assert.equal(nodes.state.textContent, "Paused");
  assert.equal(runtime.next, null);
});

test("wrong run identity is rejected without polling or review navigation", async () => {
  const { nodes, page, documentRef, runtime } = runFixture();
  gpt.initializeRun(page, documentRef, async () => ({ ok: true, json: async () => ({ run_id: "other", state: "complete", review_url: "/studio/other" }) }), runtime);
  await flush();
  assert.match(nodes.message.textContent, /different quiz request/);
  assert.equal(nodes.review.hidden, true);
  assert.equal(runtime.next, null);
});

test("single lecture button sends optional instructions without required fields or NotebookLM", async () => {
  let submitHandler, request;
  const button = { disabled: false };
  const message = {}, review = {};
  const form = {
    reportValidity: () => true, setAttribute() {},
    querySelector: () => button,
    addEventListener: (_event, handler) => { submitHandler = handler; },
  };
  const lecture = {
    dataset: { gptLecture: "17" },
    querySelector: (selector) => ({
      "[data-gpt-quiz-form]": form, "[data-gpt-message]": message,
      "[data-gpt-review]": review,
    })[selector],
  };
  const doc = {
    cookie: "study_hub_csrf=token", baseURI: "http://127.0.0.1:60431/lectures/17",
    querySelector: (selector) => selector === "[data-gpt-lecture]" ? lecture : null,
  };
  gpt.initialize(doc, async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ run_id: "run-17", state: "queued", review_url: "/lectures/gpt-runs/run-17" }) };
  });
  await submitHandler({ preventDefault() {} });
  assert.equal(request.url, "/lectures/17/gpt-quiz");
  assert.deepEqual(JSON.parse(request.options.body), {instructions: ""});
  assert.equal(button.disabled, false);
  assert.equal(review.hidden, false);
  assert.equal(review.href, "http://127.0.0.1:60431/lectures/gpt-runs/run-17");
  form.elements = {instructions: {value: "  Only slides 2–6  "}};
  await submitHandler({preventDefault() {}});
  assert.deepEqual(JSON.parse(request.options.body), {instructions: "Only slides 2–6"});
});

test("Connect opens on the current device, keeps a popup fallback, and closes failed sign-in tabs", async () => {
  for (const outcome of ["opened", "blocked", "failed"]) {
    const nodes = Object.fromEntries(["message", "model", "login-link", "code", "code-row", "challenge"].map(name => [`[data-gpt-${name}]`, {}]));
    const button = { dataset: { gptAction: "login" }, addEventListener(_, fn) { this.click = fn; } };
    const settings = { querySelector: selector => nodes[selector], querySelectorAll: () => [button], setAttribute() {} };
    const popup = { opener: {}, location: { replace(url) { this.url = url; } }, close() { this.closed = true; } };
    const order = [];
    const doc = {
      baseURI: "https://studyhub.example/settings", cookie: "study_hub_csrf=token",
      querySelector: selector => selector === "[data-gpt-settings]" ? settings : null,
      defaultView: { open(url, target) { order.push("open"); assert.equal(url, "about:blank"); assert.equal(target, "_blank"); return outcome === "blocked" ? null : popup; } },
    };
    gpt.initialize(doc, async () => {
      order.push("request");
      return { ok: outcome !== "failed", json: async () => outcome === "failed" ? { detail: "Runtime unavailable" } : {
        login_id: "login-1", url: "https://auth.openai.com/codex/device", user_code: "TEST-CODE",
      } };
    });
    await button.click();
    assert.deepEqual(order, ["open", "request"]);
    assert.equal(button.disabled, false);
    if (outcome === "failed") {
      assert.equal(popup.closed, true);
      assert.equal(nodes["[data-gpt-message]"].textContent, "Runtime unavailable");
    } else {
      assert.equal(nodes["[data-gpt-login-link]"].href, "https://auth.openai.com/codex/device");
      assert.equal(nodes["[data-gpt-code]"].textContent, "TEST-CODE");
      assert.equal(nodes["[data-gpt-challenge]"].hidden, false);
      if (outcome === "opened") {
        assert.equal(popup.opener, null);
        assert.equal(popup.location.url, nodes["[data-gpt-login-link]"].href);
      }
    }
  }
});
