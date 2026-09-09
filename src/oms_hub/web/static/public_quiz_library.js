((root) => {
  "use strict";

  const progressKey = (token, version) => `oms-study-hub-quiz:${token}:v${version}`;
  const viewStateKey = (pathname) => `oms-study-hub-quiz-library:${pathname}`;

  const readViewState = (storage, pathname) => {
    try {
      const value = JSON.parse(storage?.getItem(viewStateKey(pathname)) || "null");
      return value && Array.isArray(value.expanded) ? value : null;
    } catch (_error) {
      return null;
    }
  };

  const saveViewState = (documentRef, storage) => {
    try {
      const expanded = [...documentRef.querySelectorAll(".disclosure[aria-expanded='true']")]
        .map((button) => button.dataset.focusKey).filter(Boolean);
      storage?.setItem(viewStateKey(documentRef.location?.pathname || root.location?.pathname || ""), JSON.stringify({
        expanded,
        scrollY: documentRef.defaultView?.scrollY || 0,
      }));
    } catch (_error) {
      // Browser storage is optional; the library remains usable without it.
    }
  };

  const restoreViewState = (documentRef, storage) => {
    const state = readViewState(storage, documentRef.location?.pathname || root.location?.pathname || "");
    if (!state) return;
    for (const key of state.expanded) {
      const button = [...documentRef.querySelectorAll(".disclosure")]
        .find((candidate) => candidate.dataset.focusKey === key);
      if (button) setExpanded(button, true);
    }
    documentRef.defaultView?.requestAnimationFrame?.(() => {
      documentRef.defaultView?.scrollTo?.(0, Number(state.scrollY) || 0);
    });
  };

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

  const requestConfirmation = (documentRef, options, invoker) => (
    root.StudyHubShell?.confirmAction
      ? root.StudyHubShell.confirmAction(documentRef, options, invoker, documentRef.defaultView)
      : Promise.resolve(typeof root.confirm === "function" && root.confirm(options.message))
  );

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
      return typeof payload?.detail === "string" && payload.detail.trim()
        ? payload.detail
        : fallback;
    } catch (_error) {
      return fallback;
    }
  };

  const messageNode = (documentRef) => documentRef.querySelector("[data-reset-message]");
  const report = (documentRef, message) => { messageNode(documentRef).textContent = message; };

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

  const reorderStates = new WeakMap();
  const activeReorders = new WeakMap();

  const orderRows = (list) => (
    list?.querySelectorAll ? [...list.querySelectorAll("[data-quiz-order-row]")] : []
  );

  const tokenOrder = (list) => orderRows(list).map((row) => row.dataset.quizToken);

  const sameOrder = (left, right) => (
    left.length === right.length && left.every((token, index) => token === right[index])
  );

  const sameTokens = (left, right) => (
    left.length === right.length
    && new Set(left).size === left.length
    && new Set(right).size === right.length
    && left.every((token) => right.includes(token))
  );

  const applyTokenOrder = (list, tokens) => {
    const rows = orderRows(list);
    const currentTokens = rows.map((row) => row.dataset.quizToken);
    if (!sameTokens(currentTokens, tokens) || typeof list?.appendChild !== "function") return false;
    const rowsByToken = new Map(rows.map((row) => [row.dataset.quizToken, row]));
    tokens.forEach((token) => list.appendChild(rowsByToken.get(token)));
    return true;
  };

  const reorderState = (list) => {
    if (!reorderStates.has(list)) {
      reorderStates.set(list, {
        pending: false,
        sortable: null,
        disabledControls: [],
        originalOrder: null,
        cancelled: false,
      });
    }
    return reorderStates.get(list);
  };

  const setReorderPending = (list, pending) => {
    const state = reorderState(list);
    state.pending = pending;
    if (pending) {
      state.disabledControls = [...list.querySelectorAll("button, input, select, textarea")]
        .map((control) => ({ control, disabled: control.disabled }));
      state.disabledControls.forEach(({ control }) => { control.disabled = true; });
      list.classList?.add("is-order-saving");
    } else {
      state.disabledControls.forEach(({ control, disabled }) => { control.disabled = disabled; });
      state.disabledControls = [];
      list.classList?.remove("is-order-saving");
    }
    state.sortable?.option?.("disabled", pending);
  };

  const unconfirmedOrderMessage = (detail, restored) => {
    const prefix = detail && detail !== "Quiz order could not be saved." ? `${detail} ` : "";
    const view = restored ? " The previous order is shown." : "";
    return `${prefix}The quiz order could not be confirmed.${view} Refresh this page to verify the saved order.`;
  };

  const saveQuizOrder = async (
    documentRef,
    list,
    row,
    originalOrder,
    requestedOrder,
    fetchImpl = root.fetch,
  ) => {
    const state = reorderState(list);
    if (state.pending || sameOrder(originalOrder, requestedOrder)) return false;
    if (!sameTokens(originalOrder, requestedOrder)) {
      const restored = applyTokenOrder(list, originalOrder);
      report(documentRef, unconfirmedOrderMessage(null, restored));
      return false;
    }
    setReorderPending(list, true);
    try {
      const response = await fetchImpl(row.dataset.orderUrl, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "",
        },
        body: JSON.stringify({ ordered_tokens: requestedOrder }),
      });
      if (!response.ok) {
        throw new Error(await errorMessage(response, "Quiz order could not be saved."));
      }
      const payload = await response.json();
      if (
        payload?.token !== row.dataset.quizToken
        || !Array.isArray(payload?.ordered_tokens)
        || !sameTokens(originalOrder, payload.ordered_tokens)
      ) {
        throw new Error("Quiz order could not be saved.");
      }
      if (!applyTokenOrder(list, payload.ordered_tokens)) {
        throw new Error("Quiz order could not be saved.");
      }
      report(documentRef, "Quiz order saved.");
      return true;
    } catch (error) {
      const restored = applyTokenOrder(list, originalOrder);
      const detail = error instanceof Error ? error.message : "Quiz order could not be saved.";
      report(documentRef, unconfirmedOrderMessage(detail, restored));
      return false;
    } finally {
      setReorderPending(list, false);
      row.querySelector?.("[data-quiz-drag-handle]")?.focus?.({ preventScroll: true });
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

  const prefersReducedMotion = (documentRef) => Boolean(
    documentRef.defaultView?.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches,
  );

  const bindSortableReorder = (
    documentRef,
    list,
    SortableImpl = root.Sortable,
    fetchImpl = root.fetch,
  ) => {
    if (!SortableImpl?.create || orderRows(list).length < 2) return null;
    const state = reorderState(list);
    const sortable = SortableImpl.create(list, {
      group: { pull: false, put: false },
      direction: "vertical",
      draggable: "[data-quiz-order-row]",
      handle: "[data-quiz-drag-handle]",
      dataIdAttr: "data-quiz-token",
      animation: prefersReducedMotion(documentRef) ? 0 : 200,
      easing: "cubic-bezier(0.16, 1, 0.3, 1)",
      forceFallback: true,
      fallbackOnBody: true,
      fallbackTolerance: 6,
      touchStartThreshold: 4,
      scroll: true,
      bubbleScroll: true,
      scrollSensitivity: 64,
      scrollSpeed: 12,
      ghostClass: "quiz-order-ghost",
      chosenClass: "quiz-order-chosen",
      dragClass: "quiz-order-dragging",
      fallbackClass: "quiz-order-fallback",
      onStart(event) {
        state.originalOrder = tokenOrder(list);
        state.cancelled = false;
        activeReorders.set(documentRef, list);
        event.item?.classList?.add("is-dragging");
      },
      async onEnd(event) {
        event.item?.classList?.remove("is-dragging");
        activeReorders.delete(documentRef);
        const originalOrder = state.originalOrder || tokenOrder(list);
        state.originalOrder = null;
        const cancelled = state.cancelled
          || ["pointercancel", "touchcancel"].includes(event.originalEvent?.type);
        state.cancelled = false;
        if (cancelled) {
          applyTokenOrder(list, originalOrder);
          return false;
        }
        const requestedOrder = tokenOrder(list);
        if (sameOrder(originalOrder, requestedOrder)) return false;
        return saveQuizOrder(
          documentRef,
          list,
          event.item,
          originalOrder,
          requestedOrder,
          fetchImpl,
        );
      },
    });
    state.sortable = sortable;
    return sortable;
  };

  const moveRow = (row, direction) => {
    const list = row?.parentElement;
    const rows = orderRows(list);
    const index = rows.indexOf(row);
    if (direction === "up" && index > 0) {
      list.insertBefore(row, rows[index - 1]);
      return true;
    }
    if (direction === "down" && index >= 0 && index < rows.length - 1) {
      list.insertBefore(rows[index + 1], row);
      return true;
    }
    return false;
  };

  const bindKeyboardReorder = (documentRef, handle, fetchImpl = root.fetch) => {
    handle.addEventListener("keydown", async (event) => {
      const row = handle.closest?.("[data-quiz-order-row]");
      const list = row?.parentElement;
      const rows = orderRows(list);
      const direction = keyboardReorderDirection(event.key, rows.indexOf(row), rows.length);
      if (!direction) return;
      event.preventDefault();
      const state = reorderState(list);
      if (state.pending || state.originalOrder) return;
      const originalOrder = tokenOrder(list);
      if (!moveRow(row, direction)) return;
      await saveQuizOrder(
        documentRef,
        list,
        row,
        originalOrder,
        tokenOrder(list),
        fetchImpl,
      );
    });
  };

  const cancelActiveReorder = (documentRef) => {
    const list = activeReorders.get(documentRef);
    if (!list) return false;
    const state = reorderState(list);
    state.cancelled = true;
    if (state.originalOrder) {
      state.sortable?.sort?.(state.originalOrder);
      applyTokenOrder(list, state.originalOrder);
    }
    return true;
  };

  const initialize = (documentRef, storage) => {
    const surface = documentRef.querySelector("[data-quiz-library]");
    if (surface?.dataset.libraryInitialized) return;
    if (surface) surface.dataset.libraryInitialized = "true";
    documentRef.querySelectorAll(".disclosure").forEach((button) => {
      button.addEventListener("click", () => {
        setExpanded(button, button.getAttribute("aria-expanded") !== "true");
        saveViewState(documentRef, storage);
      });
    });
    restoreViewState(documentRef, storage);
    documentRef.defaultView?.addEventListener?.("pagehide", () => saveViewState(documentRef, storage));
    const refresh = () => {
      documentRef.querySelectorAll("[data-quiz-row]").forEach((row) => {
        setProgressPill(row, readProgress(storage, row.dataset.quizToken, row.dataset.quizVersion));
      });
    };
    documentRef.querySelectorAll("[data-reset-quiz]").forEach((button) => {
      button.addEventListener("click", async () => {
        closeOverflow(button);
        if (!await requestConfirmation(documentRef, {
          title: "Reset quiz progress?",
          message: "This clears this quiz's saved answers and progress on this browser.",
          confirmLabel: "Reset quiz",
          cancelLabel: "Keep progress",
        }, button)) return;
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
        if (!await requestConfirmation(documentRef, {
          title: "Remove this released quiz?",
          message: "The quiz will leave the public library. Its source and run history will be preserved.",
          confirmLabel: "Remove quiz",
          cancelLabel: "Keep quiz",
        }, button)) return;
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
    documentRef.querySelectorAll("[data-edit-questions]").forEach((button) => {
      button.addEventListener("click", async () => {
        closeOverflow(button);
        button.disabled = true;
        try {
          const response = await root.fetch(button.dataset.editUrl, {
            method: "POST",
            headers: { "X-CSRF-Token": cookieValue(documentRef.cookie, "study_hub_csrf") || "" },
          });
          if (!response.ok) throw new Error(await errorMessage(response, "Quiz editor could not be opened."));
          root.location?.assign?.((await response.json()).review_url);
        } catch (error) {
          button.disabled = false;
          report(documentRef, error instanceof Error ? error.message : "Quiz editor could not be opened.");
        }
      });
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
    const reorderLists = new Set();
    documentRef.querySelectorAll("[data-quiz-drag-handle]").forEach((handle) => {
      const list = handle.closest?.("[data-quiz-order-row]")?.parentElement;
      if (list) reorderLists.add(list);
      bindKeyboardReorder(documentRef, handle);
    });
    reorderLists.forEach((list) => bindSortableReorder(documentRef, list));
    documentRef.addEventListener?.("keydown", (event) => {
      if (event.key === "Escape" && cancelActiveReorder(documentRef)) event.preventDefault?.();
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
    viewStateKey, readViewState, saveViewState, restoreViewState,
    tryResetProgress,
    cookieValue, managementRequest, setExpanded, keyboardReorderDirection,
    tokenOrder, applyTokenOrder, saveQuizOrder, bindSortableReorder, bindKeyboardReorder,
    cancelActiveReorder,
    applyRenamedTitle, applyUnpublish, connectedFocusable, quizCountLabel,
    openContextMenu,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) bootstrap(root.document);
})(typeof globalThis === "undefined" ? this : globalThis);
