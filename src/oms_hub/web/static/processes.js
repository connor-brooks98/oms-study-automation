(function (root) {
  "use strict";
  const complete = new Set(["complete", "completed", "ready", "attached", "discarded", "deleted", "canceled", "cancelled", "removed"]);
  function selectItems(items, query, filter, lectureId = null) {
    const needle = query.trim().toLowerCase();
    return items.filter(item => (!lectureId || String(item.lecture_id) === lectureId)
      && (filter === "all" || complete.has(item.state) === (filter === "complete"))
      && [item.title, item.family, item.stage, item.backend, item.error].join(" ").toLowerCase().includes(needle));
  }
  function card(documentRef, item, onAction, helpers) {
    const node = (tag, text, className) => {
      const el = documentRef.createElement(tag);
      if (text) el.textContent = text;
      if (className) el.className = className;
      return el;
    };
    const article = node("article", "", "card sh-card process-card");
    article.dataset.processCard = item.id;
    article.append(node("p", item.family.replaceAll("_", " "), "mono-label"));
    article.append(node("h2", item.title));
    article.append(node("p", item.control_label || item.state.replaceAll("_", " "), "sh-pill"));
    const updated = item.updated_at ? new Date(item.updated_at).toLocaleString() : "";
    article.append(node("p", [item.stage?.replaceAll("_", " "), item.backend, updated].filter(Boolean).join(" · "), "process-card-meta"));
    if (item.error) article.append(node("p", item.error, "process-card-error"));
    if (item.next_attempt_at) article.append(node("p", `Next attempt: ${new Date(item.next_attempt_at).toLocaleString()}`, "process-card-meta"));
    const actions = node("div", "", "process-card-actions");
    if (item.detail_url) {
      try {
        const link = node("a", "Open details", "sh-btn sh-btn--secondary");
        link.href = helpers.safeLink(item.detail_url, documentRef.baseURI);
        actions.append(link);
      } catch (_) { /* Omit unexpected destinations. */ }
    }
    const reasons = [];
    for (const action of ["pause", "restart", "remove"]) {
      const config = item.actions?.[action];
      const label = action[0].toUpperCase() + action.slice(1);
      const button = node("button", label, "sh-btn sh-btn--secondary");
      button.type = "button";
      button.disabled = !config?.enabled;
      if (config?.reason && !config.enabled) reasons.push(`${label}: ${config.reason}`);
      button.addEventListener("click", () => onAction(item, action));
      actions.append(button);
    }
    article.append(actions);
    if (reasons.length) article.append(node("p", [...new Set(reasons)].join(" "), "process-card-reasons"));
    return article;
  }
  function initialize(documentRef, fetchImpl = root.fetch.bind(root), runtime = root, helpers = root.StudyGeneration) {
    const page = documentRef.querySelector("[data-processes]");
    if (!page) return;
    const find = name => page.querySelector(`[data-process-${name}]`);
    const list = find("list"), message = find("message"), search = find("search"), filter = find("filter");
    const lectureId = new URL(documentRef.baseURI).searchParams.get("lecture_id");
    if (lectureId) {
      find("scope").hidden = false;
      find("scope").textContent = `Showing processes for lecture #${lectureId}.`;
      find("all").hidden = false;
    }
    let items = [], busy = false, stopped = false, timer;
    function render() {
      const visible = selectItems(items, search.value, filter.value, lectureId);
      list.replaceChildren(...visible.map(item => card(documentRef, item, act, helpers)));
      find("empty").hidden = visible.length > 0;
    }
    function schedule() {
      runtime.clearTimeout(timer);
      if (!stopped) timer = runtime.setTimeout(() => {
        if (documentRef.hidden || documentRef.activeElement?.closest("[data-process-card]")) schedule();
        else refresh();
      }, 5000);
    }
    async function refresh(actionMessage = "") {
      if (busy || stopped) return;
      busy = true;
      runtime.clearTimeout(timer);
      find("refresh").disabled = true;
      try {
        const response = await fetchImpl("/api/processes", {credentials: "same-origin", cache: "no-store"});
        const payload = await response.json();
        if (!response.ok) throw Error(helpers.errorMessage(payload));
        if (!Array.isArray(payload.items)) throw Error("Processes could not be loaded.");
        if (stopped) return;
        items = payload.items;
        render();
        message.textContent = actionMessage || `${items.length} ${items.length === 1 ? "process" : "processes"}. Updates automatically.${payload.completed_history_truncated ? " Showing the most recent 100 completed events." : ""}`;
      } catch (error) { if (!stopped) message.textContent = error.message || "Processes could not be loaded."; }
      finally { busy = false; find("refresh").disabled = false; schedule(); }
    }
    async function act(item, action) {
      if (busy || stopped) return;
      busy = true;
      runtime.clearTimeout(timer);
      list.querySelectorAll("button").forEach(button => { button.disabled = true; });
      message.textContent = `${action === "restart" ? "Restarting" : action === "pause" ? "Pausing" : "Removing"} ${item.title}…`;
      let resultMessage;
      try {
        const payload = await helpers.post(documentRef, fetchImpl, `/api/processes/${encodeURIComponent(item.family)}/${encodeURIComponent(item.job_id)}/${action}`);
        resultMessage = action === "remove" && payload.item?.hidden
          ? "Removed from Processes. Files and history are retained."
          : payload.item?.control_label || "Process updated.";
      } catch (error) { resultMessage = error.message || "The process could not be updated."; }
      finally { busy = false; }
      await refresh(resultMessage);
    }
    search.addEventListener("input", render);
    filter.addEventListener("change", render);
    find("refresh").addEventListener("click", () => refresh());
    const stop = () => { stopped = true; runtime.clearTimeout(timer); };
    runtime.addEventListener("pagehide", stop, {once: true});
    refresh();
    return {refresh, stop};
  }
  const api = {selectItems, card, initialize};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), {once: true});
    else initialize(root.document);
  }
})(typeof globalThis === "undefined" ? this : globalThis);
