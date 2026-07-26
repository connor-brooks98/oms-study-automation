const test = require("node:test");
const assert = require("node:assert/strict");

const settings = require("../../src/oms_hub/web/static/settings.js");

test("password toggle reveals only the current input", () => {
  const input = { type: "password" };
  const button = {
    textContent: "",
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };

  settings.togglePassword(input, button, "OpenAI");
  assert.equal(input.type, "text");
  assert.equal(button.textContent, "Hide");
  assert.equal(button.attributes["aria-label"], "Hide OpenAI credential");

  settings.togglePassword(input, button, "OpenAI");
  assert.equal(input.type, "password");
  assert.equal(button.textContent, "Show");
});

test("test states include text and color-independent classes", () => {
  assert.deepEqual(settings.testPresentation("testing"), {
    label: "Testing…",
    className: "is-testing",
  });
  assert.deepEqual(settings.testPresentation("connected"), {
    label: "Connected",
    className: "is-connected",
  });
  assert.deepEqual(settings.testPresentation("failed"), {
    label: "Connection failed",
    className: "is-failed",
  });
});

test("postJson sends CSRF protection and never puts a credential in the URL", async () => {
  let captured;
  const fakeFetch = async (url, options) => {
    captured = { url, options };
    return { ok: true, json: async () => ({ configured: true }) };
  };

  await settings.postJson(
    fakeFetch,
    "/settings/ai/openai/credential",
    { credential: "sentinel-secret" },
    "csrf-token",
  );

  assert.equal(captured.url, "/settings/ai/openai/credential");
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers["X-CSRF-Token"], "csrf-token");
  assert.equal(captured.options.headers["Content-Type"], "application/json");
  assert.equal(captured.url.includes("sentinel-secret"), false);
  assert.equal(captured.options.body.includes("sentinel-secret"), true);
});

test("diagnostics return safe text fields without HTML", () => {
  const lines = settings.diagnosticLines({
    source: "provider_authentication",
    message: "Gemini rejected the credential",
    http_status: 401,
    next_action: "Replace this provider credential and test again.",
  }, "correlation-1");

  assert.deepEqual(lines, [
    "Provider authentication issue",
    "Gemini rejected the credential",
    "HTTP 401",
    "Replace this provider credential and test again.",
    "Study Hub reference: correlation-1",
  ]);
});
