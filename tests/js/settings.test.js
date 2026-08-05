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

test("invalid provider requests have a distinct safe diagnostic", () => {
  const lines = settings.diagnosticLines({
    source: "provider_request",
    message: "Anthropic rejected the request",
    http_status: 400,
    next_action: "Review the provider request format and try again.",
  }, "correlation-2");

  assert.deepEqual(lines, [
    "Provider request issue",
    "Anthropic rejected the request",
    "HTTP 400",
    "Review the provider request format and try again.",
    "Study Hub reference: correlation-2",
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

test("Notebook connecting state is rendered without Google Docs surfaces", () => {
  const badge = {
    textContent: "",
    classList: { toggle() {} },
  };
  const message = { textContent: "" };
  const card = {
    querySelector(selector) {
      if (selector === "[data-notebook-badge]") return badge;
      if (selector === "[data-notebook-status]") return message;
      throw new Error(`unexpected selector: ${selector}`);
    },
  };

  settings.renderNotebookStatus(card, {
    state: "connecting",
    message: "Complete Google sign-in in the browser window.",
  });

  assert.equal(badge.textContent, "Connecting");
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

test("transcript prompt routes use the shared prompt settings endpoints", () => {
  assert.deepEqual(settings.promptRoutes("transcript"), {
    select: "/settings/generation/prompts/transcript/select",
    save: "/settings/generation/prompts/transcript",
    test: "/settings/generation/prompts/transcript/test",
  });
});

test("catalog status reports valid choices and warnings", () => {
  assert.equal(settings.catalogMessage({
    state: "valid",
    choice_count: 3,
    issues: [{ message: "bad.md: unsupported schema" }],
  }), "3 prompt choices are ready. 1 warning: bad.md: unsupported schema");
});

test("postJson surfaces a friendly message when the response body is not JSON", async () => {
  const fakeFetch = async () => ({
    ok: false,
    status: 500,
    json: async () => {
      throw new SyntaxError("Unexpected token < in JSON at position 0");
    },
  });

  await assert.rejects(
    settings.postJson(
      fakeFetch,
      "/api/settings/task-assignments/transcripts",
      { provider: "openai", model: "gpt-4o-mini" },
      "csrf-token",
      "PUT",
    ),
    /Study Hub rejected the request\./,
  );
});

test("getJson surfaces a friendly message when the response body is not JSON", async () => {
  const fakeFetch = async () => ({
    ok: false,
    json: async () => {
      throw new SyntaxError("Unexpected token < in JSON at position 0");
    },
  });

  await assert.rejects(
    settings.getJson(fakeFetch, "/api/settings/providers/openai/models"),
    /Study Hub rejected the request\./,
  );
});

test("modelOptionValues inserts a saved model that is missing from the fetched list", () => {
  assert.deepEqual(
    settings.modelOptionValues(["gpt-4o", "gpt-4o-mini"], "custom/my-model"),
    ["custom/my-model", "gpt-4o", "gpt-4o-mini"],
  );
});

test("modelOptionValues deduplicates and leaves an already-listed model in place", () => {
  assert.deepEqual(
    settings.modelOptionValues(["gpt-4o", "gpt-4o-mini", "gpt-4o"], "gpt-4o-mini"),
    ["gpt-4o", "gpt-4o-mini"],
  );
});

// -- Minimal fake DOM sufficient to drive settings.initialize() end to end --

class FakeClassList {
  constructor() {
    this.tokens = new Set();
  }

  add(name) {
    this.tokens.add(name);
  }

  remove(name) {
    this.tokens.delete(name);
  }

  toggle(name, force) {
    const shouldHave = force === undefined ? !this.tokens.has(name) : force;
    if (shouldHave) this.tokens.add(name);
    else this.tokens.delete(name);
    return shouldHave;
  }

  contains(name) {
    return this.tokens.has(name);
  }
}

const toCamelCase = (name) => name.replace(/-([a-z0-9])/g, (_, char) => char.toUpperCase());

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.textContent = "";
    this.value = "";
    this._listeners = {};
  }

  setDataset(key, value = "") {
    this.dataset[toCamelCase(key)] = value;
    return this;
  }

  append(...nodes) {
    nodes.forEach((node) => this.children.push(node));
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  addEventListener(type, handler) {
    (this._listeners[type] = this._listeners[type] || []).push(handler);
  }

  dispatchEvent(type, event = {}) {
    return (this._listeners[type] || []).map((handler) => handler(event));
  }

  querySelectorAll(selector) {
    const matches = [];
    const walk = (node) => {
      node.children.forEach((child) => {
        if (matchesSelector(child, selector)) matches.push(child);
        walk(child);
      });
    };
    walk(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

const matchesSelector = (element, selector) => {
  if (selector.startsWith("[") && selector.endsWith("]")) {
    const attribute = selector.slice(1, -1);
    if (attribute.startsWith("data-")) {
      return Object.prototype.hasOwnProperty.call(
        element.dataset,
        toCamelCase(attribute.slice(5)),
      );
    }
    return false;
  }
  return element.tagName.toLowerCase() === selector.toLowerCase();
};

class FakeDocument extends FakeElement {
  constructor() {
    super("#document");
    this.cookie = "study_hub_csrf=test-csrf-token";
    this.readyState = "complete";
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }
}

const flush = async () => {
  await new Promise((resolve) => { setImmediate(resolve); });
};

const buildAssignmentRow = (documentRef, { task, provider, model, gate }) => {
  const row = documentRef.createElement("article");
  row.setDataset("assignment-row");
  row.dataset.task = task;

  const providerSelect = documentRef.createElement("select");
  providerSelect.setDataset("assignment-provider");
  providerSelect.value = provider;
  row.append(providerSelect);

  const modelSelect = documentRef.createElement("select");
  modelSelect.setDataset("assignment-model");
  modelSelect.value = model;
  modelSelect.append((() => {
    const option = documentRef.createElement("option");
    option.value = model;
    option.textContent = model;
    return option;
  })());
  row.append(modelSelect);

  const customModel = documentRef.createElement("input");
  customModel.setDataset("assignment-custom");
  customModel.hidden = true;
  row.append(customModel);

  const saveButton = documentRef.createElement("button");
  saveButton.setDataset("save-assignment");
  row.append(saveButton);

  const keyState = documentRef.createElement("span");
  keyState.setDataset("assignment-key");
  keyState.textContent = "Key not configured";
  row.append(keyState);

  const message = documentRef.createElement("p");
  message.setDataset("assignment-message");
  row.append(message);

  if (gate !== undefined) {
    const gateInput = documentRef.createElement("input");
    gateInput.setDataset("openrouter-gate");
    gateInput.checked = gate;
    row.append(gateInput);
  }

  return row;
};

test("assignment row populates its model dropdown from a mocked models fetch", async () => {
  const documentRef = new FakeDocument();
  const row = buildAssignmentRow(documentRef, {
    task: "transcripts",
    provider: "openai",
    model: "gpt-4o-mini",
  });
  documentRef.append(row);

  const requestedUrls = [];
  const fetchImpl = async (url) => {
    requestedUrls.push(url);
    return {
      ok: true,
      json: async () => ({ models: ["gpt-4o", "gpt-4o-mini"], source: "live" }),
    };
  };

  settings.initialize(documentRef, fetchImpl);
  await flush();

  assert.equal(requestedUrls[0], "/api/settings/providers/openai/models");
  const modelSelect = row.querySelector("[data-assignment-model]");
  assert.deepEqual(
    modelSelect.children.map((option) => option.value),
    ["gpt-4o", "gpt-4o-mini", "__custom__"],
  );
  assert.equal(modelSelect.value, "gpt-4o-mini");
});

test("assignment row model dropdown repopulates when the provider changes", async () => {
  const documentRef = new FakeDocument();
  const row = buildAssignmentRow(documentRef, {
    task: "anki_curation",
    provider: "openai",
    model: "gpt-4o-mini",
  });
  documentRef.append(row);

  const modelsByProvider = {
    "/api/settings/providers/openai/models": ["gpt-4o", "gpt-4o-mini"],
    "/api/settings/providers/gemini/models": ["gemini-2.5-pro", "gemini-2.5-flash"],
  };
  const fetchImpl = async (url) => ({
    ok: true,
    json: async () => ({ models: modelsByProvider[url] || [], source: "live" }),
  });

  settings.initialize(documentRef, fetchImpl);
  await flush();

  const providerSelect = row.querySelector("[data-assignment-provider]");
  const modelSelect = row.querySelector("[data-assignment-model]");
  providerSelect.value = "gemini";
  providerSelect.dispatchEvent("change");
  await flush();

  assert.deepEqual(
    modelSelect.children.map((option) => option.value),
    ["gemini-2.5-pro", "gemini-2.5-flash", "__custom__"],
  );
  assert.equal(modelSelect.value, "gemini-2.5-pro");
});

test("assignment row disables its controls while a save request is in flight", async () => {
  const documentRef = new FakeDocument();
  const row = buildAssignmentRow(documentRef, {
    task: "transcripts",
    provider: "openai",
    model: "gpt-4o-mini",
  });
  documentRef.append(row);

  let resolveSave;
  const fetchImpl = async (url) => {
    if (url === "/api/settings/providers/openai/models") {
      return { ok: true, json: async () => ({ models: ["gpt-4o-mini"], source: "fallback" }) };
    }
    if (url === "/api/settings/task-assignments/transcripts") {
      return new Promise((resolve) => {
        resolveSave = () => resolve({
          ok: true,
          json: async () => ({
            task: "transcripts",
            provider: "openai",
            model: "gpt-4o-mini",
            key_configured: true,
          }),
        });
      });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  settings.initialize(documentRef, fetchImpl);
  await flush();

  const providerSelect = row.querySelector("[data-assignment-provider]");
  const modelSelect = row.querySelector("[data-assignment-model]");
  const saveButton = row.querySelector("[data-save-assignment]");
  const keyState = row.querySelector("[data-assignment-key]");

  const pending = saveButton.dispatchEvent("click");
  assert.equal(providerSelect.disabled, true);
  assert.equal(modelSelect.disabled, true);
  assert.equal(saveButton.disabled, true);

  resolveSave();
  await Promise.all(pending);
  await flush();

  assert.equal(providerSelect.disabled, false);
  assert.equal(modelSelect.disabled, false);
  assert.equal(saveButton.disabled, false);
  assert.equal(keyState.textContent, "Key configured");
  assert.equal(keyState.classList.contains("is-configured"), true);
});

test("assignment row accuracy gate toggle posts to the existing gate route", async () => {
  const documentRef = new FakeDocument();
  const row = buildAssignmentRow(documentRef, {
    task: "accuracy_review",
    provider: "openrouter",
    model: "openrouter/auto",
    gate: false,
  });
  documentRef.append(row);

  const posted = [];
  const fetchImpl = async (url, options) => {
    if (url === "/api/settings/providers/openrouter/models") {
      return { ok: true, json: async () => ({ models: ["openrouter/auto"], source: "fallback" }) };
    }
    if (url === "/settings/ai/openrouter/gate") {
      posted.push(JSON.parse(options.body));
      return { ok: true, json: async () => ({ enabled: true }) };
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  settings.initialize(documentRef, fetchImpl);
  await flush();

  const gate = row.querySelector("[data-openrouter-gate]");
  gate.checked = true;
  await Promise.all(gate.dispatchEvent("change"));

  assert.deepEqual(posted, [{ enabled: true }]);
  assert.equal(gate.checked, true);
});

test("anki prompt directory test button disables while its request is in flight", async () => {
  const documentRef = new FakeDocument();
  const card = documentRef.createElement("article");
  card.setDataset("anki-prompt-directory");

  const input = documentRef.createElement("input");
  input.setDataset("anki-prompt-directory-path");
  input.value = "C:\\Vault\\Anki Prompts";
  card.append(input);

  const saveButton = documentRef.createElement("button");
  saveButton.setDataset("save-anki-prompt-directory");
  card.append(saveButton);

  const testButton = documentRef.createElement("button");
  testButton.setDataset("test-anki-prompt-directory");
  card.append(testButton);

  const message = documentRef.createElement("p");
  message.setDataset("anki-prompt-directory-message");
  card.append(message);

  documentRef.append(card);

  let resolveTest;
  const fetchImpl = async (url) => {
    if (url === "/settings/anki/prompts/directory/test") {
      return new Promise((resolve) => {
        resolveTest = () => resolve({
          ok: true,
          json: async () => ({ choice_count: 2, issues: [] }),
        });
      });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  settings.initialize(documentRef, fetchImpl);

  const pending = testButton.dispatchEvent("click");
  assert.equal(testButton.disabled, true);

  resolveTest();
  await Promise.all(pending);
  await flush();

  assert.equal(testButton.disabled, false);
  assert.equal(message.textContent, "2 prompt choices are ready.");
});

test("populateModelSelect inserts a saved model missing from the list and keeps it selected", () => {
  const documentRef = new FakeDocument();
  const select = documentRef.createElement("select");
  const customInput = documentRef.createElement("input");
  customInput.hidden = true;

  settings.populateModelSelect(
    documentRef,
    select,
    customInput,
    ["gpt-4o", "gpt-4o-mini"],
    "custom/my-model",
  );

  assert.deepEqual(
    select.children.map((option) => option.value),
    ["custom/my-model", "gpt-4o", "gpt-4o-mini", "__custom__"],
  );
  assert.equal(select.value, "custom/my-model");
  assert.equal(customInput.hidden, true);
});

test("populateModelSelect reveals the custom input when no models are available", () => {
  const documentRef = new FakeDocument();
  const select = documentRef.createElement("select");
  const customInput = documentRef.createElement("input");

  settings.populateModelSelect(documentRef, select, customInput, [], "");

  assert.equal(select.value, "__custom__");
  assert.equal(customInput.hidden, false);
});

test("selecting the custom model option reveals the free-text input", () => {
  const select = { value: "__custom__" };
  const customInput = { hidden: true };

  settings.syncCustomModelVisibility(select, customInput);
  assert.equal(customInput.hidden, false);

  select.value = "gpt-4o";
  settings.syncCustomModelVisibility(select, customInput);
  assert.equal(customInput.hidden, true);
});

test("resolvedModelValue reads the free-text field only when custom is selected", () => {
  const select = { value: "gpt-4o" };
  const customInput = { value: "  ignored  " };
  assert.equal(settings.resolvedModelValue(select, customInput), "gpt-4o");

  select.value = "__custom__";
  customInput.value = "  custom/model-id  ";
  assert.equal(settings.resolvedModelValue(select, customInput), "custom/model-id");
});
