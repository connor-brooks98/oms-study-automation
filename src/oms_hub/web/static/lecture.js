((root) => {
  "use strict";

  const csrfToken = (documentRef) => {
    const prefix = "study_hub_csrf=";
    const cookie = documentRef.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  };

  const render = (card, status) => {
    const message = card.querySelector("[data-generation-message]");
    const active = ["queued", "running"].includes(status.state);
    const labels = {
      queued: "Queued on the Study Hub device.",
      running: `Working: ${(status.stage || "starting").replaceAll("_", " ")}.`,
      paused: status.message || "Reconnect Google, then try again.",
      failed: status.message || "Generation stopped. You can retry.",
      complete: "Ready.",
      ready: "Ready to generate.",
    };
    message.textContent = labels[status.state] || "Status updated.";
    card.querySelector("[data-generate]").disabled = active;
    if (status.url) {
      let link = card.querySelector("[data-generation-link]");
      if (!link) {
        link = card.ownerDocument.createElement("a");
        link.className = "button primary";
        link.dataset.generationLink = "";
        card.querySelector(".file-actions").prepend(link);
      }
      link.href = status.url;
      if (card.dataset.kind === "quiz") {
        link.textContent = "Take Lecture Quiz";
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } else {
        link.textContent = "Open Lecture Outline";
      }
    }
    return active;
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const match = root.location.pathname.match(/^\/lectures\/(\d+)/);
    if (!match) return;
    const lectureId = match[1];
    let pollTimer;

    const refresh = async () => {
      const response = await fetchImpl(`/lectures/${lectureId}/generation-status`, {
        cache: "no-store",
      });
      const payload = await response.json();
      let active = false;
      documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
        active = render(card, payload[card.dataset.kind]) || active;
      });
      if (active) pollTimer = root.setTimeout(refresh, 2500);
    };

    documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
      card.querySelector("[data-generate]").addEventListener("click", async () => {
        const button = card.querySelector("[data-generate]");
        const message = card.querySelector("[data-generation-message]");
        button.disabled = true;
        message.textContent = "Adding this generation to the queue…";
        try {
          const response = await fetchImpl(
            `/lectures/${lectureId}/${card.dataset.kind}`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken(documentRef),
              },
              body: "{}",
              cache: "no-store",
            },
          );
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.detail || "Generation could not start.");
          }
          render(card, payload);
          root.clearTimeout(pollTimer);
          pollTimer = root.setTimeout(refresh, 1000);
        } catch (error) {
          message.textContent = error.message;
          button.disabled = false;
        }
      });
    });
    void refresh();
  };

  const api = { csrfToken, initialize, render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => initialize(root.document));
  }
})(typeof globalThis === "undefined" ? this : globalThis);
