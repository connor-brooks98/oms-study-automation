((root) => {
  "use strict";

  const validateSelection = (files, kind, lectureId) => {
    if (!/^[1-9]\d*$/.test(String(lectureId))) throw new Error("This upload needs an exact lecture.");
    if (files.length !== 1) throw new Error("Choose one file for this panel.");
    const file = files[0];
    const allowed = kind === "slides" ? /\.(pptx|pdf)$/i : kind === "transcripts" ? /\.txt$/i : null;
    if (!allowed || !allowed.test(file.name)) throw new Error(kind === "slides" ? "Choose a PPTX or PDF for Slides." : "Choose a TXT file for Transcript.");
    if (!file.size) throw new Error("The selected file is empty.");
    return file;
  };

  const request = async (fetchImpl, url, options = {}) => {
    const controller = new AbortController();
    const timer = root.setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetchImpl(url, { ...options, signal: controller.signal, cache: "no-store" });
      const payload = response.status === 204 ? {} : await response.json();
      if (!response.ok) {
        const detail = typeof payload.detail === "string" ? payload.detail : payload.errors?.map((item) => item.detail).join(" ");
        throw new Error(detail || "Study Hub could not complete this request. Check this lecture’s processes before trying again.");
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error("The request outcome could not be confirmed. Check this lecture’s processes before trying again.");
      throw error;
    } finally {
      root.clearTimeout(timer);
    }
  };

  const uploadFile = async ({ file, kind, lectureId, csrf, fetchImpl, onProgress = () => {} }) => {
    validateSelection([file], kind, lectureId);
    const headers = { "X-CSRF-Token": csrf };
    if (file.size <= 8 * 1024 * 1024) {
      const body = new FormData();
      body.append("lecture_id", String(lectureId));
      body.append("files", file);
      return request(fetchImpl, `/uploads/${kind}`, { method: "POST", headers, body });
    }
    const sha256 = Array.from(new Uint8Array(await root.crypto.subtle.digest("SHA-256", await file.arrayBuffer())), (n) => n.toString(16).padStart(2, "0")).join("");
    const slotId = root.crypto.randomUUID();
    const jsonHeaders = { ...headers, "Content-Type": "application/json" };
    const created = await request(fetchImpl, "/api/upload-manifests", {
      method: "POST", headers: jsonHeaders,
      body: JSON.stringify({ kind, lecture_id: Number(lectureId), files: [{ slot_id: slotId, filename: file.name, size_bytes: file.size, sha256 }] }),
    });
    const manifest = encodeURIComponent(created.manifest_id);
    let finalizing = false;
    try {
      const session = await request(fetchImpl, "/api/upload-chunks", {
        method: "POST", headers: jsonHeaders,
        body: JSON.stringify({ kind, filename: file.name, total_size: file.size, sha256, manifest_id: created.manifest_id, slot_id: slotId }),
      });
      const chunk = encodeURIComponent(session.session_id);
      let offset = 0;
      while (offset < file.size) {
        const end = Math.min(file.size, offset + 5 * 1024 * 1024);
        const result = await request(fetchImpl, `/api/upload-chunks/${chunk}?offset=${offset}`, {
          method: "PUT", headers, body: file.slice(offset, end),
        });
        if (result.received !== end) throw new Error("Upload progress could not be verified. Check processes before retrying.");
        offset = result.received;
        onProgress(Math.round(100 * offset / file.size));
      }
      await request(fetchImpl, `/api/upload-chunks/${chunk}/finalize?lecture_id=${encodeURIComponent(lectureId)}`, { method: "POST", headers });
      finalizing = true;
      return await request(fetchImpl, `/api/upload-manifests/${manifest}/finalize`, { method: "POST", headers });
    } catch (error) {
      // Once finalization is sent its acceptance may be durable. Leave recovery to
      // existing server reconciliation; never resend or erase an uncertain outcome.
      if (!finalizing) {
        try { await request(fetchImpl, `/api/upload-manifests/${manifest}`, { method: "DELETE", headers }); } catch (_) { /* Server expiry retains recoverable staging. */ }
      }
      throw error;
    }
  };

  const pollBatch = async (batchId, { fetchImpl, onStatus, sleep = (ms) => new Promise((resolve) => root.setTimeout(resolve, ms)), attempts = 120 }) => {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const batch = await request(fetchImpl, `/api/upload-batches/${encodeURIComponent(batchId)}`);
      onStatus(batch);
      if (batch.lifecycle === "terminal" || batch.items.some((item) => item.state === "awaiting_confirmation")) return batch;
      await sleep(2000);
    }
    throw new Error("Files are still processing. Follow this lecture’s processes, then refresh files.");
  };

  const syncSharedMaterialState = (documentRef, updated) => {
    for (const selector of ["[data-processing-checklist]", "[data-material-reviews]"]) {
      const current = documentRef.querySelector(selector);
      const next = updated.querySelector(selector);
      if (!current || !next) continue;
      if (selector === "[data-processing-checklist]") next.open = current.open;
      current.replaceWith(next);
    }

  };

  const initialize = (documentRef, fetchImpl) => {
    const initializePanel = (panel) => {
      const form = panel.querySelector("[data-material-upload]");
      const input = panel.querySelector("[data-material-file]");
      const zone = panel.querySelector("[data-material-dropzone]");
      const picker = panel.querySelector("[data-material-picker]");
      const submit = picker || panel.querySelector("[data-material-submit]");
      const status = panel.querySelector("[data-material-status]");
      const recovery = panel.querySelector("[data-material-recovery]");
      const decision = panel.querySelector("[data-material-decision]");
      const { kind, lectureId, csrf } = panel.dataset;
      let selection = null;
      let busy = false;
      let pending = null;
      const setBusy = (value) => {
        busy = value;
        if (input) input.disabled = value || Boolean(pending);
        if (submit) submit.disabled = value || Boolean(pending);
        zone?.setAttribute("aria-disabled", String(value || Boolean(pending)));
        panel.setAttribute("aria-busy", String(value));
        panel.querySelectorAll("form:not([data-material-upload]) button").forEach((button) => { button.disabled = value; });
      };
      const report = (message) => { status.textContent = message; recovery.hidden = false; };
      const refresh = async () => {
        const response = await fetchImpl(`/lectures/${lectureId}`, { cache: "no-store" });
        if (!response.ok) throw new Error("Could not refresh files. Use Refresh files to try again.");
        const updated = new root.DOMParser().parseFromString(await response.text(), "text/html");
        const next = updated.querySelector(`[data-material-panel][data-kind="${kind}"][data-lecture-id="${lectureId}"]`);
        if (!next) throw new Error("Could not refresh files. Open the lecture again.");
        syncSharedMaterialState(documentRef, updated);
        panel.replaceWith(next);
        initializePanel(next);
      };
      const follow = async (batchId) => {
        const batch = await pollBatch(batchId, { fetchImpl, onStatus: (batch) => {
          const item = batch.items[0];
          report(item?.error || `File ${String(item?.state || "processing").replaceAll("_", " ")}.`);
        } });
        const item = batch.items.find((candidate) => candidate.state === "awaiting_confirmation");
        if (item) {
          pending = { itemId: item.id, batchId };
          decision.hidden = false;
          report("Choose whether to process this transcript replacement.");
        } else if (batch.items.every((candidate) => candidate.state === "complete")) {
          await refresh();
        } else {
          report(batch.items.find((candidate) => candidate.error)?.error || "The upload needs attention. Review replacements or open this lecture’s processes.");
        }
      };
      const choose = (files) => {
        if (busy || pending) return;
        selection = null;
        try {
          selection = validateSelection(Array.from(files), kind, lectureId);
          status.textContent = `${selection.name} selected. Ready to upload to this lecture.`;
        } catch (error) { report(error.message); }
      };
      if (form) {
        const chooseAndUpload = (files) => {
          if (busy || pending) return;
          choose(files);
          if (picker && selection) form.requestSubmit();
        };
        picker?.addEventListener("click", () => {
          if (!busy && !pending) { input.value = ""; input.click(); }
        });
        input.addEventListener("change", () => chooseAndUpload(input.files));
        ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
          event.preventDefault();
          if (!busy && !pending) zone.classList.add("is-dragging");
        }));
        zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
        zone.addEventListener("drop", (event) => {
          event.preventDefault();
          zone.classList.remove("is-dragging");
          chooseAndUpload(event.dataTransfer.files);
        });
        // Native file validation cannot see a dropped File held in this panel.
        form.noValidate = true;
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          if (busy || pending) return;
          try {
            const chosen = validateSelection(selection ? [selection] : [], kind, lectureId);
            setBusy(true);
            report("Uploading to this lecture…");
            const result = await uploadFile({ file: chosen, kind, lectureId, csrf, fetchImpl, onProgress: (percent) => { status.textContent = `Uploading ${percent}%`; } });
            selection = null;
            input.value = "";
            await follow(result.batch_id);
          } catch (error) {
            report(error.message);
            if (/Restore the retained material/i.test(error.message)) {
              const removed = panel.querySelector("[data-material-removed]");
              if (removed) removed.open = true;
            }
          } finally { setBusy(false); }
        });
      }
      const options = panel.querySelector(".material-options");
      options?.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          options.open = false;
          options.querySelector("summary").focus();
        }
      });
      for (const action of ["confirm", "discard"]) {
        panel.querySelector(`[data-material-${action}]`).addEventListener("click", async () => {
          if (!pending || busy) return;
          setBusy(true);
          decision.querySelectorAll("button").forEach((button) => { button.disabled = true; });
          try {
            await request(fetchImpl, `/api/upload-items/${encodeURIComponent(pending.itemId)}/${action}`, { method: "POST", headers: { "X-CSRF-Token": csrf } });
            const batchId = pending.batchId;
            pending = null;
            decision.hidden = true;
            if (action === "confirm") await follow(batchId);
            else await refresh();
          } catch (error) { report(error.message); }
          finally {
            setBusy(false);
            decision.querySelectorAll("button").forEach((button) => { button.disabled = false; });
          }
        });
      }
      panel.querySelector("[data-material-refresh]").addEventListener("click", async (event) => {
        event.preventDefault();
        if (busy || pending) return;
        try { setBusy(true); await refresh(); } catch (error) { report(error.message); } finally { setBusy(false); }
      });
      panel.querySelectorAll("form:not([data-material-upload])").forEach((actionForm) => actionForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (busy || pending) return;
        try {
          setBusy(true);
          if (actionForm.dataset.materialRemove) {
            const label = actionForm.dataset.materialRemove;
            const message = `Remove this ${label} from the lecture? Original files are retained and can be restored using Restore.`;
            const accepted = root.StudyHubShell
              ? await root.StudyHubShell.confirmAction(documentRef, {
                title: `Delete ${label}?`, message, confirmLabel: `Delete ${label}`, cancelLabel: "Keep it",
              }, event.submitter)
              : root.confirm?.(message);
            if (!accepted) { setBusy(false); event.submitter?.focus(); return; }
          }
          const response = await fetchImpl(actionForm.action, { method: "POST", body: new FormData(actionForm), headers: { "X-CSRF-Token": csrf } });
          if (!response.ok) {
            const payload = await response.json();
            throw new Error(payload.detail || "Material change could not finish.");
          }
          await refresh();
        } catch (error) { report(error.message || "Check the lecture before retrying."); }
        finally { setBusy(false); }
      }));
    };
    documentRef.querySelectorAll("[data-material-panel]").forEach(initializePanel);
  };

  if (typeof module !== "undefined" && module.exports) module.exports = { validateSelection, uploadFile, pollBatch, syncSharedMaterialState, initialize };
  if (root.document) root.document.addEventListener("DOMContentLoaded", () => initialize(root.document, root.fetch.bind(root)));
})(typeof globalThis === "undefined" ? this : globalThis);
