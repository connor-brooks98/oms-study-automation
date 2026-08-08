(() => {
  const storagePrefix = "study-hub:disclosure:";

  function setExpanded(button, expanded) {
    const target = document.getElementById(button.getAttribute("aria-controls"));
    if (!target) return;
    button.setAttribute("aria-expanded", String(expanded));
    button.querySelector(".sh-disclose")?.classList.toggle("is-open", expanded);
    target.hidden = !expanded;
  }

  document.querySelectorAll("[data-disclosure]").forEach((button) => {
    const key = storagePrefix + button.dataset.storageKey;
    try {
      const saved = sessionStorage.getItem(key);
      if (saved !== null) setExpanded(button, saved === "true");
    } catch (_) {
      // Storage can be unavailable in privacy modes; server defaults still work.
    }
    button.addEventListener("click", () => {
      const expanded = button.getAttribute("aria-expanded") !== "true";
      setExpanded(button, expanded);
      try {
        sessionStorage.setItem(key, String(expanded));
      } catch (_) {
        // Disclosure behavior does not depend on persistence.
      }
    });
  });
})();
