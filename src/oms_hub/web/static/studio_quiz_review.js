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

  const candidateSelectionPayload = (candidateId) => ({ image_candidate_id: candidateId });
  const candidateSelectionUrl = (runId, questionId) => `/studio/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(questionId)}/image-selection`;

  const reindexChoiceRows = (group, questionId = "") => {
    Array.from(group?.querySelectorAll?.(".studio-review-choice") || []).forEach((row, index) => {
      const choice = row.querySelector?.('input[name="choice"]');
      const correct = row.querySelector?.('input[name="correct_index"]');
      const overflow = row.querySelector?.("details");
      const summary = overflow?.querySelector?.("summary");
      const remove = row.querySelector?.("[data-remove-choice]");
      choice?.setAttribute?.("aria-label", `Choice ${index + 1}`);
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
    overflow.className = "studio-review-choice-overflow";
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
    overflow.append(summary, remove);
    row.append(choiceInput, correctLabel, overflow);
    return row;
  };

  const renderCandidates = (documentRef, question) => {
    const section = documentRef.createElement("section");
    section.className = "studio-review-candidates";
    section.append(text(documentRef, "h4", "Image candidates", "sh-section-label"));
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

  const renderQuestion = (documentRef, question) => {
    const card = documentRef.createElement("article");
    card.className = "sh-card studio-review-question";
    card.dataset.questionId = question.id;
    card.id = questionAnchor(question.id);
    card.append(text(documentRef, "h3", question.original_identifier ? `Question ${question.original_identifier}` : question.id));
    card.append(text(documentRef, "p", `Answer provenance: ${question.provenance || "unresolved"}`, "sh-row__meta"));
    card.append(text(documentRef, "p", `Extraction confidence: ${question.confidence}`, "sh-row__meta"));
    const refs = documentRef.createElement("ul");
    refs.className = "studio-review-sources";
    refs.setAttribute("aria-label", "Source references");
    question.source_refs.forEach((ref) => refs.append(text(documentRef, "li", `${ref.source_id} · ${ref.segment_key} · ${ref.locator}`)));
    card.append(refs);
    const form = documentRef.createElement("form");
    form.dataset.questionEdit = question.id;
    form.className = "studio-review-form";
    form.append(textarea(documentRef, "Question stem", "stem", question.stem, `question:${question.id}:stem`));
    const choices = documentRef.createElement("section");
    choices.className = "sh-card studio-review-choice-group";
    choices.dataset.choices = "true";
    choices.append(text(documentRef, "p", "Choices (select the correct answer)", "sh-section-label"));
    question.choices.forEach((choice, index) => choices.append(choiceRow(documentRef, choice, index, question.correct_index, question.id)));
    const add = documentRef.createElement("button");
    add.type = "button";
    add.dataset.addChoice = "true";
    add.dataset.focusKey = `question:${question.id}:add-choice`;
    add.className = "sh-btn sh-btn--secondary";
    add.textContent = "Add choice";
    choices.append(add);
    form.append(choices, textarea(documentRef, "Rationale", "rationale", question.rationale, `question:${question.id}:rationale`));
    form.append(input(documentRef, "Topic", "topic", question.topic, "text", `question:${question.id}:topic`));
    form.append(input(documentRef, "Area", "area", question.area, "text", `question:${question.id}:area`));
    form.append(input(documentRef, "Learning objective", "learning_objective", question.learning_objective, "text", `question:${question.id}:learning-objective`));
    const save = documentRef.createElement("button");
    save.type = "submit";
    save.className = "sh-btn sh-btn--primary";
    save.dataset.focusKey = `question:${question.id}:save`;
    save.textContent = "Save question";
    form.append(save);
    card.append(form);
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
    if (question.candidates.length) card.append(renderCandidates(documentRef, question));
    return card;
  };

  const renderIssues = (documentRef, page, payload) => {
    const target = page.querySelector("[data-review-blockers]");
    target.replaceChildren();
    const issues = payload.issues || payload.blockers.map((message) => ({
      question_id: message.split(":", 1)[0], display_label: message.split(":", 1)[0], type: "review", message, role: "err",
    }));
    const hasBlockingRunDiagnostic = (payload.run_diagnostics || []).some((diagnostic) => (
      diagnostic.severity === "blocker"
      && !(diagnostic.overridable && diagnostic.acknowledged)
    ));
    if (!issues.length && !hasBlockingRunDiagnostic) {
      const ready = documentRef.createElement("section");
      ready.className = "sh-empty";
      ready.append(text(documentRef, "h3", "Ready for preview and publication.", "sh-empty__title"));
      target.append(ready);
      return;
    }
    target.append(text(documentRef, "p", issueSummary(issues), "sh-pill sh-pill--bare"));
    groupIssues(issues).forEach((group) => {
      const details = documentRef.createElement("details");
      details.className = "studio-review-issue-group sh-card";
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
    publish.disabled = !canPublish(payload.blockers);
    const preview = page.querySelector("[data-preview-link]");
    preview.hidden = !payload.preview_url;
    preview.href = payload.preview_url || "";
    const questions = page.querySelector("[data-review-questions]");
    const existing = new Map(Array.from(
      questions.querySelectorAll?.("[data-question-id]") || [],
      (card) => [card.dataset.questionId, card],
    ));
    const rendered = [];
    if (shouldRenderNoCandidateEmpty(payload)) {
      const empty = documentRef.createElement("section");
      empty.className = "sh-empty";
      empty.append(text(documentRef, "h3", "No image candidates were found.", "sh-empty__title"));
      empty.append(text(documentRef, "p", "Continue answer review or add images only where required.", "sh-empty__text"));
      rendered.push(empty);
    }
    payload.questions.forEach((question) => {
      const current = existing.get(question.id);
      rendered.push(current?.dataset.dirty === "true"
        ? current
        : renderQuestion(documentRef, question));
      existing.delete(question.id);
    });
    existing.forEach((card) => {
      if (card.dataset.dirty === "true") rendered.push(card);
    });
    questions.replaceChildren(...rendered);
    restoreRenderState(page, state);
  };

  const applyQuestionSave = (documentRef, page, form, payload) => {
    const card = form.closest?.("[data-question-id]");
    if (card) delete card.dataset.dirty;
    render(documentRef, page, payload);
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
      const form = event.target.closest?.("[data-question-edit]");
      if (!form) return;
      event.preventDefault();
      const choices = [...form.querySelectorAll('input[name="choice"]')].map((field) => field.value);
      const correct = form.querySelector('input[name="correct_index"]:checked');
      const payload = normalizedEditPayload({
        stem: form.querySelector('[name="stem"]').value, choices, correct_index: correct ? correct.value : -1,
        rationale: form.querySelector('[name="rationale"]').value, topic: form.querySelector('[name="topic"]').value,
        area: form.querySelector('[name="area"]').value, learning_objective: form.querySelector('[name="learning_objective"]').value,
      });
      if (payload.choices.length < 2 || payload.choices.length > 8 || payload.correct_index < 0 || payload.correct_index >= payload.choices.length) {
        message.textContent = "Provide two to eight choices and select the correct answer.";
        return;
      }
      if (!payload.rationale) {
        message.textContent = "Provide an answer rationale before saving.";
        return;
      }
      try {
        const updated = await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(form.dataset.questionEdit)}`, "PATCH", payload);
        applyQuestionSave(documentRef, page, form, updated);
        message.textContent = "Question saved.";
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Question could not be saved.";
      }
    });
    const markDirty = (event) => {
      const card = event.target.closest?.("[data-question-edit]")?.closest?.("[data-question-id]");
      if (card) card.dataset.dirty = "true";
    };
    page.addEventListener("input", markDirty);
    page.addEventListener("change", markDirty);
    page.addEventListener("click", async (event) => {
      const add = event.target.closest?.("[data-add-choice]");
      if (add) {
        const group = add.closest("[data-choices]");
        const card = add.closest("[data-question-id]");
        const rows = group.querySelectorAll(".studio-review-choice");
        const questionId = add.closest("[data-question-id]")?.dataset.questionId || "";
        if (rows.length < 8) {
          group.insertBefore(choiceRow(documentRef, "", rows.length, -1, questionId), add);
          reindexChoiceRows(group, questionId);
          if (card) card.dataset.dirty = "true";
        }
        return;
      }
      const remove = event.target.closest?.("[data-remove-choice]");
      if (remove) {
        removeChoiceRow(remove);
        return;
      }
      const candidate = event.target.closest?.("[data-select-candidate]");
      const acknowledgement = event.target.closest?.("[data-acknowledge-run-diagnostic]");
      const verify = event.target.closest?.("[data-verify-question]");
      const publish = event.target.closest?.("[data-publish-quiz]");
      try {
        if (acknowledgement) {
          await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/run-diagnostics/${encodeURIComponent(acknowledgement.dataset.acknowledgeRunDiagnostic)}/acknowledgement`, "POST");
          await refresh();
          message.textContent = "Run diagnostic acknowledged.";
        } else if (candidate) {
          const card = candidate.closest("[data-question-id]");
          await send(candidateSelectionUrl(page.dataset.runId, card.dataset.questionId), "POST", candidateSelectionPayload(candidate.dataset.selectCandidate));
          await refresh();
          message.textContent = "Image selected.";
        } else if (verify) {
          await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(verify.dataset.verifyQuestion)}/verify-answer`, "POST");
          await refresh();
          message.textContent = "Answer verified.";
        } else if (publish) {
          if (publish.disabled) return;
          const result = await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/publication`, "POST");
          if (result.published_url) root.location.assign(result.published_url);
        }
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Review update failed.";
      }
    });
    refresh().catch((error) => { message.textContent = error instanceof Error ? error.message : "Review data is unavailable."; });
  };

  const api = {
    blockersText, canPublish, questionAnchor, issueSummary, groupIssues, hasImageReviewIssues,
    shouldRenderNoCandidateEmpty, normalizedEditPayload,
    candidateSelectionPayload, candidateSelectionUrl, captureRenderState,
    restoreRenderState, reindexChoiceRows, removeChoiceRow, choiceRow,
    initialize, render, renderRunDiagnostics, applyQuestionSave, reviewErrorMessage,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
})(typeof globalThis === "undefined" ? this : globalThis);
