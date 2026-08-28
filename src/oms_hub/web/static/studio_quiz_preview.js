((root) => {
  "use strict";

  const csrf = (documentRef) => {
    const item = documentRef.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("study_hub_csrf="));
    return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
  };

  const publishQuiz = async (fetchImpl, url, csrfToken) => {
    const response = await fetchImpl(url, {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (
      !response.ok
      || typeof payload.published_url !== "string"
      || !/^\/public\/quizzes\/[0-9a-f]{64}$/.test(payload.published_url)
    ) {
      throw new Error(payload.detail || "Quiz could not be published.");
    }
    return payload.published_url;
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const panel = documentRef.querySelector("[data-studio-preview]");
    if (!panel) return;
    const button = panel.querySelector("[data-publish-quiz]");
    const message = panel.querySelector("[data-publish-message]");
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.dataset.state = "loading";
      button.setAttribute("aria-busy", "true");
      message.textContent = "Publishing…";
      try {
        const url = await publishQuiz(
          fetchImpl,
          panel.dataset.publishUrl,
          csrf(documentRef),
        );
        button.dataset.state = "success";
        button.removeAttribute("aria-busy");
        message.textContent = "Quiz published. Opening the released quiz.";
        await new Promise((resolve) => (root.setTimeout || setTimeout)(resolve, 550));
        root.location.assign(url);
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Quiz could not be published.";
        button.dataset.state = "error";
        button.removeAttribute("aria-busy");
        button.disabled = false;
      }
    });
  };

  const api = { initialize, publishQuiz };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener(
      "DOMContentLoaded",
      () => initialize(root.document),
      { once: true },
    );
  }
})(typeof globalThis === "undefined" ? this : globalThis);
