((root) => {
  "use strict";

  const progressKey = (token, version) => `oms-study-hub-quiz:${token}:v${version}`;

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
    if (total > 0 && (answered >= total || Number(value.currentIndex || 0) >= total)) {
      return "Complete";
    }
    if (answered > 0 || interacted || Number(value.currentIndex || 0) > 0) return "In progress";
    return "Not started";
  };

  const progressClass = (label) => ({
    "In progress": "sh-pill--info",
    Complete: "sh-pill--ok",
  }[label] || "");

  const readProgress = (storage, token, version) => {
    try {
      return progressLabel(JSON.parse(storage.getItem(progressKey(token, version))), version);
    } catch (_error) {
      return "Not started";
    }
  };

  const resetProgress = (storage, token, version) => storage.removeItem(progressKey(token, version));

  const cookieValue = (cookie, name) => {
    const prefix = `${name}=`;
    const value = String(cookie || "").split(";").map((part) => part.trim())
      .find((part) => part.startsWith(prefix));
    return value ? decodeURIComponent(value.slice(prefix.length)) : null;
  };

  const setExpanded = (button, expanded) => {
    button.setAttribute("aria-expanded", String(expanded));
    button.querySelector(".sh-disclose")?.classList.toggle("is-open", expanded);
    const panel = button.ownerDocument.getElementById(button.getAttribute("aria-controls"));
    if (panel) panel.hidden = !expanded;
  };

  const errorMessage = async (response, fallback) => {
    try {
      const payload = await response.json();
      return payload.detail || fallback;
    } catch (_error) {
      return fallback;
    }
  };

  const messageNode = (documentRef) => documentRef.querySelector("[data-reset-message]");
  const report = (documentRef, message) => { messageNode(documentRef).textContent = message; };
  const reorderFailureStorageKey = "oms-study-hub-quiz-reorder-failure";

  const storeReorderFailure = (storage, message) => {
    try {
      storage?.setItem(reorderFailureStorageKey, message);
    } catch (_error) {
      // Reload remains safer than a stale order even if session storage is unavailable.
    }
  };

  const consumeReorderFailure = (documentRef, storage) => {
    try {
      const message = storage?.getItem(reorderFailureStorageKey);
      if (!message) return;
      storage.removeItem(reorderFailureStorageKey);
      report(documentRef, message);
    } catch (_error) {
      // A storage failure must not prevent the library from loading.
    }
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
      if (!response.ok) throw new Error(await errorMessage(response, "Quiz management update failed."));
      root.location?.reload?.();
    } catch (error) {
      button.disabled = false;
      report(documentRef, error instanceof Error ? error.message : "Quiz management update failed.");
    }
  };

  const quizCountLabel = (count) => `${count} quiz${count === 1 ? "" : "zes"}`;

  const applyRenamedTitle = (documentRef, token, title, focusTarget) => {
    documentRef.querySelectorAll?.(`[data-quiz-title-for="${token}"]`).forEach((surface) => {
      if (surface.dataset?.quizDragHandle !== undefined) {
        surface.setAttribute?.("aria-label", `Reorder ${title}. Use Arrow Up or Arrow Down.`);
      } else if (surface.dataset?.resetQuiz !== undefined) {
        surface.setAttribute?.("aria-label", `Restart ${title}`);
        surface.setAttribute?.("title", `Restart ${title}`);
      } else if (surface.tagName === "SUMMARY") {
        surface.setAttribute?.("aria-label", `More actions for ${title}`);
      } else if (surface.value !== undefined && surface.dataset?.titleInput !== undefined) {
        surface.value = title;
      } else {
        surface.textContent = title;
      }
    });
    focusTarget?.focus?.();
  };

  const firstFocusable = (container) => (
    container?.querySelector?.("[data-focus-key], a[href], button:not([disabled]), input:not([disabled])")
    || null
  );

  const connectedFocusable = (candidate) => (
    candidate && candidate.isConnected !== false && !candidate.disabled
      ? candidate
      : null
  );

  const applyUnpublish = (documentRef, row, response) => {
    const exam = row.closest?.("[data-exam-key]")
      || documentRef.querySelector?.(`[data-exam-key="${response.exam_key}"]`);
    const course = row.closest?.("[data-course-key]")
      || documentRef.querySelector?.(`[data-course-key="${response.course_key}"]`);
    const nextRow = row.nextElementSibling;
    const previousRow = row.previousElementSibling;
    row.remove();
    const courseCount = course?.querySelector?.("[data-course-count]");
    if (courseCount) courseCount.textContent = quizCountLabel(response.course_quiz_count);
    const examCount = exam?.querySelector?.("[data-exam-count]");
    if (examCount) examCount.textContent = quizCountLabel(response.exam_quiz_count);
    if (response.exam_quiz_count === 0) exam?.remove?.();
    if (response.course_quiz_count === 0) course?.remove?.();
    const fallback = connectedFocusable(firstFocusable(nextRow))
      || connectedFocusable(firstFocusable(previousRow))
      || connectedFocusable(firstFocusable(exam))
      || connectedFocusable(firstFocusable(course))
      || connectedFocusable(documentRef.querySelector?.("[data-quiz-library]"));
    fallback?.focus?.();
  };

  // A direction endpoint moves one position. A longer pointer drop is therefore
  // deliberately represented as sequential, server-authoritative moves.
  const directionSequence = (fromIndex, toIndex) => {
    if (!Number.isInteger(fromIndex) || !Number.isInteger(toIndex)) return [];
    const direction = toIndex > fromIndex ? "down" : "up";
    return Array(Math.abs(toIndex - fromIndex)).fill(direction);
  };

  const orderRows = (row) => {
    const parent = row.parentElement;
    return parent?.querySelectorAll
      ? [...parent.querySelectorAll("[data-quiz-order-row]")]
      : [];
  };

  const reorderRequest = async (
    documentRef,
    control,
    row,
    directions,
    fetchImpl = root.fetch,
    sessionStorageRef = root.sessionStorage,
  ) => {
    if (!directions.length) return false;
    control.disabled = true;
    let completedSteps = 0;
    try {
      for (const direction of directions) {
        const response = await fetchImpl(row.dataset.orderUrl, {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "",
          },
          body: JSON.stringify({ direction }),
        });
        if (!response.ok) throw new Error(await errorMessage(response, "Quiz order could not be updated."));
        completedSteps += 1;
      }
      root.location?.reload?.();
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "Quiz order could not be updated.";
      if (completedSteps > 0) {
        storeReorderFailure(sessionStorageRef, message);
        root.location?.reload?.();
        return false;
      }
      control.disabled = false;
      report(documentRef, message);
      return false;
    }
  };

  const keyboardReorderDirection = (key, index, length) => {
    if (key === "ArrowUp" && index > 0) return "up";
    if (key === "ArrowDown" && index >= 0 && index < length - 1) return "down";
    return null;
  };

  const closeOverflow = (element) => {
    const menu = element.closest?.("[data-quiz-overflow]");
    if (menu) menu.open = false;
  };

  const setProgressPill = (row, label) => {
    const pill = row.querySelector("[data-quiz-progress]");
    if (!pill) return;
    pill.textContent = label;
    if (pill.classList) {
      pill.classList.remove("sh-pill--info", "sh-pill--ok");
      const stateClass = progressClass(label);
      if (stateClass) pill.classList.add(stateClass);
    }
  };

  const bindPointerReorder = (documentRef, handle) => {
    let dragging = null;
    const finish = async (event) => {
      if (!dragging || (event.pointerId !== undefined && event.pointerId !== dragging.pointerId)) return;
      const source = dragging.row;
      source.classList?.remove("is-dragging");
      const target = documentRef.elementFromPoint?.(event.clientX, event.clientY)
        ?.closest?.("[data-quiz-order-row]");
      const rows = orderRows(source);
      const directions = target && target.parentElement === source.parentElement
        ? directionSequence(rows.indexOf(source), rows.indexOf(target))
        : [];
      dragging = null;
      await reorderRequest(documentRef, handle, source, directions);
    };
    handle.addEventListener("pointerdown", (event) => {
      const row = handle.closest?.("[data-quiz-order-row]");
      if (!row || event.button > 0) return;
      dragging = { row, pointerId: event.pointerId };
      handle.setPointerCapture?.(event.pointerId);
      row.classList?.add("is-dragging");
    });
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", (event) => {
      if (dragging?.pointerId === event.pointerId) {
        dragging.row.classList?.remove("is-dragging");
        dragging = null;
      }
    });
  };

  const initialize = (documentRef, storage) => {
    consumeReorderFailure(documentRef, root.sessionStorage);
    documentRef.querySelectorAll(".disclosure").forEach((button) => {
      button.addEventListener("click", () => setExpanded(button, button.getAttribute("aria-expanded") !== "true"));
    });
    const refresh = () => {
      documentRef.querySelectorAll("[data-quiz-row]").forEach((row) => {
        setProgressPill(row, readProgress(storage, row.dataset.quizToken, row.dataset.quizVersion));
      });
    };
    documentRef.querySelectorAll("[data-reset-quiz]").forEach((button) => {
      button.addEventListener("click", () => {
        closeOverflow(button);
        if (typeof root.confirm === "function" && !root.confirm("Reset this quiz on this browser?")) return;
        try {
          resetProgress(storage, button.dataset.quizToken, button.dataset.quizVersion);
          refresh();
          report(documentRef, "That quiz's progress was reset on this browser.");
        } catch (_error) {
          report(documentRef, "Quiz progress could not be reset.");
        }
      });
    });
    documentRef.querySelectorAll("[data-remove-quiz]").forEach((button) => {
      button.addEventListener("click", async () => {
        closeOverflow(button);
        if (typeof root.confirm === "function" && !root.confirm("Remove this released quiz? Its source and run history will be preserved.")) return;
        button.disabled = true;
        try {
          const response = await root.fetch(button.dataset.removeUrl, {
            method: "DELETE",
            headers: { "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "" },
          });
          if (!response.ok) throw new Error(await errorMessage(response, "Quiz could not be unpublished."));
          resetProgress(storage, button.dataset.quizToken, button.dataset.quizVersion);
          applyUnpublish(
            documentRef,
            button.closest(".lecture-row"),
            await response.json(),
          );
          report(documentRef, "The released quiz was removed.");
        } catch (error) {
          button.disabled = false;
          report(documentRef, error instanceof Error ? error.message : "Quiz could not be unpublished.");
        }
      });
    });
    documentRef.querySelectorAll("[data-title-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const input = form.querySelector("[data-title-input]");
        const saveButton = form.querySelector("[data-save-title]");
        const cleanedTitle = String(input?.value || "").trim();
        if (!cleanedTitle) return report(documentRef, "Quiz title cannot be blank.");
        saveButton.disabled = true;
        try {
          const response = await root.fetch(form.dataset.titleUrl, {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "",
            },
            body: JSON.stringify({ title: cleanedTitle }),
          });
          if (!response.ok) throw new Error(await errorMessage(response, "Quiz title could not be updated."));
          const renamed = await response.json();
          saveButton.disabled = false;
          applyRenamedTitle(
            documentRef,
            renamed.token,
            renamed.title,
            form.querySelector("[data-title-input]"),
          );
          report(documentRef, "Quiz title updated.");
        } catch (error) {
          saveButton.disabled = false;
          report(documentRef, error instanceof Error ? error.message : "Quiz title could not be updated.");
        }
      });
    });
    documentRef.querySelectorAll("[data-move-quiz-library]").forEach((button) => {
      button.addEventListener("click", async () => {
        closeOverflow(button);
        await managementRequest(documentRef, button, button.dataset.libraryUrl, { section: button.dataset.targetSection });
      });
    });
    documentRef.querySelectorAll("[data-quiz-drag-handle]").forEach((handle) => {
      bindPointerReorder(documentRef, handle);
      handle.addEventListener("keydown", async (event) => {
        const row = handle.closest?.("[data-quiz-order-row]");
        const rows = row ? orderRows(row) : [];
        const index = rows.indexOf(row);
        const direction = keyboardReorderDirection(event.key, index, rows.length);
        if (!direction) return;
        event.preventDefault();
        await reorderRequest(documentRef, handle, row, [direction]);
      });
    });
    documentRef.addEventListener?.("keydown", (event) => {
      if (event.key === "Escape") documentRef.querySelectorAll("[data-quiz-overflow][open]").forEach((menu) => {
        menu.open = false;
        menu.querySelector("summary")?.focus?.();
      });
    });
    refresh();
  };

  const api = {
    initialize, progressKey, progressLabel, progressClass, readProgress, resetProgress,
    cookieValue, managementRequest, setExpanded, directionSequence, reorderRequest,
    reorderFailureStorageKey, storeReorderFailure, consumeReorderFailure, keyboardReorderDirection,
    applyRenamedTitle, applyUnpublish, connectedFocusable, quizCountLabel,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) root.document.addEventListener("DOMContentLoaded", () => initialize(root.document, root.localStorage), { once: true });
})(typeof globalThis === "undefined" ? this : globalThis);
