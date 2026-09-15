(() => {
  "use strict";
  const root = document.querySelector("[data-extendlm]");
  if (!root) return;
  const find = (name) => root.querySelector(`[data-${name}]`);
  const message = find("message"), account = find("account"), notebook = find("notebook");
  let cursor = "", timer, busy = false, signedIn = false;
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
    find("accounts").disabled = busy || !signedIn;
    account.disabled = busy || account.options.length < 2;
    notebook.disabled = busy || notebook.options.length < 2;
    find("upload").disabled = busy || !notebook.value;
    if (find("lecture-upload")) find("lecture-upload").disabled = busy || !notebook.value;
  }
  async function action(work) {
    if (busy) return;
    busy = true; availability(); message.textContent = "Working…";
    try { await work(); message.textContent = ""; }
    catch (error) { message.textContent = error.message; }
    finally { busy = false; availability(); }
  }
  function option(value, text) {
    const item = document.createElement("option"); item.value = value; item.textContent = text; return item;
  }
  async function loadNotebooks(continuation = "") {
    const data = await post("/notebooks", { cursor: continuation });
    if (!continuation) notebook.replaceChildren(option("", "Choose a notebook"));
    data.items.forEach((item) => notebook.append(option(item.id, item.title || "Untitled notebook")));
    cursor = data.next_cursor; find("more").hidden = !cursor; notebook.disabled = false;
    availability();
  }
  function renderJobs(jobs) {
    const list = find("jobs"); list.replaceChildren();
    if (!jobs.length) { list.textContent = "No uploads yet."; return; }
    jobs.forEach((job) => {
      const article = document.createElement("article"); article.className = "settings-subsection";
      const heading = document.createElement("h3"); heading.textContent = `${job.notebook_title} · ${job.status.replaceAll("_", " ")}`;
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
        button.textContent = operation === "resume" ? "Resume this upload" : "Check indexing";
        button.addEventListener("click", () => action(async () => {
          await post(`/jobs/${encodeURIComponent(job.id)}/${operation}`); await refresh();
        })); article.append(button);
      }
      list.append(article);
    });
  }
  async function refresh() {
    clearTimeout(timer);
    const response = await fetch(endpoint + "/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Hub sign-in is unavailable. Reload the page.");
    const data = await response.json();
    signedIn = data.signed_in;
    find("auth-status").textContent = data.signed_in ? "Hub authorization saved. Refresh connections to verify your browser and Google account." : "Sign in to authorize uploads from this browser.";
    find("disconnect").hidden = !data.signed_in;
    find("accounts").disabled = !data.signed_in;
    renderJobs(data.jobs || []);
    availability();
    if ((data.jobs || []).some((j) => ["queued", "uploading"].includes(j.status))) {
      timer = setTimeout(() => refresh().catch((e) => { message.textContent = e.message; }), 4000);
    }
  }
  find("accounts").addEventListener("click", () => action(async () => {
    notebook.replaceChildren(option("", "Choose a notebook")); notebook.disabled = true;
    find("more").hidden = true; account.disabled = true;
    const data = await post("/accounts");
    account.replaceChildren(option("", "Choose an account"));
    data.accounts.forEach((item) => account.append(option(item.id, item.label)));
    account.disabled = !data.accounts.length;
    if (!data.accounts.length) throw new Error("No connected browser. Open ExtendLM, sign into both accounts, and enable MCP.");
  }));
  account.addEventListener("change", () => action(async () => {
    notebook.replaceChildren(option("", "Choose a notebook")); notebook.disabled = true;
    if (!account.value) return;
    await post("/select", { choice: account.value }); await loadNotebooks();
  }));
  notebook.addEventListener("change", availability);
  find("more").addEventListener("click", () => action(() => loadNotebooks(cursor)));
  find("disconnect").addEventListener("click", () => action(async () => {
    await post("/disconnect"); notebook.replaceChildren(option("", "Choose a notebook"));
    account.replaceChildren(option("", "Choose an account")); account.disabled = true;
    notebook.disabled = true; find("more").hidden = true; await refresh();
  }));
  find("files-form").addEventListener("submit", (event) => {
    event.preventDefault();
    action(async () => {
      const body = new FormData(event.target); body.set("notebook_id", notebook.value);
      await post("/uploads", body); event.target.reset(); await refresh();
    });
  });
  if (find("lecture-upload")) find("lecture-upload").addEventListener("click", () => action(async () => {
    await post(`/lectures/${root.dataset.lectureId}/upload`, { notebook_id: notebook.value }); await refresh();
  }));
  refresh().catch((error) => { message.textContent = error.message; });
})();
