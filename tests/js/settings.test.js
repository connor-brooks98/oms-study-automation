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

test("settings initialize immediately when the page is already loaded", () => {
  let starts = 0;
  const documentRef = {
    readyState: "complete",
    addEventListener() {
      throw new Error("DOMContentLoaded listener should not be registered");
    },
  };

  settings.runWhenReady(documentRef, () => {
    starts += 1;
  });

  assert.equal(starts, 1);
});

test("settings wait for DOMContentLoaded while the page is loading", () => {
  let listener;
  let starts = 0;
  const documentRef = {
    readyState: "loading",
    addEventListener(name, callback) {
      assert.equal(name, "DOMContentLoaded");
      listener = callback;
    },
  };

  settings.runWhenReady(documentRef, () => {
    starts += 1;
  });
  assert.equal(starts, 0);

  listener();
  assert.equal(starts, 1);
});

test("Google connecting state keeps every service in connecting state", () => {
  const surfaces = ["notebook", "gemini", "docs"].map((name) => ({
    dataset: { googleSurface: name },
    textContent: "",
    className: "",
  }));
  const badge = {
    textContent: "",
    classList: { toggle() {} },
  };
  const message = { textContent: "" };
  const card = {
    querySelector(selector) {
      if (selector === "[data-google-badge]") return badge;
      if (selector === "[data-google-status]") return message;
      throw new Error(`unexpected selector: ${selector}`);
    },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-google-surface]");
      return surfaces;
    },
  };

  settings.renderGoogleStatus(card, {
    state: "connecting",
    account_email: null,
    surfaces: [],
    message: "Complete Google sign-in in the browser window.",
  });

  assert.equal(badge.textContent, "Connecting");
  assert.deepEqual(
    surfaces.map((surface) => surface.textContent),
    ["connecting", "connecting", "connecting"],
  );
  assert.equal(
    message.textContent,
    "Complete Google sign-in in the browser window.",
  );
});

test("prompt action changes from select to save after a path is chosen", () => {
  assert.equal(settings.promptPathAction(""), "select");
  assert.equal(settings.promptPathAction("   "), "select");
  assert.equal(
    settings.promptPathAction("C:\\Vault\\Outline Prompt.md"),
    "save",
  );
});
