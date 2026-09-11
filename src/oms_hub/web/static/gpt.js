(function (root) {
  "use strict";

  const objectives = (text) => String(text).split(/\r?\n/).map((line) => line.trim())
    .filter(Boolean).map((text, index) => ({ id: `lo-${index + 1}`, text }));
  const errorMessage = (payload, fallback = "Request failed. Please try again.") => {
    if (typeof payload?.detail === "string") return payload.detail;
    if (Array.isArray(payload?.detail)) return payload.detail.map((item) => item.msg).filter(Boolean).join("; ") || fallback;
    return typeof payload?.message === "string" ? payload.message : fallback;
  };
  const statusText = (status) => {
    const account = status.account_connected ? "connected" : "not connected";
    const readiness = status.error_code === "capability_unverified" ? "capabilities not verified"
      : status.state === "connected" && !status.error_code ? "ready"
        : status.state === "limited" ? "paused at usage limit" : status.state || "not checked";
    const reset = status.reset_at ? ` Reset time: ${status.reset_at}.` : "";
    const error = status.error_code && status.error_code !== "capability_unverified" ? ` Reason: ${status.error_code}.` : "";
    return `Account: ${account}. Generation readiness: ${readiness}.${reset}${error}`;
  };
  const csrfToken = (documentRef) => {
    const token = documentRef.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith("study_hub_csrf="));
    return token ? decodeURIComponent(token.slice("study_hub_csrf=".length)) : "";
  };
  const post = async (documentRef, fetchImpl, path, body = {}) => {
    const response = await fetchImpl(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken(documentRef) },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(payload, `Request failed (${response.status}).`));
    return payload;
  };
  const safeLink = (value, base, login = false) => {
    const url = new URL(value, base);
    if (url.username || url.password || (login ? url.protocol !== "https:" : url.origin !== new URL(base).origin || !["https:", "http:"].includes(url.protocol))) {
      throw new Error("The server returned an invalid link.");
    }
    return url.href;
  };

  function initialize(documentRef, fetchImpl = root.fetch.bind(root)) {
    const settings = documentRef.querySelector("[data-gpt-settings]");
    if (settings) {
      const find = (selector) => settings.querySelector(selector);
      const message = find("[data-gpt-message]");
      const model = find("[data-gpt-model]");
      const buttons = [...settings.querySelectorAll("[data-gpt-action]")];
      let loginId = null;
      let busy = false;
      for (const button of buttons) button.addEventListener("click", async () => {
        if (busy) return;
        busy = true;
        settings.setAttribute("aria-busy", "true");
        buttons.forEach((item) => { item.disabled = true; });
        const action = button.dataset.gptAction;
        message.textContent = "Working…";
        try {
          const body = action === "model" ? { model: model.value } : action === "cancel" ? { login_id: loginId } : {};
          const payload = await post(documentRef, fetchImpl, `/settings/generation/codex/${action}`, body);
          if (action === "login") {
            if (!payload.login_id || !payload.url) throw new Error("The server did not return a sign-in challenge.");
            find("[data-gpt-login-link]").href = safeLink(payload.url, documentRef.baseURI, true);
            loginId = payload.login_id;
            find("[data-gpt-code]").textContent = payload.user_code || "";
            find("[data-gpt-code-row]").hidden = !payload.user_code;
            find("[data-gpt-challenge]").hidden = false;
            message.textContent = "Open the sign-in link, then check connection when finished.";
          } else if (action === "status") {
            find("[data-gpt-status]").textContent = statusText(payload);
            const selected = payload.selected_model || model.value;
            model.replaceChildren();
            const ids = Array.isArray(payload.model_ids) ? payload.model_ids : [];
            for (const id of ids) {
              const option = documentRef.createElement("option");
              option.value = id;
              option.textContent = `${id}${payload.image_model_ids?.includes(id) ? " (images)" : ""}`;
              option.selected = id === selected;
              model.append(option);
            }
            if (!ids.length) {
              const option = documentRef.createElement("option");
              option.value = "";
              option.textContent = "No models available";
              model.append(option);
            }
            model.disabled = !ids.length;
            if (payload.account_connected) { loginId = null; find("[data-gpt-challenge]").hidden = true; }
            message.textContent = "Connection checked.";
          } else if (action === "cancel") {
            loginId = null;
            find("[data-gpt-challenge]").hidden = true;
            message.textContent = "Sign-in canceled.";
          } else message.textContent = "Model saved.";
        } catch (error) {
          message.textContent = error.message || "Request failed.";
        } finally {
          busy = false;
          settings.setAttribute("aria-busy", "false");
          buttons.forEach((item) => { item.disabled = item.dataset.gptAction === "model" && model.disabled; });
        }
      });
    }

    const lecture = documentRef.querySelector("[data-gpt-lecture]");
    if (lecture) {
      const form = lecture.querySelector("[data-gpt-quiz-form]");
      const message = lecture.querySelector("[data-gpt-message]");
      const review = lecture.querySelector("[data-gpt-review]");
      const submit = form.querySelector("[type=submit]");
      let busy = false;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (busy || !form.reportValidity()) return;
        const parsed = objectives(form.elements.objectives.value);
        const label = form.elements.label.value.trim();
        if (!parsed.length || !label) { message.textContent = "Enter a quiz title and at least one learning objective."; return; }
        busy = true;
        submit.disabled = true;
        form.setAttribute("aria-busy", "true");
        message.textContent = "Creating quiz request…";
        review.hidden = true;
        try {
          const payload = await post(documentRef, fetchImpl, `/lectures/${encodeURIComponent(lecture.dataset.gptLecture)}/gpt-quiz`, {
            label, objectives: parsed, require_images: form.elements.require_images.checked,
          });
          if (!payload.run_id || !payload.review_url) throw new Error("The server did not return a quiz review link.");
          review.href = safeLink(payload.review_url, documentRef.baseURI);
          review.hidden = false;
          message.textContent = `Quiz request ${payload.state || "created"}. Open question review to follow its progress.`;
        } catch (error) { message.textContent = error.message || "Request failed."; }
        finally { busy = false; submit.disabled = false; form.setAttribute("aria-busy", "false"); }
      });
    }
  }

  const api = { objectives, errorMessage, statusText, csrfToken, post, safeLink, initialize };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
    else initialize(root.document);
  }
})(typeof globalThis === "undefined" ? this : globalThis);
