const test = require("node:test");
const assert = require("node:assert/strict");

const examPasses = require("../../src/oms_hub/web/static/exam_passes.js");

class FakeElement {
  constructor() {
    this.dataset = {};
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.listeners = {};
    this.children = {};
    this.classList = {
      add() {}, remove() {},
    };
  }

  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }

  async dispatch(type) {
    for (const listener of this.listeners[type] || []) await listener({ preventDefault() {} });
  }

  querySelector(selector) { return this.children[selector] || null; }

  showPicker() { this.showPickerCalls = (this.showPickerCalls || 0) + 1; }
}

const pageFixture = () => {
  const page = new FakeElement();
  page.dataset = { subject: "Heme/Lymph", examNumber: "2", examDate: "" };
  const open = new FakeElement();
  const form = new FakeElement();
  const input = new FakeElement();
  const date = new FakeElement();
  const state = new FakeElement();
  const days = new FakeElement();
  const hours = new FakeElement();
  const feedback = new FakeElement();
  const dialogTitle = new FakeElement();
  dialogTitle.textContent = "Set exam date";
  const documentRef = {
    cookie: "study_hub_csrf=csrf-token",
    querySelector(selector) {
      return ({
        "[data-exam-page]": page,
        "[data-open-date]": open,
        "[data-exam-date-form]": form,
        "[data-exam-date-input]": input,
        "[data-exam-date-label]": date,
        "[data-countdown-state]": state,
        "[data-countdown-days]": days,
        "[data-countdown-hours]": hours,
        "[data-exam-date-feedback]": feedback,
        "[data-exam-date-dialog-title]": dialogTitle,
      })[selector] || null;
    },
  };
  return { date, days, dialogTitle, documentRef, feedback, form, hours, input, open, page, state };
};

test("countdown uses local 8:00 AM without UTC date parsing", () => {
  const target = examPasses.localDateAtEight("2026-09-02");

  assert.equal(target.getFullYear(), 2026);
  assert.equal(target.getMonth(), 8);
  assert.equal(target.getDate(), 2);
  assert.equal(target.getHours(), 8);
  assert.equal(target.getMinutes(), 0);
  assert.deepEqual(
    examPasses.countdownParts("2026-09-02", new Date(2026, 7, 31, 9, 0)),
    { days: 1, hours: 23, state: "future" },
  );
});

test("countdown distinguishes missing, invalid, past, and reached exam dates", () => {
  const now = new Date(2026, 7, 31, 8, 1);

  assert.deepEqual(examPasses.countdownParts("", now), { days: 0, hours: 0, state: "missing" });
  assert.deepEqual(examPasses.countdownParts("not-a-date", now), { days: 0, hours: 0, state: "invalid" });
  assert.deepEqual(examPasses.countdownParts("2026-08-30", now), { days: 0, hours: 0, state: "past" });
  assert.deepEqual(
    examPasses.countdownParts("2026-08-31", now),
    { days: 0, hours: 0, state: "exam-day-reached" },
  );
});

test("initializer seeds a missing date and saves it with the CSRF token", async (t) => {
  const fixture = pageFixture();
  const requests = [];
  const fetchImpl = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, async json() { return { exam_date: "2026-09-02" }; } };
  };

  const cleanup = examPasses.initialize(
    fixture.documentRef, fetchImpl, () => new Date(2026, 7, 31, 9, 0),
  );
  t.after(cleanup);
  assert.equal(fixture.date.textContent, "No date selected");
  await fixture.open.dispatch("click");
  assert.equal(fixture.input.value, "2026-08-31");
  assert.equal(fixture.input.showPickerCalls, 1);

  fixture.input.value = "2026-09-02";
  await fixture.form.dispatch("submit");

  assert.deepEqual(requests, [{
    url: "/api/lectures/exams/2/date?subject=Heme%2FLymph",
    options: {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf-token" },
      body: '{"exam_date":"2026-09-02"}',
      cache: "no-store",
    },
  }]);
  assert.equal(fixture.page.dataset.examDate, "2026-09-02");
  assert.equal(fixture.date.textContent, "Sep 2, 2026");
  assert.equal(fixture.feedback.textContent, "Exam date saved.");
});

test("opening restores the saved date and discards an unsaved edit", async (t) => {
  const fixture = pageFixture();
  fixture.page.dataset.examDate = "2026-09-02";
  const cleanup = examPasses.initialize(
    fixture.documentRef, undefined, () => new Date(2026, 7, 31, 9, 0),
  );
  t.after(cleanup);

  await fixture.open.dispatch("click");
  assert.equal(fixture.input.value, "2026-09-02");
  assert.equal(fixture.dialogTitle.textContent, "Change exam date");

  fixture.input.value = "2026-10-01";
  await fixture.open.dispatch("click");
  assert.equal(fixture.input.value, "2026-09-02");
  assert.equal(fixture.input.showPickerCalls, 2);
});
