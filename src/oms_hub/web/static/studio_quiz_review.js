((root) => {
  "use strict";

  const blockersText = (blockers) => (
    blockers.length ? blockers.join("\n") : "Ready for preview and publication."
  );

  const canPublish = (blockers) => blockers.length === 0;

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
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        payload[key] = String(values[key] || "").trim() || null;
      }
    });
    return payload;
  };

  const candidateSelectionPayload = (candidateId) => ({ image_candidate_id: candidateId });

  const candidateSelectionUrl = (runId, questionId) => (
    `/studio/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(questionId)}/image-selection`
  );

  const csrf = (documentRef) => {
    const value = documentRef.cookie.split(";").map((item) => item.trim())
      .find((item) => item.startsWith("study_hub_csrf="));
    return value ? decodeURIComponent(value.split("=").slice(1).join("=")) : "";
  };

  const text = (documentRef, tagName, value, className = "") => {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  };

  const input = (documentRef, labelValue, name, value, type = "text") => {
    const label = documentRef.createElement("label");
    label.append(text(documentRef, "span", labelValue));
    const field = documentRef.createElement("input");
    field.type = type;
    field.name = name;
    field.value = value ?? "";
    label.append(field);
    return label;
  };

  const textarea = (documentRef, labelValue, name, value) => {
    const label = documentRef.createElement("label");
    label.append(text(documentRef, "span", labelValue));
    const field = documentRef.createElement("textarea");
    field.name = name;
    field.value = value ?? "";
    label.append(field);
    return label;
  };

  const choiceRow = (documentRef, choice, index, correctIndex) => {
    const row = documentRef.createElement("div");
    row.className = "studio-review-choice";
    const choiceInput = documentRef.createElement("input");
    choiceInput.type = "text";
    choiceInput.name = "choice";
    choiceInput.value = choice;
    choiceInput.setAttribute("aria-label", `Choice ${index + 1}`);
    const correct = documentRef.createElement("input");
    correct.type = "radio";
    correct.name = "correct_index";
    correct.value = String(index);
    correct.checked = index === correctIndex;
    correct.setAttribute("aria-label", `Correct choice ${index + 1}`);
    const remove = documentRef.createElement("button");
    remove.type = "button";
    remove.dataset.removeChoice = "true";
    remove.textContent = "Remove";
    row.append(choiceInput, correct, remove);
    return row;
  };

  const renderCandidates = (documentRef, question) => {
    const section = documentRef.createElement("section");
    section.className = "studio-review-candidates";
    section.append(text(documentRef, "h3", "Image candidates"));
    if (!question.candidates.length) {
      section.append(text(documentRef, "p", "No image candidates were found."));
      return section;
    }
    const list = documentRef.createElement("div");
    question.candidates.forEach((candidate) => {
      const card = documentRef.createElement("figure");
      card.className = "studio-review-candidate";
      if (candidate.candidate_id === question.selected_candidate_id) {
        card.classList.add("is-selected");
      }
      const image = documentRef.createElement("img");
      image.src = candidate.preview_url;
      image.alt = `Candidate image from ${candidate.source_title}`;
      image.loading = "lazy";
      card.append(image);
      card.append(text(
        documentRef,
        "figcaption",
        `${candidate.origin} · ${candidate.source_title} · ${candidate.locator}`,
      ));
      const select = documentRef.createElement("button");
      select.type = "button";
      select.dataset.selectCandidate = candidate.candidate_id;
      select.textContent = candidate.candidate_id === question.selected_candidate_id
        ? "Selected"
        : "Use this image";
      select.disabled = candidate.candidate_id === question.selected_candidate_id;
      card.append(select);
      list.append(card);
    });
    section.append(list);
    return section;
  };

  const renderQuestion = (documentRef, question) => {
    const card = documentRef.createElement("article");
    card.className = "card studio-review-question";
    card.dataset.questionId = question.id;
    const heading = text(
      documentRef,
      "h3",
      question.original_identifier ? `Question ${question.original_identifier}` : question.id,
    );
    const badge = text(documentRef, "p", `Answer provenance: ${question.provenance || "unresolved"}`);
    const confidence = text(documentRef, "p", `Extraction confidence: ${question.confidence}`);
    card.append(heading, badge, confidence);
    const refs = documentRef.createElement("ul");
    refs.setAttribute("aria-label", "Source references");
    question.source_refs.forEach((ref) => refs.append(text(
      documentRef,
      "li",
      `${ref.source_id} · ${ref.segment_key} · ${ref.locator}`,
    )));
    card.append(refs);
    const form = documentRef.createElement("form");
    form.dataset.questionEdit = question.id;
    form.append(textarea(documentRef, "Question stem", "stem", question.stem));
    const choices = documentRef.createElement("fieldset");
    choices.dataset.choices = "true";
    choices.append(text(documentRef, "legend", "Choices (select the correct answer)"));
    question.choices.forEach((choice, index) => choices.append(
      choiceRow(documentRef, choice, index, question.correct_index),
    ));
    const add = documentRef.createElement("button");
    add.type = "button";
    add.dataset.addChoice = "true";
    add.textContent = "Add choice";
    choices.append(add);
    form.append(choices, textarea(documentRef, "Rationale", "rationale", question.rationale));
    form.append(input(documentRef, "Topic", "topic", question.topic));
    form.append(input(documentRef, "Area", "area", question.area));
    form.append(input(documentRef, "Learning objective", "learning_objective", question.learning_objective));
    const save = documentRef.createElement("button");
    save.type = "submit";
    save.textContent = "Save question";
    form.append(save);
    card.append(form);
    if (question.verification_required) {
      const verify = documentRef.createElement("button");
      verify.type = "button";
      verify.dataset.verifyQuestion = question.id;
      verify.textContent = question.verified_at ? "Answer verified" : "Verify answer";
      verify.disabled = Boolean(question.verified_at);
      card.append(verify);
    }
    card.append(renderCandidates(documentRef, question));
    return card;
  };

  const render = (documentRef, page, payload) => {
    const blockers = page.querySelector("[data-review-blockers]");
    blockers.replaceChildren();
    payload.blockers.forEach((blocker) => blockers.append(text(documentRef, "li", blocker)));
    if (!payload.blockers.length) blockers.append(text(documentRef, "li", blockersText([])));
    const publish = page.querySelector("[data-publish-quiz]");
    publish.disabled = !canPublish(payload.blockers);
    const preview = page.querySelector("[data-preview-link]");
    preview.hidden = !payload.preview_url;
    preview.href = payload.preview_url || "";
    const questions = page.querySelector("[data-review-questions]");
    questions.replaceChildren(...payload.questions.map((question) => renderQuestion(documentRef, question)));
  };

  const safeJson = async (response) => {
    try {
      return await response.json();
    } catch (_error) {
      return {};
    }
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const page = documentRef.querySelector("[data-practice-review]");
    if (!page || page.dataset.bound === "true") return;
    page.dataset.bound = "true";
    const message = page.querySelector("[data-review-message]");
    let generation = 0;
    const refresh = async () => {
      const request = ++generation;
      const response = await fetchImpl(page.dataset.reviewUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const payload = await safeJson(response);
      if (!response.ok) throw new Error(payload.detail || "Review data is unavailable.");
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
      if (!response.ok) throw new Error(payload.detail || "Review update was rejected.");
      return payload;
    };
    page.addEventListener("submit", async (event) => {
      const form = event.target.closest?.("[data-question-edit]");
      if (!form) return;
      event.preventDefault();
      const choices = [...form.querySelectorAll('input[name="choice"]')].map((field) => field.value);
      const correct = form.querySelector('input[name="correct_index"]:checked');
      const payload = normalizedEditPayload({
        stem: form.querySelector('[name="stem"]').value,
        choices,
        correct_index: correct ? correct.value : -1,
        rationale: form.querySelector('[name="rationale"]').value,
        topic: form.querySelector('[name="topic"]').value,
        area: form.querySelector('[name="area"]').value,
        learning_objective: form.querySelector('[name="learning_objective"]').value,
      });
      if (payload.choices.length < 2 || payload.choices.length > 8 || payload.correct_index < 0 || payload.correct_index >= payload.choices.length) {
        message.textContent = "Provide two to eight choices and select the correct answer.";
        return;
      }
      try {
        await send(`/studio/runs/${encodeURIComponent(page.dataset.runId)}/questions/${encodeURIComponent(form.dataset.questionEdit)}`, "PATCH", payload);
        await refresh();
        message.textContent = "Question saved.";
      } catch (error) {
        message.textContent = error instanceof Error ? error.message : "Question could not be saved.";
      }
    });
    page.addEventListener("click", async (event) => {
      const add = event.target.closest?.("[data-add-choice]");
      if (add) {
        const fieldset = add.closest("fieldset");
        const rows = fieldset.querySelectorAll(".studio-review-choice");
        if (rows.length < 8) fieldset.insertBefore(choiceRow(documentRef, "", rows.length, -1), add);
        return;
      }
      const remove = event.target.closest?.("[data-remove-choice]");
      if (remove) {
        const fieldset = remove.closest("fieldset");
        if (fieldset.querySelectorAll(".studio-review-choice").length > 2) remove.closest(".studio-review-choice").remove();
        return;
      }
      const candidate = event.target.closest?.("[data-select-candidate]");
      const verify = event.target.closest?.("[data-verify-question]");
      const publish = event.target.closest?.("[data-publish-quiz]");
      try {
        if (candidate) {
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
    refresh().catch((error) => {
      message.textContent = error instanceof Error ? error.message : "Review data is unavailable.";
    });
  };

  const api = { blockersText, canPublish, normalizedEditPayload, candidateSelectionPayload, candidateSelectionUrl, initialize, render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) root.document.addEventListener("DOMContentLoaded", () => initialize(root.document), { once: true });
})(typeof globalThis === "undefined" ? this : globalThis);
