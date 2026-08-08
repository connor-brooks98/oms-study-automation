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
    const completeMessage = card.dataset.kind === "quiz"
      ? "1 Study Hub quiz is ready."
      : "Lecture outline PDF is ready.";
    const labels = {
      queued: "Queued on the Study Hub device.",
      running: `Working: ${(status.stage || "starting").replaceAll("_", " ")}.`,
      paused: status.message || "Reconnect Google, then try again.",
      failed: status.message || "Generation stopped. You can retry.",
      complete: completeMessage,
      ready: "Ready to generate.",
    };
    message.textContent = labels[status.state] || "Status updated.";
    card.querySelector("[data-generate]").disabled = active;
    if (status.url) {
      let link = card.querySelector("[data-generation-link]");
      if (!link) {
        link = card.ownerDocument.createElement("a");
        link.className = "button primary sh-btn sh-btn--primary";
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

  const runWhenReady = (documentRef, callback) => {
    if (documentRef.readyState === "loading") {
      documentRef.addEventListener("DOMContentLoaded", callback, { once: true });
      return;
    }
    callback();
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const match = root.location.pathname.match(/^\/lectures\/(\d+)/);
    if (!match) return;
    const lectureId = match[1];
    let pollTimer;
    const basePollDelayMs = 2500;
    const maxPollDelayMs = 30000;
    let pollDelayMs = basePollDelayMs;

    const scheduleRefresh = (delay) => {
      pollTimer = root.setTimeout(() => {
        refresh().catch(handlePollError);
      }, delay);
    };

    const handlePollError = (error) => {
      documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
        const message = card.querySelector("[data-generation-message]");
        if (message) message.textContent = `${error.message} Retrying…`;
      });
      pollDelayMs = Math.min(pollDelayMs * 2, maxPollDelayMs);
      scheduleRefresh(pollDelayMs);
    };

    const refresh = async () => {
      const response = await fetchImpl(`/lectures/${lectureId}/generation-status`, {
        cache: "no-store",
      });
      const payload = await response.json();
      let active = false;
      documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
        active = render(card, payload[card.dataset.kind]) || active;
      });
      if (active) {
        pollDelayMs = basePollDelayMs;
        scheduleRefresh(pollDelayMs);
      }
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
          pollDelayMs = basePollDelayMs;
          scheduleRefresh(1000);
        } catch (error) {
          message.textContent = error.message;
          button.disabled = false;
        }
      });
    });
    void refresh().catch(handlePollError);
  };

  const api = { csrfToken, initialize, render, runWhenReady };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    runWhenReady(
      root.document,
      () => initialize(root.document),
    );
  }
})(typeof globalThis === "undefined" ? this : globalThis);
