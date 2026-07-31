((root) => {
  "use strict";

  const csrf = (documentRef) => {
    const item = documentRef.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("study_hub_csrf="));
    return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
  };

  const textNode = (documentRef, tagName, className, value) => {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  };

  const renderReview = (documentRef, container, payload) => {
    container.replaceChildren();
    if (payload.resolved && payload.preview_url) {
      const preview = documentRef.createElement("a");
      preview.className = "button primary studio-preview-link";
      preview.href = payload.preview_url;
      preview.textContent = "Preview quiz";
      container.append(preview);
    }
    if (!payload.requirements?.length) {
      container.append(textNode(
        documentRef,
        "p",
        "studio-image-empty",
        "No image requirements remain.",
      ));
      return;
    }
    payload.requirements.forEach((requirement) => {
      const card = documentRef.createElement("article");
      card.className = "card studio-image-card";
      card.dataset.imageKey = requirement.image_key;
      card.append(
        textNode(documentRef, "h2", "", requirement.image_key),
        textNode(documentRef, "p", "studio-image-source", `Source: ${requirement.source_title}`),
        textNode(documentRef, "p", "studio-image-locator", `Find it: ${requirement.locator}`),
        textNode(documentRef, "p", "studio-image-description", requirement.description),
      );

      const status = requirement.uploaded
        ? `Uploaded ${requirement.original_filename} · ${requirement.width} × ${requirement.height}`
        : "Image still needed";
      card.append(textNode(documentRef, "p", "studio-image-upload-status", status));

      const form = documentRef.createElement("form");
      form.dataset.uploadKey = requirement.image_key;
      form.className = "studio-image-upload";
      const input = documentRef.createElement("input");
      input.type = "file";
      input.name = "file";
      input.accept = "image/png,image/jpeg,image/webp";
      input.required = true;
      const upload = documentRef.createElement("button");
      upload.type = "submit";
      upload.className = "button secondary compact";
      upload.textContent = requirement.uploaded ? "Replace image" : "Upload image";
      form.append(input, upload);
      card.append(form);

      const questions = documentRef.createElement("ul");
      questions.className = "studio-image-question-list";
      requirement.questions.forEach((question) => {
        const row = documentRef.createElement("li");
        row.dataset.questionId = question.id;
        if (question.overridden) row.className = "is-overridden";
        const copy = textNode(
          documentRef,
          "span",
          "studio-image-question-copy",
          `Question ${question.number}: ${question.stem}`,
        );
        const toggle = documentRef.createElement("button");
        toggle.type = "button";
        toggle.className = "button secondary compact";
        toggle.dataset.overrideQuestion = question.id;
        toggle.dataset.overridden = String(question.overridden);
        toggle.textContent = question.overridden ? "Require image" : "No image needed";
        row.append(copy, toggle);
        questions.append(row);
      });
      card.append(questions);
      container.append(card);
    });
  };

  const safeJson = async (response) => {
    try {
      return await response.json();
    } catch (_error) {
      return {};
    }
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const page = documentRef.querySelector("[data-image-review-page]");
    if (!page) return;
    const container = page.querySelector("[data-image-review-list]");
    const message = page.querySelector("[data-image-review-message]");
    const runId = page.dataset.runId;

    const refresh = async () => {
      const response = await fetchImpl(page.dataset.statusUrl, { cache: "no-store" });
      const payload = await safeJson(response);
      if (!response.ok) throw new Error(payload.detail || "Image review could not be loaded.");
      renderReview(documentRef, container, payload);
      return payload;
    };

    page.addEventListener("submit", async (event) => {
      const form = event.target.closest?.("[data-upload-key]");
      if (!form) return;
      event.preventDefault();
      const file = form.querySelector("input[type=file]")?.files?.[0];
      if (!file) {
        message.textContent = "Choose an image first.";
        return;
      }
      const body = new FormData();
      body.append("file", file);
      const token = csrf(documentRef);
      try {
        const response = await fetchImpl(
          `/studio/runs/${encodeURIComponent(runId)}/images/${encodeURIComponent(form.dataset.uploadKey)}`,
          { method: "POST", headers: { "X-CSRF-Token": token }, body },
        );
        const payload = await safeJson(response);
        if (!response.ok) throw new Error(payload.detail || "Image could not be uploaded.");
        message.textContent = "Image saved.";
        await refresh();
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Image could not be uploaded.";
      }
    });

    page.addEventListener("click", async (event) => {
      const button = event.target.closest?.("[data-override-question]");
      if (!button) return;
      const overridden = button.dataset.overridden === "true";
      if (!overridden && !root.confirm("Mark this question as not needing an image?")) return;
      button.disabled = true;
      const token = csrf(documentRef);
      try {
        const response = await fetchImpl(
          `/studio/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(button.dataset.overrideQuestion)}/image-override`,
          { method: overridden ? "DELETE" : "PUT", headers: { "X-CSRF-Token": token } },
        );
        const payload = await safeJson(response);
        if (!response.ok) throw new Error(payload.detail || "Question could not be updated.");
        renderReview(documentRef, container, payload);
        message.textContent = overridden ? "Image requirement restored." : "Question marked as not needing an image.";
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Question could not be updated.";
        button.disabled = false;
      }
    });

    refresh().catch((error) => {
      message.textContent = error instanceof Error ? error.message : "Image review could not be loaded.";
    });
  };

  const api = { initialize, renderReview };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener(
      "DOMContentLoaded",
      () => initialize(root.document),
      { once: true },
    );
  }
})(typeof globalThis === "undefined" ? this : globalThis);
