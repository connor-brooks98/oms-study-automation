async function queueExtendLMFiles(post, files, notebookId, onQueued) {
  if (!files.length || files.length > 20) throw new Error("Choose between 1 and 20 files.");
  // Separate requests keep a batch from exceeding the private Hub proxy's body limit.
  for (const file of files) {
    const body = new FormData();
    body.set("files", file); body.set("notebook_id", notebookId);
    await post("/uploads", body);
    await onQueued();
  }
}
if (typeof module !== "undefined") module.exports = { queueExtendLMFiles };
(() => {
  "use strict";
  if (typeof document === "undefined") return;
  const root = document.querySelector("[data-extendlm]");
  if (!root) return;
  const find = (name) => root.querySelector(`[data-${name}]`);
  const message = find("message"), account = find("account"), notebook = find("notebook");
  let cursor = "", timer, busy = false, signedIn = false, notebooksLoaded = false;
  let destination = null, choosing = false, accountsLoaded = false, closed = false;
  const indexingChecks = new Map();
  const lectureId = root.dataset.lectureId;
  const endpoint = "/settings/extendlm";
  async function post(path, fields = {}) {
    const body = fields instanceof FormData ? fields : new FormData();
    if (!(fields instanceof FormData)) Object.entries(fields).forEach(([k, v]) => body.set(k, v));
    body.set("csrf_token", root.dataset.csrf);
    const response = await fetch(endpoint + path, { method: "POST", body });
    let data;
    try { data = await response.json(); } catch { throw new Error("Hub session expired or response unavailable. Reload and sign in."); }
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The Hub could not complete this request.");
    return data;
  }
  function availability() {
    root.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
    if (find("accounts")) find("accounts").disabled = busy || !signedIn;
    if (account) account.disabled = busy || account.options.length < 2;
    notebook.disabled = busy || notebook.options.length < 2;
    if (find("upload")) find("upload").disabled = busy || !notebook.value;
    if (find("lecture-upload")) find("lecture-upload").disabled = busy || !signedIn || !destination;
    if (find("save-destination")) find("save-destination").disabled = busy || !notebook.value;
  }
  async function action(work) {
    if (busy) return;
    busy = true; availability(); message.textContent = "Working…";
    try { await work(); message.textContent = ""; }
    catch (error) {
      // A deleted destination or expired grant may have invalidated saved setup.
      try { await refresh(false); } catch { /* Keep the original actionable failure. */ }
      message.textContent = error.message;
    }
    finally { busy = false; availability(); }
  }
  function option(value, text) {
    const item = document.createElement("option"); item.value = value; item.textContent = text; return item;
  }
  async function loadNotebooks(continuation = "") {
    const data = await post("/notebooks", { cursor: continuation });
    if (!continuation) notebook.replaceChildren(option("", "Choose an exam notebook"));
    data.items.forEach((item) => notebook.append(option(item.id, item.title || "Untitled notebook")));
    cursor = data.next_cursor; find("more").hidden = !cursor; notebook.disabled = false;
    availability();
  }
  async function loadAccounts() {
    const data = await post("/accounts");
    account.replaceChildren(option("", "Choose an account"));
    data.accounts.forEach((item) => account.append(option(item.id, item.label)));
    accountsLoaded = true;
    if (!data.accounts.length) throw new Error("No connected browser. Open ExtendLM, sign into both accounts, and enable MCP.");
  }
  function renderJobs(jobs) {
    const list = find("jobs"); list.replaceChildren();
    if (!jobs.length) { list.textContent = "No uploads yet."; return; }
    jobs.forEach((job) => {
      const article = document.createElement("article"); article.className = "settings-subsection";
      const heading = document.createElement("h3"); heading.textContent = `${job.notebook_title} · ${job.status.replaceAll("_", " ")}${job.account_label ? ` · ${job.account_label}` : ""}`;
      const status = document.createElement("p"); status.textContent = job.message;
      const files = document.createElement("ul");
      job.files.forEach((file) => {
        const item = document.createElement("li");
        item.textContent = `${file.filename} — ${file.stage}${file.source_status ? ` · ${file.source_status}` : ""}`;
        files.append(item);
      });
      article.append(heading, status, files);
      const operation = job.status === "needs_attention" ? "resume" :
        ["accepted", "partial", "ready", "indexing_failed"].includes(job.status) ? "check" : null;
      if (operation) {
        const button = document.createElement("button"); button.type = "button"; button.className = "sh-btn sh-btn--secondary";
        button.textContent = operation === "resume" ? "Resume this upload" : "Refresh indexing";
        button.addEventListener("click", () => action(async () => {
          await post(`/jobs/${encodeURIComponent(job.id)}/${operation}`); await refresh();
        })); article.append(button);
      }
      list.append(article);
    });
  }
  async function refresh(checkIndexing = true) {
    if (closed) return;
    clearTimeout(timer);
    const response = await fetch(endpoint + "/status" + (lectureId ? `?lecture_id=${encodeURIComponent(lectureId)}` : ""), { cache: "no-store" });
    if (!response.ok) throw new Error("Hub sign-in is unavailable. Reload the page.");
    const data = await response.json();
    signedIn = data.signed_in;
    const configured = signedIn && data.selected;
    destination = configured ? data.destination || null : null;
    if (find("auth-status")) find("auth-status").textContent = configured ? `Setup saved · ${data.account_label || "Selected browser/account"}. Choose an exam destination from a lecture.` : signedIn ? "Signed in. Choose your browser and Google account below to finish setup." : "Sign in again to authorize uploads from this browser.";
    if (find("disconnect")) find("disconnect").hidden = !signedIn;
    if (find("setup-needed")) find("setup-needed").hidden = !!configured;
    if (!configured) {
      notebook.replaceChildren(option("", "Choose an exam notebook"));
      notebooksLoaded = false;
      find("more").hidden = true;
    }
    if (find("destination")) find("destination").textContent = destination ? `${destination.notebook_title} · ${destination.account_label || data.account_label || "Selected browser/account"}` : configured ? `Choose a destination for this exam · ${data.account_label || "Selected browser/account"}` : "";
    if (find("lecture-upload")) find("lecture-upload").textContent = destination ? `Send to ${destination.notebook_title}` : "Send to NotebookLM";
    if (find("change-destination")) find("change-destination").hidden = !destination;
    if (find("destination-picker")) find("destination-picker").hidden = !configured || (!!destination && !choosing);
    renderJobs(data.jobs || []);
    availability();
    if (signedIn && account && !accountsLoaded) {
      await loadAccounts();
      return refresh(checkIndexing);
    }
    if (configured && !notebooksLoaded && (!lectureId || !destination || choosing)) {
      notebooksLoaded = true;
      await loadNotebooks();
    }
    let indexingPending = false;
    if (signedIn && checkIndexing) {
      for (const job of data.jobs || []) {
        const count = indexingChecks.get(job.id) || 0;
        if (!["accepted", "partial"].includes(job.status) || count >= 15 ||
            job.files.some(f => f.source_status === "failed") ||
            job.files.every(f => f.source_status === "ready")) continue;
        indexingChecks.set(job.id, count + 1);
        await post(`/jobs/${encodeURIComponent(job.id)}/check`);
        indexingPending = true;
      }
    }
    if (signedIn && (indexingPending || (data.jobs || []).some((j) => ["queued", "uploading"].includes(j.status)))) {
      timer = setTimeout(() => {
        if (busy) { timer = setTimeout(() => refresh().catch((e) => { message.textContent = e.message; }), 4000); return; }
        return refresh().catch((e) => { message.textContent = e.message; });
      }, 4000);
    }
  }
  find("connect-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    action(async () => {
      const data = await post("/connect", new FormData(event.target));
      // Explicit navigation respects the Hub's same-origin form-action policy.
      window.location.assign(data.authorization_url);
    });
  });
  find("accounts")?.addEventListener("click", () => action(async () => {
    notebook.replaceChildren(option("", "Choose an exam notebook")); notebook.disabled = true;
    find("more").hidden = true; account.disabled = true;
    await loadAccounts();
    notebooksLoaded = false;
    await refresh();
  }));
  account?.addEventListener("change", () => action(async () => {
    notebook.replaceChildren(option("", "Choose an exam notebook")); notebook.disabled = true;
    if (!account.value) return;
    await post("/select", { choice: account.value });
    notebooksLoaded = false; await refresh();
  }));
  notebook.addEventListener("change", availability);
  find("more").addEventListener("click", () => action(() => loadNotebooks(cursor)));
  find("disconnect")?.addEventListener("click", () => action(async () => {
    await post("/disconnect"); notebooksLoaded = false; accountsLoaded = false; notebook.replaceChildren(option("", "Choose an exam notebook"));
    account.replaceChildren(option("", "Choose an account")); account.disabled = true;
    notebook.disabled = true; find("more").hidden = true; await refresh();
  }));
  find("files-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    action(async () => {
      const files = Array.from(event.target.querySelector("input[type=file]").files);
      await queueExtendLMFiles(post, files, notebook.value, refresh);
      event.target.reset();
    });
  });
  if (find("lecture-upload")) find("lecture-upload").addEventListener("click", () => action(async () => {
    await post(`/lectures/${lectureId}/upload`); await refresh();
  }));
  find("save-destination")?.addEventListener("click", () => action(async () => {
    const data = await post(`/lectures/${lectureId}/destination`, { notebook_id: notebook.value });
    destination = data.destination; choosing = false; await refresh();
  }));
  find("change-destination")?.addEventListener("click", () => action(async () => {
    choosing = true; notebooksLoaded = false; await refresh();
  }));
  window.addEventListener("pagehide", () => { closed = true; clearTimeout(timer); });
  refresh().catch((error) => { message.textContent = error.message; });
})();
