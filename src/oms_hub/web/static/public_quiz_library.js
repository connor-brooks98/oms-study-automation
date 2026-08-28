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

  const browserStorage = (name) => {
    try {
      return root[name];
    } catch (_error) {
      return null;
    }
  };

  const tryResetProgress = (storage, token, version) => {
    try {
      resetProgress(storage, token, version);
      return true;
    } catch (_error) {
      return false;
    }
  };

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
    if (!panel) return;
    panel.hidden = !expanded;
    if (expanded) {
      panel.classList?.remove("t-page-enter");
      void panel.offsetWidth;
      panel.classList?.add("t-page-enter");
      panel.addEventListener?.("animationend", () => panel.classList?.remove("t-page-enter"), { once: true });
    }
    if (!expanded) {
      Array.from(panel.querySelectorAll?.(".disclosure[aria-expanded='true']") || [])
        .forEach((descendant) => setExpanded(descendant, false));
    }
  };

  const errorMessage = async (response, fallback) => {
    try {
      const payload = await response.json();
      return payload.detail || fallback;
    } catch (_error) {
      return fallback;
    }
  };

  const structuredPayload = (title, questions) => JSON.stringify({ title, questions });

  const readStructuredQuestions = (form) => [...form.querySelectorAll("[data-payload-question]")].map((question) => {
    const pairs = [...question.querySelectorAll("[data-choice]")].map((field, index) => ({
      choice: field.value.trim(), correct: question.querySelectorAll("[data-correct]")[index]?.checked,
    }));
    const choices = pairs.map((pair) => pair.choice);
    const correct = pairs.findIndex((pair) => pair.correct);
    const image = JSON.parse(question.dataset.imageRef || "null");
    return {
      stem: question.querySelector("[data-stem]").value.trim(), choices, correct_index: correct,
      rationale: question.querySelector("[data-rationale]").value.trim(),
      topic: question.querySelector("[data-topic]").value.trim() || null,
      area: question.querySelector("[data-area]").value.trim() || null,
      learning_objective: question.querySelector("[data-learning-objective]").value.trim() || null,
      image_ref: image,
    };
  });

  const editorChoiceRow = (documentRef, value = "", checked = false) => {
    const label = documentRef.createElement("label");
    const radio = documentRef.createElement("input");
    radio.type = "radio"; radio.dataset.correct = "true"; radio.checked = checked;
    const input = documentRef.createElement("input");
    input.dataset.choice = "true"; input.required = true; input.value = value;
    const remove = documentRef.createElement("button");
    remove.type = "button"; remove.dataset.removeChoice = "true"; remove.textContent = "Remove choice";
    remove.className = "sh-btn sh-btn--danger";
    label.append(radio, input, remove);
    return label;
  };
  const editorQuestion = (documentRef, question = {}) => {
    const fieldset = documentRef.createElement("fieldset");
    fieldset.dataset.payloadQuestion = "true";
    fieldset.dataset.imageRef = JSON.stringify(question.image_ref || null);
    [["stem", "Stem"], ["rationale", "Rationale"], ["topic", "Topic"], ["area", "Area"], ["learningObjective", "Learning objective"]].forEach(([key, labelText]) => {
      const label = documentRef.createElement("label"); label.textContent = labelText;
      const input = documentRef.createElement(key === "stem" || key === "rationale" ? "textarea" : "input");
      input.dataset[key === "learningObjective" ? "learningObjective" : key] = "true";
      input.required = key === "stem" || key === "rationale";
      input.value = question[key === "learningObjective" ? "learning_objective" : key] || "";
      label.append(input); fieldset.append(label);
    });
    const choices = documentRef.createElement("div"); choices.dataset.choices = "true";
    (question.choices || ["", ""]).forEach((choice, index) => {
      choices.append(editorChoiceRow(documentRef, choice, index === Number(question.correct_index || 0)));
    });
    const add = documentRef.createElement("button"); add.type = "button"; add.dataset.addChoice = "true"; add.textContent = "Add choice"; add.className = "sh-btn sh-btn--secondary";
    const remove = documentRef.createElement("button"); remove.type = "button"; remove.dataset.removeQuestion = "true"; remove.textContent = "Remove question"; remove.className = "sh-btn sh-btn--danger";
    fieldset.append(choices, add, remove); return fieldset;
  };

  const loadPayloadEditor = async (documentRef, details) => {
    if (details.dataset.loaded || details.dataset.loading) return;
    details.dataset.loading = "true";
    const status = details.querySelector("[data-payload-loading]");
    status.textContent = "Loading questions…";
    try {
      const response = await root.fetch(details.dataset.payloadUrl, { cache: "no-store" });
      if (!response.ok) throw new Error(await errorMessage(response, "Quiz questions could not be loaded."));
      const payload = await response.json();
      const form = details.querySelector("[data-payload-form]");
      form.querySelector("[data-payload-title]").value = payload.title;
      const group = form.querySelector("[data-payload-questions]");
      (payload.questions || []).forEach((question) => group.append(editorQuestion(documentRef, question)));
      form.hidden = false;
      status.hidden = true;
      details.dataset.loaded = "true";
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "Quiz questions could not be loaded.";
    } finally {
      delete details.dataset.loading;
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

  const applyReorder = (row, directions) => {
    const parent = row?.parentElement;
    if (!parent || typeof parent.insertBefore !== "function") return false;
    for (const direction of directions) {
      if (direction === "up") {
        const previous = row.previousElementSibling;
        if (previous) parent.insertBefore(row, previous);
      } else if (direction === "down") {
        const next = row.nextElementSibling;
        if (next) parent.insertBefore(next, row);
      }
    }
    row.classList?.remove("t-list-settle");
    void row.offsetWidth;
    row.classList?.add("t-list-settle");
    row.addEventListener?.("animationend", () => row.classList?.remove("t-list-settle"), { once: true });
    return true;
  };

  const reorderRequest = async (
    documentRef,
    control,
    row,
    directions,
    fetchImpl = root.fetch,
    sessionStorageRef = browserStorage("sessionStorage"),
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
      applyReorder(row, directions);
      control.disabled = false;
      control.focus?.({ preventScroll: true });
      report(documentRef, "Quiz order updated.");
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

  const openContextMenu = (documentRef, row, event) => {
    const menu = row?.querySelector?.("[data-quiz-overflow]");
    if (!menu) return false;
    event.preventDefault?.();
    documentRef.querySelectorAll?.("[data-quiz-overflow][open]").forEach((open) => {
      if (open !== menu) open.open = false;
    });
    menu.open = true;
    menu.querySelector?.("summary")?.setAttribute?.("aria-expanded", "true");
    return true;
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
    const clearTarget = () => documentRef.querySelectorAll?.(".is-drop-target")
      .forEach((row) => row.classList?.remove("is-drop-target"));
    const finish = async (event) => {
      if (!dragging || (event.pointerId !== undefined && event.pointerId !== dragging.pointerId)) return;
      const source = dragging.row;
      source.classList?.remove("is-dragging");
      clearTarget();
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
    handle.addEventListener("pointermove", (event) => {
      if (!dragging || event.pointerId !== dragging.pointerId) return;
      clearTarget();
      const target = documentRef.elementFromPoint?.(event.clientX, event.clientY)
        ?.closest?.("[data-quiz-order-row]");
      if (target && target !== dragging.row && target.parentElement === dragging.row.parentElement) {
        target.classList?.add("is-drop-target");
      }
    });
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", (event) => {
      if (dragging?.pointerId === event.pointerId) {
        dragging.row.classList?.remove("is-dragging");
        clearTarget();
        dragging = null;
      }
    });
  };

  const initialize = (documentRef, storage) => {
    const surface = documentRef.querySelector("[data-quiz-library]");
    if (surface?.dataset.libraryInitialized) return;
    if (surface) surface.dataset.libraryInitialized = "true";
    consumeReorderFailure(documentRef, browserStorage("sessionStorage"));
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
          const payload = await response.json();
          const progressCleared = tryResetProgress(
            storage,
            button.dataset.quizToken,
            button.dataset.quizVersion,
          );
          applyUnpublish(
            documentRef,
            button.closest(".lecture-row"),
            payload,
          );
          report(
            documentRef,
            progressCleared
              ? "The released quiz was removed."
              : "The released quiz was removed, but this browser's saved progress could not be cleared.",
          );
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
    documentRef.querySelectorAll("[data-payload-editor]").forEach((details) => {
      details.addEventListener("toggle", () => { if (details.open) loadPayloadEditor(documentRef, details); });
    });
    documentRef.querySelectorAll("[data-payload-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type='submit']");
        const questions = readStructuredQuestions(form);
        if (!questions.length || questions.some((question) => question.choices.length < 2 || question.choices.length > 8 || question.choices.some((choice) => !choice) || question.correct_index < 0)) {
          return report(documentRef, "Each quiz needs one question, two to eight choices, and a correct answer.");
        }
        button.disabled = true;
        try {
          const response = await root.fetch(form.dataset.payloadUrl, {
            method: "PATCH",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "" },
            body: JSON.stringify({ payload_json: structuredPayload(form.querySelector("[data-payload-title]").value, questions) }),
          });
          if (!response.ok) throw new Error(await errorMessage(response, "Quiz questions could not be updated."));
          root.location?.reload?.();
        } catch (error) {
          button.disabled = false;
          report(documentRef, error instanceof Error ? error.message : "Quiz questions could not be updated.");
        }
      });
    });
    documentRef.addEventListener("click", (event) => {
      const addQuestion = event.target.closest?.("[data-add-question]");
      if (addQuestion) {
        const group = addQuestion.closest("form").querySelector("[data-payload-questions]");
        if (group.querySelectorAll("[data-payload-question]").length < 100) group.append(editorQuestion(documentRef));
        return;
      }
      const removeQuestion = event.target.closest?.("[data-remove-question]");
      if (removeQuestion) {
        const form = removeQuestion.closest("form");
        if (form.querySelectorAll("[data-payload-question]").length > 1) removeQuestion.closest("[data-payload-question]").remove();
        return;
      }
      const removeChoice = event.target.closest?.("[data-remove-choice]");
      if (removeChoice) {
        const group = removeChoice.closest("[data-choices]");
        if (group.querySelectorAll("[data-choice]").length > 2) removeChoice.closest("label").remove();
        return;
      }
      const addChoice = event.target.closest?.("[data-add-choice]");
      if (addChoice) {
        const group = addChoice.closest("[data-payload-question]").querySelector("[data-choices]");
        if (group.querySelectorAll("[data-choice]").length < 8) group.append(editorChoiceRow(documentRef));
      }
    });
    documentRef.addEventListener("change", (event) => {
      const radio = event.target.closest?.("[data-correct]");
      if (radio?.checked) radio.closest("[data-payload-question]")
        .querySelectorAll("[data-correct]").forEach((item) => { if (item !== radio) item.checked = false; });
    });
    documentRef.querySelectorAll("[data-view-flags]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const response = await root.fetch(button.dataset.flagsUrl, { cache: "no-store" });
          if (!response.ok) throw new Error(await errorMessage(response, "Quiz flags could not be read."));
          const flags = (await response.json()).flags || [];
          report(documentRef, flags.length
            ? flags.map((flag) => `${flag.question_id}: ${flag.reason} (${flag.count})`).join(" · ")
            : "No open question flags.");
        } catch (error) { report(documentRef, error instanceof Error ? error.message : "Quiz flags could not be read."); }
      });
    });
    documentRef.addEventListener?.("click", (event) => {
      documentRef.querySelectorAll?.("[data-quiz-overflow][open]").forEach((menu) => {
        if (!menu.contains?.(event.target)) menu.open = false;
      });
    });
    documentRef.querySelectorAll?.("[data-context-trigger]").forEach((row) => {
      row.addEventListener?.("contextmenu", (event) => {
        if (event.target?.closest?.("input, textarea, select")) return;
        openContextMenu(documentRef, row, event);
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

  const bootstrap = (documentRef, storage = browserStorage("localStorage")) => {
    if (documentRef.readyState === "loading") {
      documentRef.addEventListener("DOMContentLoaded", () => initialize(documentRef, storage), { once: true });
    } else {
      initialize(documentRef, storage);
    }
  };

  const api = {
    initialize, bootstrap, progressKey, progressLabel, progressClass, readProgress, resetProgress,
    tryResetProgress,
    cookieValue, managementRequest, setExpanded, directionSequence, reorderRequest,
    bindPointerReorder,
    reorderFailureStorageKey, storeReorderFailure, consumeReorderFailure, keyboardReorderDirection,
    applyRenamedTitle, applyUnpublish, connectedFocusable, quizCountLabel, structuredPayload, readStructuredQuestions,
    editorQuestion, loadPayloadEditor, openContextMenu, applyReorder,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) bootstrap(root.document);
})(typeof globalThis === "undefined" ? this : globalThis);
