((root) => {
  "use strict";

  const progressKey = (token, version) => (
    `oms-study-hub-quiz:${token}:v${version}`
  );

  const progressLabel = (value, version) => {
    if (!value || Number(value.version) !== Number(version)) return "Not started";
    const questions = Object.values(value.questions || {});
    const answered = questions.filter((question) => question?.submitted).length;
    const interacted = questions.some((question) => (
      Boolean(question?.selectedChoiceId)
      || (question?.eliminatedChoiceIds || []).length > 0
      || (question?.highlights || []).length > 0
    ));
    const total = questions.length;
    if (
      total > 0
      && (
        answered >= total
        || Number(value.currentIndex || 0) >= total
      )
    ) return "Completed";
    if (
      answered > 0
      || interacted
      || Number(value.currentIndex || 0) > 0
    ) return "In progress";
    return "Not started";
  };

  const readProgress = (storage, token, version) => {
    try {
      return progressLabel(
        JSON.parse(storage.getItem(progressKey(token, version))),
        version,
      );
    } catch (_error) {
      return "Not started";
    }
  };

  const resetProgress = (storage, token, version) => {
    storage.removeItem(progressKey(token, version));
  };

  const setExpanded = (button, expanded) => {
    button.setAttribute("aria-expanded", String(expanded));
    const panel = button.ownerDocument.getElementById(
      button.getAttribute("aria-controls"),
    );
    if (panel) panel.hidden = !expanded;
  };

  const initialize = (documentRef, storage) => {
    documentRef.querySelectorAll(".disclosure").forEach((button) => {
      button.addEventListener("click", () => {
        setExpanded(button, button.getAttribute("aria-expanded") !== "true");
      });
    });
    const refresh = () => {
      documentRef.querySelectorAll("[data-quiz-row]").forEach((row) => {
        row.querySelector("[data-quiz-progress]").textContent = readProgress(
          storage,
          row.dataset.quizToken,
          row.dataset.quizVersion,
        );
      });
    };
    documentRef.querySelectorAll("[data-reset-quiz]").forEach((button) => {
      button.addEventListener("click", () => {
        try {
          resetProgress(
            storage,
            button.dataset.quizToken,
            button.dataset.quizVersion,
          );
          refresh();
          documentRef.querySelector("[data-reset-message]").textContent =
            "That quiz's progress was reset on this browser.";
        } catch (_error) {
          documentRef.querySelector("[data-reset-message]").textContent =
            "Quiz progress could not be reset.";
        }
      });
    });
    refresh();
    const reset = documentRef.querySelector("[data-reset-progress]");
    if (reset) {
      reset.addEventListener("click", () => {
        try {
          const keys = [];
          for (let index = 0; index < storage.length; index += 1) {
            const key = storage.key(index);
            if (key && key.startsWith("oms-study-hub-quiz:")) keys.push(key);
          }
          keys.forEach((key) => storage.removeItem(key));
          refresh();
          documentRef.querySelector("[data-reset-message]").textContent =
            "Quiz progress was reset on this browser.";
        } catch (_error) {
          documentRef.querySelector("[data-reset-message]").textContent =
            "Quiz progress could not be reset.";
        }
      });
    }
  };

  const api = {
    initialize,
    progressKey,
    progressLabel,
    readProgress,
    resetProgress,
    setExpanded,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => {
      initialize(root.document, root.localStorage);
    }, { once: true });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
