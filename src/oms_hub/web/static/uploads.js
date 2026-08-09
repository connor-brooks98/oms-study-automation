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

  const freezeManifest = (files, createId = () => crypto.randomUUID()) => (
    Object.freeze(Array.from(files, (file) => Object.freeze({
      file,
      slotId: createId(),
      filename: file.name,
      size: file.size,
    })))
  );

  const batchIsTerminal = (batch) => batch.lifecycle === "terminal";

  const selectionIsLocked = (activeSubmission) => Boolean(activeSubmission);

  const itemErrorText = (item) => item.error || "";

  const rejectionDetail = (payload, fallback) => {
    if (payload?.detail) return payload.detail;
    if (Array.isArray(payload?.errors)) {
      return payload.errors.map((error) => (
        `${error.filename || "Upload"}: ${error.detail || "rejected"}`
      )).join(" ");
    }
    return fallback;
  };

  const requestWithTimeout = async (
    fetchImpl,
    url,
    options = {},
    timeoutMs = 120000,
  ) => {
    const controller = new AbortController();
    const external = options.signal;
    if (external?.aborted) {
      throw new DOMException("Cancelled", "AbortError");
    }
    const cancel = () => controller.abort();
    external?.addEventListener("abort", cancel, { once: true });
    let timedOut = false;
    const timer = root.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      return await fetchImpl(url, { ...options, signal: controller.signal });
    } catch (error) {
      if (timedOut) throw new Error("Upload request timed out.");
      throw error;
    } finally {
      root.clearTimeout(timer);
      external?.removeEventListener("abort", cancel);
    }
  };

  const createDecisionWait = (signal, deadline = Infinity) => {
    let resume;
    const promise = new Promise((resolve, reject) => {
      let timer = null;
      let settled = false;
      const settle = (callback, value) => {
        if (settled) return;
        settled = true;
        signal.removeEventListener("abort", abort);
        if (timer !== null) root.clearTimeout(timer);
        callback(value);
      };
      const abort = () => settle(
        reject, new DOMException("Cancelled", "AbortError"),
      );
      if (signal.aborted) {
        abort();
        return;
      }
      resume = () => settle(resolve);
      signal.addEventListener("abort", abort, { once: true });
      if (Number.isFinite(deadline)) {
        const remaining = deadline - Date.now();
        if (remaining <= 0) {
          settle(reject, new Error("Upload status timed out before a terminal result."));
        } else {
          timer = root.setTimeout(() => {
            settle(reject, new Error("Upload status timed out before a terminal result."));
          }, remaining);
        }
      }
    });
    return { promise, resume: () => resume?.() };
  };

  const chunkFinalizeUrl = (sessionId, lectureId) => (
    `/api/upload-chunks/${encodeURIComponent(sessionId)}/finalize${
      lectureId ? `?lecture_id=${encodeURIComponent(lectureId)}` : ""
    }`
  );

  const postDecision = async (
    fetchImpl,
    itemId,
    decision,
    token,
    signal,
    timeoutMs = 120000,
  ) => {
    const response = await requestWithTimeout(
      fetchImpl,
      `/api/upload-items/${encodeURIComponent(itemId)}/${decision}`,
      {
        method: "POST",
        headers: { "X-CSRF-Token": token },
        cache: "no-store",
        signal,
      },
      timeoutMs,
    );
    const payload = response.status === 204 ? {} : await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Study Hub rejected the decision.");
    }
    return payload;
  };

  const handleDecisionDialogCancel = (event, activeSubmission) => {
    event.preventDefault();
    if (activeSubmission) activeSubmission.controller.abort();
  };

  const cancelManifest = async (fetchImpl, manifestId, headers) => {
    const response = await requestWithTimeout(
      fetchImpl,
      `/api/upload-manifests/${encodeURIComponent(manifestId)}`,
      { method: "DELETE", headers, keepalive: true },
      5000,
    );
    const payload = await response.json();
    if (response.status === 409 && payload.batch_id) {
      return { finalized: true, batchId: payload.batch_id };
    }
    if (!response.ok) throw new Error("Upload cancellation could not be confirmed.");
    return { finalized: false, batchId: null };
  };

  const waitForDecision = async (signal, deadline, setResume, onRejected) => {
    const decision = createDecisionWait(signal, deadline);
    setResume(decision.resume);
    try {
      await decision.promise;
    } catch (error) {
      onRejected();
      throw error;
    } finally {
      setResume(null);
    }
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
    const kind = form.dataset.kind;
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
    let activeSubmission = null;

    const csrfHeaders = (headers = {}) => ({
      ...headers,
      "X-CSRF-Token": csrfToken(documentRef),
    });

    const formatBytes = (bytes) => {
      if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    };

    const showFiles = (files) => {
      if (selectionIsLocked(activeSubmission)) return;
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
      items.replaceChildren();
      batch.items.forEach((item) => {
        const row = documentRef.createElement("li");
        const name = documentRef.createElement("span");
        const state = documentRef.createElement("span");
        name.textContent = item.original_filename;
        state.textContent = itemStateLabel(item);
        row.append(name, state);
        if (itemErrorText(item)) {
          const error = documentRef.createElement("span");
          error.className = "upload-item-error";
          error.textContent = itemErrorText(item);
          row.append(error);
        }
        items.append(row);
      });
      status.textContent = `Batch ${batch.outcome.replaceAll("_", " ")}.`;
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

    const clearPausedDecision = () => {
      if (dialog?.open) dialog.close();
      pausedItem = null;
      pausedBatchId = null;
      resumeDecision = null;
    };

    const fetchWithTimeout = (url, options = {}, timeoutMs = 120000) => (
      requestWithTimeout(fetchImpl, url, options, timeoutMs)
    );

    const pollBatch = async (batchId, signal, deadline) => {
      let delay = 750;
      while (Date.now() < deadline) {
        if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
        const response = await fetchWithTimeout(`/api/upload-batches/${batchId}`, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          signal,
        });
        if (!response.ok) throw new Error("Could not read upload status.");
        const batch = await response.json();
        renderBatch(batch);
        if (showConfirmation(batch, batchId)) {
          await waitForDecision(
            signal,
            deadline,
            (resume) => { resumeDecision = resume; },
            clearPausedDecision,
          );
          delay = 750;
          continue;
        }
        if (batchIsTerminal(batch)) return batch;
        await new Promise((resolve) => root.setTimeout(resolve, delay));
        delay = Math.min(delay * 1.7, 8000);
      }
      throw new Error("Upload status timed out before a terminal result.");
    };

    const multipartUpload = (manifestId, slots, signal) => new Promise((resolve, reject) => {
      const body = new FormData();
      slots.forEach((slot) => {
        body.append("files", slot.file);
        body.append("slot_ids", slot.slotId);
      });
      body.append("manifest_id", manifestId);
      if (lectureId) body.append("lecture_id", lectureId);
      const request = new XMLHttpRequest();
      request.open("POST", `/uploads/${kind}`);
      request.setRequestHeader(
        "X-CSRF-Token",
        csrfToken(documentRef),
      );
      request.responseType = "json";
      request.timeout = 120000;
      const abort = () => request.abort();
      signal.addEventListener("abort", abort, { once: true });
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
              rejectionDetail(request.response, "Upload was rejected."),
            ),
          );
        }
      });
      request.addEventListener(
        "error",
        () => reject(new Error("Upload connection failed.")),
      );
      request.addEventListener(
        "abort",
        () => reject(new DOMException("Cancelled", "AbortError")),
      );
      request.addEventListener(
        "timeout",
        () => reject(new Error("Upload request timed out.")),
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

    const chunkUpload = async (manifestId, slot, signal) => {
      const file = slot.file;
      const created = await fetchWithTimeout("/api/upload-chunks", {
        method: "POST",
        headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          kind,
          filename: file.name,
          total_size: file.size,
          sha256: await sha256(file),
          manifest_id: manifestId,
          slot_id: slot.slotId,
        }),
        signal,
      });
      if (!created.ok) {
        throw new Error(
          (await created.json()).detail || "Upload could not start.",
        );
      }
      const session = await created.json();
      let offset = 0;
      while (offset < file.size) {
        const chunk = file.slice(offset, offset + chunkSize);
        const response = await fetchWithTimeout(
          `/api/upload-chunks/${session.session_id}?offset=${offset}`,
          { method: "PUT", headers: csrfHeaders(), body: chunk, signal },
        );
        if (!response.ok) {
          throw new Error(
            (await response.json()).detail || "A file chunk failed.",
          );
        }
        offset = (await response.json()).received;
        setProgress((offset / file.size) * 100);
      }
      const finalized = await fetchWithTimeout(
        chunkFinalizeUrl(session.session_id, lectureId),
        { method: "POST", headers: csrfHeaders(), signal },
      );
      if (!finalized.ok) {
        throw new Error(
          (await finalized.json()).detail || "Upload could not finish.",
        );
      }
      return finalized.json();
    };

    const createManifest = async (slots, signal) => {
      const files = [];
      for (const slot of slots) {
        files.push({
          slot_id: slot.slotId,
          filename: slot.filename,
          size_bytes: slot.size,
          sha256: await sha256(slot.file),
        });
      }
      const response = await fetchWithTimeout("/api/upload-manifests", {
        method: "POST",
        headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ kind, lecture_id: lectureId || null, files }),
        signal,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(rejectionDetail(payload, "Upload could not start."));
      return payload.manifest_id;
    };

    browse.addEventListener("click", () => {
      if (!selectionIsLocked(activeSubmission)) input.click();
    });
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
      if (selectionIsLocked(activeSubmission)) return;
      input.files = event.dataTransfer.files;
      showFiles(input.files);
    });

    if (dialog) {
      dialog.addEventListener("cancel", (event) => {
        handleDecisionDialogCancel(event, activeSubmission);
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
            activeSubmission?.controller.signal,
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
      if (activeSubmission) {
        activeSubmission.controller.abort();
        return;
      }
      if (!chosenFiles.length) {
        status.textContent = "Choose at least one file.";
        input.focus();
        return;
      }
      const snapshot = freezeManifest(chosenFiles);
      const controller = new AbortController();
      const deadline = Date.now() + (20 * 60 * 1000);
      activeSubmission = { controller, manifestId: null };
      input.disabled = true;
      browse.disabled = true;
      zone.setAttribute("aria-disabled", "true");
      submit.textContent = "Cancel upload";
      status.textContent = "Uploading to the NUC…";
      items.replaceChildren();
      try {
        const manifestId = await createManifest(snapshot, controller.signal);
        activeSubmission.manifestId = manifestId;
        const small = snapshot.filter((slot) => slot.size <= chunkThreshold);
        const large = snapshot.filter((slot) => slot.size > chunkThreshold);
        if (small.length) await multipartUpload(manifestId, small, controller.signal);
        for (const slot of large) {
          status.textContent = `Uploading ${slot.filename}…`;
          await chunkUpload(manifestId, slot, controller.signal);
        }
        const finalized = await fetchWithTimeout(
          `/api/upload-manifests/${encodeURIComponent(manifestId)}/finalize`,
          { method: "POST", headers: csrfHeaders(), signal: controller.signal },
        );
        const result = await finalized.json();
        if (!finalized.ok) {
          throw new Error(rejectionDetail(result, "Upload was rejected."));
        }
        setProgress(100);
        await pollBatch(result.batch_id, controller.signal, deadline);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          const manifestId = activeSubmission?.manifestId;
          if (manifestId) {
            try {
              const cancelled = await cancelManifest(
                fetchImpl, manifestId, csrfHeaders(),
              );
              if (cancelled.finalized) {
                clearPausedDecision();
                status.textContent = "Upload was already finalized.";
                return;
              }
            } catch (_cancelError) {
              clearPausedDecision();
              status.textContent = "Upload cancellation could not be confirmed.";
              return;
            }
          }
          clearPausedDecision();
          status.textContent = "Upload cancelled.";
        } else {
          clearPausedDecision();
          status.textContent = error instanceof Error
            ? error.message
            : "Upload failed.";
        }
      } finally {
        activeSubmission = null;
        input.disabled = false;
        browse.disabled = false;
        zone.removeAttribute("aria-disabled");
        submit.textContent = "Upload files";
      }
    });
  };

  const api = {
    chunkFinalizeUrl,
    csrfToken,
    batchIsTerminal,
    createDecisionWait,
    handleDecisionDialogCancel,
    cancelManifest,
    waitForDecision,
    freezeManifest,
    itemErrorText,
    rejectionDetail,
    requestWithTimeout,
    selectionIsLocked,
    formatLecture,
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
