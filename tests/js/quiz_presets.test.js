const test = require("node:test");
const assert = require("node:assert/strict");
const gpt = require("../../src/oms_hub/web/static/gpt.js");

function setup(fetchImpl) {
  const element = () => ({ value: "", children: [], events: {}, disabled: false,
    addEventListener(event, handler) { this.events[event] = handler; },
    replaceChildren() { this.children = []; }, append(item) { this.children.push(item); },
  });
  const fields = Object.fromEntries(["select", "name", "message", "apply", "save", "delete"].map((key) => [key, element()]));
  const controls = { querySelector: (selector) => fields[selector.match(/data-preset-(.+)\]/)[1]] };
  const instructions = { value: "My unsaved instructions" };
  const form = { elements: { instructions }, querySelector: () => controls };
  const document = { cookie: "study_hub_csrf=token", createElement: element };
  return { fields, instructions, ready: gpt.initializePresets(form, document, fetchImpl) };
}
const ok = (payload) => ({ ok: true, json: async () => payload });

test("loading and selecting preserve typed instructions; explicit apply never generates", async () => {
  const calls = [];
  const ui = setup(async (url, options) => {
    calls.push({ url, options });
    return ok({ presets: [{ id: "a", name: "<Teacher>", instructions: "Saved instructions" }] });
  });
  await ui.ready;
  assert.equal(ui.instructions.value, "My unsaved instructions");
  let prevented = false;
  ui.fields.name.events.keydown({ key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  ui.fields.select.value = "a";
  ui.fields.select.events.change();
  assert.equal(ui.fields.name.value, "<Teacher>");
  assert.equal(ui.instructions.value, "My unsaved instructions");
  ui.fields.apply.events.click();
  assert.equal(ui.instructions.value, "Saved instructions");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "GET");
  assert.equal(ui.fields.select.children[1].textContent, "<Teacher>");
});

test("save creates then renames selected preset; deletion and network failure retain edits", async () => {
  const calls = [];
  let fail = false;
  const ui = setup(async (url, options) => {
    calls.push({ url, options });
    if (fail) throw new Error("Disconnected");
    if (options.method === "GET") return ok({ presets: [] });
    if (options.method === "DELETE") return ok({ deleted: true });
    return ok({ id: "saved", ...JSON.parse(options.body) });
  });
  await ui.ready;
  ui.fields.name.value = "Teacher";
  await ui.fields.save.events.click();
  assert.equal(calls[1].options.method, "POST");
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "token");
  assert.equal(calls[1].options.credentials, "same-origin");
  assert.equal(ui.fields.select.value, "saved");
  ui.fields.name.value = "Renamed";
  ui.instructions.value = "Updated";
  await ui.fields.save.events.click();
  assert.equal(calls[2].options.method, "PUT");
  assert.equal(calls[2].url, "/study/quiz-presets/saved");
  await ui.fields.delete.events.click();
  assert.equal(calls[3].options.method, "DELETE");
  assert.equal(ui.instructions.value, "Updated");
  assert.equal(ui.fields.select.value, "");
  fail = true;
  await ui.fields.save.events.click();
  assert.equal(ui.instructions.value, "Updated");
  assert.equal(ui.fields.save.disabled, false);
  assert.equal(ui.fields.message.textContent, "Disconnected");
  assert.ok(calls.every((call) => call.url.startsWith("/study/quiz-presets")));
});

test("malformed preset response leaves controls and typed instructions usable", async () => {
  const ui = setup(async () => ok({ presets: null }));
  await ui.ready;
  assert.equal(ui.instructions.value, "My unsaved instructions");
  assert.equal(ui.fields.name.disabled, false);
  assert.equal(ui.fields.save.disabled, false);
  assert.equal(ui.fields.apply.disabled, true);
  assert.match(ui.fields.message.textContent, /Unable to load saved presets/);
});
