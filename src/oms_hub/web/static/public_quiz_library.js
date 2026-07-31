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
    if (!storage) return "Not started";
    try {
      return progressLabel(
        JSON.parse(storage.getItem(progressKey(token, version))),
        version,
      );
    } catch (_error) {
      return "Not started";
    }
  };

  const setExpanded = (button, expanded) => {
    button.setAttribute("aria-expanded", String(expanded));
    const panel = button.ownerDocument.getElementById(
      button.getAttribute("aria-controls"),
    );
    if (panel) panel.hidden = !expanded;
  };

  const acquireStorage = (view) => {
    try {
      return view?.localStorage || null;
    } catch (_error) {
      return null;
    }
  };

  const resetProgress = (storage, confirmImpl) => {
    if (!confirmImpl("Reset all quiz progress stored in this browser?")) {
      return "cancelled";
    }
    try {
      if (!storage) throw new Error("browser storage unavailable");
      const keys = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.startsWith("oms-study-hub-quiz:")) keys.push(key);
      }
      keys.forEach((key) => storage.removeItem(key));
      return "reset";
    } catch (_error) {
      return "failed";
    }
  };

  const initialize = (
    documentRef,
    storage,
    confirmImpl = () => false,
  ) => {
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
    refresh();
    const reset = documentRef.querySelector("[data-reset-progress]");
    if (reset) {
      reset.addEventListener("click", () => {
        const result = resetProgress(storage, confirmImpl);
        if (result === "cancelled") return;
        if (result === "reset") {
          refresh();
          documentRef.querySelector("[data-reset-message]").textContent =
            "Quiz progress was reset on this browser.";
        } else {
          documentRef.querySelector("[data-reset-message]").textContent =
            "Quiz progress could not be reset.";
        }
      });
    }
  };

  const api = {
    acquireStorage,
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
      initialize(
        root.document,
        acquireStorage(root),
        typeof root.confirm === "function"
          ? root.confirm.bind(root)
          : () => false,
      );
    }, { once: true });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
