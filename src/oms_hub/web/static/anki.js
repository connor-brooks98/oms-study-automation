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

  const requestJson = async (documentRef, fetchImpl, url, options = {}) => {
    const headers = {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.method && options.method !== "GET"
        ? { "X-CSRF-Token": csrfToken(documentRef) }
        : {}),
      ...(options.headers || {}),
    };
    const response = await fetchImpl(url, {
      cache: "no-store",
      ...options,
      headers,
    });
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      const detail = Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg).join("; ")
        : payload.detail;
      throw new Error(detail || "Study Hub could not complete that request.");
    }
    return payload;
  };

  const commaValues = (value) => [
    ...new Set(
      String(value || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];

  const element = (documentRef, name, className, text) => {
    const node = documentRef.createElement(name);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const readableState = (state) => String(state || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());

  const sourceLabel = (kind) => ({
    slides: "Lecture slides",
    transcripts: "Lecture transcript",
  })[kind] || readableState(kind);

  const parseLecturePayload = (value) => {
    const payload = JSON.parse(String(value || "[]"));
    if (!Array.isArray(payload)) {
      throw new Error("Lecture data must be a list.");
    }
    return payload;
  };

  const resolveLecture = (lectures, lectureId) => {
    const selectedId = Number(lectureId);
    if (!Number.isInteger(selectedId) || selectedId < 1) return null;
    return lectures.find((lecture) => Number(lecture.id) === selectedId)
      || null;
  };

  const renderSourceChoices = (documentRef, lecture) => {
    const container = documentRef.querySelector("#anki-source-revisions");
    if (!container) return;
    container.replaceChildren();
    const revisions = Array.isArray(lecture?.revisions)
      ? lecture.revisions
      : [];
    if (!revisions.length) {
      container.append(
        element(
          documentRef,
          "p",
          "quiet-state",
          lecture
            ? "This lecture has no current slides or transcript."
            : "Choose a lecture to see its current slides and transcript.",
        ),
      );
      return;
    }
    revisions.forEach((revision) => {
      const label = element(documentRef, "label", "anki-source-option");
      const input = documentRef.createElement("input");
      input.type = "checkbox";
      input.name = "source_revision_ids";
      input.value = revision.id;
      input.checked = true;
      const copy = element(documentRef, "span");
      copy.append(
        element(documentRef, "strong", "", sourceLabel(revision.kind)),
        element(
          documentRef,
          "small",
          "",
          `Current revision · ${String(revision.source_sha256).slice(0, 10)}`,
        ),
      );
      label.append(input, copy);
      container.append(label);
    });
  };

  const jobRow = (documentRef, job) => {
    const row = element(documentRef, "a", "anki-job-row");
    row.href = `/anki/jobs/${job.id}`;
    row.dataset.jobId = job.id;
    const dot = element(
      documentRef,
      "span",
      `status-dot status-${job.state}`,
    );
    const description = element(documentRef, "span");
    description.append(
      element(documentRef, "strong", "", `Lecture ${job.lecture_id}`),
      element(documentRef, "small", "", job.target_deck),
    );
    const state = element(
      documentRef,
      "span",
      `status-pill status-${job.state}`,
      readableState(job.state),
    );
    const time = element(
      documentRef,
      "time",
      "",
      String(job.updated_at || "").slice(0, 16).replace("T", " "),
    );
    row.append(dot, description, state, time);
    return row;
  };

  const refreshJobs = async (documentRef, fetchImpl) => {
    const container = documentRef.querySelector("#anki-job-list");
    if (!container) return;
    const payload = await requestJson(
      documentRef,
      fetchImpl,
      "/api/anki/jobs",
    );
    container.replaceChildren();
    if (!payload.jobs.length) {
      const empty = element(documentRef, "div", "empty-state anki-empty-compact");
      empty.append(
        element(documentRef, "h2", "", "No curation runs yet"),
        element(
          documentRef,
          "p",
          "",
          "Your first run will appear here and remain resumable if Study Hub restarts.",
        ),
      );
      container.append(empty);
      return;
    }
    payload.jobs.forEach((job) => container.append(jobRow(documentRef, job)));
  };

  const initializeHome = (documentRef, fetchImpl) => {
    const form = documentRef.querySelector("#anki-create-form");
    if (!form) return;
    const lectureId = form.elements.lecture_id;
    const targetTag = form.elements.target_tag;
    const selectedLabel = documentRef.querySelector(
      "[data-selected-lecture]",
    );
    const lectureButtons = [
      ...documentRef.querySelectorAll("[data-lecture-id]"),
    ];
    let lectures;
    try {
      lectures = parseLecturePayload(
        documentRef.querySelector("#anki-lecture-data")?.textContent,
      );
    } catch {
      lectures = [];
      lectureButtons.forEach((button) => {
        button.disabled = true;
      });
      documentRef.querySelector("#anki-source-revisions").replaceChildren(
        element(
          documentRef,
          "p",
          "quiet-state",
          "Study Hub could not load the lecture source catalog. Reload the page.",
        ),
      );
    }
    lectureButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const lecture = resolveLecture(lectures, button.dataset.lectureId);
        if (!lecture) return;
        lectureId.value = String(lecture.id);
        targetTag.value = String(lecture.target_tag || "");
        lectureButtons.forEach((candidate) => {
          candidate.setAttribute(
            "aria-pressed",
            String(candidate === button),
          );
        });
        if (selectedLabel) {
          selectedLabel.textContent = (
            `${lecture.subject} · Exam ${lecture.exam_number} · `
            + `Lecture ${lecture.lecture_number} — ${lecture.topic}`
          );
        }
        renderSourceChoices(documentRef, lecture);
      });
    });
    renderSourceChoices(documentRef, null);

    const refresh = documentRef.querySelector("[data-refresh-jobs]");
    refresh?.addEventListener("click", async () => {
      refresh.disabled = true;
      try {
        await refreshJobs(documentRef, fetchImpl);
      } finally {
        refresh.disabled = false;
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = documentRef.querySelector("#anki-create-message");
      const submit = form.querySelector("[type=submit]");
      const values = new FormData(form);
      if (!String(values.get("lecture_id") || "").trim()) {
        message.textContent = "Choose a lecture.";
        return;
      }
      const selectedSources = [
        ...form.querySelectorAll(
          "input[name=source_revision_ids]:checked",
        ),
      ].map((input) => Number(input.value));
      if (!selectedSources.length) {
        message.textContent = "Choose at least one lecture source.";
        return;
      }
      const body = {
        contract_version: 1,
        lecture_id: Number(values.get("lecture_id")),
        block_id: String(values.get("block_id") || "").trim() || null,
        source_revision_ids: selectedSources,
        deck_allowlist: commaValues(values.get("deck_allowlist")),
        tag_allowlist: commaValues(values.get("tag_allowlist")),
        target_deck: String(values.get("target_deck") || "").trim(),
        target_tag: String(values.get("target_tag") || "").trim(),
        index_snapshot_id: String(
          values.get("index_snapshot_id") || "",
        ).trim(),
        instruction_text: String(
          values.get("instruction_text") || "",
        ).trim(),
        lcl_prompt_version: String(
          values.get("lcl_prompt_version") || "",
        ).trim(),
        judgment_rubric_version: String(
          values.get("judgment_rubric_version") || "",
        ).trim(),
        gap_prompt_version: String(
          values.get("gap_prompt_version") || "",
        ).trim(),
        provider: String(values.get("provider") || "").trim(),
        model: String(values.get("model") || "").trim(),
      };
      submit.disabled = true;
      message.textContent = "Pinning sources and adding this run to the queue…";
      try {
        const job = await requestJson(
          documentRef,
          fetchImpl,
          "/api/anki/jobs",
          { method: "POST", body: JSON.stringify(body) },
        );
        root.location.assign(`/anki/jobs/${job.id}`);
      } catch (error) {
        message.textContent = error.message;
        submit.disabled = false;
      }
    });
  };

  const emptyGroup = (documentRef, message) => {
    const empty = element(documentRef, "div", "anki-group-empty");
    empty.textContent = message;
    return empty;
  };

  const tagEditor = (documentRef, candidate, policy) => {
    const wrapper = element(documentRef, "div", "anki-tag-editor");
    const heading = element(documentRef, "div", "anki-tag-heading");
    heading.append(
      element(documentRef, "strong", "", "Card tags"),
      element(
        documentRef,
        "span",
        "",
        "Source-managed tags are locked",
      ),
    );
    wrapper.append(heading);
    const tags = candidate.note?.tags || [];
    const currentTags = candidate.note?.current_tags
      || tags.map((tag) => tag.value);
    const protectedTags = tags.filter((tag) => tag.locked);
    const editableTags = tags.filter((tag) => !tag.locked);
    const editableCurrentTags = currentTags.filter((tag) => {
      const displayed = tags.find(
        (item) => item.value.toLocaleLowerCase() === tag.toLocaleLowerCase(),
      );
      return !displayed?.locked;
    });
    const protectedList = element(documentRef, "div", "anki-tag-chips");
    protectedTags.forEach((tag) => {
      const chip = element(
        documentRef,
        "span",
        "anki-tag-chip is-locked",
        tag.value,
      );
      chip.title = "Protected source tag";
      chip.prepend(element(documentRef, "span", "", "⌑ "));
      protectedList.append(chip);
    });
    if (protectedTags.length) wrapper.append(protectedList);

    const label = element(documentRef, "label", "anki-editable-tags");
    label.append(element(documentRef, "span", "", "Editable lecture tags"));
    const input = documentRef.createElement("input");
    input.type = "text";
    input.value = editableTags.map((tag) => tag.value).join(", ");
    input.placeholder = "OMS::Reviewed, AnkiHub_Optional::LMU_OMS_II::…";
    input.dataset.tagEditor = "";
    input.dataset.noteId = candidate.note_id;
    input.dataset.before = JSON.stringify(currentTags);
    input.dataset.protected = JSON.stringify(
      protectedTags.map((tag) => tag.value),
    );
    input.dataset.editableBefore = JSON.stringify(
      editableCurrentTags,
    );
    input.dataset.expectedHash = candidate.note?.tag_hash || "";
    input.dataset.policyVersion = policy.version || "";
    if (!candidate.note) {
      input.disabled = true;
      input.placeholder = "Current Anki tags unavailable";
    }
    label.append(input);
    wrapper.append(label);
    return wrapper;
  };

  const candidateCard = (documentRef, candidate, policy) => {
    const card = element(documentRef, "article", "anki-match-card");
    card.dataset.noteId = candidate.note_id;
    const header = element(documentRef, "div", "anki-card-choice");
    const label = documentRef.createElement("label");
    label.className = "anki-select-control";
    const checkbox = documentRef.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = candidate.selected;
    checkbox.dataset.candidateSelection = "";
    checkbox.value = candidate.note_id;
    label.append(
      checkbox,
      element(documentRef, "span", "", "Use this existing card"),
    );
    const score = element(
      documentRef,
      "span",
      "anki-confidence",
      `${Math.round(candidate.confidence * 100)}% confidence`,
    );
    header.append(label, score);

    const front = element(
      documentRef,
      "div",
      "anki-card-front",
      candidate.note?.text || `Anki note ${candidate.note_id}`,
    );
    const extra = element(
      documentRef,
      "p",
      "anki-card-extra",
      candidate.note?.extra || "Card text is not available in this index.",
    );
    const details = documentRef.createElement("details");
    details.className = "anki-match-details";
    const summary = documentRef.createElement("summary");
    summary.textContent = "Why this matched";
    const reason = element(documentRef, "p", "", candidate.reason);
    const facts = element(documentRef, "dl", "anki-score-list");
    Object.entries(candidate.scores || {}).forEach(([name, value]) => {
      const row = element(documentRef, "div");
      row.append(
        element(documentRef, "dt", "", readableState(name)),
        element(
          documentRef,
          "dd",
          "",
          Number(value).toFixed(3),
        ),
      );
      facts.append(row);
    });
    details.append(summary, reason, facts);
    card.append(
      header,
      front,
      extra,
      details,
      tagEditor(documentRef, candidate, policy),
    );
    return card;
  };

  const citationCard = (documentRef, citation) => {
    const details = documentRef.createElement("details");
    details.className = "anki-citation";
    const summary = documentRef.createElement("summary");
    const locations = (citation.source_refs || [])
      .map((reference) => `${readableState(reference.source_kind)} · ${reference.locator}`)
      .join(", ");
    summary.textContent = locations || citation.evidence_id;
    details.append(
      summary,
      element(documentRef, "p", "", citation.statement),
    );
    return details;
  };

  const generatedCard = (documentRef, card) => {
    const article = element(documentRef, "article", "anki-generated-card");
    article.dataset.conceptId = card.concept_id;
    const header = element(documentRef, "div", "anki-card-choice");
    const label = documentRef.createElement("label");
    label.className = "anki-select-control";
    const checkbox = documentRef.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = card.selected;
    checkbox.dataset.gapSelection = "";
    label.append(checkbox, element(documentRef, "span", "", "Create this card"));
    const validation = element(
      documentRef,
      "span",
      `anki-validation is-${card.validation_state}`,
      readableState(card.validation_state),
    );
    header.append(label, validation);

    const textLabel = element(documentRef, "label", "anki-card-field");
    textLabel.append(element(documentRef, "span", "", "Cloze card"));
    const text = documentRef.createElement("textarea");
    text.rows = 3;
    text.value = card.text;
    text.dataset.gapText = "";
    textLabel.append(text);

    const extraLabel = element(documentRef, "label", "anki-card-field");
    extraLabel.append(element(documentRef, "span", "", "Extra"));
    const extra = documentRef.createElement("textarea");
    extra.rows = 3;
    extra.value = card.extra;
    extra.dataset.gapExtra = "";
    extraLabel.append(extra);

    const citations = element(documentRef, "div", "anki-citations");
    citations.append(
      element(
        documentRef,
        "strong",
        "",
        `Sources (${card.citations.length})`,
      ),
    );
    card.citations.forEach((citation) => {
      citations.append(citationCard(documentRef, citation));
    });
    article.append(header, textLabel, extraLabel, citations);
    return article;
  };

  const unresolvedCard = (documentRef, item) => {
    const article = element(documentRef, "article", "anki-unresolved-card");
    article.append(
      element(
        documentRef,
        "strong",
        "",
        readableState(item.concept_id),
      ),
      element(documentRef, "span", "status-pill", readableState(item.status)),
      element(documentRef, "p", "", item.reason),
    );
    return article;
  };

  const setGroup = (documentRef, selector, items, factory, emptyMessage) => {
    const container = documentRef.querySelector(selector);
    container.replaceChildren();
    if (!items.length) {
      container.append(emptyGroup(documentRef, emptyMessage));
      return;
    }
    items.forEach((item) => container.append(factory(item)));
  };

  const renderReview = (documentRef, review) => {
    const groups = review.groups;
    documentRef.querySelector("[data-count-pass1]").textContent =
      groups.pass_1_matches.length;
    documentRef.querySelector("[data-count-pass2]").textContent =
      groups.recovered_in_pass_2.length;
    documentRef.querySelector("[data-count-generated]").textContent =
      groups.generated_cards.length;
    documentRef.querySelector("[data-count-unresolved]").textContent =
      groups.unresolved.length;
    setGroup(
      documentRef,
      "[data-group-pass1]",
      groups.pass_1_matches,
      (candidate) => candidateCard(documentRef, candidate, review.tag_policy),
      "No first-pass matches were retained.",
    );
    setGroup(
      documentRef,
      "[data-group-pass2]",
      groups.recovered_in_pass_2,
      (candidate) => candidateCard(documentRef, candidate, review.tag_policy),
      "No additional existing cards were recovered.",
    );
    setGroup(
      documentRef,
      "[data-group-generated]",
      groups.generated_cards,
      (card) => generatedCard(documentRef, card),
      "No new cards are needed for this lecture.",
    );
    setGroup(
      documentRef,
      "[data-group-unresolved]",
      groups.unresolved,
      (item) => unresolvedCard(documentRef, item),
      "No concepts remain unresolved.",
    );
    const editable = review.can_edit;
    documentRef
      .querySelectorAll(
        "[data-candidate-selection], [data-gap-selection], "
          + "[data-gap-text], [data-gap-extra], [data-tag-editor]",
      )
      .forEach((control) => {
        control.disabled = control.disabled || !editable;
      });
    documentRef.querySelector("[data-save-review]").disabled = !editable;
    documentRef.querySelector("[data-build-envelope]").disabled =
      !review.can_build_envelope;
    documentRef.querySelector("#anki-processing").hidden = true;
    documentRef.querySelector("#anki-review-content").hidden = false;
  };

  const stageOrder = [
    "queued",
    "preflight",
    "building_source_index",
    "building_lcl",
    "retrieving_pass_1",
    "judging_pass_1",
    "localizing_missed_concepts",
    "retrieving_pass_2",
    "judging_pass_2",
    "deduping",
    "generating_gaps",
    "ready_for_review",
  ];

  const renderProcessing = (documentRef, job) => {
    const index = Math.max(stageOrder.indexOf(job.state), 0);
    const percent = Math.max(
      6,
      Math.round(((index + 1) / stageOrder.length) * 100),
    );
    documentRef.querySelector("#anki-job-state").textContent =
      readableState(job.state);
    documentRef.querySelector("#anki-job-state").className =
      `status-pill status-${job.state}`;
    documentRef.querySelector("#anki-processing-label").textContent =
      readableState(job.state);
    documentRef.querySelector("#anki-processing-count").textContent =
      `${percent}%`;
    documentRef.querySelector("#anki-processing-progress").style.width =
      `${percent}%`;
    documentRef.querySelector("#anki-processing-note").textContent =
      job.error || (
        job.state === "failed"
          ? "Curation stopped. The details above explain what needs attention."
          : "This run is resumable. You may leave this page while Study Hub works."
      );
  };

  const editableTagPatch = (input) => {
    const before = JSON.parse(input.dataset.before || "[]");
    const protectedTags = JSON.parse(input.dataset.protected || "[]");
    const editableBefore = JSON.parse(
      input.dataset.editableBefore || "[]",
    );
    const editableAfter = commaValues(input.value);
    const beforeByKey = new Map(
      editableBefore.map((tag) => [tag.toLocaleLowerCase(), tag]),
    );
    const afterByKey = new Map(
      editableAfter.map((tag) => [tag.toLocaleLowerCase(), tag]),
    );
    const addTags = [
      ...afterByKey.entries(),
    ].filter(([key]) => !beforeByKey.has(key)).map(([, tag]) => tag);
    const removeTags = [
      ...beforeByKey.entries(),
    ].filter(([key]) => !afterByKey.has(key)).map(([, tag]) => tag);
    if (!addTags.length && !removeTags.length) return null;
    return {
      contract_version: 1,
      note_id: Number(input.dataset.noteId),
      before,
      after: [...protectedTags, ...editableAfter],
      add_tags: addTags,
      remove_tags: removeTags,
      expected_tag_hash: input.dataset.expectedHash,
      tag_policy_version: input.dataset.policyVersion,
    };
  };

  const collectReview = (documentRef, revision) => ({
    contract_version: 1,
    expected_revision: revision,
    reviewer: "local-user",
    candidate_selections: Object.fromEntries(
      [...documentRef.querySelectorAll("[data-candidate-selection]")]
        .map((input) => [input.value, input.checked]),
    ),
    gap_edits: [
      ...documentRef.querySelectorAll(".anki-generated-card"),
    ].map((card) => ({
      contract_version: 1,
      concept_id: card.dataset.conceptId,
      text: card.querySelector("[data-gap-text]").value,
      extra: card.querySelector("[data-gap-extra]").value,
      selected: card.querySelector("[data-gap-selection]").checked,
    })),
    tag_patches: [
      ...documentRef.querySelectorAll("[data-tag-editor]"),
    ].map(editableTagPatch).filter(Boolean),
  });

  const showRecovery = (documentRef, recovery, applyState) => {
    const notice = documentRef.querySelector("#anki-recovery");
    notice.hidden = false;
    notice.dataset.kind = recovery.kind;
    notice.querySelector("[data-recovery-title]").textContent = ({
      complete: "Anki is up to date",
      no_changes: "No Anki changes were made",
      retry_sync: "Local changes are safe; sync needs a retry",
      sync_blocked: "Local changes need sync attention",
      verification_mismatch: "Verification found a mismatch",
      manual_attention: "Local changes need review",
    })[recovery.kind] || "Apply status";
    notice.querySelector("[data-recovery-message]").textContent =
      recovery.message;
    notice.querySelector("[data-retry-sync]").hidden =
      !["applied_local_sync_retryable", "applied_local_sync_blocked"]
        .includes(applyState);
  };

  const fillApplyPlan = (documentRef, summary) => {
    documentRef.querySelector("[data-plan-created]").textContent =
      summary.notes_created;
    documentRef.querySelector("[data-plan-retagged]").textContent =
      summary.existing_notes_retagged;
    documentRef.querySelector("[data-plan-added]").textContent =
      summary.tags_added;
    documentRef.querySelector("[data-plan-removed]").textContent =
      summary.tags_removed;
  };

  const initializeReview = (documentRef, fetchImpl) => {
    const page = documentRef.querySelector("[data-anki-review]");
    if (!page) return;
    const jobId = page.dataset.jobId;
    const message = documentRef.querySelector("#anki-review-message");
    const dialog = documentRef.querySelector("#anki-apply-dialog");
    let revision = 0;
    let pollTimer;

    const refreshJob = async () => {
      const job = await requestJson(
        documentRef,
        fetchImpl,
        `/api/anki/jobs/${jobId}`,
      );
      renderProcessing(documentRef, job);
      if (job.recovery.kind !== "pending") {
        showRecovery(documentRef, job.recovery, job.apply_state);
      }
      if (job.state === "ready_for_review") {
        const review = await requestJson(
          documentRef,
          fetchImpl,
          `/api/anki/jobs/${jobId}/review`,
        );
        revision = review.job.review_revision;
        renderReview(documentRef, review);
        return;
      }
      if ([
        "envelope_pending",
        "applying_local",
        "syncing",
        "verifying",
        "complete",
      ].includes(job.state)) {
        const review = await requestJson(
          documentRef,
          fetchImpl,
          `/api/anki/jobs/${jobId}/review`,
        );
        revision = review.job.review_revision;
        renderReview(documentRef, review);
        if (job.envelope?.plan_summary && job.state === "envelope_pending") {
          fillApplyPlan(documentRef, job.envelope.plan_summary);
          dialog.showModal();
        }
        return;
      }
      if (!["failed", "canceled"].includes(job.state)) {
        pollTimer = root.setTimeout(refreshJob, 2500);
      }
    };

    const saveReview = async () => {
      const body = collectReview(documentRef, revision);
      const saved = await requestJson(
        documentRef,
        fetchImpl,
        `/api/anki/jobs/${jobId}/review`,
        { method: "PUT", body: JSON.stringify(body) },
      );
      revision = saved.revision;
      return saved;
    };

    documentRef.querySelector("[data-save-review]")
      ?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        message.textContent = "Saving this review…";
        try {
          await saveReview();
          message.textContent = `Review revision ${revision} saved. No Anki changes were made.`;
        } catch (error) {
          message.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });

    documentRef.querySelector("[data-build-envelope]")
      ?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        message.textContent = "Validating and freezing this review…";
        try {
          await saveReview();
          const plan = await requestJson(
            documentRef,
            fetchImpl,
            `/api/anki/jobs/${jobId}/envelope`,
            {
              method: "POST",
              body: JSON.stringify({
                contract_version: 1,
                review_revision: revision,
              }),
            },
          );
          fillApplyPlan(documentRef, plan.summary);
          message.textContent =
            "Apply plan is frozen. Inspect the final counts before confirming.";
          dialog.showModal();
          button.disabled = true;
          documentRef.querySelector("[data-save-review]").disabled = true;
        } catch (error) {
          message.textContent = error.message;
          button.disabled = false;
        }
      });

    documentRef.querySelector("[data-confirm-apply]")
      ?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const errorNode = dialog.querySelector("[data-dialog-error]");
        button.disabled = true;
        errorNode.textContent =
          "Syncing first, then applying the frozen plan…";
        try {
          const result = await requestJson(
            documentRef,
            fetchImpl,
            `/api/anki/jobs/${jobId}/apply`,
            {
              method: "POST",
              body: JSON.stringify({
                contract_version: 1,
                review_revision: revision,
                confirmation: "APPLY TO ANKI",
              }),
            },
          );
          dialog.close();
          showRecovery(documentRef, result.recovery, result.apply_state);
          message.textContent = result.recovery.message;
        } catch (error) {
          errorNode.textContent = error.message;
          button.disabled = false;
        }
      });

    documentRef.querySelector("[data-retry-sync]")
      ?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const result = await requestJson(
            documentRef,
            fetchImpl,
            `/api/anki/jobs/${jobId}/retry-sync`,
            {
              method: "POST",
              body: JSON.stringify({
                contract_version: 1,
                confirmation: "RETRY SYNC",
              }),
            },
          );
          showRecovery(documentRef, result.recovery, result.apply_state);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });

    void refreshJob().catch((error) => {
      documentRef.querySelector("#anki-processing-label").textContent =
        "Review unavailable";
      documentRef.querySelector("#anki-processing-note").textContent =
        error.message;
    });
    root.addEventListener?.("pagehide", () => root.clearTimeout(pollTimer));
  };

  const initialize = (
    documentRef,
    fetchImpl = root.fetch.bind(root),
  ) => {
    initializeHome(documentRef, fetchImpl);
    initializeReview(documentRef, fetchImpl);
  };

  const runWhenReady = (documentRef, callback) => {
    if (documentRef.readyState === "loading") {
      documentRef.addEventListener("DOMContentLoaded", callback, {
        once: true,
      });
      return;
    }
    callback();
  };

  const api = {
    collectReview,
    commaValues,
    csrfToken,
    editableTagPatch,
    initialize,
    parseLecturePayload,
    readableState,
    renderProcessing,
    resolveLecture,
    runWhenReady,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    runWhenReady(root.document, () => initialize(root.document));
  }
})(typeof globalThis === "undefined" ? this : globalThis);
