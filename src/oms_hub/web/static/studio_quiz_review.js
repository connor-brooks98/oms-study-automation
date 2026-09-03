((root) => {
  "use strict";

  const blockersText = (blockers) => blockers.length ? blockers.join("\n") : "Ready for preview and publication.";
  const canPublish = (blockers) => blockers.length === 0;
  const questionAnchor = (questionId) => {
    const bytes = new TextEncoder().encode(String(questionId));
    const encoded = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `question-${bytes.length}-${encoded}`;
  };

  const issueSummary = (issues) => {
    const blocking = issues.filter((issue) => issue.role === "err").length;
    const warnings = issues.filter((issue) => issue.role === "warn").length;
    return `${issues.length} ${issues.length === 1 ? "issue" : "issues"} · ${blocking} blocking ${blocking === 1 ? "issue" : "issues"} · ${warnings} ${warnings === 1 ? "warning" : "warnings"}`;
  };

  const groupIssues = (issues) => {
    const groups = new Map();
    issues.forEach((issue) => {
      const type = issue.type || "review";
      if (!groups.has(type)) groups.set(type, []);
      groups.get(type).push(issue);
    });
    return [...groups.entries()].map(([type, entries]) => ({ type, issues: entries }));
  };

  const hasImageReviewIssues = (issues) => issues.some((issue) => (
    issue.type === "image" || issue.code === "required_image_unresolved"
  ));

  const shouldRenderNoCandidateEmpty = (payload) => (
    payload.questions.length > 0
    && hasImageReviewIssues(payload.issues || [])
    && !payload.questions.some((question) => question.candidates.length)
  );

  const normalizedEditPayload = (values) => {
    const choices = (values.choices || []).map((choice) => String(choice).trim()).filter(Boolean);
    const correctIndex = Number(values.correct_index);
    const payload = {
      stem: String(values.stem || "").trim(),
      choices,
      correct_index: Number.isInteger(correctIndex) ? correctIndex : -1,
      rationale: String(values.rationale || "").trim(),
    };
    ["topic", "area", "learning_objective"].forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(values, key)) payload[key] = String(values[key] || "").trim() || null;
    });
    return payload;
  };

  const normalizedMatchingEditPayload = (values) => {
    const payload = {
      kind: "matching",
      stem: String(values.stem || "").trim(),
      prompts: (values.prompts || []).map((prompt) => {
        const index = prompt.correct_index === "" || prompt.correct_index === null
          ? null : Number(prompt.correct_index);
        return {
          id: String(prompt.id),
          label: String(prompt.label || "").trim(),
          text: String(prompt.text || "").trim(),
          correct_index: Number.isInteger(index) ? index : null,
        };
      }),
      choices: (values.choices || []).map((choice) => String(choice).trim()),
      rationale: String(values.rationale || "").trim() || null,
    };
    ["topic", "area", "learning_objective"].forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        payload[key] = String(values[key] || "").trim() || null;
      }
    });
    return payload;
  };

  const candidateSelectionPayload = (candidateId) => ({ image_candidate_id: candidateId });
  const candidateSelectionUrl = (runId, questionId) => `/studio/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(questionId)}/image-selection`;

  const reindexChoiceRows = (group, questionId = "") => {
    Array.from(group?.querySelectorAll?.(".studio-review-choice") || []).forEach((row, index) => {
      const choice = row.querySelector?.('input[name="choice"]');
      const ordinal = row.querySelector?.("[data-matching-choice-ordinal]");
      const correct = row.querySelector?.('input[name="correct_index"]');
      const overflow = row.querySelector?.("details");
      const summary = overflow?.querySelector?.("summary");
      const remove = row.querySelector?.("[data-remove-choice]");
      choice?.setAttribute?.("aria-label", `Choice ${index + 1}`);
      if (ordinal) ordinal.textContent = `${index + 1}.`;
      correct?.setAttribute?.("aria-label", `Correct choice ${index + 1}`);
      summary?.setAttribute?.("aria-label", `More actions for choice ${index + 1}`);
      if (correct) correct.value = String(index);
      if (!questionId) return;
      if (choice) choice.dataset.focusKey = `question:${questionId}:choice:${index}`;
      if (correct) correct.dataset.focusKey = `question:${questionId}:correct:${index}`;
      if (overflow) overflow.dataset.stateKey = `question:${questionId}:choice:${index}:overflow`;
      if (summary) summary.dataset.focusKey = `question:${questionId}:choice:${index}:overflow:summary`;
      if (remove) remove.dataset.focusKey = `question:${questionId}:choice:${index}:remove`;
    });
  };

  const reindexMatchingChoiceRows = (
    documentRef, group, promptContainer, removedIndex = null, questionId = "",
  ) => {
    reindexChoiceRows(group, questionId);
    const choices = Array.from(group.querySelectorAll('input[name="choice"]'), (field) => field.value);
    Array.from(promptContainer.querySelectorAll("select")).forEach((select) => {
      const current = select.value === "" ? null : Number(select.value);
      const nextIndex = removedIndex === null ? current : current === removedIndex
        ? null : current > removedIndex ? current - 1 : current;
      const unresolved = documentRef.createElement("option");
      unresolved.value = "";
      unresolved.textContent = "Unresolved";
      const options = choices.map((choice, index) => {
        const option = documentRef.createElement("option");
        option.value = String(index);
        option.textContent = `${index + 1}. ${choice}`;
        return option;
      });
      select.replaceChildren(unresolved, ...options);
      select.value = nextIndex === null ? "" : String(nextIndex);
    });
  };

  const removeChoiceRow = (remove) => {
    const row = remove.closest?.(".studio-review-choice");
    const group = remove.closest?.("[data-choices]");
    const card = remove.closest?.("[data-question-id]");
    const rows = Array.from(group?.querySelectorAll?.(".studio-review-choice") || []);
    if (!row || !group || !card || rows.length <= 2) return false;
    const removedIndex = rows.indexOf(row);
    row.remove();
    card.dataset.dirty = "true";
    reindexChoiceRows(group, card.dataset.questionId || "");
    const remaining = Array.from(group.querySelectorAll?.(".studio-review-choice") || []);
    const fallbackRow = remaining[Math.min(Math.max(removedIndex, 0), remaining.length - 1)];
    const fallback = fallbackRow?.querySelector?.("[data-focus-key]")
      || group.querySelector?.("[data-add-choice]")
      || card;
    fallback?.focus?.({ preventScroll: true });
    return true;
  };

  const removeMatchingChoiceRow = (documentRef, remove) => {
    const row = remove.closest?.(".studio-review-choice");
    const group = remove.closest?.(".studio-review-matching-bank");
    const card = remove.closest?.("[data-question-id]");
    const promptContainer = card?.querySelector?.("[data-matching-prompts]");
    const rows = Array.from(group?.querySelectorAll?.(".studio-review-choice") || []);
    if (!row || !group || !card || !promptContainer || rows.length <= 2) return false;
    const removedIndex = rows.indexOf(row);
    row.remove();
    card.dataset.dirty = "true";
    reindexMatchingChoiceRows(
      documentRef, group, promptContainer, removedIndex, card.dataset.questionId || "",
    );
    const remaining = Array.from(group.querySelectorAll?.(".studio-review-choice") || []);
    const fallbackRow = remaining[Math.min(Math.max(removedIndex, 0), remaining.length - 1)];
    const fallback = fallbackRow?.querySelector?.("[data-focus-key]")
      || group.querySelector?.("[data-add-choice]")
      || card;
    fallback?.focus?.({ preventScroll: true });
    return true;
  };

  const csrf = (documentRef) => {
    const value = documentRef.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith("study_hub_csrf="));
    return value ? decodeURIComponent(value.split("=").slice(1).join("=")) : "";
  };

  const captureRenderState = (documentRef, page) => ({
    openKeys: new Set(Array.from(
      page.querySelectorAll?.("details[data-state-key]") || [],
      (details) => details.open ? details.dataset.stateKey : null,
    ).filter(Boolean)),
    focusKey: documentRef.activeElement?.dataset?.focusKey || null,
  });

  const restoreRenderState = (page, state) => {
    Array.from(page.querySelectorAll?.("details[data-state-key]") || []).forEach((details) => {
      if (state.openKeys.has(details.dataset.stateKey)) details.open = true;
    });
    const focus = Array.from(page.querySelectorAll?.("[data-focus-key]") || [])
      .find((element) => element.dataset.focusKey === state.focusKey);
    if (!state.focusKey) return;
    if (focus?.focus && !focus.disabled && focus.isConnected !== false) {
      focus.focus({ preventScroll: true });
      return;
    }
    page.focus?.({ preventScroll: true });
  };

  const text = (documentRef, tagName, value, className = "") => {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  };

  const input = (documentRef, labelValue, name, value, type = "text", focusKey = "") => {
    const label = documentRef.createElement("label");
    label.append(text(documentRef, "span", labelValue, "sh-field-label"));
    const field = documentRef.createElement("input");
    field.type = type;
    field.name = name;
    field.value = value ?? "";
    field.className = "sh-input";
    if (focusKey) field.dataset.focusKey = focusKey;
    label.append(field);
    return label;
  };

  const textarea = (documentRef, labelValue, name, value, focusKey = "") => {
    const label = documentRef.createElement("label");
    label.append(text(documentRef, "span", labelValue, "sh-field-label"));
    const field = documentRef.createElement("textarea");
    field.name = name;
    field.value = value ?? "";
    field.className = "sh-textarea";
    if (focusKey) field.dataset.focusKey = focusKey;
    label.append(field);
    return label;
  };

  const choiceRow = (documentRef, choice, index, correctIndex, questionId = "") => {
    const row = documentRef.createElement("div");
    row.className = "studio-review-choice";
    const choiceInput = documentRef.createElement("input");
    choiceInput.type = "text";
    choiceInput.name = "choice";
    choiceInput.value = choice;
    choiceInput.className = "sh-input";
    if (questionId) choiceInput.dataset.focusKey = `question:${questionId}:choice:${index}`;
    choiceInput.setAttribute("aria-label", `Choice ${index + 1}`);
    const correctLabel = documentRef.createElement("label");
    correctLabel.className = "sh-check";
    const correct = documentRef.createElement("input");
    correct.type = "radio";
    correct.name = "correct_index";
    correct.value = String(index);
    correct.checked = index === correctIndex;
    if (questionId) correct.dataset.focusKey = `question:${questionId}:correct:${index}`;
    correct.setAttribute("aria-label", `Correct choice ${index + 1}`);
    correctLabel.append(correct, text(documentRef, "span", "Correct"));
    const overflow = documentRef.createElement("details");
    overflow.className = "studio-review-choice-overflow t-context-menu";
    const summary = text(documentRef, "summary", "⋯", "sh-iconbtn");
    if (questionId) {
      overflow.dataset.stateKey = `question:${questionId}:choice:${index}:overflow`;
      summary.dataset.focusKey = `${overflow.dataset.stateKey}:summary`;
    }
    summary.setAttribute("aria-label", `More actions for choice ${index + 1}`);
    const remove = documentRef.createElement("button");
    remove.type = "button";
    remove.dataset.removeChoice = "true";
    if (questionId) remove.dataset.focusKey = `question:${questionId}:choice:${index}:remove`;
    remove.className = "sh-btn sh-btn--danger";
    remove.textContent = "Remove choice";
    const menu = documentRef.createElement("div");
    menu.className = "studio-review-choice-menu t-context-menu__panel";
    menu.append(remove);
    overflow.append(summary, menu);
    row.append(choiceInput, correctLabel, overflow);
    return row;
  };

  const matchingChoiceRow = (documentRef, choice, index, questionId = "") => {
    const row = documentRef.createElement("div");
    row.className = "studio-review-choice";
    const ordinal = text(documentRef, "span", `${index + 1}.`, "studio-review-choice-ordinal");
    ordinal.dataset.matchingChoiceOrdinal = "true";
    const choiceInput = documentRef.createElement("input");
    choiceInput.type = "text";
    choiceInput.name = "choice";
    choiceInput.value = choice;
    choiceInput.className = "sh-input";
    choiceInput.setAttribute("aria-label", `Choice ${index + 1}`);
    if (questionId) choiceInput.dataset.focusKey = `question:${questionId}:choice:${index}`;
    const overflow = documentRef.createElement("details");
    overflow.className = "studio-review-choice-overflow t-context-menu";
    const summary = text(documentRef, "summary", "⋯", "sh-iconbtn");
    summary.setAttribute("aria-label", `More actions for choice ${index + 1}`);
    const remove = documentRef.createElement("button");
    remove.type = "button";
    remove.dataset.removeChoice = "true";
    remove.className = "sh-btn sh-btn--danger";
    remove.textContent = "Remove choice";
    if (questionId) {
      overflow.dataset.stateKey = `question:${questionId}:choice:${index}:overflow`;
      summary.dataset.focusKey = `${overflow.dataset.stateKey}:summary`;
      remove.dataset.focusKey = `question:${questionId}:choice:${index}:remove`;
    }
    const menu = documentRef.createElement("div");
    menu.className = "studio-review-choice-menu t-context-menu__panel";
    menu.append(remove);
    overflow.append(summary, menu);
    row.append(ordinal, choiceInput, overflow);
    return row;
  };

  const matchingPromptRow = (documentRef, prompt, choices, questionId) => {
    const row = documentRef.createElement("div");
    row.className = "studio-review-matching-prompt";
    row.dataset.matchingPrompt = "true";
    row.dataset.promptId = prompt.id;
    const labelField = input(documentRef, "Label", "prompt_label", prompt.label, "text", `question:${questionId}:prompt:${prompt.id}:label`);
    const textField = input(documentRef, "Prompt", "prompt_text", prompt.text, "text", `question:${questionId}:prompt:${prompt.id}:text`);
    const mapping = documentRef.createElement("label");
    mapping.append(text(documentRef, "span", "Correct choice", "sh-field-label"));
    const select = documentRef.createElement("select");
    select.name = "correct_index";
    select.className = "sh-select";
    select.dataset.focusKey = `question:${questionId}:prompt:${prompt.id}:correct`;
    select.setAttribute("aria-label", `Correct choice for prompt ${prompt.label}`);
    const unresolved = documentRef.createElement("option");
    unresolved.value = "";
    unresolved.textContent = "Unresolved";
    const options = choices.map((choice, index) => {
      const option = documentRef.createElement("option");
      option.value = String(index);
      option.textContent = `${index + 1}. ${choice}`;
      return option;
    });
    select.append(unresolved, ...options);
    select.value = Number.isInteger(prompt.correct_index) ? String(prompt.correct_index) : "";
    mapping.append(select);
    row.append(labelField, textField, mapping);
    return row;
  };

  const renderCandidates = (documentRef, question) => {
    const section = documentRef.createElement("details");
    section.className = "studio-review-candidates studio-review-disclosure t-accordion";
    section.dataset.stateKey = `question:${question.id}:images`;
    section.open = Boolean(question.image_required && !question.image_not_needed);
    const imageState = question.image_attached
      ? "attached"
      : question.image_required && !question.image_not_needed
        ? "required"
        : "optional";
    section.append(text(documentRef, "summary", `Question image · ${imageState}`, "sh-section-label"));
    if (question.image_required) {
      const toggle = documentRef.createElement("button");
      toggle.type = "button";
      toggle.dataset.imageOverride = question.id;
      toggle.dataset.imageNotNeeded = String(Boolean(question.image_not_needed));
      toggle.className = "sh-btn sh-btn--secondary";
      toggle.textContent = question.image_not_needed ? "Require image" : "No image needed";
      section.append(toggle);
    }
    if (question.image_attached) {
      section.append(text(documentRef, "p", "An image is attached.", "sh-row__meta"));
      if (question.image_preview_url && !question.selected_candidate_id) {
        const preview = documentRef.createElement("img");
        preview.src = question.image_preview_url;
        preview.alt = "Uploaded question image";
        preview.loading = "lazy";
        section.append(preview);
      }
    }
    const upload = documentRef.createElement("form");
    upload.dataset.imageUpload = question.id;
    upload.className = "studio-review-image-upload";
    upload.enctype = "multipart/form-data";
    const uploadLabel = documentRef.createElement("label");
    uploadLabel.className = "sh-btn sh-btn--secondary studio-review-file-picker";
    const uploadInput = documentRef.createElement("input");
    uploadInput.type = "file";
    uploadInput.name = "file";
    uploadInput.accept = "image/png,image/jpeg,image/webp";
    uploadInput.required = true;
    uploadInput.className = "sr-only";
    uploadLabel.append(uploadInput, text(documentRef, "span", "Choose image"));
    const uploadButton = documentRef.createElement("button");
    uploadButton.type = "submit";
    uploadButton.className = "sh-btn sh-btn--secondary";
    uploadButton.textContent = "Upload image";
    upload.append(uploadLabel, uploadButton);
    section.append(upload);
    const list = documentRef.createElement("div");
    list.className = "studio-review-candidate-list";
    question.candidates.forEach((candidate) => {
      const card = documentRef.createElement("figure");
      card.className = "sh-card studio-review-candidate";
      if (candidate.candidate_id === question.selected_candidate_id) card.classList.add("is-selected");
      const image = documentRef.createElement("img");
      image.src = candidate.preview_url;
      image.alt = `Candidate image from ${candidate.source_title}`;
      image.loading = "lazy";
      card.append(image);
      card.append(text(documentRef, "figcaption", `${candidate.origin} · ${candidate.source_title} · ${candidate.locator}`));
      const select = documentRef.createElement("button");
      select.type = "button";
      select.dataset.selectCandidate = candidate.candidate_id;
      select.dataset.focusKey = `question:${question.id}:candidate:${candidate.candidate_id}`;
      select.className = "sh-btn sh-btn--secondary";
      select.textContent = candidate.candidate_id === question.selected_candidate_id ? "Selected" : "Use this image";
      select.disabled = candidate.candidate_id === question.selected_candidate_id;
      card.append(select);
      list.append(card);
    });
    section.append(list);
    return section;
  };

  const renderQuestion = (documentRef, question, issues = []) => {
    const card = documentRef.createElement("article");
    card.className = "sh-card studio-review-question";
    card.dataset.questionId = question.id;
    card.id = questionAnchor(question.id);
    card.append(text(documentRef, "h3", question.original_identifier ? `Question ${question.original_identifier}` : question.id));
    if (issues.length) {
      const blocking = issues.some((issue) => issue.role === "err");
      card.append(text(
        documentRef,
        "p",
        `${blocking ? "Needs review" : "Review note"}: ${issues.map((issue) => issue.message).join(" · ")}`,
        `sh-pill sh-pill--${blocking ? "err" : "warn"}`,
      ));
    }
    const sourceDetails = documentRef.createElement("details");
    sourceDetails.className = "studio-review-disclosure t-accordion";
    sourceDetails.dataset.stateKey = `question:${question.id}:sources`;
    sourceDetails.append(text(
      documentRef,
      "summary",
      `Source context · ${question.source_refs.length} ${question.source_refs.length === 1 ? "reference" : "references"}`,
      "sh-section-label",
    ));
    const refs = documentRef.createElement("ul");
    refs.className = "studio-review-sources";
    refs.setAttribute("aria-label", "Source references");
    refs.append(
      text(documentRef, "li", `Answer provenance · ${question.provenance || "unresolved"}`),
      text(documentRef, "li", `Extraction confidence · ${question.confidence}`),
    );
    question.source_refs.forEach((ref) => refs.append(text(documentRef, "li", `${ref.source_id} · ${ref.segment_key} · ${ref.locator}`)));
    sourceDetails.append(refs);
    card.append(sourceDetails);
    const form = documentRef.createElement("form");
    form.dataset.questionEdit = question.id;
    form.dataset.questionKind = question.kind || "multiple_choice";
    form.className = "studio-review-form";
    form.id = `${card.id}-edit`;
    form.append(textarea(documentRef, "Question stem", "stem", question.stem, `question:${question.id}:stem`));
    const choices = documentRef.createElement("section");
    const matching = question.kind === "matching";
    choices.className = `sh-card studio-review-choice-group${matching ? " studio-review-matching-bank" : ""}`;
    choices.dataset.choices = "true";
    choices.append(text(documentRef, "p", matching ? "Choice bank" : "Choices (select the correct answer)", "sh-section-label"));
    question.choices.forEach((choice, index) => choices.append(
      matching
        ? matchingChoiceRow(documentRef, choice, index, question.id)
        : choiceRow(documentRef, choice, index, question.correct_index, question.id),
    ));
    const add = documentRef.createElement("button");
    add.type = "button";
    add.dataset.addChoice = "true";
    add.dataset.focusKey = `question:${question.id}:add-choice`;
    add.className = "sh-btn sh-btn--secondary";
    add.textContent = "Add choice";
    choices.append(add);
    form.append(choices);
    if (matching) {
      const prompts = documentRef.createElement("section");
      prompts.className = "studio-review-matching-prompts";
      prompts.dataset.matchingPrompts = "true";
      prompts.append(text(documentRef, "p", "Prompts", "sh-section-label"));
      question.prompts.forEach((prompt) => prompts.append(
        matchingPromptRow(documentRef, prompt, question.choices, question.id),
      ));
      form.append(prompts);
    }
    form.append(textarea(documentRef, "Rationale", "rationale", question.rationale, `question:${question.id}:rationale`));
    const classification = documentRef.createElement("details");
    classification.className = "studio-review-disclosure t-accordion";
    classification.dataset.stateKey = `question:${question.id}:classification`;
    classification.append(text(documentRef, "summary", "Classification details", "sh-section-label"));
    const classificationFields = documentRef.createElement("div");
    classificationFields.className = "studio-review-classification";
    classificationFields.append(
      input(documentRef, "Topic", "topic", question.topic, "text", `question:${question.id}:topic`),
      input(documentRef, "Area", "area", question.area, "text", `question:${question.id}:area`),
      input(documentRef, "Learning objective", "learning_objective", question.learning_objective, "text", `question:${question.id}:learning-objective`),
    );
    classification.append(classificationFields);
    form.append(classification);
    card.append(form, renderCandidates(documentRef, question));
    const save = documentRef.createElement("button");
    save.type = "submit";
    save.setAttribute("form", form.id);
    save.className = "sh-btn sh-btn--primary sh-btn--block sh-btn--stateful";
    save.dataset.state = "idle";
    save.dataset.focusKey = `question:${question.id}:save`;
    save.textContent = "Save changes";
    const status = text(documentRef, "p", "", "studio-review-question-message");
    status.dataset.questionMessage = "true";
    status.dataset.toastSource = "true";
    status.setAttribute("aria-live", "polite");
    if (question.verification_required) {
      const verify = documentRef.createElement("button");
      verify.type = "button";
      verify.dataset.verifyQuestion = question.id;
      verify.dataset.focusKey = `question:${question.id}:verify`;
      verify.className = "sh-btn sh-btn--secondary";
      verify.textContent = question.verified_at ? "Answer verified" : "Verify answer";
      verify.disabled = Boolean(question.verified_at);
      card.append(verify);
    }
    card.append(status, save);
    return card;
  };

  const setReviewTab = (questions, tab) => {
    const selected = tab === "ready" ? "ready" : "needs-review";
    questions.dataset.activeReviewTab = selected;
    Array.from(questions.querySelectorAll?.("[data-review-tab]") || []).forEach((button) => {
      const active = button.dataset.reviewTab === selected;
      button.classList.toggle("sh-seg__btn--active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    Array.from(questions.querySelectorAll?.("[data-review-panel]") || []).forEach((panel) => {
      panel.hidden = panel.dataset.reviewPanel !== selected;
    });
    const active = Array.from(questions.querySelectorAll?.("[data-review-tab]") || [])
      .find((button) => button.dataset.reviewTab === selected);
    const pill = questions.querySelector?.("[data-review-pill]");
    if (active && pill && Number.isFinite(active.offsetWidth) && Number.isFinite(active.offsetLeft)) {
      pill.style.width = `${active.offsetWidth}px`;
      pill.style.transform = `translateX(${active.offsetLeft - pill.offsetLeft}px)`;
    }
  };

  const moveReviewTab = (tab, key) => {
    const tabs = Array.from(tab.parentElement?.querySelectorAll?.("[data-review-tab]") || []);
    const current = tabs.indexOf(tab);
    if (current < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(key)) return false;
    const index = key === "Home"
      ? 0
      : key === "End"
        ? tabs.length - 1
        : (current + (key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setReviewTab(tab.parentElement.parentElement, tabs[index].dataset.reviewTab);
    tabs[index].focus();
    return true;
  };

  const renderIssues = (documentRef, page, payload) => {
    const target = page.querySelector("[data-review-blockers]");
    const checks = target.closest?.(".studio-review-checks");
    const checkSummary = checks?.querySelector?.("[data-review-check-summary]");
    target.replaceChildren();
    const issues = payload.issues || payload.blockers.map((message) => ({
      question_id: message.split(":", 1)[0], display_label: message.split(":", 1)[0], type: "review", message, role: "err",
    }));
    const hasBlockingRunDiagnostic = (payload.run_diagnostics || []).some((diagnostic) => (
      diagnostic.severity === "blocker"
      && !(diagnostic.overridable && diagnostic.acknowledged)
    ));
    if (!issues.length && !hasBlockingRunDiagnostic) {
      if (checkSummary) checkSummary.textContent = "Ready for preview";
      if (checks) checks.open = false;
      target.append(text(documentRef, "p", "Ready for preview and publication.", "studio-review-ready"));
      return;
    }
    if (checkSummary) {
      const diagnosticCount = (payload.run_diagnostics || []).length;
      checkSummary.textContent = issues.length
        ? issueSummary(issues)
        : `${diagnosticCount} publication ${diagnosticCount === 1 ? "check" : "checks"}`;
    }
    target.append(text(documentRef, "p", issueSummary(issues), "sh-pill sh-pill--bare"));
    groupIssues(issues).forEach((group) => {
      const details = documentRef.createElement("details");
      details.className = "studio-review-issue-group sh-card t-accordion";
      details.dataset.stateKey = `issue-group:${group.type}`;
      const summary = documentRef.createElement("summary");
      summary.dataset.focusKey = `${details.dataset.stateKey}:summary`;
      summary.append(text(documentRef, "span", `${group.type.replaceAll("_", " ")} (${group.issues.length})`, "sh-section-label"));
      details.append(summary);
      const list = documentRef.createElement("ul");
      group.issues.forEach((issue) => {
        const item = documentRef.createElement("li");
        const link = documentRef.createElement("a");
        link.href = `#${questionAnchor(issue.question_id)}`;
        link.textContent = `${issue.display_label || issue.question_id}: ${issue.message}`;
        const role = issue.role === "warn" ? "warn" : "err";
        item.append(text(documentRef, "span", role === "err" ? "Blocking" : "Warning", `sh-pill sh-pill--${role}`), link);
        list.append(item);
      });
      details.append(list);
      target.append(details);
    });
  };

  const renderRunDiagnostics = (documentRef, page, payload) => {
    const target = page.querySelector("[data-review-blockers]");
    (payload.run_diagnostics || []).forEach((diagnostic) => {
      const item = documentRef.createElement("section");
      item.className = "studio-review-issue-group sh-card";
      item.append(text(documentRef, "p", diagnostic.message, "sh-section-label"));
      if (diagnostic.overridable && !diagnostic.acknowledged) {
        const acknowledge = documentRef.createElement("button");
        acknowledge.type = "button";
        acknowledge.className = "sh-btn sh-btn--secondary";
        acknowledge.dataset.acknowledgeRunDiagnostic = diagnostic.code;
        acknowledge.textContent = "Acknowledge";
        item.append(acknowledge);
      } else if (!diagnostic.overridable) {
        item.append(text(documentRef, "p", "Blocking diagnostic; acknowledgement is unavailable."));
      }
      target.append(item);
    });
  };

  const render = (documentRef, page, payload) => {
    const state = captureRenderState(documentRef, page);
    renderIssues(documentRef, page, payload);
    renderRunDiagnostics(documentRef, page, payload);
    const publish = page.querySelector("[data-publish-quiz]");
    if (publish) publish.disabled = !canPublish(payload.blockers);
    const preview = page.querySelector("[data-preview-link]");
    preview.hidden = !payload.preview_url;
    preview.href = payload.preview_url || "";
    const questions = page.querySelector("[data-review-questions]");
    const existing = new Map(Array.from(
      questions.querySelectorAll?.("[data-question-id]") || [],
      (card) => [card.dataset.questionId, card],
    ));
    const blockingQuestionIds = new Set(
      (payload.issues || []).filter((issue) => issue.role === "err").map((issue) => issue.question_id),
    );
    const needsReview = [];
    const ready = [];
    payload.questions.forEach((question) => {
      const current = existing.get(question.id);
      const card = current?.dataset.dirty === "true"
        ? current
        : renderQuestion(
          documentRef,
          question,
          (payload.issues || []).filter((issue) => issue.question_id === question.id),
        );
      (blockingQuestionIds.has(question.id) ? needsReview : ready).push(card);
      existing.delete(question.id);
    });
    existing.forEach((card) => {
      if (card.dataset.dirty === "true") needsReview.push(card);
    });
    const tabs = documentRef.createElement("div");
    tabs.className = "sh-seg studio-review-tabs t-tabs";
    tabs.setAttribute("role", "tablist");
    const pill = documentRef.createElement("span");
    pill.className = "t-tabs-pill";
    pill.dataset.reviewPill = "true";
    pill.setAttribute("aria-hidden", "true");
    const needsTab = documentRef.createElement("button");
    needsTab.type = "button";
    needsTab.className = "sh-btn sh-btn--secondary sh-seg__btn t-tab t-number";
    needsTab.dataset.reviewTab = "needs-review";
    needsTab.setAttribute("role", "tab");
    needsTab.id = "studio-review-tab-needs-review";
    needsTab.setAttribute("aria-controls", "studio-review-panel-needs-review");
    needsTab.textContent = `Needs review (${needsReview.length})`;
    const readyTab = documentRef.createElement("button");
    readyTab.type = "button";
    readyTab.className = "sh-btn sh-btn--secondary sh-seg__btn t-tab t-number";
    readyTab.dataset.reviewTab = "ready";
    readyTab.setAttribute("role", "tab");
    readyTab.id = "studio-review-tab-ready";
    readyTab.setAttribute("aria-controls", "studio-review-panel-ready");
    readyTab.textContent = `Ready (${ready.length})`;
    tabs.append(pill, needsTab, readyTab);
    const needsPanel = documentRef.createElement("section");
    needsPanel.dataset.reviewPanel = "needs-review";
    needsPanel.setAttribute("role", "tabpanel");
    needsPanel.id = "studio-review-panel-needs-review";
    needsPanel.setAttribute("aria-labelledby", needsTab.id);
    const readyPanel = documentRef.createElement("section");
    readyPanel.dataset.reviewPanel = "ready";
    readyPanel.setAttribute("role", "tabpanel");
    readyPanel.id = "studio-review-panel-ready";
    readyPanel.setAttribute("aria-labelledby", readyTab.id);
    if (needsReview.length) needsPanel.append(...needsReview);
    else needsPanel.append(text(documentRef, "p", "No question issues remain.", "sh-empty__text"));
    if (ready.length) readyPanel.append(...ready);
    else readyPanel.append(text(documentRef, "p", "Questions move here as their issues are resolved.", "sh-empty__text"));
    questions.replaceChildren(tabs, needsPanel, readyPanel);
    const requestedTab = questions.dataset.activeReviewTab;
    const activeTab = requestedTab === "needs-review" && needsReview.length === 0 && ready.length
      ? "ready"
      : requestedTab || (needsReview.length ? "needs-review" : "ready");
    setReviewTab(questions, activeTab);
    restoreRenderState(page, state);
  };

  const applyQuestionSave = (documentRef, page, form, payload) => {
    const card = form.closest?.("[data-question-id]");
    const questionId = card?.dataset.questionId;
    if (card) delete card.dataset.dirty;
    render(documentRef, page, payload);
    const saved = Array.from(page.querySelectorAll?.("[data-question-id]") || [])
      .find((item) => item.dataset.questionId === questionId);
    return saved?.querySelector?.("[data-question-message]") || null;
  };

  const questionMessage = (page, questionId) => {
    const card = Array.from(page.querySelectorAll?.("[data-question-id]") || [])
      .find((item) => item.dataset.questionId === questionId);
    return card?.querySelector?.("[data-question-message]") || null;
  };

  const withQuestionSavePending = async (card, operation) => {
    const save = Array.from(card?.querySelectorAll?.("[data-focus-key]") || [])
      .find((item) => item.dataset.focusKey?.endsWith(":save"));
    if (save) save.disabled = true;
    try {
      return await operation();
    } finally {
      if (save) save.disabled = false;
    }
  };

  const safeJson = async (response) => { try { return await response.json(); } catch (_error) { return {}; } };

  const reviewErrorMessage = (payload, fallback) => {
    const error = payload && typeof payload === "object" ? payload.error : null;
    if (error && typeof error === "object") {
      const message = typeof error.message === "string" && error.message
        ? error.message
        : fallback;
      const recovery = typeof error.recovery === "string" ? error.recovery.trim() : "";
      return recovery ? `${message} ${recovery}` : message;
    }
    return typeof payload?.detail === "string" && payload.detail ? payload.detail : fallback;
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const page = documentRef.querySelector("[data-practice-review]");
    if (!page || page.dataset.bound === "true") return;
    page.dataset.bound = "true";
    const message = page.querySelector("[data-review-message]");
    let generation = 0;
    const refresh = async () => {
      const request = ++generation;
      const response = await fetchImpl(page.dataset.reviewUrl, { headers: { Accept: "application/json" }, cache: "no-store" });
      const payload = await safeJson(response);
      if (!response.ok) throw new Error(reviewErrorMessage(payload, "Review data is unavailable."));
      if (request === generation) render(documentRef, page, payload);
      return payload;
    };
    const send = async (url, method, body) => {
      const response = await fetchImpl(url, {
        method,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf(documentRef) },
        body: JSON.stringify(body || {}),
      });
      const payload = await safeJson(response);
      if (!response.ok) throw new Error(reviewErrorMessage(payload, "Review update was rejected."));
      return payload;
    };
    page.addEventListener("submit", async (event) => {
      const imageUpload = event.target.closest?.("[data-image-upload]");
      if (imageUpload) {
        event.preventDefault();
        const submitButton = event.submitter || imageUpload.querySelector('button[type="submit"]');
        const questionId = imageUpload.dataset.imageUpload;
        const file = imageUpload.querySelector('input[name="file"]')?.files?.[0];
        const localMessage = questionMessage(page, questionId) || message;
        if (!file) {
          localMessage.textContent = "Choose an image before uploading.";
          return;
        }
        if (submitButton) submitButton.disabled = true;
        localMessage.textContent = "Uploading image…";
        try {
          await withQuestionSavePending(imageUpload.closest?.("[data-question-id]"), async () => {
            const body = new root.FormData();
            body.append("file", file);
            const response = await fetchImpl(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(questionId)}/image`, {
              method: "POST", headers: { "X-CSRF-Token": csrf(documentRef) }, body,
            });
            const updated = await safeJson(response);
            if (!response.ok) throw new Error(reviewErrorMessage(updated, "Image upload was rejected."));
            render(documentRef, page, updated);
            (questionMessage(page, questionId) || message).textContent = "Image uploaded.";
          });
        } catch (error) {
          localMessage.textContent = error instanceof Error ? error.message : "Image could not be uploaded.";
          if (submitButton) {
            submitButton.dataset.state = "error";
            submitButton.disabled = false;
          }
        }
        return;
      }
      const form = event.target.closest?.("[data-question-edit]");
      if (!form) return;
      event.preventDefault();
      const choices = [...form.querySelectorAll('input[name="choice"]')].map((field) => field.value);
      const localMessage = form.closest?.("[data-question-id]")?.querySelector?.("[data-question-message]") || message;
      const shared = {
        stem: form.querySelector('[name="stem"]').value,
        choices,
        rationale: form.querySelector('[name="rationale"]').value,
        topic: form.querySelector('[name="topic"]').value,
        area: form.querySelector('[name="area"]').value,
        learning_objective: form.querySelector('[name="learning_objective"]').value,
      };
      const payload = form.dataset.questionKind === "matching"
        ? normalizedMatchingEditPayload({
          ...shared,
          prompts: Array.from(form.querySelectorAll("[data-matching-prompt]"), (row) => ({
            id: row.dataset.promptId,
            label: row.querySelector('[name="prompt_label"]').value,
            text: row.querySelector('[name="prompt_text"]').value,
            correct_index: row.querySelector('select[name="correct_index"]').value,
          })),
        })
        : normalizedEditPayload({
          ...shared,
          correct_index: form.querySelector('input[name="correct_index"]:checked')?.value ?? -1,
        });
      const matchingValid = payload.kind === "matching"
        && payload.prompts.length >= 2 && payload.prompts.length <= 8
        && payload.prompts.every((prompt) => prompt.label && prompt.text)
        && payload.choices.every(Boolean);
      if (
        payload.choices.length < 2 || payload.choices.length > 8
        || (payload.kind === "matching" ? !matchingValid : payload.correct_index < 0 || payload.correct_index >= payload.choices.length)
      ) {
        localMessage.textContent = payload.kind === "matching"
          ? "Provide two to eight non-empty prompts and choices."
          : "Provide two to eight choices and select the correct answer.";
        return;
      }
      if (payload.kind !== "matching" && !payload.rationale) {
        localMessage.textContent = "Provide an answer rationale before saving.";
        return;
      }
      const submitButton = event.submitter || form.querySelector('button[type="submit"]');
      if (submitButton) submitButton.disabled = true;
      localMessage.textContent = "Saving…";
      try {
        const updated = await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(form.dataset.questionEdit)}`, "PATCH", payload);
        const savedMessage = applyQuestionSave(documentRef, page, form, updated) || message;
        const savedCard = savedMessage.closest?.("[data-question-id]");
        const savedButton = savedCard?.querySelector?.('[data-focus-key$=":save"]');
        if (savedButton) {
          savedButton.disabled = false;
          savedButton.dataset.state = "success";
          root.setTimeout?.(() => { savedButton.dataset.state = "idle"; }, 1200);
        }
        const remaining = (updated.issues || []).filter((issue) => issue.question_id === form.dataset.questionEdit);
        savedMessage.textContent = remaining.length
          ? `Question saved. Remaining: ${remaining.map((issue) => issue.message).join(" · ")}`
          : "Question saved.";
      } catch (error) {
        localMessage.textContent = error instanceof Error ? error.message : "Question could not be saved.";
        if (submitButton) {
          submitButton.dataset.state = "error";
          submitButton.disabled = false;
        }
      }
    });
    const markDirty = (event) => {
      const card = event.target.closest?.("[data-question-edit]")?.closest?.("[data-question-id]");
      if (card) card.dataset.dirty = "true";
    };
    page.addEventListener("input", markDirty);
    page.addEventListener("change", markDirty);
    page.addEventListener("keydown", (event) => {
      const reviewTab = event.target.closest?.("[data-review-tab]");
      if (reviewTab && moveReviewTab(reviewTab, event.key)) event.preventDefault();
    });
    page.addEventListener("click", async (event) => {
      const reviewTab = event.target.closest?.("[data-review-tab]");
      if (reviewTab) {
        setReviewTab(page.querySelector("[data-review-questions]"), reviewTab.dataset.reviewTab);
        return;
      }
      const add = event.target.closest?.("[data-add-choice]");
      if (add) {
        const group = add.closest("[data-choices]");
        const card = add.closest("[data-question-id]");
        const rows = group.querySelectorAll(".studio-review-choice");
        const questionId = add.closest("[data-question-id]")?.dataset.questionId || "";
        if (rows.length < 8) {
          const matching = group.closest?.(".studio-review-matching-bank");
          group.insertBefore(
            matching
              ? matchingChoiceRow(documentRef, "", rows.length, questionId)
              : choiceRow(documentRef, "", rows.length, -1, questionId),
            add,
          );
          if (matching) {
            reindexMatchingChoiceRows(
              documentRef, group, card.querySelector("[data-matching-prompts]"), null, questionId,
            );
          } else reindexChoiceRows(group, questionId);
          if (card) card.dataset.dirty = "true";
        }
        return;
      }
      const remove = event.target.closest?.("[data-remove-choice]");
      if (remove) {
        if (remove.closest?.(".studio-review-matching-bank")) {
          removeMatchingChoiceRow(documentRef, remove);
        } else removeChoiceRow(remove);
        return;
      }
      const candidate = event.target.closest?.("[data-select-candidate]");
      const imageOverride = event.target.closest?.("[data-image-override]");
      const acknowledgement = event.target.closest?.("[data-acknowledge-run-diagnostic]");
      const verify = event.target.closest?.("[data-verify-question]");
      try {
        if (imageOverride) {
          const questionId = imageOverride.dataset.imageOverride;
          const method = imageOverride.dataset.imageNotNeeded === "true" ? "DELETE" : "PUT";
          const updated = await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(questionId)}/image-override`, method);
          render(documentRef, page, updated);
          const localMessage = questionMessage(page, questionId) || message;
          localMessage.textContent = method === "PUT" ? "Image marked as not needed." : "Image is required again.";
        } else if (acknowledgement) {
          await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/run-diagnostics/${encodeURIComponent(acknowledgement.dataset.acknowledgeRunDiagnostic)}/acknowledgement`, "POST");
          await refresh();
          message.textContent = "Run diagnostic acknowledged.";
        } else if (candidate) {
          const card = candidate.closest("[data-question-id]");
          await withQuestionSavePending(card, async () => {
            const updated = await send(candidateSelectionUrl(page.dataset.runId, card.dataset.questionId), "POST", candidateSelectionPayload(candidate.dataset.selectCandidate));
            render(documentRef, page, updated);
            (questionMessage(page, card.dataset.questionId) || message).textContent = "Image selected.";
          });
        } else if (verify) {
          await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(verify.dataset.verifyQuestion)}/verify-answer`, "POST");
          await refresh();
          message.textContent = "Answer verified.";
        }
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Review update failed.";
      }
    });
    refresh().catch((error) => { message.textContent = error instanceof Error ? error.message : "Review data is unavailable."; });
  };

  const api = {
    blockersText, canPublish, questionAnchor, issueSummary, groupIssues, hasImageReviewIssues,
    shouldRenderNoCandidateEmpty, normalizedEditPayload, normalizedMatchingEditPayload,
    candidateSelectionPayload, candidateSelectionUrl, captureRenderState,
    restoreRenderState, reindexChoiceRows, reindexMatchingChoiceRows, removeChoiceRow,
    removeMatchingChoiceRow, choiceRow, matchingChoiceRow, matchingPromptRow,
    initialize, render, renderRunDiagnostics, setReviewTab, moveReviewTab, applyQuestionSave, questionMessage, withQuestionSavePending, reviewErrorMessage,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
})(typeof globalThis === "undefined" ? this : globalThis);
