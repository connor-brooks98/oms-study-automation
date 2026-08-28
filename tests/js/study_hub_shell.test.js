const test = require("node:test");
const assert = require("node:assert/strict");

const shell = require("../../src/oms_hub/web/static/study_hub_shell.js");

test("command queries are normalized and match both labels and supporting text", () => {
  assert.equal(shell.normalizeQuery("  Quiz BUILDER "), "quiz builder");
  assert.equal(shell.matchesCommand("Upload slides Convert PowerPoint files", "powerpoint"), true);
  assert.equal(shell.matchesCommand("Review Resolve stopped workflows", "anki"), false);
});

test("command selection wraps in both directions", () => {
  assert.equal(shell.nextIndex(-1, 1, 4), 0);
  assert.equal(shell.nextIndex(3, 1, 4), 0);
  assert.equal(shell.nextIndex(0, -1, 4), 3);
  assert.equal(shell.nextIndex(0, 1, 0), -1);
});

test("transition timing honors CSS units and reduced-motion preferences", () => {
  const windowRef = {
    document: { documentElement: {} },
    matchMedia: () => ({ matches: false }),
    getComputedStyle: () => ({ getPropertyValue: () => "0.15s" }),
  };
  assert.equal(shell.transitionDelay(windowRef, "--modal-duration"), 150);
  windowRef.matchMedia = () => ({ matches: true });
  assert.equal(shell.transitionDelay(windowRef, "--modal-duration"), 0);
});

test("button semantics reserve stateful motion for submissions and map action icons", () => {
  const submission = {
    matches: (selector) => selector === "button.sh-btn",
    getAttribute: () => "submit",
    textContent: "Save changes",
  };
  const download = { ...submission, getAttribute: () => "button", textContent: "Download lecture" };
  const tab = {
    ...submission,
    matches: (selector) => selector === "button.sh-btn" || selector === '[role="tab"], .sh-seg__btn',
    getAttribute: () => "button",
    textContent: "Generate Quiz",
  };
  assert.equal(shell.isStatefulAction(submission), true);
  assert.equal(shell.isStatefulAction(download), false);
  assert.equal(shell.isStatefulAction(tab), false);
  assert.equal(shell.buttonIcon(download.textContent), "download");
  assert.equal(shell.buttonIcon("Remove source"), "trash");
  assert.equal(shell.buttonIcon("Continue"), "continue");
  assert.equal(shell.buttonIcon("Test connection"), "");
});

test("toast feedback deduplicates the same message and keeps its semantic tone", () => {
  const region = {
    children: [],
    append(item) { item.isConnected = true; this.children.push(item); },
    querySelectorAll() { return this.children; },
    querySelector() { return this.children[0] || null; },
  };
  const documentRef = {
    querySelector: () => region,
    createElement: () => ({
      dataset: {}, className: "", isConnected: false,
      classList: { contains: () => false, add() {} },
      setAttribute() {}, addEventListener() {},
      append(...children) { this.children = children; },
      remove() { this.isConnected = false; },
    }),
  };
  const windowRef = { setTimeout() {} };
  const first = shell.showToast(documentRef, "Question saved.", "success", windowRef);
  const duplicate = shell.showToast(documentRef, "Question saved.", "success", windowRef);
  assert.equal(first, duplicate);
  assert.equal(first.dataset.tone, "success");
  assert.equal(region.children.length, 1);
  assert.equal(shell.toastTone("Review update failed.", { getAttribute: () => "alert" }), "error");
});

test("custom confirmation resolves only after its own action button is chosen", async () => {
  const control = (text = "") => ({
    textContent: text,
    handlers: {},
    classList: { toggle() {} },
    addEventListener(type, handler) { this.handlers[type] = handler; },
    removeEventListener(type) { delete this.handlers[type]; },
    focus() {},
  });
  const title = control();
  const message = control();
  const accept = control("Continue");
  const close = control("×");
  const cancel = control("Keep it");
  const dialog = {
    open: false,
    handlers: {},
    classList: { add() {}, remove() {} },
    querySelector(selector) {
      return ({ "[data-confirm-title]": title, "[data-confirm-message]": message, "[data-confirm-accept]": accept })[selector];
    },
    querySelectorAll: () => [close, cancel],
    addEventListener(type, handler) { this.handlers[type] = handler; },
    removeEventListener(type) { delete this.handlers[type]; },
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
  const documentRef = { defaultView: {}, querySelector: () => dialog };
  const windowRef = { requestAnimationFrame: (callback) => callback(), setTimeout: (callback) => callback() };

  const decision = shell.confirmAction(documentRef, {
    title: "Reset quiz progress?",
    message: "Saved answers will be cleared.",
    confirmLabel: "Reset quiz",
  }, null, windowRef);
  accept.handlers.click();

  assert.equal(await decision, true);
  assert.equal(title.textContent, "Reset quiz progress?");
  assert.equal(message.textContent, "Saved answers will be cleared.");
  assert.equal(dialog.open, false);
});
