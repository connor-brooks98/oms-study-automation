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
  const runPresentation = (state) => ({
    label: ({ queued: "Queued", running: "Generating questions", paused: "Paused", interrupted: "Interrupted", failed: "Failed", awaiting_review: "Ready for question review", complete: "Complete" })[state] || "Status unavailable",
    active: ["queued", "running"].includes(state),
    resume: ["paused", "interrupted", "failed"].includes(state),
    review: ["awaiting_review", "complete"].includes(state),
  });

  function initializeRun(page, documentRef, fetchImpl, runtime = root) {
    const runId = page.dataset.gptRun;
    const path = `/settings/generation/codex/runs/${encodeURIComponent(runId)}`;
    const find = (name) => page.querySelector(`[data-gpt-run-${name}]`);
    const buttons = [find("refresh"), find("cancel"), find("resume")];
    let busy = false;
    let stopped = false;
    let timer;
    async function update(action) {
      if (busy || stopped) return;
      busy = true;
      runtime.clearTimeout(timer);
      buttons.forEach((button) => { button.disabled = true; });
      page.setAttribute("aria-busy", "true");
      find("message").textContent = action ? "Updating quiz request…" : "Checking quiz progress…";
      let active = false;
      try {
        if (action) await post(documentRef, fetchImpl, `${path}/${action}`);
        if (stopped) return;
        const response = await fetchImpl(path, { credentials: "same-origin", cache: "no-store" });
        const payload = await response.json().catch(() => ({}));
        if (stopped) return;
        if (!response.ok) throw new Error(errorMessage(payload, `Request failed (${response.status}).`));
        if (payload.run_id !== runId) throw new Error("The server returned a different quiz request. Refresh to retry.");
        const view = runPresentation(payload.state);
        find("state").textContent = view.label;
        find("cancel").hidden = !view.active;
        find("resume").hidden = !view.resume;
        find("review").hidden = true;
        if (view.review && payload.review_url) {
          find("review").href = safeLink(payload.review_url, documentRef.baseURI);
          find("review").hidden = false;
        }
        find("error").textContent = typeof payload.error === "string" ? payload.error : "";
        find("diagnostic").textContent = typeof payload.diagnostic_source === "string" ? payload.diagnostic_source : "";
        find("message").textContent = view.active ? "Progress updates every 3 seconds." : "Progress is up to date.";
        active = view.active;
      } catch (error) {
        if (!stopped) {
          find("message").textContent = error.message || "Unable to load progress. Refresh to retry.";
          find("cancel").hidden = find("resume").hidden = find("review").hidden = true;
        }
      } finally {
        busy = false;
        if (!stopped) {
          page.setAttribute("aria-busy", "false");
          buttons.forEach((button) => { button.disabled = false; });
          if (active) timer = runtime.setTimeout(() => update(), 3000);
        }
      }
    }
    find("refresh").addEventListener("click", () => update());
    for (const action of ["cancel", "resume"]) find(action).addEventListener("click", () => update(action));
    const stop = () => { stopped = true; runtime.clearTimeout(timer); };
    runtime.addEventListener?.("pagehide", stop, { once: true });
    update();
    return stop;
  }

  function initializePresets(form, documentRef, fetchImpl) {
    const controls = form.querySelector("[data-quiz-presets]");
    if (!controls?.querySelector) return;
    const find = (name) => controls.querySelector(`[data-preset-${name}]`);
    const select = find("select"), name = find("name"), message = find("message");
    const apply = find("apply"), save = find("save"), remove = find("delete");
    const instructions = form.elements.instructions;
    let presets = [], busy = false;
    const selected = () => presets.find((preset) => preset.id === select.value);
    const buttons = () => {
      select.disabled = name.disabled = save.disabled = busy;
      apply.disabled = remove.disabled = busy || !selected();
    };
    const render = (id = "") => {
      select.replaceChildren();
      for (const preset of [{ id: "", name: "New preset" }, ...presets]) {
        const option = documentRef.createElement("option");
        option.value = preset.id;
        option.textContent = preset.name;
        select.append(option);
      }
      select.value = id;
      buttons();
    };
    const request = async (method, id = "", body) => {
      const response = await fetchImpl(`/study/quiz-presets${id ? `/${encodeURIComponent(id)}` : ""}`, {
        method, credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken(documentRef) },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(payload, "Unable to update presets."));
      return payload;
    };
    const act = async (operation) => {
      if (busy) return;
      busy = true;
      buttons();
      try { await operation(); }
      catch (error) { message.textContent = error.message || "Unable to update presets."; }
      finally { busy = false; buttons(); }
    };
    select.addEventListener("change", () => {
      name.value = selected()?.name || "";
      buttons(); // Choosing a preset never overwrites the editable instructions.
    });
    name.addEventListener("keydown", (event) => {
      // This input shares the quiz form; Enter must not dispatch a generation.
      if (event.key === "Enter") event.preventDefault();
    });
    apply.addEventListener("click", () => {
      if (busy || !selected()) return;
      instructions.value = selected().instructions;
      message.textContent = "Preset applied. You can edit the instructions before generating.";
    });
    save.addEventListener("click", () => act(async () => {
      if (!name.value.trim() || !instructions.value.trim()) throw new Error("Enter a preset name and nonblank quiz instructions.");
      const id = selected()?.id || "";
      const preset = await request(id ? "PUT" : "POST", id, { name: name.value, instructions: instructions.value });
      presets = presets.filter((item) => item.id !== preset.id).concat(preset);
      render(preset.id);
      name.value = preset.name;
      message.textContent = "Preset saved.";
    }));
    remove.addEventListener("click", () => act(async () => {
      const id = selected()?.id;
      if (!id) return;
      await request("DELETE", id);
      presets = presets.filter((item) => item.id !== id);
      render();
      message.textContent = "Preset deleted. Your current instructions are still editable and can be saved again.";
    }));
    return act(async () => {
      const payload = await request("GET");
      if (!Array.isArray(payload.presets) || payload.presets.length > 30 || !payload.presets.every((preset) =>
        preset && typeof preset.id === "string" && typeof preset.name === "string" && typeof preset.instructions === "string")) {
        throw new Error("Unable to load saved presets. Your quiz instructions are still editable.");
      }
      presets = payload.presets;
      render();
    });
  }

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
        let loginWindow = null;
        try {
          // Reserve the tab during the click so asynchronous login does not trigger popup blocking.
          if (action === "login") {
            loginWindow = documentRef.defaultView.open("about:blank", "_blank");
            if (loginWindow) loginWindow.opener = null;
          }
          const body = action === "model" ? { model: model.value } : action === "cancel" ? { login_id: loginId } : {};
          const payload = await post(documentRef, fetchImpl, `/settings/generation/codex/${action}`, body);
          if (action === "login") {
            if (!payload.login_id || !payload.url) throw new Error("The server did not return a sign-in challenge.");
            find("[data-gpt-login-link]").href = safeLink(payload.url, documentRef.baseURI, true);
            loginId = payload.login_id;
            find("[data-gpt-code]").textContent = payload.user_code || "";
            find("[data-gpt-code-row]").hidden = !payload.user_code;
            find("[data-gpt-challenge]").hidden = false;
            if (loginWindow && !loginWindow.closed) loginWindow.location.replace(find("[data-gpt-login-link]").href);
            message.textContent = "Sign in in the new tab on this device, or open the link below. Enter the code if asked, then check connection.";
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
          if (loginWindow && !loginWindow.closed) loginWindow.close();
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
      initializePresets(form, documentRef, fetchImpl);
      let busy = false;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (busy || !form.reportValidity()) return;
        busy = true;
        submit.disabled = true;
        form.setAttribute("aria-busy", "true");
        message.textContent = "Creating quiz request…";
        review.hidden = true;
        try {
          const payload = await post(documentRef, fetchImpl, `/lectures/${encodeURIComponent(lecture.dataset.gptLecture)}/gpt-quiz`, {
            instructions: form.elements?.instructions?.value.trim() || "",
          });
          if (!payload.run_id || !payload.review_url) throw new Error("The server did not return a quiz review link.");
          review.href = safeLink(payload.review_url, documentRef.baseURI);
          review.textContent = "Follow quiz progress";
          review.hidden = false;
          message.textContent = `Quiz request ${payload.state || "created"}. Follow quiz progress to see when questions are ready for review.`;
        } catch (error) { message.textContent = error.message || "Request failed."; }
        finally { busy = false; submit.disabled = false; form.setAttribute("aria-busy", "false"); }
      });
    }
    const run = documentRef.querySelector("[data-gpt-run]");
    if (run) return initializeRun(run, documentRef, fetchImpl);
  }

  const api = { objectives, errorMessage, statusText, csrfToken, post, safeLink, runPresentation, initializeRun, initializePresets, initialize };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
    else initialize(root.document);
  }
})(typeof globalThis === "undefined" ? this : globalThis);
