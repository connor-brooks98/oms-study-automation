((root) => {
  "use strict";

  const storagePrefix = "study-hub:disclosure:";
  const recentLectureKey = "study-hub:recent-lecture";

  const nestedExpanded = (parentOpen, saved) => (
    parentOpen && saved === "true"
  );

  const setExpanded = (documentRef, button, expanded) => {
    const target = documentRef.getElementById(button.getAttribute("aria-controls"));
    if (!target) return;
    button.setAttribute("aria-expanded", String(expanded));
    button.querySelector(".sh-disclose")?.classList.toggle("is-open", expanded);
    target.hidden = !expanded;
  };

  const collapseDescendants = (documentRef, storage, courseButton) => {
    const panel = documentRef.getElementById(courseButton.getAttribute("aria-controls"));
    panel?.querySelectorAll(".exam-toggle").forEach((examButton) => {
      setExpanded(documentRef, examButton, false);
      try {
        storage?.setItem(storagePrefix + examButton.dataset.storageKey, "false");
      } catch (_) {
        // Disclosure behavior does not depend on persistence.
      }
    });
  };

  const savedState = (storage, button) => {
    try {
      const saved = storage?.getItem(storagePrefix + button.dataset.storageKey);
      return saved === null || saved === undefined ? undefined : saved === "true";
    } catch (_) {
      return undefined;
    }
  };

  const initializeRecentLecture = (documentRef, storage) => {
    const container = documentRef.querySelector?.("[data-recent-lecture]");
    if (!container) return;
    try {
      const lectures = JSON.parse(container.dataset.lectures || "[]");
      const recent = JSON.parse(storage?.getItem(recentLectureKey) || "null");
      const valid = lectures.find((lecture) => lecture.id === recent?.id);
      if (!valid) return;
      container.querySelector("[data-recent-title]").textContent = `Continue ${valid.title}`;
      container.querySelector("[data-recent-copy]").textContent = "Return to your most recently opened lecture workspace.";
      const link = container.querySelector("[data-recent-link]");
      link.href = `/lectures/${valid.id}`;
      link.textContent = "Resume lecture";
    } catch (_) {
      // The neutral library shortcut remains usable when storage is unavailable.
    }
  };

  const filterLectures = (documentRef, query) => {
    const normalized = query.trim().toLowerCase();
    const rows = [...documentRef.querySelectorAll("[data-lecture-row]")];
    let matches = 0;
    rows.forEach((row) => {
      const visible = !normalized || row.dataset.searchText.toLowerCase().includes(normalized);
      row.hidden = !visible;
      if (!visible) return;
      matches += 1;
      if (normalized) {
        const exam = row.closest(".exam-group");
        const course = row.closest(".course-group");
        const examButton = exam?.querySelector(".exam-toggle");
        const courseButton = course?.querySelector(".course-toggle");
        if (courseButton) setExpanded(documentRef, courseButton, true);
        if (examButton) setExpanded(documentRef, examButton, true);
      }
    });
    documentRef.querySelectorAll(".exam-group").forEach((exam) => {
      exam.hidden = Boolean(normalized) && !exam.querySelector("[data-lecture-row]:not([hidden])");
    });
    documentRef.querySelectorAll(".course-group").forEach((course) => {
      course.hidden = Boolean(normalized) && !course.querySelector("[data-lecture-row]:not([hidden])");
    });
    return { matches, total: rows.length };
  };

  const initialize = (documentRef, suppliedStorage) => {
    let storage = suppliedStorage;
    if (storage === undefined) {
      try {
        storage = root.sessionStorage;
      } catch (_) {
        storage = undefined;
      }
    }
    let recentStorage;
    try {
      recentStorage = root.localStorage;
    } catch (_) {
      recentStorage = undefined;
    }
    initializeRecentLecture(documentRef, recentStorage);
    const buttons = [...documentRef.querySelectorAll("[data-disclosure]")];
    let disclosureSnapshot;
    const search = documentRef.querySelector?.("[data-lecture-search]");
    const searchStatus = documentRef.querySelector?.("[data-lecture-search-status]");
    search?.addEventListener("input", () => {
      const searching = Boolean(search.value.trim());
      if (searching && !disclosureSnapshot) {
        disclosureSnapshot = new Map(buttons.map((button) => [
          button,
          button.getAttribute("aria-expanded") === "true",
        ]));
      }
      const result = filterLectures(documentRef, search.value);
      if (!searching && disclosureSnapshot) {
        disclosureSnapshot.forEach((expanded, button) => setExpanded(documentRef, button, expanded));
        disclosureSnapshot = undefined;
      }
      if (searchStatus) searchStatus.textContent = searching
        ? `${result.matches} of ${result.total} lectures match.`
        : "";
    });
    const courses = buttons.filter((button) => button.classList.contains("course-toggle"));
    const exams = buttons.filter((button) => button.classList.contains("exam-toggle"));
    courses.forEach((button) => {
      const restored = savedState(storage, button);
      setExpanded(
        documentRef,
        button,
        restored === undefined
          ? button.getAttribute("aria-expanded") === "true"
          : restored,
      );
    });
    exams.forEach((button) => {
      const restored = savedState(storage, button);
      const parentCourse = button.closest(".course-group")?.querySelector(".course-toggle");
      const parentOpen = parentCourse?.getAttribute("aria-expanded") === "true";
      const requested = restored === undefined
        ? button.getAttribute("aria-expanded") === "true"
        : restored;
      setExpanded(documentRef, button, nestedExpanded(parentOpen, String(requested)));
    });
    buttons.forEach((button) => {
      const key = storagePrefix + button.dataset.storageKey;
      button.addEventListener("click", () => {
        const expanded = button.getAttribute("aria-expanded") !== "true";
        setExpanded(documentRef, button, expanded);
        if (button.classList.contains("course-toggle") && !expanded) {
          collapseDescendants(documentRef, storage, button);
        }
        try {
          storage?.setItem(key, String(expanded));
        } catch (_) {
          // Disclosure behavior does not depend on persistence.
        }
      });
    });
  };

  const api = { collapseDescendants, filterLectures, initialize, initializeRecentLecture, nestedExpanded, savedState, setExpanded };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) initialize(root.document);
})(typeof globalThis === "undefined" ? this : globalThis);
