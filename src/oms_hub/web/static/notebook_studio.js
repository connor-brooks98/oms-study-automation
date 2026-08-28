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

  const requestConfirmation = (documentRef, options, invoker) => (
    root.StudyHubShell?.confirmAction
      ? root.StudyHubShell.confirmAction(documentRef, options, invoker, documentRef.defaultView)
      : Promise.resolve(typeof root.confirm === "function" && root.confirm(options.message))
  );

  const hasActiveSources = (sources) => sources.some(
    (source) => ["pending", "attaching", "deleting"].includes(source.state),
  );

  const hasActiveRuns = (runs) => runs.some(
    (run) => ["queued", "running", "retrying"].includes(run.state),
  );

  const importRoleAllowsNotebook = (role) => (
    role === "supporting_reference" || role === "combined_questions_answers"
  );

  const captureRenderState = (documentRef, container) => ({
    openKeys: new Set(Array.from(
      container.querySelectorAll?.("details[data-state-key]") || [],
      (details) => details.open ? details.dataset.stateKey : null,
    ).filter(Boolean)),
    focusKey: documentRef.activeElement?.dataset?.focusKey || null,
  });

  const connectedFocusable = (element) => (
    element && element.isConnected !== false && !element.disabled && element.focus
      ? element
      : null
  );

  const restoreRenderState = (container, state) => {
    const keyed = Array.from(container.querySelectorAll?.("[data-state-key]") || []);
    keyed.forEach((element) => {
      if (state.openKeys.has(element.dataset.stateKey)) element.open = true;
    });
    if (!state.focusKey) return;
    const focusable = Array.from(container.querySelectorAll?.("[data-focus-key]") || [])
      .filter(connectedFocusable);
    const runKey = state.focusKey.match(/^(run:[^:]+):/)?.[1] || null;
    const focus = focusable.find((element) => element.dataset.focusKey === state.focusKey)
      || (runKey && focusable.find((element) => element.dataset.focusKey.startsWith(`${runKey}:`)))
      || focusable[0]
      || connectedFocusable(container);
    focus?.focus({ preventScroll: true });
  };

  const applyImportRoleState = (form) => {
    const role = form.querySelector("[data-import-role]")?.value || "questions";
    const checkbox = form.querySelector("[data-import-notebook]");
    if (checkbox) {
      checkbox.disabled = !importRoleAllowsNotebook(role);
      if (checkbox.disabled) checkbox.checked = false;
    }
    return { role, attach_to_notebook: Boolean(checkbox?.checked) };
  };

  const buildImportSourceFormData = (
    form, course, exam, token, FormDataConstructor = root.FormData,
  ) => {
    const roleState = applyImportRoleState(form);
    const body = new FormDataConstructor(form);
    body.set("role", roleState.role);
    body.set("attach_to_notebook", String(roleState.attach_to_notebook));
    body.set("subject", course.value);
    body.set("exam_number", exam.value);
    body.set("csrf_token", token);
    return { body, roleState };
  };

  const workflowPanelState = (workflow) => ({
    generate: workflow === "generate",
    import: workflow === "import",
  });

  const scopeUrl = (navigation) => {
    const href = navigation?.location?.href;
    if (!href) return null;
    try {
      return new URL(href);
    } catch (_error) {
      return null;
    }
  };

  const selectedCourseOption = (course, value) => {
    const normalized = normalizeSubject(value || "");
    if (!normalized) return null;
    return Array.from(course.options || []).find(
      (option) => normalizeSubject(option.value || "") === normalized,
    ) || null;
  };

  const selectedWorkflowFromUrl = (url) => (
    url?.searchParams.get("workflow") === "import" ? "import" : "generate"
  );

  const toggleClass = (element, className, enabled) => {
    if (element.classList) {
      element.classList.toggle(className, enabled);
      return;
    }
    const classes = new Set(String(element.className || "").split(/\s+/).filter(Boolean));
    if (enabled) classes.add(className);
    else classes.delete(className);
    element.className = [...classes].join(" ");
  };

  const setWorkflowState = (page, workflow) => {
    const state = workflowPanelState(workflow);
    page.querySelectorAll("[data-workflow-panel]").forEach((panel) => {
      panel.hidden = !state[panel.dataset.workflowPanel];
    });
    page.querySelectorAll("[data-workflow-tab]").forEach((tab) => {
      const active = tab.dataset.workflowTab === workflow;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      toggleClass(tab, "primary", active);
      toggleClass(tab, "secondary", !active);
      toggleClass(tab, "sh-seg__btn--active", active);
      toggleClass(tab, "sh-btn--primary", active);
      toggleClass(tab, "sh-btn--secondary", !active);
    });
    const activeTab = Array.from(page.querySelectorAll("[data-workflow-tab]"))
      .find((tab) => tab.dataset.workflowTab === workflow);
    const pill = page.querySelector?.("[data-workflow-pill]");
    if (activeTab && pill) {
      pill.style.width = `${activeTab.offsetWidth}px`;
      pill.style.transform = `translateX(${activeTab.offsetLeft - pill.offsetLeft}px)`;
    }
  };

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
        remove.className = "button secondary compact sh-btn sh-btn--secondary";
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
      label.className = "sh-check";
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
    const state = captureRenderState(documentRef, container);
    container.replaceChildren();
    if (!runs.length) {
      const note = documentRef.createElement("p");
      note.textContent = "No prompt runs yet.";
      container.append(note);
      restoreRenderState(container, state);
      return false;
    }
    runs.forEach((run) => {
      const card = documentRef.createElement("article");
      card.className = "sh-card studio-run";
      card.dataset.runId = run.id;
      card.dataset.stateKey = `run:${run.id}`;
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
        images.className = "button primary compact sh-btn sh-btn--primary";
        images.href = run.image_review_url;
        images.dataset.focusKey = `run:${run.id}:images`;
        images.textContent = "Add images";
        card.append(images);
      }
      if (run.review_url) {
        const review = documentRef.createElement("a");
        review.className = "button primary compact sh-btn sh-btn--primary";
        review.href = run.review_url;
        review.dataset.focusKey = `run:${run.id}:review`;
        review.textContent = "Review questions";
        card.append(review);
      }
      if (run.published_url) {
        const link = documentRef.createElement("a");
        link.className = "button secondary compact sh-btn sh-btn--secondary";
        link.href = run.published_url;
        link.dataset.focusKey = `run:${run.id}:published`;
        link.textContent = "Open published quiz";
        card.append(link);
        const unpublish = documentRef.createElement("button");
        unpublish.type = "button";
        unpublish.className = "button secondary compact sh-btn sh-btn--secondary";
        unpublish.dataset.unpublishRun = run.id;
        unpublish.dataset.focusKey = `run:${run.id}:unpublish`;
        unpublish.textContent = "Unpublish";
        card.append(unpublish);
      }
      if (["awaiting_images", "awaiting_review", "complete", "failed"].includes(run.state)) {
        const actions = documentRef.createElement("div");
        actions.className = "studio-run-actions";
        const rerun = documentRef.createElement("button");
        rerun.type = "button";
        rerun.className = "button secondary compact sh-btn sh-btn--secondary";
        rerun.dataset.rerun = run.id;
        rerun.dataset.focusKey = `run:${run.id}:rerun`;
        rerun.textContent = "↻";
        rerun.ariaLabel = "Re-run this quiz";
        rerun.title = "Re-run";
        rerun.setAttribute?.("aria-label", "Re-run this quiz");
        actions.append(rerun);
        const remove = documentRef.createElement("button");
        remove.type = "button";
        remove.className = "button danger compact sh-btn sh-btn--danger";
        remove.dataset.removeRun = run.id;
        remove.dataset.focusKey = `run:${run.id}:remove`;
        remove.textContent = "×";
        remove.ariaLabel = "Remove run from history";
        remove.title = "Remove from history";
        remove.setAttribute?.("aria-label", "Remove run from history");
        actions.append(remove);
        card.append(actions);
      }
      const attempts = run.attempt_history || [];
      attempts.forEach((attempt) => {
        if (!attempt.error) return;
        const details = documentRef.createElement("details");
        details.dataset.stateKey = `run:${run.id}:attempt:${attempt.attempt_number}`;
        const summary = documentRef.createElement("summary");
        summary.dataset.focusKey = `${details.dataset.stateKey}:summary`;
        summary.textContent = `Attempt ${attempt.attempt_number} · ${attempt.diagnostic_source}`;
        const error = documentRef.createElement("p");
        error.textContent = attempt.error;
        details.append(summary, error);
        card.append(details);
      });
      container.append(card);
    });
    restoreRenderState(container, state);
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

  const importRowIsIncluded = (row) => (
    row.querySelector("[data-import-row-included]")?.checked !== false
  );

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
    sources: Array.from(rows).filter(importRowIsIncluded).map((row) => ({
      source_id: row.dataset.sourceId,
      role: row.querySelector("[data-import-row-role]")?.value || row.dataset.role,
      attach_to_notebook: (() => {
        const role = row.querySelector("[data-import-row-role]")?.value || row.dataset.role;
        return importRoleAllowsNotebook(role)
          && Boolean(row.querySelector("[data-import-row-notebook]")?.checked);
      })(),
    })),
  });

  const appendImportSource = (documentRef, list, source, role, attachToNotebook) => {
    list.querySelector("[data-import-empty]")?.remove();
    const row = documentRef.createElement("li");
    row.dataset.importSourceRow = "true";
    row.dataset.sourceId = source.id;
    row.dataset.role = role;
    const sourceTitle = source.title || source.id;
    const included = documentRef.createElement("input");
    included.type = "checkbox";
    included.id = `import-source-included-${source.id}`;
    included.dataset.importRowIncluded = "true";
    included.checked = true;
    included.setAttribute("aria-label", `Use ${sourceTitle} for this run`);
    const title = documentRef.createElement("strong");
    title.className = "studio-import-source-title";
    title.textContent = sourceTitle;
    const useText = documentRef.createElement("span");
    useText.className = "studio-import-source-use";
    useText.textContent = "Use for this run";
    const sourceCopy = documentRef.createElement("span");
    sourceCopy.className = "studio-import-source-copy";
    sourceCopy.append(title, useText);
    const sourceChoice = documentRef.createElement("label");
    sourceChoice.className = "sh-check studio-import-source-choice";
    sourceChoice.htmlFor = included.id;
    sourceChoice.append(included, sourceCopy);
    const select = documentRef.createElement("select");
    select.id = `import-source-role-${source.id}`;
    select.dataset.importRowRole = "true";
    select.className = "sh-select";
    const roleLabel = documentRef.createElement("label");
    roleLabel.htmlFor = select.id;
    roleLabel.textContent = "Role";
    roleLabel.className = "sh-field-label studio-import-row-role";
    [
      ["questions", "Questions"], ["answer_key", "Answer key"],
      ["supporting_reference", "Supporting reference"],
      ["combined_questions_answers", "Combined questions and answers"],
    ].forEach(([value, label]) => {
      const option = documentRef.createElement("option");
      option.value = value; option.textContent = label; option.selected = value === role;
      select.append(option);
    });
    roleLabel.append(select);
    const notebook = documentRef.createElement("input");
    notebook.type = "checkbox";
    notebook.dataset.importRowNotebook = "true";
    notebook.checked = attachToNotebook && importRoleAllowsNotebook(role);
    notebook.disabled = !importRoleAllowsNotebook(role);
    const notebookLabel = documentRef.createElement("label");
    notebookLabel.className = "sh-check studio-import-row-notebook";
    notebookLabel.append(notebook, documentRef.createTextNode(" Use in NotebookLM for missing answers"));
    const remove = documentRef.createElement("button");
    remove.type = "button"; remove.dataset.removeImportSource = "true"; remove.textContent = "Remove";
    remove.className = "sh-btn sh-btn--danger";
    select.addEventListener("change", () => {
      notebook.disabled = !importRoleAllowsNotebook(select.value);
      if (notebook.disabled) notebook.checked = false;
    });
    row.append(sourceChoice, roleLabel, notebookLabel, remove);
    list.append(row);
  };

  const hydrateImportSources = (documentRef, list, sources) => {
    const readyImports = sources.filter((source) => (
      source.state === "ready"
      && source.purpose === "local_import"
      && source.import_defaults
    ));
    const readyImportIds = new Set(readyImports.map((source) => source.id));
    const existing = new Set();
    Array.from(list.querySelectorAll?.("[data-import-source-row]") || []).forEach((row) => {
      const sourceId = row.dataset.sourceId;
      if (existing.has(sourceId) || !readyImportIds.has(sourceId)) row.remove?.();
      else existing.add(sourceId);
    });
    readyImports.forEach((source) => {
      if (existing.has(source.id)) return;
      const defaults = source.import_defaults;
      appendImportSource(
        documentRef,
        list,
        source,
        defaults.role || "questions",
        importRoleAllowsNotebook(defaults.role) && Boolean(defaults.attach_to_notebook),
      );
      existing.add(source.id);
    });
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
    exam.value = "";
    exam.disabled = !course.value;
  };

  const restoreScopeFromUrl = (documentRef, course, exam, navigation) => {
    const url = scopeUrl(navigation);
    const workflow = selectedWorkflowFromUrl(url);
    const courseOption = selectedCourseOption(
      course,
      url?.searchParams.get("subject") || "",
    );
    course.value = courseOption?.value || "";
    populateExams(documentRef, course, exam);
    const requestedExam = url?.searchParams.get("exam") || "";
    const validExam = Array.from(exam.options || []).some(
      (option) => option.value === requestedExam && requestedExam !== "",
    );
    exam.value = validExam ? requestedExam : "";
    return { workflow, scopeValid: Boolean(course.value && exam.value) };
  };

  const updateScopeUrl = (course, exam, workflow, navigation) => {
    const url = scopeUrl(navigation);
    if (!url || !navigation?.history?.replaceState) return;
    if (course.value) {
      url.searchParams.set("subject", normalizeSubject(course.value));
    } else {
      url.searchParams.delete("subject");
    }
    if (course.value && exam.value) url.searchParams.set("exam", exam.value);
    else url.searchParams.delete("exam");
    if (workflow === "import") url.searchParams.set("workflow", "import");
    else url.searchParams.delete("workflow");
    navigation.history.replaceState(navigation.history.state, "", url.toString());
  };

  const clearImportSources = (documentRef, list) => {
    hydrateImportSources(documentRef, list, []);
    list.querySelector("[data-import-empty]")?.remove();
    const empty = documentRef.createElement("li");
    empty.dataset.importEmpty = "true";
    empty.textContent = "Add at least one local source.";
    list.append(empty);
  };

  const restoreFailedAction = (target, status, detail) => {
    if (status) status.textContent = detail;
    if (!target) return;
    target.disabled = false;
    if (target.isConnected !== false) target.focus?.({ preventScroll: true });
  };

  const initialize = (
    documentRef,
    fetchImpl = root.fetch.bind(root),
    navigation = root,
  ) => {
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
    let refreshGeneration = 0;

    let selectedWorkflow = "generate";
    const workflowTabs = Array.from(page.querySelectorAll("[data-workflow-tab]"));
    workflowTabs.forEach((tab, index) => {
      tab.addEventListener("click", () => {
        selectedWorkflow = tab.dataset.workflowTab;
        setWorkflowState(page, selectedWorkflow);
        updateScopeUrl(course, exam, selectedWorkflow, navigation);
      });
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? workflowTabs.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + workflowTabs.length) % workflowTabs.length;
        selectedWorkflow = workflowTabs[next].dataset.workflowTab;
        setWorkflowState(page, selectedWorkflow);
        updateScopeUrl(course, exam, selectedWorkflow, navigation);
        workflowTabs[next].focus();
      });
    });
    const restoredScope = restoreScopeFromUrl(documentRef, course, exam, navigation);
    selectedWorkflow = restoredScope.workflow;
    setWorkflowState(page, selectedWorkflow);
    updateScopeUrl(course, exam, selectedWorkflow, navigation);
    root.addEventListener?.("resize", () => {
      const active = workflowTabs.find((tab) => tab.getAttribute("aria-selected") === "true");
      if (active) setWorkflowState(page, active.dataset.workflowTab);
    });

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
      const generation = ++refreshGeneration;
      pollHandle = null;
      if (!course.value || !exam.value) return;
      const subjectKey = normalizeSubject(course.value);
      const examNumber = exam.value;
      const query = `subject_key=${encodeURIComponent(subjectKey)}&exam_number=${encodeURIComponent(examNumber)}`;
      try {
        const [sourcePayload, runPayload] = await Promise.all([
          loadJson(`/studio/sources?${query}`),
          loadJson(`/studio/runs?${query}`),
        ]);
        if (
          generation !== refreshGeneration
          || normalizeSubject(course.value) !== subjectKey
          || exam.value !== examNumber
        ) return;
        if (pollStatus) pollStatus.textContent = "";
        if (sourceStatus) sourceStatus.textContent = "";
        pollDelayMs = basePollDelayMs;
        const activeSources = renderSources(documentRef, list, sourcePayload.sources || []);
        renderSourcePicker(documentRef, picker, sourcePayload.sources || []);
        filterSourcePicker(picker, sourceFilter.value);
        hydrateImportSources(documentRef, importSourceList, sourcePayload.sources || []);
        const activeRuns = renderRuns(documentRef, runList, runPayload.runs || []);
        if (activeSources || activeRuns) scheduleRefresh(pollDelayMs);
      } catch (error) {
        if (generation !== refreshGeneration) return;
        // Keep the previously rendered lists in place; surface the failure
        // in the dedicated status region and keep polling with backoff.
        const message = error instanceof Error ? error.message : "Quiz Builder status could not be loaded.";
        if (pollStatus) pollStatus.textContent = `${message} Retrying…`;
        pollDelayMs = Math.min(pollDelayMs * 2, maxPollDelayMs);
        scheduleRefresh(pollDelayMs);
      }
    };

    course.addEventListener("change", () => {
      refreshGeneration += 1;
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = null;
      populateExams(documentRef, course, exam);
      clearImportSources(documentRef, importSourceList);
      list.textContent = "Select an exam to view sources.";
      picker.textContent = "Select a source course and exam first.";
      runList.textContent = "Select a source course and exam to view runs.";
      updateScopeUrl(course, exam, selectedWorkflow, navigation);
    });
    exam.addEventListener("change", () => {
      refreshGeneration += 1;
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = null;
      clearImportSources(documentRef, importSourceList);
      updateScopeUrl(course, exam, selectedWorkflow, navigation);
      if (exam.value) {
        list.textContent = "";
        const loading = documentRef.createElement("li");
        loading.textContent = "Loading sources…";
        list.append(loading);
        if (sourceStatus) sourceStatus.textContent = "Loading sources…";
      }
      return refresh();
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
      const removeRunButton = event.target.closest?.("[data-remove-run]");
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
      const target = deleteButton || rerunButton || removeRunButton || unpublishButton;
      if (!target) return;
      const token = csrf(documentRef);
      let url;
      let method;
      if (deleteButton) {
        if (!await requestConfirmation(documentRef, {
          title: "Delete this source?",
          message: "This removes the source from NotebookLM and future selections.",
          confirmLabel: "Delete source",
          cancelLabel: "Keep source",
        }, deleteButton)) return;
        url = `/studio/sources/${encodeURIComponent(deleteButton.dataset.deleteSource)}`;
        method = "DELETE";
      } else if (rerunButton) {
        url = `/studio/runs/${encodeURIComponent(rerunButton.dataset.rerun)}/rerun`;
        method = "POST";
      } else if (removeRunButton) {
        if (!await requestConfirmation(documentRef, {
          title: "Remove this run from history?",
          message: "The run will leave Quiz Builder history. Any published quiz will remain available.",
          confirmLabel: "Remove run",
          cancelLabel: "Keep run",
        }, removeRunButton)) return;
        url = `/studio/runs/${encodeURIComponent(removeRunButton.dataset.removeRun)}`;
        method = "DELETE";
      } else {
        if (!await requestConfirmation(documentRef, {
          title: "Unpublish this quiz?",
          message: "The quiz will leave the public library. Private run history will be retained.",
          confirmLabel: "Unpublish quiz",
          cancelLabel: "Keep quiz",
        }, unpublishButton)) return;
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
        restoreFailedAction(target, deleteButton ? sourceStatus : pollStatus, detail);
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
        const token = csrf(documentRef);
        const { body, roleState } = buildImportSourceFormData(
          form, course, exam, token,
        );
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
      const includedRows = Array.from(rows).filter(importRowIsIncluded);
      if (!course.value || !exam.value || !importDestinationCourse.value || !importDestinationExam.value || !includedRows.length) {
        message.textContent = "Select source and publication course/exam, then check at least one source for this run.";
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

    navigation?.addEventListener?.("popstate", () => {
      refreshGeneration += 1;
      if (pollHandle !== null) root.clearTimeout(pollHandle);
      pollHandle = null;
      const restored = restoreScopeFromUrl(
        documentRef,
        course,
        exam,
        navigation,
      );
      selectedWorkflow = restored.workflow;
      setWorkflowState(page, selectedWorkflow);
      clearImportSources(documentRef, importSourceList);
      if (restored.scopeValid) refresh();
    });

    if (restoredScope.scopeValid) return refresh();
    return Promise.resolve();
  };

  const api = {
    appendImportSource,
    buildRunPayload,
    buildImportRunPayload,
    buildImportSourceFormData,
    applyImportRoleState,
    filterSourcePicker,
    hasActiveRuns,
    hasActiveSources,
    hydrateImportSources,
    initialize,
    imageUrlFromDrop,
    normalizeSubject,
    renderRuns,
    renderSources,
    retryStatus,
    restoreFailedAction,
    restoreScopeFromUrl,
    selectAllAttachedSources,
    setWorkflowState,
    updateScopeUrl,
    workflowPanelState,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
