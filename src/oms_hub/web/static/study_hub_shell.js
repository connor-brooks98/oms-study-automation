(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.StudyHubShell = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function normalizeQuery(value) {
    return String(value || "").trim().toLocaleLowerCase();
  }

  function matchesCommand(label, query) {
    const needle = normalizeQuery(query);
    return !needle || normalizeQuery(label).includes(needle);
  }

  function nextIndex(current, direction, count) {
    if (!count) return -1;
    const start = current < 0 ? (direction > 0 ? -1 : 0) : current;
    return (start + direction + count) % count;
  }

  function transitionDelay(windowRef, variableName, fallback = 150) {
    if (windowRef?.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return 0;
    const styles = windowRef?.getComputedStyle?.(windowRef.document.documentElement);
    const raw = styles?.getPropertyValue(variableName).trim();
    if (!raw) return fallback;
    const value = Number.parseFloat(raw);
    return raw.endsWith("s") && !raw.endsWith("ms") ? value * 1000 : value;
  }

  function buttonIcon(label) {
    const text = normalizeQuery(label);
    if (/\b(download|export)\b/.test(text)) return "download";
    if (/\b(remove|delete|discard|trash)\b/.test(text)) return "trash";
    if (/^(continue|next|open|take|start|review|preview|apply|approve|browse|go to)\b/.test(text)) return "continue";
    return "";
  }

  function isStatefulAction(button) {
    if (!button?.matches?.("button.sh-btn")) return false;
    if (button.matches?.('[role="tab"], .sh-seg__btn')) return false;
    if ((button.getAttribute("type") || "submit").toLocaleLowerCase() === "submit") return true;
    return /^(save|submit|publish|apply|approve|assign|attach|upload|queue|generate|process|stage|connect)\b/.test(
      normalizeQuery(button.textContent),
    );
  }

  function setButtonState(button, state = "idle") {
    if (!button) return;
    button.classList.add("sh-btn--stateful");
    button.dataset.state = state;
    if (state === "loading") button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
  }

  function enhanceButton(button) {
    if (!button?.classList?.contains("sh-btn") || button.dataset.motionButton === "true") return;
    button.dataset.motionButton = "true";
    const icon = buttonIcon(button.textContent);
    if (icon) button.classList.add(`sh-btn--${icon}`);
    if (isStatefulAction(button)) {
      button.classList.add("sh-btn--stateful");
      button.dataset.state ||= "idle";
    }
    button.addEventListener("pointerdown", (event) => {
      if (button.disabled || button.classList.contains("is-disabled")) return;
      const rect = button.getBoundingClientRect();
      const ripple = button.ownerDocument.createElement("span");
      const size = Math.max(rect.width, rect.height) * 2;
      ripple.className = "sh-btn__ripple";
      ripple.style.setProperty("--ripple-x", `${event.clientX - rect.left}px`);
      ripple.style.setProperty("--ripple-y", `${event.clientY - rect.top}px`);
      ripple.style.setProperty("--ripple-size", `${size}px`);
      ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
      button.append(ripple);
    });
  }

  function toastTone(message, source) {
    const value = normalizeQuery(message);
    if (source?.getAttribute?.("role") === "alert" || /\b(error|failed|rejected|unavailable|could not|cannot)\b/.test(value)) return "error";
    if (/\b(warning|remaining|needs review)\b/.test(value)) return "warning";
    if (/\b(saved|uploaded|selected|verified|acknowledged|published|ready|complete|reset)\b/.test(value)) return "success";
    return "info";
  }

  function showToast(documentRef, message, tone = "info", windowRef) {
    const region = documentRef?.querySelector?.("[data-toast-region]");
    const value = String(message || "").trim();
    if (!region || !value) return null;
    const duplicate = Array.from(region.querySelectorAll?.(".t-toast") || [])
      .find((toast) => toast.dataset.message === value);
    if (duplicate) return duplicate;
    const toast = documentRef.createElement("div");
    toast.className = "t-toast";
    toast.dataset.tone = tone;
    toast.dataset.message = value;
    const copy = documentRef.createElement("span");
    copy.className = "t-toast__copy";
    copy.textContent = value;
    const close = documentRef.createElement("button");
    close.type = "button";
    close.className = "t-toast__close";
    close.setAttribute("aria-label", "Dismiss notification");
    close.textContent = "×";
    const remove = () => {
      if (!toast.isConnected || toast.classList.contains("is-leaving")) return;
      toast.classList.add("is-leaving");
      const finish = () => toast.remove();
      (windowRef?.setTimeout || setTimeout)(finish, transitionDelay(windowRef, "--dur-short", 180));
    };
    close.addEventListener("click", remove);
    toast.append(copy, close);
    region.append(toast);
    while ((region.querySelectorAll?.(".t-toast") || []).length > 3) region.querySelector(".t-toast")?.remove();
    (windowRef?.setTimeout || setTimeout)(remove, 4500);
    return toast;
  }

  function confirmAction(documentRef, options = {}, invoker, windowRef) {
    const win = windowRef || documentRef?.defaultView;
    const dialog = documentRef?.querySelector?.("[data-confirm-dialog]");
    const message = String(options.message || "Are you sure?");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(typeof win?.confirm === "function" ? win.confirm(message) : false);
    }
    const title = dialog.querySelector("[data-confirm-title]");
    const copy = dialog.querySelector("[data-confirm-message]");
    const accept = dialog.querySelector("[data-confirm-accept]");
    const cancels = Array.from(dialog.querySelectorAll("[data-confirm-cancel]"));
    title.textContent = options.title || "Confirm this action";
    copy.textContent = message;
    accept.textContent = options.confirmLabel || "Continue";
    cancels.forEach((button) => {
      if (button.textContent !== "×") button.textContent = options.cancelLabel || "Keep it";
    });
    accept.classList.toggle("sh-btn--danger", options.tone !== "primary");
    accept.classList.toggle("sh-btn--primary", options.tone === "primary");
    return new Promise((resolve) => {
      let settled = false;
      const finish = (accepted) => {
        if (settled) return;
        settled = true;
        accept.removeEventListener("click", acceptAction);
        cancels.forEach((button) => button.removeEventListener("click", cancelAction));
        dialog.removeEventListener("cancel", cancelEvent);
        dialog.removeEventListener("click", backdropEvent);
        dialog.classList.remove("is-open");
        dialog.classList.add("is-closing");
        const close = () => {
          dialog.classList.remove("is-closing");
          if (dialog.open) dialog.close();
          invoker?.focus?.({ preventScroll: true });
          resolve(accepted);
        };
        (win?.setTimeout || setTimeout)(close, transitionDelay(win, "--modal-duration"));
      };
      const acceptAction = () => finish(true);
      const cancelAction = () => finish(false);
      const cancelEvent = (event) => { event.preventDefault(); finish(false); };
      const backdropEvent = (event) => { if (event.target === dialog) finish(false); };
      accept.addEventListener("click", acceptAction);
      cancels.forEach((button) => button.addEventListener("click", cancelAction));
      dialog.addEventListener("cancel", cancelEvent);
      dialog.addEventListener("click", backdropEvent);
      if (!dialog.open) dialog.showModal();
      (win?.requestAnimationFrame || ((callback) => callback()))(() => dialog.classList.add("is-open"));
      cancels.find((button) => button.textContent !== "×")?.focus?.();
    });
  }

  function initialize(documentRef, windowRef) {
    if (!documentRef) return;
    const win = windowRef || (typeof window !== "undefined" ? window : null);
    const invokers = new Map();
    const dropdownTimers = new WeakMap();
    const dialogTimers = new WeakMap();
    const buttonObserver = win?.MutationObserver ? new win.MutationObserver((records) => {
      records.forEach((record) => {
        if (record.type === "childList") {
          record.addedNodes.forEach((node) => {
            if (node.nodeType !== 1) return;
            if (node.matches?.(".sh-btn")) enhanceButton(node);
            node.querySelectorAll?.(".sh-btn").forEach(enhanceButton);
          });
        }
        if (record.type === "attributes" && record.target.matches?.(".sh-btn--stateful")) {
          const button = record.target;
          if (button.disabled && button.dataset.state === "idle") setButtonState(button, "loading");
          else if (!button.disabled && button.dataset.state === "loading") setButtonState(button, "idle");
        }
      });
    }) : null;
    const liveValues = new WeakMap();
    const toastSources = () => Array.from(documentRef.querySelectorAll?.("[data-toast-source]") || []);
    const notifyToastSource = (source) => {
      const value = String(source?.textContent || "").trim();
      if (liveValues.get(source) === value) return;
      liveValues.set(source, value);
      if (!value || /(?:…|\.\.\.)$/.test(value)) return;
      showToast(documentRef, value, toastTone(value, source), win);
    };
    toastSources().forEach((source) => liveValues.set(source, String(source.textContent || "").trim()));
    const toastObserver = win?.MutationObserver ? new win.MutationObserver((records) => {
      const sources = new Set();
      records.forEach((record) => {
        const source = record.target?.nodeType === 1
          ? record.target.closest?.("[data-toast-source]")
          : record.target?.parentElement?.closest?.("[data-toast-source]");
        if (source) sources.add(source);
        record.addedNodes?.forEach?.((node) => {
          if (node.nodeType !== 1) return;
          if (node.matches?.("[data-toast-source]")) sources.add(node);
          node.querySelectorAll?.("[data-toast-source]").forEach((item) => sources.add(item));
        });
      });
      sources.forEach(notifyToastSource);
    }) : null;

    documentRef.querySelectorAll(".sh-btn").forEach(enhanceButton);
    buttonObserver?.observe(documentRef.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    toastObserver?.observe(documentRef.body, { childList: true, subtree: true, characterData: true });
    documentRef.addEventListener("submit", (event) => {
      const button = event.submitter;
      if (
        button?.classList?.contains("sh-btn--stateful")
        && (!event.defaultPrevented || button.disabled)
      ) setButtonState(button, "loading");
    });

    function restoreFocus(dialog) {
      const invoker = invokers.get(dialog);
      const isVisible = !invoker?.getClientRects || invoker.getClientRects().length > 0;
      if (invoker && isVisible && !invoker.disabled && typeof invoker.focus === "function") {
        invoker.focus();
        return;
      }
      documentRef.getElementById("main-content")?.focus();
    }

    function openDropdown(details) {
      const menu = details.querySelector(".t-dropdown");
      if (!menu) return;
      if (dropdownTimers.has(details)) win?.clearTimeout(dropdownTimers.get(details));
      menu.classList.remove("is-closing");
      details.open = true;
      details.querySelector("summary")?.setAttribute("aria-expanded", "true");
      (win?.requestAnimationFrame || ((callback) => callback()))(() => menu.classList.add("is-open"));
    }

    function closeDropdown(details, restore = false) {
      const menu = details.querySelector(".t-dropdown");
      const summary = details.querySelector("summary");
      if (!menu || !details.open) return;
      menu.classList.remove("is-open");
      menu.classList.add("is-closing");
      summary?.setAttribute("aria-expanded", "false");
      const finish = () => {
        menu.classList.remove("is-closing");
        details.open = false;
        dropdownTimers.delete(details);
        if (restore) summary?.focus();
      };
      const timer = win?.setTimeout(finish, transitionDelay(win, "--dropdown-duration")) ?? setTimeout(finish, 150);
      dropdownTimers.set(details, timer);
    }

    function openDialog(dialog, invoker) {
      if (!dialog || typeof dialog.showModal !== "function") return;
      if (dialogTimers.has(dialog)) win?.clearTimeout(dialogTimers.get(dialog));
      if (invoker) invokers.set(dialog, invoker);
      dialog.classList.remove("is-closing");
      if (!dialog.open) dialog.showModal();
      (win?.requestAnimationFrame || ((callback) => callback()))(() => dialog.classList.add("is-open"));
      dialog.querySelector(
        "[autofocus], [data-dialog-initial-focus], input, a, button:not([data-close-dialog])"
      )?.focus();
    }

    function closeDialog(dialog) {
      if (!dialog?.open || dialog.classList.contains("is-closing")) return;
      dialog.classList.remove("is-open");
      dialog.classList.add("is-closing");
      const finish = () => {
        dialog.classList.remove("is-closing");
        dialog.close();
        dialogTimers.delete(dialog);
      };
      const timer = win?.setTimeout(finish, transitionDelay(win, "--modal-duration")) ?? setTimeout(finish, 150);
      dialogTimers.set(dialog, timer);
    }

    documentRef.querySelectorAll("details.sh-more").forEach((details) => {
      const summary = details.querySelector("summary");
      summary?.setAttribute("aria-expanded", String(details.open));
      summary?.addEventListener("click", (event) => {
        event.preventDefault();
        if (details.open) closeDropdown(details);
        else openDropdown(details);
      });
    });
    documentRef.addEventListener("click", (event) => {
      documentRef.querySelectorAll("details.sh-more[open]").forEach((details) => {
        if (!details.contains(event.target)) closeDropdown(details);
      });
    });

    documentRef.querySelectorAll("[data-open-dialog]").forEach((trigger) => {
      trigger.addEventListener("click", () => {
        const dialog = documentRef.getElementById(trigger.dataset.openDialog);
        openDialog(dialog, trigger);
      });
    });

    documentRef.querySelectorAll(".sh-dialog:not([data-confirm-dialog])").forEach((dialog) => {
      dialog.querySelectorAll("[data-close-dialog]").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.preventDefault();
          closeDialog(dialog);
        });
      });
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) closeDialog(dialog);
      });
      dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        closeDialog(dialog);
      });
      dialog.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        closeDialog(dialog);
      });
      dialog.addEventListener("close", () => {
        dialog.classList.remove("is-open", "is-closing");
        restoreFocus(dialog);
      });
    });

    const command = documentRef.getElementById("command-palette");
    const query = command && command.querySelector("[data-command-query]");
    const items = command ? Array.from(command.querySelectorAll("[data-command-item]")) : [];
    const empty = command && command.querySelector("[data-command-empty]");
    let activeIndex = -1;

    command?.addEventListener("close", () => {
      if (query) query.value = "";
      items.forEach((item) => {
        item.hidden = false;
        item.removeAttribute("data-active");
      });
      if (empty) empty.hidden = true;
      activeIndex = -1;
    });

    function visibleItems() {
      return items.filter((item) => !item.hidden);
    }

    function setActive(itemList, index) {
      items.forEach((item) => item.removeAttribute("data-active"));
      activeIndex = index;
      if (itemList[index]) {
        itemList[index].setAttribute("data-active", "true");
        itemList[index].focus();
      }
    }

    if (query) {
      query.addEventListener("input", () => {
        items.forEach((item) => { item.hidden = !matchesCommand(item.textContent, query.value); });
        if (empty) empty.hidden = visibleItems().length > 0;
        activeIndex = -1;
      });
      command.addEventListener("keydown", (event) => {
        const list = visibleItems();
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setActive(list, nextIndex(activeIndex, event.key === "ArrowDown" ? 1 : -1, list.length));
        }
      });
    }

    if (win) {
      win.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          documentRef.querySelectorAll("details.sh-more[open]").forEach((details) => closeDropdown(details, true));
        }
        if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
          event.preventDefault();
          if (command && !command.open) {
            const trigger = documentRef.querySelector('[data-open-dialog="command-palette"]');
            openDialog(command, trigger);
          }
        }
      });
    }
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initialize(document));
    else initialize(document);
  }

  return {
    initialize, matchesCommand, nextIndex, normalizeQuery, transitionDelay,
    buttonIcon, confirmAction, enhanceButton, isStatefulAction, setButtonState, showToast, toastTone,
  };
});
