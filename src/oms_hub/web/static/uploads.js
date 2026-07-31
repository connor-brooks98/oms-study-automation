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

  const formatLecture = (warning) => {
    const number = String(warning.lecture_number).padStart(2, "0");
    return `${warning.subject} · Lecture ${number} · ${warning.topic}`;
  };

  const nextConfirmation = (batch) => batch.items.find(
    (item) => item.state === "awaiting_confirmation"
      && item.duplicate_warning,
  ) || null;

  const chunkFinalizeUrl = (sessionId, lectureId) => (
    `/api/upload-chunks/${encodeURIComponent(sessionId)}/finalize${
      lectureId ? `?lecture_id=${encodeURIComponent(lectureId)}` : ""
    }`
  );

  const fileKind = (name) => {
    const extension = String(name).toLowerCase().split(".").pop();
    if (extension === "pptx") return "slides";
    if (extension === "txt") return "transcripts";
    return null;
  };

  const postDecision = async (
    fetchImpl,
    itemId,
    decision,
    token,
  ) => {
    const response = await fetchImpl(
      `/api/upload-items/${encodeURIComponent(itemId)}/${decision}`,
      {
        method: "POST",
        headers: { "X-CSRF-Token": token },
        cache: "no-store",
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "Study Hub rejected the decision.");
    }
    return payload;
  };

  const initialize = (documentRef, fetchImpl) => {
    const form = documentRef.querySelector("[data-upload-form]");
    if (!form) return;

    const input = form.querySelector("#upload-files");
    const browse = form.querySelector(".upload-browse");
    const zone = form.querySelector("[data-drop-zone]");
    const selected = form.querySelector("[data-selected-files]");
    const status = documentRef.querySelector("[data-upload-status]");
    const items = documentRef.querySelector("[data-upload-items]");
    const progressWrap = documentRef.querySelector("[data-progress-wrap]");
    const progressBar = documentRef.querySelector("[data-progress-bar]");
    const submit = form.querySelector(".upload-submit");
    const lectureId = form.dataset.lectureId || "";
    const dialog = documentRef.querySelector("[data-duplicate-dialog]");
    const dialogLecture = dialog?.querySelector("[data-duplicate-lecture]");
    const dialogError = dialog?.querySelector("[data-duplicate-error]");
    const confirmButton = dialog?.querySelector("[data-confirm-duplicate]");
    const discardButton = dialog?.querySelector("[data-discard-duplicate]");
    const chunkThreshold = 8 * 1024 * 1024;
    const chunkSize = 5 * 1024 * 1024;
    let chosenFiles = [];
    let pausedItem = null;
    let pausedBatchId = null;
    let resumeDecision = null;
    const batchItems = new Map();

    const csrfHeaders = (headers = {}) => ({
      ...headers,
      "X-CSRF-Token": csrfToken(documentRef),
    });

    const formatBytes = (bytes) => {
      if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    };

    const showFiles = (files) => {
      chosenFiles = Array.from(files);
      selected.replaceChildren();
      chosenFiles.forEach((file) => {
        const row = documentRef.createElement("div");
        row.className = "selected-file";
        const name = documentRef.createElement("span");
        const size = documentRef.createElement("span");
        name.textContent = file.name;
        size.textContent = formatBytes(file.size);
        row.append(name, size);
        selected.append(row);
      });
      status.textContent = chosenFiles.length
        ? `${chosenFiles.length} file${chosenFiles.length === 1 ? "" : "s"} ready.`
        : "Ready for files.";
    };

    const setProgress = (value) => {
      progressWrap.hidden = false;
      progressBar.style.width = `${Math.max(0, Math.min(100, value))}%`;
    };

    const itemStateLabel = (item) => {
      if (
        item.state === "complete"
        && (item.evidence || []).includes(
          "Exact transcript already processed",
        )
      ) {
        return "Already processed · no API request made";
      }
      if (item.state === "awaiting_confirmation") {
        return "Waiting for your decision";
      }
      return item.state.replaceAll("_", " ");
    };

    const renderBatch = (batch) => {
      batchItems.set(batch.id, batch.items);
      items.replaceChildren();
      Array.from(batchItems.values()).flat().forEach((item) => {
        const row = documentRef.createElement("li");
        const name = documentRef.createElement("span");
        const state = documentRef.createElement("span");
        name.textContent = item.original_filename;
        state.textContent = itemStateLabel(item);
        row.append(name, state);
        items.append(row);
      });
      status.textContent = `Batch ${batch.state.replaceAll("_", " ")}.`;
    };

    const showConfirmation = (batch, batchId) => {
      if (!dialog) return false;
      const pending = nextConfirmation(batch);
      if (!pending) return false;
      pausedItem = pending;
      pausedBatchId = batchId;
      dialogLecture.textContent = formatLecture(pending.duplicate_warning);
      dialogError.textContent = "";
      if (!dialog.open) dialog.showModal();
      discardButton.focus();
      return true;
    };

    const pollBatch = async (batchId) => {
      let delay = 750;
      const terminal = new Set([
        "complete",
        "discarded",
        "quarantined",
        "needs_review",
        "failed",
      ]);
      for (let attempt = 0; attempt < 12; attempt += 1) {
        const response = await fetchImpl(`/api/upload-batches/${batchId}`, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        const batch = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(batch.detail || "Could not read upload status.");
        }
        renderBatch(batch);
        if (showConfirmation(batch, batchId)) {
          await new Promise((resolve) => {
            resumeDecision = resolve;
          });
          delay = 750;
          continue;
        }
        if (terminal.has(batch.state)) return batch;
        await new Promise((resolve) => root.setTimeout(resolve, delay));
        delay = Math.min(delay * 1.7, 8000);
      }
      status.textContent = "Files are queued on the NUC. You can leave this page.";
      return null;
    };

    const multipartUpload = (files, kind) => new Promise((resolve, reject) => {
      const body = new FormData();
      files.forEach((file) => body.append("files", file));
      if (lectureId) body.append("lecture_id", lectureId);
      const request = new XMLHttpRequest();
      request.open("POST", `/uploads/${kind}`);
      request.setRequestHeader(
        "X-CSRF-Token",
        csrfToken(documentRef),
      );
      request.responseType = "json";
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) {
          setProgress((event.loaded / event.total) * 100);
        }
      });
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) {
          resolve(request.response);
        } else {
          reject(
            new Error(
              request.response?.detail || "Upload was rejected.",
            ),
          );
        }
      });
      request.addEventListener(
        "error",
        () => reject(new Error("Upload connection failed.")),
      );
      request.send(body);
    });

    const sha256 = async (file) => {
      const digest = await crypto.subtle.digest(
        "SHA-256",
        await file.arrayBuffer(),
      );
      return Array.from(new Uint8Array(digest))
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
    };

    const chunkUpload = async (file, kind) => {
      const created = await fetchImpl("/api/upload-chunks", {
        method: "POST",
        headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          kind,
          filename: file.name,
          total_size: file.size,
          sha256: await sha256(file),
        }),
      });
      const session = await created.json().catch(() => ({}));
      if (!created.ok) {
        throw new Error(
          session.detail || "Upload could not start.",
        );
      }
      if (typeof session.session_id !== "string") {
        throw new Error("Upload could not start.");
      }
      let offset = 0;
      while (offset < file.size) {
        const chunk = file.slice(offset, offset + chunkSize);
        const response = await fetchImpl(
          `/api/upload-chunks/${session.session_id}?offset=${offset}`,
          { method: "PUT", headers: csrfHeaders(), body: chunk },
        );
        const chunkResult = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            chunkResult.detail || "A file chunk failed.",
          );
        }
        if (
          !Number.isInteger(chunkResult.received)
          || chunkResult.received <= offset
        ) {
          throw new Error("A file chunk failed.");
        }
        offset = chunkResult.received;
        setProgress((offset / file.size) * 100);
      }
      const finalized = await fetchImpl(
        chunkFinalizeUrl(session.session_id, lectureId),
        { method: "POST", headers: csrfHeaders() },
      );
      const result = await finalized.json().catch(() => ({}));
      if (!finalized.ok) {
        throw new Error(
          result.detail || "Upload could not finish.",
        );
      }
      if (typeof result.batch_id !== "string") {
        throw new Error("Upload could not finish.");
      }
      return result;
    };

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

    if (dialog) {
      dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
      });
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) event.preventDefault();
      });

      const decide = async (decision) => {
        if (!pausedItem || !pausedBatchId) return;
        confirmButton.disabled = true;
        discardButton.disabled = true;
        dialogError.textContent = "";
        try {
          await postDecision(
            fetchImpl,
            pausedItem.id,
            decision,
            csrfToken(documentRef),
          );
          const resume = resumeDecision;
          pausedItem = null;
          pausedBatchId = null;
          resumeDecision = null;
          dialog.close();
          if (resume) resume();
        } catch (error) {
          dialogError.textContent = error instanceof Error
            ? error.message
            : "Study Hub could not save your decision.";
        } finally {
          confirmButton.disabled = false;
          discardButton.disabled = false;
        }
      };

      confirmButton.addEventListener(
        "click",
        () => decide("confirm"),
      );
      discardButton.addEventListener(
        "click",
        () => decide("discard"),
      );
    }

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
      batchItems.clear();
      try {
        const unsupported = chosenFiles.find((file) => !fileKind(file.name));
        if (unsupported) {
          throw new Error(`${unsupported.name} is not a PPTX or TXT file.`);
        }
        const groups = ["slides", "transcripts"].map((kind) => ({
          kind,
          files: chosenFiles.filter((file) => fileKind(file.name) === kind),
        })).filter((group) => group.files.length);
        for (const group of groups) {
          if (group.files.some((file) => file.size > chunkThreshold)) {
            for (const file of group.files) {
              status.textContent = `Uploading ${file.name}…`;
              const result = await chunkUpload(file, group.kind);
              await pollBatch(result.batch_id);
            }
          } else {
            const result = await multipartUpload(group.files, group.kind);
            setProgress(100);
            await pollBatch(result.batch_id);
          }
        }
      } catch (error) {
        status.textContent = error instanceof Error
          ? error.message
          : "Upload failed.";
      } finally {
        submit.disabled = false;
      }
    });
  };

  const api = {
    chunkFinalizeUrl,
    csrfToken,
    formatLecture,
    fileKind,
    initialize,
    nextConfirmation,
    postDecision,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => {
      initialize(root.document, root.fetch.bind(root));
    });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
