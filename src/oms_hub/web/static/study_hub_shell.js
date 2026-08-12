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

  function initialize(documentRef, windowRef) {
    if (!documentRef) return;
    const win = windowRef || (typeof window !== "undefined" ? window : null);
    const invokers = new Map();

    function restoreFocus(dialog) {
      const invoker = invokers.get(dialog);
      if (invoker && typeof invoker.focus === "function") invoker.focus();
    }

    documentRef.querySelectorAll("[data-open-dialog]").forEach((trigger) => {
      trigger.addEventListener("click", () => {
        const dialog = documentRef.getElementById(trigger.dataset.openDialog);
        if (!dialog || typeof dialog.showModal !== "function") return;
        invokers.set(dialog, trigger);
        dialog.showModal();
        const target = dialog.querySelector(
          "[autofocus], [data-dialog-initial-focus], input, a, button:not([data-close-dialog])"
        );
        if (target) target.focus();
      });
    });

    documentRef.querySelectorAll(".sh-dialog").forEach((dialog) => {
      dialog.querySelectorAll("[data-close-dialog]").forEach((button) => {
        button.addEventListener("click", () => dialog.close());
      });
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
      dialog.addEventListener("close", () => restoreFocus(dialog));
    });

    const command = documentRef.getElementById("command-palette");
    const query = command && command.querySelector("[data-command-query]");
    const items = command ? Array.from(command.querySelectorAll("[data-command-item]")) : [];
    const empty = command && command.querySelector("[data-command-empty]");
    let activeIndex = -1;

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
        if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
          event.preventDefault();
          if (command && !command.open) {
            const trigger = documentRef.querySelector('[data-open-dialog="command-palette"]');
            if (trigger) invokers.set(command, trigger);
            command.showModal();
            if (query) query.focus();
          }
        }
      });
    }
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initialize(document));
    else initialize(document);
  }

  return { initialize, matchesCommand, nextIndex, normalizeQuery };
});
