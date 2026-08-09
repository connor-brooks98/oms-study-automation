((root) => {
  "use strict";

  const storagePrefix = "study-hub:disclosure:";

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

  const initialize = (documentRef, suppliedStorage) => {
    let storage = suppliedStorage;
    if (storage === undefined) {
      try {
        storage = root.sessionStorage;
      } catch (_) {
        storage = undefined;
      }
    }
    const buttons = [...documentRef.querySelectorAll("[data-disclosure]")];
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

  const api = { collapseDescendants, initialize, nestedExpanded, savedState, setExpanded };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) initialize(root.document);
})(typeof globalThis === "undefined" ? this : globalThis);
