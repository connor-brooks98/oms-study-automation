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

  const initialize = (documentRef, storage = root.sessionStorage) => {
    documentRef.querySelectorAll("[data-disclosure]").forEach((button) => {
      const key = storagePrefix + button.dataset.storageKey;
      try {
        const saved = storage?.getItem(key);
        const parentCourse = button.closest(".course-group")?.querySelector(".course-toggle");
        const parentOpen = !parentCourse || parentCourse.getAttribute("aria-expanded") === "true";
        if (saved !== null && saved !== undefined) {
          setExpanded(
            documentRef,
            button,
            parentCourse ? nestedExpanded(parentOpen, saved) : saved === "true",
          );
        }
      } catch (_) {
        // Storage can be unavailable in privacy modes; server defaults still work.
      }
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

  const api = { collapseDescendants, initialize, nestedExpanded, setExpanded };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) initialize(root.document);
})(typeof globalThis === "undefined" ? this : globalThis);
