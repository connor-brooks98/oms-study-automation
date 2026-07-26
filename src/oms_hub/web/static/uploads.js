(() => {
  const form = document.querySelector("[data-upload-form]");
  if (!form) return;

  const input = form.querySelector("#upload-files");
  const browse = form.querySelector(".upload-browse");
  const zone = form.querySelector("[data-drop-zone]");
  const selected = form.querySelector("[data-selected-files]");
  const status = document.querySelector("[data-upload-status]");
  const items = document.querySelector("[data-upload-items]");
  const progressWrap = document.querySelector("[data-progress-wrap]");
  const progressBar = document.querySelector("[data-progress-bar]");
  const submit = form.querySelector(".upload-submit");
  const kind = form.dataset.kind;
  const chunkThreshold = 8 * 1024 * 1024;
  const chunkSize = 5 * 1024 * 1024;
  let chosenFiles = [];

  const csrfToken = () => {
    const prefix = "study_hub_csrf=";
    const cookie = document.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  };

  const csrfHeaders = (headers = {}) => ({
    ...headers,
    "X-CSRF-Token": csrfToken(),
  });

  const formatBytes = (bytes) => {
    if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  function showFiles(files) {
    chosenFiles = Array.from(files);
    selected.replaceChildren();
    chosenFiles.forEach((file) => {
      const row = document.createElement("div");
      row.className = "selected-file";
      const name = document.createElement("span");
      const size = document.createElement("span");
      name.textContent = file.name;
      size.textContent = formatBytes(file.size);
      row.append(name, size);
      selected.append(row);
    });
    status.textContent = chosenFiles.length
      ? `${chosenFiles.length} file${chosenFiles.length === 1 ? "" : "s"} ready.`
      : "Ready for files.";
  }

  function setProgress(value) {
    progressWrap.hidden = false;
    progressBar.style.width = `${Math.max(0, Math.min(100, value))}%`;
  }

  function renderBatch(batch) {
    items.replaceChildren();
    batch.items.forEach((item) => {
      const row = document.createElement("li");
      const name = document.createElement("span");
      const state = document.createElement("span");
      name.textContent = item.original_filename;
      state.textContent = item.state.replaceAll("_", " ");
      row.append(name, state);
      items.append(row);
    });
    status.textContent = `Batch ${batch.state.replaceAll("_", " ")}.`;
  }

  async function pollBatch(batchId) {
    let delay = 750;
    const terminal = new Set(["complete", "quarantined", "needs_review", "failed"]);
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const response = await fetch(`/api/upload-batches/${batchId}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Could not read upload status.");
      const batch = await response.json();
      renderBatch(batch);
      if (terminal.has(batch.state)) return batch;
      await new Promise((resolve) => window.setTimeout(resolve, delay));
      delay = Math.min(delay * 1.7, 8000);
    }
    status.textContent = "Files are queued on the NUC. You can leave this page.";
    return null;
  }

  function multipartUpload(files) {
    return new Promise((resolve, reject) => {
      const body = new FormData();
      files.forEach((file) => body.append("files", file));
      const request = new XMLHttpRequest();
      request.open("POST", `/uploads/${kind}`);
      request.setRequestHeader("X-CSRF-Token", csrfToken());
      request.responseType = "json";
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) setProgress((event.loaded / event.total) * 100);
      });
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) resolve(request.response);
        else reject(new Error(request.response?.detail || "Upload was rejected."));
      });
      request.addEventListener("error", () => reject(new Error("Upload connection failed.")));
      request.send(body);
    });
  }

  async function sha256(file) {
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }

  async function chunkUpload(file) {
    const created = await fetch("/api/upload-chunks", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        kind,
        filename: file.name,
        total_size: file.size,
        sha256: await sha256(file),
      }),
    });
    if (!created.ok) throw new Error((await created.json()).detail || "Upload could not start.");
    const session = await created.json();
    let offset = 0;
    while (offset < file.size) {
      const chunk = file.slice(offset, offset + chunkSize);
      const response = await fetch(
        `/api/upload-chunks/${session.session_id}?offset=${offset}`,
        { method: "PUT", headers: csrfHeaders(), body: chunk },
      );
      if (!response.ok) throw new Error((await response.json()).detail || "A file chunk failed.");
      offset = (await response.json()).received;
      setProgress((offset / file.size) * 100);
    }
    const finalized = await fetch(
      `/api/upload-chunks/${session.session_id}/finalize`,
      { method: "POST", headers: csrfHeaders() },
    );
    if (!finalized.ok) throw new Error((await finalized.json()).detail || "Upload could not finish.");
    return finalized.json();
  }

  browse.addEventListener("click", () => input.click());
  input.addEventListener("change", () => showFiles(input.files));
  ["dragenter", "dragover"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
    });
  });
  zone.addEventListener("drop", (event) => {
    input.files = event.dataTransfer.files;
    showFiles(input.files);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!chosenFiles.length) {
      status.textContent = "Choose at least one file.";
      input.focus();
      return;
    }
    submit.disabled = true;
    status.textContent = "Uploading to the NUC…";
    items.replaceChildren();
    try {
      if (chosenFiles.some((file) => file.size > chunkThreshold)) {
        for (let index = 0; index < chosenFiles.length; index += 1) {
          status.textContent = `Uploading ${chosenFiles[index].name}…`;
          const result = await chunkUpload(chosenFiles[index]);
          await pollBatch(result.batch_id);
        }
      } else {
        const result = await multipartUpload(chosenFiles);
        setProgress(100);
        await pollBatch(result.batch_id);
      }
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "Upload failed.";
    } finally {
      submit.disabled = false;
    }
  });
})();
