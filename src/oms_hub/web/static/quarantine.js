((root) => {
  "use strict";

  const csrfToken = (documentRef) => {
    const match = documentRef.cookie.split(";").map((value) => value.trim())
      .find((value) => value.startsWith("study_hub_csrf="));
    return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : "";
  };

  const groupedLectures = (options) => {
    const groups = {};
    options.forEach((option) => {
      const subject = option.dataset.subject;
      const exam = option.dataset.exam;
      groups[subject] ||= {};
      groups[subject][exam] ||= [];
      groups[subject][exam].push({
        id: option.value,
        label: option.textContent,
      });
    });
    return groups;
  };

  const replaceOptions = (documentRef, select, label, values) => {
    select.replaceChildren();
    const placeholder = documentRef.createElement("option");
    placeholder.value = "";
    placeholder.textContent = label;
    select.append(placeholder);
    values.forEach(({ value, text }) => {
      const option = documentRef.createElement("option");
      option.value = value;
      option.textContent = text;
      select.append(option);
    });
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const page = documentRef.querySelector("[data-quarantine-page]");
    if (!page) return;
    const source = page.querySelector("[data-lecture-source]");
    if (!source) return;
    const groups = groupedLectures(Array.from(source.options));
    const course = page.querySelector("[data-course-select]");
    const exam = page.querySelector("[data-exam-select]");
    const lecture = page.querySelector("[data-lecture-select]");
    replaceOptions(documentRef, course, "Select course", Object.keys(groups).sort().map((value) => ({ value, text: value })));
    course.addEventListener("change", () => {
      const exams = groups[course.value] || {};
      replaceOptions(documentRef, exam, "Select exam", Object.keys(exams).sort((a, b) => Number(a) - Number(b)).map((value) => ({ value, text: `Exam ${value}` })));
      exam.disabled = !course.value;
      replaceOptions(documentRef, lecture, "Select lecture", []);
      lecture.disabled = true;
    });
    exam.addEventListener("change", () => {
      const values = groups[course.value]?.[exam.value] || [];
      replaceOptions(documentRef, lecture, "Select lecture", values.map((item) => ({ value: item.id, text: item.label })));
      lecture.disabled = !exam.value;
    });
    page.querySelector("[data-select-all]").addEventListener("click", () => {
      page.querySelectorAll("[data-item-select]").forEach((input) => { input.checked = true; });
    });
    page.querySelector("[data-assign-selected]").addEventListener("click", async () => {
      const cards = Array.from(page.querySelectorAll("[data-quarantine-item]")).filter((card) => card.querySelector("[data-item-select]").checked);
      const message = page.querySelector("[data-quarantine-message]");
      if (!cards.length || !lecture.value) { message.textContent = "Select files and a lecture first."; return; }
      try {
        const response = await fetchImpl("/quarantine/assign", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken(documentRef) },
          body: JSON.stringify({ item_ids: cards.map((card) => card.dataset.itemId), lecture_id: Number(lecture.value) }),
          cache: "no-store",
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Assignment could not be saved.");
        const completed = new Set(payload.items.map((item) => item.id));
        cards.filter((card) => completed.has(card.dataset.itemId)).forEach((card) => card.remove());
        page.querySelector("[data-waiting-count]").textContent = String(page.querySelectorAll("[data-quarantine-item]").length);
        message.textContent = `${completed.size} file${completed.size === 1 ? "" : "s"} assigned.`;
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Assignment could not be saved.";
      }
    });
  };

  const api = { groupedLectures, initialize, replaceOptions };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
})(typeof globalThis === "undefined" ? this : globalThis);
