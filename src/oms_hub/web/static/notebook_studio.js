((root) => {
  "use strict";

  const normalizeSubject = (value) => value.trim().toLowerCase().replace(/\s+/g, " ");

  const csrf = (documentRef) => {
    const item = documentRef.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("study_hub_csrf="));
    return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
  };

  const hasActiveSources = (sources) => sources.some(
    (source) => source.state === "pending" || source.state === "attaching",
  );

  const renderSources = (documentRef, list, sources) => {
    list.replaceChildren();
    if (!sources.length) {
      const row = documentRef.createElement("li");
      row.textContent = "No sources attached yet.";
      list.append(row);
      return false;
    }
    sources.forEach((source) => {
      const row = documentRef.createElement("li");
      const converted = source.converted_from_pptx ? " · converted from PPTX" : "";
      const error = source.error ? ` · ${source.error}` : "";
      row.textContent = `${source.title} · ${source.type} · ${source.state}${converted}${error}`;
      list.append(row);
    });
    return hasActiveSources(sources);
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const page = documentRef.querySelector("[data-studio-page]");
    if (!page) return;
    const course = page.querySelector("[data-studio-course]");
    const exam = page.querySelector("[data-studio-exam]");
    const list = page.querySelector("[data-source-list]");
    let pollHandle = null;

    const scheduleRefresh = () => {
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = root.setTimeout(refresh, 2000);
    };

    const refresh = async () => {
      pollHandle = null;
      if (!course.value || !exam.value) return;
      try {
        const subjectKey = normalizeSubject(course.value);
        const response = await fetchImpl(
          `/studio/sources?subject_key=${encodeURIComponent(subjectKey)}&exam_number=${encodeURIComponent(exam.value)}`,
          { cache: "no-store" },
        );
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Sources could not be loaded.");
        if (renderSources(documentRef, list, payload.sources || [])) scheduleRefresh();
      } catch (error) {
        list.textContent = error instanceof Error ? error.message : "Sources could not be loaded.";
      }
    };

    course.addEventListener("change", () => {
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = null;
      exam.replaceChildren();
      const placeholder = documentRef.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Select exam";
      exam.append(placeholder);
      const selected = course.options[course.selectedIndex];
      String(selected?.dataset.exams || "")
        .split(",")
        .filter(Boolean)
        .forEach((number) => {
          const option = documentRef.createElement("option");
          option.value = number;
          option.textContent = `Exam ${number}`;
          exam.append(option);
        });
      exam.disabled = !course.value;
      list.textContent = "Select an exam to view sources.";
    });
    exam.addEventListener("change", refresh);

    page.querySelectorAll("[data-source-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const message = form.querySelector("[data-form-message]");
        if (!course.value || !exam.value) {
          message.textContent = "Select a course and exam first.";
          return;
        }
        const token = csrf(documentRef);
        const body = new FormData(form);
        body.append("subject", course.value);
        body.append("exam_number", exam.value);
        body.append("csrf_token", token);
        try {
          const response = await fetchImpl(`/studio/sources/${form.dataset.sourceType}`, {
            method: "POST",
            headers: { "X-CSRF-Token": token },
            body,
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(payload.detail || "Source could not be queued.");
          message.textContent = "Source queued for attachment.";
          form.reset();
          await refresh();
        } catch (error) {
          message.textContent = error instanceof Error ? error.message : "Source could not be queued.";
        }
      });
    });
  };

  const api = { hasActiveSources, initialize, normalizeSubject, renderSources };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
