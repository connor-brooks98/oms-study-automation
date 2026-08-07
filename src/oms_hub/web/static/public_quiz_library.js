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

  const cookieValue = (cookie, name) => {
    const prefix = `${name}=`;
    const value = String(cookie || "").split(";").map((part) => part.trim())
      .find((part) => part.startsWith(prefix));
    return value ? decodeURIComponent(value.slice(prefix.length)) : null;
  };

  const setExpanded = (button, expanded) => {
    button.setAttribute("aria-expanded", String(expanded));
    const panel = button.ownerDocument.getElementById(
      button.getAttribute("aria-controls"),
    );
    if (panel) panel.hidden = !expanded;
  };

  const managementRequest = async (documentRef, button, url, body) => {
    button.disabled = true;
    try {
      const response = await root.fetch(url, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "",
        },
        body: JSON.stringify(body),
      });
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        // The status fallback below is clearer than a JSON parsing error.
      }
      if (!response.ok) {
        throw new Error(payload.detail || "Quiz management update failed.");
      }
      root.location?.reload?.();
    } catch (error) {
      button.disabled = false;
      documentRef.querySelector("[data-reset-message]").textContent =
        error instanceof Error ? error.message : "Quiz management update failed.";
    }
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
        if (
          typeof root.confirm === "function"
          && !root.confirm("Reset this quiz on this browser?")
        ) {
          return;
        }
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
    documentRef.querySelectorAll("[data-remove-quiz]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (
          typeof root.confirm === "function"
          && !root.confirm(
            "Remove this released quiz? Its source and run history will be preserved.",
          )
        ) {
          return;
        }
        button.disabled = true;
        try {
          const response = await root.fetch(button.dataset.removeUrl, {
            method: "DELETE",
            headers: {
              "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "",
            },
          });
          let payload = {};
          try {
            payload = await response.json();
          } catch (_error) {
            // The status fallback below is clearer than a JSON parsing error.
          }
          if (!response.ok) {
            throw new Error(payload.detail || "Quiz could not be unpublished.");
          }
          resetProgress(
            storage,
            button.dataset.quizToken,
            button.dataset.quizVersion,
          );
          button.closest(".lecture-row")?.remove();
          documentRef.querySelector("[data-reset-message]").textContent =
            "The released quiz was removed.";
        } catch (error) {
          button.disabled = false;
          documentRef.querySelector("[data-reset-message]").textContent =
            error instanceof Error ? error.message : "Quiz could not be unpublished.";
        }
      });
    });
    documentRef.querySelectorAll("[data-edit-quiz-title]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (typeof root.prompt !== "function") return;
        const title = root.prompt("Edit quiz title", button.dataset.title || "");
        if (title === null) return;
        const cleanedTitle = title.trim();
        if (!cleanedTitle) {
          documentRef.querySelector("[data-reset-message]").textContent =
            "Quiz title cannot be blank.";
          return;
        }
        await managementRequest(documentRef, button, button.dataset.titleUrl, {
          title: cleanedTitle,
        });
      });
    });
    documentRef.querySelectorAll("[data-move-quiz-library]").forEach((button) => {
      button.addEventListener("click", async () => {
        await managementRequest(documentRef, button, button.dataset.libraryUrl, {
          section: button.dataset.targetSection,
        });
      });
    });
    documentRef.querySelectorAll("[data-move-quiz-order]").forEach((button) => {
      button.addEventListener("click", async () => {
        await managementRequest(documentRef, button, button.dataset.orderUrl, {
          direction: button.dataset.direction,
        });
      });
    });
    refresh();
    const reset = documentRef.querySelector("[data-reset-progress]");
    if (reset) {
      reset.addEventListener("click", () => {
        if (
          typeof root.confirm === "function"
          && !root.confirm("Reset all quiz progress on this browser?")
        ) {
          return;
        }
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
    cookieValue,
    managementRequest,
    setExpanded,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => {
      initialize(root.document, root.localStorage);
    }, { once: true });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
