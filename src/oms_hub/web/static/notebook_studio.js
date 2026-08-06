((root) => {
  "use strict";

  const normalizeSubject = (value) => value.trim().toLowerCase().replace(/\s+/g, " ");

  const csrf = (documentRef) => {
    const item = documentRef.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("study_hub_csrf="));
    return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
  };

  const hasActiveSources = (sources) => sources.some(
    (source) => source.state === "pending" || source.state === "attaching",
  );

  const hasActiveRuns = (runs) => runs.some(
    (run) => ["queued", "running", "retrying"].includes(run.state),
  );

  const importRoleAllowsNotebook = (role) => (
    role === "supporting_reference" || role === "combined_questions_answers"
  );

  const applyImportRoleState = (form) => {
    const role = form.querySelector("[data-import-role]")?.value || "questions";
    const checkbox = form.querySelector("[data-import-notebook]");
    if (checkbox) {
      checkbox.disabled = !importRoleAllowsNotebook(role);
      if (checkbox.disabled) checkbox.checked = false;
    }
    return { role, attach_to_notebook: Boolean(checkbox?.checked) };
  };

  const workflowPanelState = (workflow) => ({
    generate: workflow === "generate",
    import: workflow === "import",
  });

  const renderSources = (documentRef, list, sources) => {
    list.replaceChildren();
    if (!sources.length) {
      const row = documentRef.createElement("li");
      row.textContent = "No sources attached yet.";
      list.append(row);
      return false;
    }
    sources.forEach((source) => {
      const row = documentRef.createElement("li");
      const converted = source.converted_from_pptx ? " · converted from PPTX" : "";
      const error = source.error ? ` · ${source.error}` : "";
      row.textContent = `${source.title} · ${source.type} · ${source.state}${converted}${error}`;
      if (source.state !== "deleted") {
        const remove = documentRef.createElement("button");
        remove.type = "button";
        remove.className = "button secondary compact";
        remove.dataset.deleteSource = source.id;
        remove.textContent = "Delete source";
        row.append(remove);
      }
      list.append(row);
    });
    return hasActiveSources(sources);
  };

  const renderSourcePicker = (documentRef, picker, sources) => {
    const selected = new Set(
      Array.from(picker.querySelectorAll("input:checked"), (input) => input.value),
    );
    picker.replaceChildren();
    const attached = sources.filter((source) => source.state === "attached");
    if (!attached.length) {
      const note = documentRef.createElement("p");
      note.textContent = "No attached sources are available. You can still run with no sources.";
      picker.append(note);
      return;
    }
    attached.forEach((source) => {
      const label = documentRef.createElement("label");
      const checkbox = documentRef.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = source.id;
      checkbox.checked = selected.has(source.id);
      label.append(checkbox);
      const text = documentRef.createElement("span");
      text.textContent = `${source.title} · ${source.type}`;
      label.append(text);
      picker.append(label);
    });
  };

  const selectAllAttachedSources = (picker) => {
    picker.querySelectorAll("input[type=checkbox]").forEach((input) => {
      input.checked = true;
    });
  };

  const imageUrlFromDrop = (documentRef, dataTransfer) => {
    const html = dataTransfer?.getData("text/html") || "";
    if (html) {
      const Parser = documentRef.defaultView?.DOMParser || root.DOMParser;
      const parsed = Parser ? new Parser().parseFromString(html, "text/html") : null;
      const image = parsed?.querySelector("img[src]");
      if (image?.src) return image.src;
    }
    const uriList = dataTransfer?.getData("text/uri-list") || "";
    const uri = uriList.split("\n").map((value) => value.trim()).find(Boolean);
    if (uri) return uri;
    return dataTransfer?.getData("text/plain")?.trim() || "";
  };

  const filterSourcePicker = (picker, query) => {
    const normalized = query.trim().toLowerCase();
    picker.querySelectorAll("label").forEach((label) => {
      label.hidden = normalized !== "" && !label.textContent.toLowerCase().includes(normalized);
    });
  };

  const retryStatus = (run, now = Date.now()) => {
    if (run.state !== "retrying" || !run.next_attempt_at) return run.state;
    const seconds = Math.max(0, Math.ceil((Date.parse(run.next_attempt_at) - now) / 1000));
    return `retrying in ${seconds}s`;
  };

  const renderRuns = (documentRef, container, runs) => {
    container.replaceChildren();
    if (!runs.length) {
      const note = documentRef.createElement("p");
      note.textContent = "No prompt runs yet.";
      container.append(note);
      return false;
    }
    runs.forEach((run) => {
      const card = documentRef.createElement("article");
      card.className = "studio-run";
      card.dataset.runId = run.id;
      const heading = documentRef.createElement("h3");
      heading.textContent = run.label;
      card.append(heading);
      const status = documentRef.createElement("p");
      const error = run.error ? ` · ${run.error}` : "";
      const state = run.state === "awaiting_images" ? "Images needed" : retryStatus(run);
      const directStages = {
        acquire: "acquiring snapshots", parse: "parsing", extract: "extracting questions",
        pair: "pairing answers", answer_notebook: "resolving answers with NotebookLM",
        answer_fallback: "resolving answers", normalize: "preparing review", review: "review ready",
      };
      const stage = run.workflow_kind === "direct_import"
        ? (directStages[run.stage] || run.stage)
        : run.stage;
      status.textContent = `${state} · ${stage} · attempt ${run.attempts}${error}`;
      card.append(status);
      if (run.image_review_url) {
        const images = documentRef.createElement("a");
        images.className = "button primary compact";
        images.href = run.image_review_url;
        images.textContent = "Add images";
        card.append(images);
      }
      if (run.review_url) {
        const review = documentRef.createElement("a");
        review.className = "button primary compact";
        review.href = run.review_url;
        review.textContent = "Review questions";
        card.append(review);
      }
      if (run.published_url) {
        const link = documentRef.createElement("a");
        link.className = "button secondary compact";
        link.href = run.published_url;
        link.textContent = "Open published quiz";
        card.append(link);
        const unpublish = documentRef.createElement("button");
        unpublish.type = "button";
        unpublish.className = "button secondary compact";
        unpublish.dataset.unpublishRun = run.id;
        unpublish.textContent = "Unpublish";
        card.append(unpublish);
      }
      if (run.state === "complete" || run.state === "failed") {
        const rerun = documentRef.createElement("button");
        rerun.type = "button";
        rerun.className = "button secondary compact";
        rerun.dataset.rerun = run.id;
        rerun.textContent = "Re-run";
        card.append(rerun);
      }
      const attempts = run.attempt_history || [];
      attempts.forEach((attempt) => {
        if (!attempt.error) return;
        const details = documentRef.createElement("details");
        const summary = documentRef.createElement("summary");
        summary.textContent = `Attempt ${attempt.attempt_number} · ${attempt.diagnostic_source}`;
        const error = documentRef.createElement("p");
        error.textContent = attempt.error;
        details.append(summary, error);
        card.append(details);
      });
      container.append(card);
    });
    return hasActiveRuns(runs);
  };

  const buildRunPayload = (form, course, exam, destinationCourse, destinationExam) => ({
    subject: course.value,
    exam_number: Number(exam.value),
    prompt: form.elements.prompt.value,
    source_ids: Array.from(
      form.ownerDocument.querySelectorAll("[data-source-picker] input:checked"),
      (input) => input.value,
    ),
    label: form.elements.label.value,
    destination_subject: destinationCourse.value,
    destination_exam_number: Number(destinationExam.value),
  });

  const buildImportRunPayload = (
    form,
    course,
    exam,
    destinationCourse,
    destinationExam,
    rows = form.ownerDocument.querySelectorAll("[data-import-source-row]"),
  ) => ({
    subject: course.value,
    exam_number: Number(exam.value),
    label: form.elements.label.value,
    destination_subject: destinationCourse.value,
    destination_exam_number: Number(destinationExam.value),
    content_kind: "practice_questions",
    sources: Array.from(rows, (row) => ({
      source_id: row.dataset.sourceId,
      role: row.querySelector("[data-import-row-role]")?.value || row.dataset.role,
      attach_to_notebook: Boolean(row.querySelector("[data-import-row-notebook]")?.checked),
    })),
  });

  const appendImportSource = (documentRef, list, source, role, attachToNotebook) => {
    list.querySelector("[data-import-empty]")?.remove();
    const row = documentRef.createElement("li");
    row.dataset.importSourceRow = "true";
    row.dataset.sourceId = source.id;
    row.dataset.role = role;
    const title = documentRef.createElement("span");
    title.textContent = source.title || source.id;
    const select = documentRef.createElement("select");
    select.id = `import-source-role-${source.id}`;
    select.dataset.importRowRole = "true";
    const roleLabel = documentRef.createElement("label");
    roleLabel.htmlFor = select.id;
    roleLabel.textContent = "Role";
    [
      ["questions", "Questions"], ["answer_key", "Answer key"],
      ["supporting_reference", "Supporting reference"],
      ["combined_questions_answers", "Combined questions and answers"],
    ].forEach(([value, label]) => {
      const option = documentRef.createElement("option");
      option.value = value; option.textContent = label; option.selected = value === role;
      select.append(option);
    });
    const notebook = documentRef.createElement("input");
    notebook.type = "checkbox";
    notebook.dataset.importRowNotebook = "true";
    notebook.checked = attachToNotebook && importRoleAllowsNotebook(role);
    notebook.disabled = !importRoleAllowsNotebook(role);
    const notebookLabel = documentRef.createElement("label");
    notebookLabel.append(notebook, documentRef.createTextNode(" Use in NotebookLM for missing answers"));
    const remove = documentRef.createElement("button");
    remove.type = "button"; remove.dataset.removeImportSource = "true"; remove.textContent = "Remove";
    select.addEventListener("change", () => {
      notebook.disabled = !importRoleAllowsNotebook(select.value);
      if (notebook.disabled) notebook.checked = false;
    });
    row.append(title, roleLabel, select, notebookLabel, remove);
    list.append(row);
  };

  const populateExams = (documentRef, course, exam) => {
    exam.replaceChildren();
    const placeholder = documentRef.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select exam";
    exam.append(placeholder);
    const selected = course.options[course.selectedIndex];
    String(selected?.dataset.exams || "")
      .split(",")
      .filter(Boolean)
      .forEach((number) => {
        const option = documentRef.createElement("option");
        option.value = number;
        option.textContent = `Exam ${number}`;
        exam.append(option);
      });
    exam.disabled = !course.value;
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const page = documentRef.querySelector("[data-studio-page]");
    if (!page) return;
    const course = page.querySelector("[data-studio-course]");
    const exam = page.querySelector("[data-studio-exam]");
    const list = page.querySelector("[data-source-list]");
    const sourceStatus = page.querySelector("[data-source-status]");
    const picker = page.querySelector("[data-source-picker]");
    const sourceFilter = page.querySelector("[data-source-filter]");
    const selectAllButton = page.querySelector("[data-select-all-sources]");
    const imageDropzone = page.querySelector("[data-image-dropzone]");
    const imageDropMessage = page.querySelector("[data-image-drop-message]");
    const runList = page.querySelector("[data-run-list]");
    const runForm = page.querySelector("[data-run-form]");
    const destinationCourse = page.querySelector("[data-destination-course]");
    const destinationExam = page.querySelector("[data-destination-exam]");
    const importRunForm = page.querySelector("[data-import-run-form]");
    const importDestinationCourse = page.querySelector("[data-import-destination-course]");
    const importDestinationExam = page.querySelector("[data-import-destination-exam]");
    const importSourceList = page.querySelector("[data-import-source-list]");
    const pollStatus = page.querySelector("[data-poll-status]");
    let pollHandle = null;
    const basePollDelayMs = 2000;
    const maxPollDelayMs = 30000;
    let pollDelayMs = basePollDelayMs;

    const setWorkflow = (workflow) => {
      const state = workflowPanelState(workflow);
      page.querySelectorAll("[data-workflow-panel]").forEach((panel) => {
        panel.hidden = !state[panel.dataset.workflowPanel];
      });
      page.querySelectorAll("[data-workflow-tab]").forEach((tab) => {
        const active = tab.dataset.workflowTab === workflow;
        tab.setAttribute("aria-pressed", String(active));
        tab.classList.toggle("primary", active);
        tab.classList.toggle("secondary", !active);
      });
    };
    page.querySelectorAll("[data-workflow-tab]").forEach((tab) => {
      tab.addEventListener("click", () => setWorkflow(tab.dataset.workflowTab));
    });
    setWorkflow("generate");

    const scheduleRefresh = (delay = basePollDelayMs) => {
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = root.setTimeout(refresh, delay);
    };

    const loadJson = async (url) => {
      const response = await fetchImpl(url, { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Quiz Builder status could not be loaded.");
      return payload;
    };

    const refresh = async () => {
      pollHandle = null;
      if (!course.value || !exam.value) return;
      const query = `subject_key=${encodeURIComponent(normalizeSubject(course.value))}&exam_number=${encodeURIComponent(exam.value)}`;
      try {
        const [sourcePayload, runPayload] = await Promise.all([
          loadJson(`/studio/sources?${query}`),
          loadJson(`/studio/runs?${query}`),
        ]);
        if (pollStatus) pollStatus.textContent = "";
        if (sourceStatus) sourceStatus.textContent = "";
        pollDelayMs = basePollDelayMs;
        const activeSources = renderSources(documentRef, list, sourcePayload.sources || []);
        renderSourcePicker(documentRef, picker, sourcePayload.sources || []);
        filterSourcePicker(picker, sourceFilter.value);
        const activeRuns = renderRuns(documentRef, runList, runPayload.runs || []);
        if (activeSources || activeRuns) scheduleRefresh(pollDelayMs);
      } catch (error) {
        // Keep the previously rendered lists in place; surface the failure
        // in the dedicated status region and keep polling with backoff.
        const message = error instanceof Error ? error.message : "Quiz Builder status could not be loaded.";
        if (pollStatus) pollStatus.textContent = `${message} Retrying…`;
        pollDelayMs = Math.min(pollDelayMs * 2, maxPollDelayMs);
        scheduleRefresh(pollDelayMs);
      }
    };

    course.addEventListener("change", () => {
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = null;
      populateExams(documentRef, course, exam);
      list.textContent = "Select an exam to view sources.";
      picker.textContent = "Select a source course and exam first.";
      runList.textContent = "Select a source course and exam to view runs.";
    });
    exam.addEventListener("change", () => {
      if (exam.value) {
        list.textContent = "";
        const loading = documentRef.createElement("li");
        loading.textContent = "Loading sources…";
        list.append(loading);
        if (sourceStatus) sourceStatus.textContent = "Loading sources…";
      }
      refresh();
    });
    destinationCourse.addEventListener("change", () => {
      populateExams(documentRef, destinationCourse, destinationExam);
    });
    importDestinationCourse?.addEventListener("change", () => {
      populateExams(documentRef, importDestinationCourse, importDestinationExam);
    });
    sourceFilter.addEventListener("input", () => {
      filterSourcePicker(picker, sourceFilter.value);
    });
    selectAllButton?.addEventListener("click", () => {
      selectAllAttachedSources(picker);
    });

    const uploadDroppedImage = async (file) => {
      if (!course.value || !exam.value) {
        imageDropMessage.textContent = "Select a course and exam before dropping an image.";
        return;
      }
      const token = csrf(documentRef);
      const body = new FormData();
      body.append("title", file.name.replace(/\.[^.]+$/, "") || "Dropped image");
      body.append("file", file, file.name || "dropped-image.png");
      body.append("subject", course.value);
      body.append("exam_number", exam.value);
      body.append("csrf_token", token);
      imageDropMessage.textContent = "Uploading image source…";
      try {
        const response = await fetchImpl("/studio/sources/file", {
          method: "POST",
          headers: { "X-CSRF-Token": token },
          body,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Image source could not be queued.");
        imageDropMessage.textContent = "Image source queued for NotebookLM.";
        await refresh();
      } catch (error) {
        imageDropMessage.textContent = error instanceof Error
          ? error.message
          : "Image source could not be queued.";
      }
    };

    const uploadDroppedImageUrl = async (url) => {
      if (!course.value || !exam.value) {
        imageDropMessage.textContent = "Select a course and exam before dropping an image.";
        return;
      }
      const token = csrf(documentRef);
      const body = new FormData();
      body.append("title", "Dropped Google image");
      body.append("url", url);
      body.append("subject", course.value);
      body.append("exam_number", exam.value);
      body.append("csrf_token", token);
      imageDropMessage.textContent = "Downloading image source…";
      try {
        const response = await fetchImpl("/studio/sources/image-url", {
          method: "POST",
          headers: { "X-CSRF-Token": token },
          body,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Image source could not be queued.");
        imageDropMessage.textContent = "Image source queued for NotebookLM.";
        await refresh();
      } catch (error) {
        imageDropMessage.textContent = error instanceof Error
          ? error.message
          : "Image source could not be queued.";
      }
    };

    imageDropzone?.addEventListener("dragover", (event) => {
      event.preventDefault();
      imageDropzone.classList.add("is-dragging");
    });
    imageDropzone?.addEventListener("dragleave", () => {
      imageDropzone.classList.remove("is-dragging");
    });
    imageDropzone?.addEventListener("drop", async (event) => {
      event.preventDefault();
      imageDropzone.classList.remove("is-dragging");
      const images = Array.from(event.dataTransfer?.files || [])
        .filter((file) => file.type.startsWith("image/"));
      if (!images.length) {
        const imageUrl = imageUrlFromDrop(documentRef, event.dataTransfer);
        if (/^https?:\/\//i.test(imageUrl)) {
          await uploadDroppedImageUrl(imageUrl);
        } else {
          imageDropMessage.textContent = "Drop a PNG, JPEG, WebP image, or an image URL.";
        }
        return;
      }
      for (const image of images) await uploadDroppedImage(image);
    });
    imageDropzone?.addEventListener("paste", async (event) => {
      const images = Array.from(event.clipboardData?.files || [])
        .filter((file) => file.type.startsWith("image/"));
      if (!images.length) return;
      event.preventDefault();
      for (const image of images) await uploadDroppedImage(image);
    });
    imageDropzone?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        page.querySelector("[data-source-type=file] input[type=file]")?.click();
      }
    });
    page.addEventListener("click", async (event) => {
      const deleteButton = event.target.closest?.("[data-delete-source]");
      const rerunButton = event.target.closest?.("[data-rerun]");
      const unpublishButton = event.target.closest?.("[data-unpublish-run]");
      const removeImportSource = event.target.closest?.("[data-remove-import-source]");
      if (removeImportSource) {
        removeImportSource.closest("[data-import-source-row]")?.remove();
        if (!importSourceList.children.length) {
          const empty = documentRef.createElement("li");
          empty.dataset.importEmpty = "true";
          empty.textContent = "Add at least one local source.";
          importSourceList.append(empty);
        }
        return;
      }
      const target = deleteButton || rerunButton || unpublishButton;
      if (!target) return;
      const token = csrf(documentRef);
      let url;
      let method;
      if (deleteButton) {
        if (!root.confirm("Delete this source from NotebookLM and future selections?")) return;
        url = `/studio/sources/${encodeURIComponent(deleteButton.dataset.deleteSource)}`;
        method = "DELETE";
      } else if (rerunButton) {
        url = `/studio/runs/${encodeURIComponent(rerunButton.dataset.rerun)}/rerun`;
        method = "POST";
      } else {
        if (!root.confirm("Unpublish this quiz? Private run history will be retained.")) return;
        url = `/studio/runs/${encodeURIComponent(unpublishButton.dataset.unpublishRun)}/publication`;
        method = "DELETE";
      }
      target.disabled = true;
      try {
        const response = await fetchImpl(url, {
          method,
          headers: { "X-CSRF-Token": token },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Quiz Builder action could not be completed.");
        await refresh();
      } catch (error) {
        const detail = error instanceof Error ? error.message : "Quiz Builder action could not be completed.";
        if (deleteButton) {
          if (sourceStatus) sourceStatus.textContent = detail;
        } else {
          runList.textContent = detail;
        }
      } finally {
        target.disabled = false;
      }
    });

    page.querySelectorAll("[data-source-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const message = form.querySelector("[data-form-message]");
        if (!course.value || !exam.value) {
          message.textContent = "Select a course and exam first.";
          return;
        }
        const token = csrf(documentRef);
        const body = new FormData(form);
        body.append("subject", course.value);
        body.append("exam_number", exam.value);
        body.append("csrf_token", token);
        const submitButton = form.querySelector('button[type="submit"]');
        if (submitButton) submitButton.disabled = true;
        try {
          const response = await fetchImpl(`/studio/sources/${form.dataset.sourceType}`, {
            method: "POST",
            headers: { "X-CSRF-Token": token },
            body,
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(payload.detail || "Source could not be queued.");
          message.textContent = "Source queued for attachment.";
          form.reset();
          await refresh();
        } catch (error) {
          message.textContent = error instanceof Error ? error.message : "Source could not be queued.";
        } finally {
          if (submitButton) submitButton.disabled = false;
        }
      });
    });

    page.querySelectorAll("[data-import-source-form]").forEach((form) => {
      applyImportRoleState(form);
      form.querySelector("[data-import-role]")?.addEventListener("change", () => {
        applyImportRoleState(form);
      });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const message = form.querySelector("[data-form-message]");
        if (!course.value || !exam.value) {
          message.textContent = "Select a course and exam first.";
          return;
        }
        const roleState = applyImportRoleState(form);
        const token = csrf(documentRef);
        const body = new FormData(form);
        body.append("subject", course.value);
        body.append("exam_number", exam.value);
        body.append("csrf_token", token);
        const submitButton = form.querySelector('button[type="submit"]');
        if (submitButton) submitButton.disabled = true;
        try {
          const response = await fetchImpl(`/studio/import/sources/${form.dataset.importSourceType}`, {
            method: "POST", headers: { "X-CSRF-Token": token }, body,
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(payload.detail || "Import source could not be queued.");
          appendImportSource(documentRef, importSourceList, {
            id: payload.id,
            title: form.elements.title.value,
          }, roleState.role, roleState.attach_to_notebook);
          message.textContent = "Local import source added.";
          form.reset();
          applyImportRoleState(form);
          await refresh();
        } catch (error) {
          message.textContent = error instanceof Error ? error.message : "Import source could not be queued.";
        } finally {
          if (submitButton) submitButton.disabled = false;
        }
      });
    });

    runForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = runForm.querySelector("[data-run-message]");
      if (!course.value || !exam.value || !destinationCourse.value || !destinationExam.value) {
        message.textContent = "Select both the source and publication course/exam.";
        return;
      }
      const token = csrf(documentRef);
      const submitButton = runForm.querySelector('button[type="submit"]');
      if (submitButton) submitButton.disabled = true;
      try {
        const response = await fetchImpl("/studio/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: JSON.stringify(
            buildRunPayload(runForm, course, exam, destinationCourse, destinationExam),
          ),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Prompt run could not be queued.");
        message.textContent = "Prompt queued in NotebookLM chat.";
        runForm.elements.prompt.value = "";
        await refresh();
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Prompt run could not be queued.";
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });

    importRunForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = importRunForm.querySelector("[data-import-run-message]");
      const rows = importSourceList.querySelectorAll("[data-import-source-row]");
      if (!course.value || !exam.value || !importDestinationCourse.value || !importDestinationExam.value || !rows.length) {
        message.textContent = "Select source and publication course/exam, then add at least one source.";
        return;
      }
      const token = csrf(documentRef);
      const submitButton = importRunForm.querySelector('button[type="submit"]');
      if (submitButton) submitButton.disabled = true;
      try {
        const response = await fetchImpl("/studio/import/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: JSON.stringify(buildImportRunPayload(
            importRunForm, course, exam, importDestinationCourse, importDestinationExam, rows,
          )),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Practice-question review could not be queued.");
        message.textContent = "Practice questions queued for local review.";
        await refresh();
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Practice-question review could not be queued.";
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  };

  const api = {
    appendImportSource,
    buildRunPayload,
    buildImportRunPayload,
    applyImportRoleState,
    filterSourcePicker,
    hasActiveRuns,
    hasActiveSources,
    initialize,
    imageUrlFromDrop,
    normalizeSubject,
    renderRuns,
    renderSources,
    retryStatus,
    selectAllAttachedSources,
    workflowPanelState,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
