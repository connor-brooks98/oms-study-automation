((root) => {
  "use strict";

  const deleteUpload = async ({ item, token, confirm, fetchImpl }) => {
    const accepted = await confirm({
      title: item.cleanup_pending ? "Retry file cleanup" : "Delete upload",
      message: `Delete “${item.original_filename}”? Only this uploaded copy will be removed.`,
      confirmLabel: item.cleanup_pending ? "Retry file cleanup" : "Delete upload",
      cancelLabel: "Keep upload",
    });
    if (!accepted) return null;
    const action = item.lecture_id ? "discard" : "delete";
    const response = await fetchImpl(`/api/upload-items/${encodeURIComponent(item.id)}/${action}`, {
      method: "POST", headers: { "X-CSRF-Token": token }, cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Could not delete upload.");
    return payload;
  };

  const token = (documentRef) => decodeURIComponent(
    (documentRef.cookie.split(";").map((value) => value.trim())
      .find((value) => value.startsWith("study_hub_csrf=")) || "=").split("=").slice(1).join("="),
  );

  const runDelete = (documentRef, button, item, fetchImpl = root.fetch.bind(root)) => deleteUpload({
    item, token: token(documentRef), fetchImpl,
    confirm: (options) => root.StudyHubShell.confirmAction(
      documentRef, options, button, documentRef.defaultView,
    ),
  });

  const initialize = (documentRef) => {
    documentRef.querySelectorAll("[data-delete-upload]").forEach((button) => {
      button.addEventListener("click", async () => {
        const card = button.closest("[data-upload-item]");
        const status = card.querySelector("[data-delete-status]");
        const controls = card.querySelectorAll("button, select, input");
        controls.forEach((control) => { control.disabled = true; });
        try {
          const result = await runDelete(documentRef, button, {
            id: button.dataset.deleteUpload,
            original_filename: button.dataset.filename,
            cleanup_pending: button.dataset.cleanupPending === "true",
            lecture_id: button.dataset.lectureId || null,
          });
          if (!result) return;
          status.textContent = result.message;
          const assignment = card.querySelector("form");
          if (assignment) {
            card.append(button);
            assignment.hidden = true;
          }
          if (result.cleanup_pending) {
            button.dataset.cleanupPending = "true";
            button.textContent = "Retry file cleanup";
          } else {
            button.hidden = true;
          }
        } catch (error) {
          status.textContent = error.message || "Could not delete upload.";
        } finally {
          controls.forEach((control) => { control.disabled = false; });
        }
      });
    });
  };

  const api = { deleteUpload, runDelete, initialize };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.StudyHubUploads = api;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initialize(document));
    else initialize(document);
  }
})(typeof globalThis === "undefined" ? window : globalThis);
