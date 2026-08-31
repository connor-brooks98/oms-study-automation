((root) => {
  "use strict";

  const csrfToken = (documentRef) => {
    const prefix = "study_hub_csrf=";
    const cookie = String(documentRef?.cookie || "").split(";").map((value) => value.trim())
      .find((value) => value.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  };

  const localDateValue = (date = new Date()) => [
    date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0"),
  ].join("-");

  const localDateAtEight = (value) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
    if (!match) return new Date(NaN);
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 8, 0, 0, 0);
    return date.getFullYear() === Number(match[1]) && date.getMonth() === Number(match[2]) - 1 && date.getDate() === Number(match[3])
      ? date : new Date(NaN);
  };

  const countdownParts = (examDate, now = new Date()) => {
    if (!examDate) return { days: 0, hours: 0, state: "missing" };
    const target = localDateAtEight(examDate);
    if (Number.isNaN(target.getTime())) return { days: 0, hours: 0, state: "invalid" };
    if (now >= target) {
      const sameDay = localDateValue(now) === localDateValue(target);
      return { days: 0, hours: 0, state: sameDay ? "exam-day-reached" : "past" };
    }
    const hoursRemaining = Math.floor((target - now) / 3_600_000);
    return { days: Math.floor(hoursRemaining / 24), hours: hoursRemaining % 24, state: "future" };
  };

  const formatDate = (value) => {
    const date = localDateAtEight(value);
    return Number.isNaN(date.getTime()) ? "No date selected" : date.toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric",
    });
  };

  const reflowNumber = (element, value) => {
    if (!element) return;
    element.textContent = String(value);
    element.classList?.remove("t-number");
    void element.offsetWidth;
    element.classList?.add("t-number");
  };

  const initialize = (documentRef, fetchImpl = root.fetch?.bind(root), now = () => new Date()) => {
    const page = documentRef?.querySelector?.("[data-exam-page]");
    if (!page) return () => {};
    const date = documentRef.querySelector("[data-exam-date-label]");
    const state = documentRef.querySelector("[data-countdown-state]");
    const days = documentRef.querySelector("[data-countdown-days]");
    const hours = documentRef.querySelector("[data-countdown-hours]");
    const open = documentRef.querySelector("[data-open-date]");
    const form = documentRef.querySelector("[data-exam-date-form]");
    const input = documentRef.querySelector("[data-exam-date-input]");
    const feedback = documentRef.querySelector("[data-exam-date-feedback]");
    const dialogTitle = documentRef.querySelector("[data-exam-date-dialog-title]");

    const render = () => {
      const parts = countdownParts(page.dataset.examDate, now());
      reflowNumber(days, parts.state === "future" ? parts.days : "—");
      reflowNumber(hours, parts.state === "future" ? parts.hours : "—");
      if (date) date.textContent = page.dataset.examDate ? formatDate(page.dataset.examDate) : (page.dataset.examDateConflict === "true" ? "Dates differ" : "No date selected");
      if (state) {
        state.textContent = {
          future: "Time remaining until your 8:00 AM target.",
          "exam-day-reached": "Exam day has reached its 8:00 AM target.",
          past: "This exam date has passed.",
          invalid: "Set a valid exam date.",
          missing: page.dataset.examDateConflict === "true" ? "Exam dates differ. Set one date for this exam." : "No exam date set.",
        }[parts.state];
      }
      if (open) open.textContent = page.dataset.examDate ? "Change Exam Date" : "Set Exam Date";
    };

    open?.addEventListener("click", () => {
      if (input) input.value = page.dataset.examDate || localDateValue(now());
      if (dialogTitle) dialogTitle.textContent = page.dataset.examDate ? "Change exam date" : "Set exam date";
      try { input?.showPicker?.(); } catch (_) { /* Native date picker is optional. */ }
    });

    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = input?.value || "";
      if (Number.isNaN(localDateAtEight(value).getTime())) {
        if (feedback) feedback.textContent = "Choose a valid exam date.";
        return;
      }
      try {
        const response = await fetchImpl(
          `/api/lectures/exams/${encodeURIComponent(page.dataset.examNumber)}/date?subject=${encodeURIComponent(page.dataset.subject)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken(documentRef) },
            body: JSON.stringify({ exam_date: value }),
            cache: "no-store",
          },
        );
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "Exam date could not be saved.");
        page.dataset.examDate = payload.exam_date;
        page.dataset.examDateConflict = "false";
        if (feedback) feedback.textContent = "Exam date saved.";
        render();
        documentRef.getElementById?.("exam-date-dialog")?.querySelector("[data-close-dialog]")?.click();
      } catch (error) {
        if (feedback) feedback.textContent = error.message || "Exam date could not be saved.";
      }
    });

    render();
    const timer = root.setInterval?.(render, 60_000);
    return () => root.clearInterval?.(timer);
  };

  const api = { countdownParts, csrfToken, initialize, localDateAtEight, localDateValue };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
    else initialize(root.document);
  }
})(typeof globalThis === "undefined" ? this : globalThis);
