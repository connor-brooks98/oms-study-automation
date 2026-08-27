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

  const requestForm = async (documentRef, fetchImpl, url, form, method = "POST") => {
    const response = await fetchImpl(url, {
      method,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "X-CSRF-Token": csrfToken(documentRef),
      },
      ...(form ? { body: form } : {}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "Card image could not be saved.");
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

  const statusTone = (state) => {
    const normalized = String(state || "").toLowerCase();
    if (["complete", "completed", "ready", "configured", "connected", "skipped"].includes(normalized)) return "sh-pill--ok";
    if (["processing", "queued", "running", "pending", "in_progress", "preflight", "building_source_index", "building_lcl", "retrieving_pass_1", "judging_pass_1", "localizing_missed_concepts", "retrieving_pass_2", "judging_pass_2", "deduping", "generating_gaps", "applying_local", "syncing", "verifying", "envelope_pending"].includes(normalized)) return "sh-pill--info";
    if (["needs_review", "review", "ready_for_review"].includes(normalized)) return "sh-pill--warn";
    if (["failed", "quarantined"].includes(normalized)) return "sh-pill--err";
    return "";
  };

  const statusDotTone = (state) => statusTone(state).replace("pill", "dot");

  const displayTimestamp = (value) => {
    const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})/);
    if (!match) return String(value || "");
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${months[Number(match[2]) - 1]} ${Number(match[3])}, ${match[1]} · ${match[4]}`;
  };

  const candidateAudit = (candidate) => {
    const audit = candidate?.provenance?.audit;
    const cardCentric = candidate?.provenance?.card_centric_v2;
    if (!audit || typeof audit !== "object") {
      return {
        label: "Coverage assessment",
        reason: String(candidate?.reason || "No assessment available."),
        subject: "",
        support: "",
        structureIssues: [],
        fieldReviews: (Array.isArray(cardCentric?.field_reviews)
          ? cardCentric.field_reviews
          : []).map((review) => `${readableState(review.field)}: ${review.reason}`),
      };
    }
    return {
      label: `Audit: ${readableState(audit.verdict)}`,
      reason: String(audit.reason || candidate?.reason || ""),
      subject: String(audit.primary_subject || ""),
      support: readableState(audit.support),
      structureIssues: (Array.isArray(audit.structure_issue)
        ? audit.structure_issue
        : []).map(readableState),
      fieldReviews: [],
    };
  };

  const convergenceDisplay = (summary) => {
    if (!summary || typeof summary !== "object") {
      return { count: "—", label: "Convergence", warning: "" };
    }
    const total = Number(summary.concepts_total || 0);
    const converged = Number(summary.concepts_converged || 0);
    const passes = Number(summary.passes_run || 0);
    const manual = Array.isArray(summary.manual_review_concept_ids)
      ? summary.manual_review_concept_ids.length
      : 0;
    return {
      count: `${converged} / ${total}`,
      label: summary.needs_manual_review
        ? `Manual review · ${passes} passes`
        : `Converged · ${passes} passes`,
      warning: manual
        ? `${manual} ${manual === 1 ? "concept was" : "concepts were"} still growing after pass 5.`
        : "",
    };
  };

  const reconciliationDisplay = (summary) => {
    if (!summary || typeof summary !== "object") {
      return {
        checks: "—",
        dropRate: "—",
        warning: "",
        uncitedCount: 0,
      };
    }
    const passed = Array.isArray(summary.passed) ? summary.passed : [];
    const failed = Array.isArray(summary.failed) ? summary.failed : [];
    const warned = Array.isArray(summary.warned) ? summary.warned : [];
    const total = passed.length + failed.length + warned.length;
    const metrics = summary.metrics || {};
    const dropRate = Number(metrics.audit_drop_rate || 0);
    const uncited = Array.isArray(metrics.uncited_passage_ids)
      ? metrics.uncited_passage_ids
      : [];
    return {
      checks: `${passed.length} / ${total}`,
      dropRate: `${Math.round(dropRate * 100)}%`,
      warning: [...failed, ...warned]
        .map((item) => `${item.assertion_id}: ${item.message}`)
        .join(" · "),
      uncitedCount: uncited.length,
    };
  };

  const reviewSurfaceDisplay = (surface) => {
    const selection = surface?.selection;
    if (!selection || typeof selection !== "object") {
      return { visible: false, sizing: "", belowFloorWarning: "" };
    }
    const count = Number(selection.selected_count);
    const floor = Number(selection.warning_floor);
    const target = Number(selection.ordinary_target);
    const cap = Number(selection.soft_cap);
    const hasSizing = [floor, target, cap].every(Number.isFinite);
    return {
      visible: true,
      sizing: hasSizing
        ? `${Number.isFinite(count) ? `${count} selected. ` : ""}${floor} is a warning floor; ${target} is the ordinary target; ${cap} is a soft cap for quality-first exceptions.`
        : "Card counts remain quality-first guidance, not quotas.",
      belowFloorWarning: selection.below_warning_floor === true
        ? `Fewer than ${floor} cards were selected. This is allowed when fewer cards meet grounding and quality requirements; do not add weak, redundant, or ungrounded cards to reach a count.`
        : "",
      selectionMetadataState: selection.selection_metadata_state || "unavailable",
    };
  };

  const reviewSelectionDisplay = (surface) => {
    const selection = surface?.selection || {};
    const metadataState = selection.selection_metadata_state || "unavailable";
    const metadata = metadataState === "complete" && Array.isArray(selection.selection_metadata)
      ? selection.selection_metadata
      : [];
    const acknowledgement = selection.overflow_acknowledgement || {};
    return {
      metadataState,
      metadataNotice: metadataState === "complete"
        ? ""
        : "Current selection metadata is incomplete or unavailable; marginal and overflow reasons are pending reconstruction.",
      selectionReasons: metadata
        .filter((item) => Number(item.selected_position) >= 66)
        .map((item) => {
          const position = Number(item.selected_position);
          if (position <= 70) {
            return `Card ${position} (${item.identity}, ${item.tier}): marginal value — ${readableState(item.marginal_value_reason)}`;
          }
          return `Card ${position} (${item.identity}, ${item.tier}): mandatory ${item.mandatory === true ? "yes" : "no"}; overflow reason — ${item.overflow_reason || "not recorded"}; signed acknowledgement ${acknowledgement.signed === true ? "recorded" : "pending"}.`;
        }),
    };
  };

  const reviewSurfaceDetails = (surface) => ({
    evidenceQuality: Array.isArray(surface?.evidence_quality) ? surface.evidence_quality : [],
    duplicates: Array.isArray(surface?.duplicate_resolutions) ? surface.duplicate_resolutions : [],
    acknowledgement: surface?.selection?.overflow_acknowledgement || {},
  });

  const reviewSurfaceList = (documentRef, selector, heading, entries, format) => {
    const container = documentRef.querySelector(selector);
    if (!container) return;
    container.replaceChildren();
    if (!entries.length) return;
    container.append(element(documentRef, "strong", "", heading));
    const list = element(documentRef, "ul", "field-help");
    entries.forEach((entry) => list.append(element(documentRef, "li", "", format(entry))));
    container.append(list);
  };

  const renderReviewSurface = (documentRef, surface) => {
    const section = documentRef.querySelector("[data-review-quality]");
    if (!section) return;
    const display = reviewSurfaceDisplay(surface);
    section.hidden = !display.visible;
    if (!display.visible) return;
    const sizing = documentRef.querySelector("[data-review-sizing]");
    sizing.textContent = display.sizing;
    const belowFloor = documentRef.querySelector("[data-review-below-floor]");
    belowFloor.textContent = display.belowFloorWarning;
    belowFloor.hidden = !display.belowFloorWarning;

    const details = reviewSurfaceDetails(surface);
    reviewSurfaceList(
      documentRef,
      "[data-review-evidence-quality]",
      "Evidence quality",
      details.evidenceQuality,
      (item) => `${item.identity}: ${readableState(item.evidence_quality)}`,
    );

    const diagnostic = surface.s2b_diagnostic;
    const poorConcepts = Array.isArray(diagnostic?.evidence_poor_concept_ids)
      ? diagnostic.evidence_poor_concept_ids
      : [];
    const matched = diagnostic?.matched_slide_passage_ids;
    if (diagnostic && typeof diagnostic === "object") {
      const details = [];
      if (poorConcepts.length) details.push(`Evidence-poor concepts: ${poorConcepts.join(", ")}`);
      if (matched && typeof matched === "object") {
        Object.entries(matched).forEach(([conceptId, passageIds]) => {
          details.push(`${conceptId}: ${(Array.isArray(passageIds) ? passageIds : []).join(", ") || "no matched slide passages"}`);
        });
      }
      if (Number.isFinite(Number(diagnostic.threshold_chars))) {
        details.push(`Diagnostic threshold: ${diagnostic.threshold_chars} matched slide characters`);
      }
      if (Number.isFinite(Number(diagnostic.total_concepts))) {
        details.push(`Concepts audited: ${diagnostic.total_concepts}`);
      }
      reviewSurfaceList(
        documentRef,
        "[data-review-s2b-diagnostic]",
        "S2b evidence diagnostic (diagnostic only)",
        details,
        (value) => value,
      );
    } else {
      reviewSurfaceList(documentRef, "[data-review-s2b-diagnostic]", "", [], (value) => value);
    }

    const selectionDisplay = reviewSelectionDisplay(surface);
    reviewSurfaceList(
      documentRef,
      "[data-review-selection-reasons]",
      selectionDisplay.metadataNotice || "Selection reasons beyond the ordinary target",
      selectionDisplay.metadataNotice ? [selectionDisplay.metadataNotice] : selectionDisplay.selectionReasons,
      (value) => value,
    );

    reviewSurfaceList(
      documentRef,
      "[data-review-duplicates]",
      "Duplicate resolutions",
      details.duplicates,
      (item) => `${item.card_id || item.fact_id}: duplicate of ${item.duplicate_of_existing_note_id ? `existing note ${item.duplicate_of_existing_note_id}` : `generated card ${item.duplicate_of_generated_card_id}`}`,
    );
  };

  const renderV3ReviewSurface = (documentRef, surface) => {
    const v3 = surface?.v3;
    if (!v3 || typeof v3 !== "object") return;
    const host = documentRef.querySelector("[data-review-quality]");
    if (!host) return;
    host.hidden = false;
    const details = element(documentRef, "details", "anki-v3-review");
    details.open = true;
    details.append(element(documentRef, "summary", "", "V3 review evidence (approval only)"));
    [
      v3.reason,
      `Policy: ${v3.policy?.sha256 || "unavailable"}; enforcement ${v3.policy?.enforcement?.present === true ? "present" : "unavailable"}${v3.policy?.enforcement?.tier ? ` (${v3.policy.enforcement.tier})` : ""}`,
      `Scope: ${(v3.scope?.fact_ids || []).join(", ") || "none"}; ${v3.scope?.degraded_mode || "unavailable"}`,
      `Sources: ${(v3.scope?.sources || []).map((item) => item.source_id || item.evidence_id).join(", ") || "none"}`,
      `Retrieval: exact ${(v3.retrieval?.exact_only_fact_ids || []).join(", ") || "none"}; polluted ${(v3.retrieval?.polluted_fact_ids || []).join(", ") || "none"}`,
      `Evidence: grounding ${v3.evidence?.grounding || "unavailable"}; ${v3.evidence?.degraded_mode || "unavailable"}`,
      ...(v3.evidence?.generated_grounding || []).map((item) => `Grounding: ${item.card_id} / ${item.fact_id}; evidence ${(item.evidence_ids || []).join(", ") || "none"}`),
      `Classification: ${(v3.classification?.tiers || []).join(", ") || "none"}; escalations ${(v3.classification?.escalations || []).length}`,
      `Selected existing: ${(v3.selected_existing_note_ids || []).join(", ") || "none"}`,
      `Selected generated: ${(v3.selected_generated_card_ids || []).join(", ") || "none"}`,
      `Resolution: duplicates ${(v3.resolution?.duplicate_fact_ids || []).join(", ") || "none"}`,
      ...(v3.resolution?.unresolved || []).map((item) => `Unresolved: ${item.fact_id || "run"} / ${item.state || "unavailable"}; ${item.reason || "unavailable"}`),
      `Cost calls: ${(v3.cost?.calls || []).length}`,
      ...(v3.cost?.calls || []).map((item) => `Cost: ${item.stage || "unavailable"} / ${item.call_id || "unavailable"}; ${item.modality || "unavailable"} / ${item.model || "unavailable"}; predicted ${item.predicted?.microusd ?? "unavailable"}, reserved ${item.reserved?.microusd ?? "unavailable"}, observed ${item.observed?.microusd ?? "unavailable"} (${item.observed_estimated === true ? "estimated" : "reported"})`),
    ].filter(Boolean).forEach((value) => details.append(element(documentRef, "p", "field-help", value)));
    host.append(details);
  };

  const sourceLabel = (kind) => ({
    slides: "Lecture slides",
    transcripts: "Lecture transcript",
    summary: "NotebookLM outline",
  })[kind] || readableState(kind);

  const hasRequiredSources = (lecture) => {
    const kinds = new Set(
      (Array.isArray(lecture?.revisions) ? lecture.revisions : [])
        .map((revision) => revision.kind),
    );
    return kinds.has("slides")
      && kinds.has("transcripts")
      && Boolean(lecture?.outline)
      && lecture?.source_ready !== false;
  };

  const sourceStatuses = (lecture) => {
    if (!lecture) {
      return { slides: "neutral", transcripts: "neutral", summary: "neutral" };
    }
    const revisions = Array.isArray(lecture.revisions) ? lecture.revisions : [];
    const kinds = new Set(revisions.map((revision) => revision.kind));
    const status = lecture.source_status || {
      slides: kinds.has("slides"),
      transcripts: kinds.has("transcripts"),
      summary: Boolean(lecture.outline),
    };
    return {
      slides: status.slides ? "ready" : "missing",
      transcripts: status.transcripts ? "ready" : "missing",
      summary: status.summary ? "ready" : "missing",
    };
  };

  const addDeckPriority = (decks, deck) => {
    const value = String(deck || "").trim();
    if (!value || decks.some((item) => item.toLocaleLowerCase() === value.toLocaleLowerCase())) {
      return [...decks];
    }
    return [...decks, value];
  };

  const deckChoiceAvailable = (choice) => Boolean(String(choice?.value || "").trim());

  const moveDeckPriority = (decks, index, offset) => {
    const destination = index + offset;
    if (index < 0 || index >= decks.length || destination < 0 || destination >= decks.length) {
      return [...decks];
    }
    const moved = [...decks];
    [moved[index], moved[destination]] = [moved[destination], moved[index]];
    return moved;
  };

  const updateStartAvailability = (form) => {
    const submit = form?.querySelector("[type=submit]");
    if (!submit) return;
    const sourceReady = form.querySelector("#anki-source-revisions")?.dataset.ready === "true";
    const hasDeck = form.querySelectorAll("input[name=deck_allowlist]").length > 0;
    submit.disabled = submit.dataset.ankiEnabled !== "true"
      || submit.dataset.promptsReady !== "true"
      || !sourceReady
      || !hasDeck;
  };

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

  const courseOptions = (lectures) => [
    ...new Set(
      lectures
        .map((lecture) => String(lecture.subject || "").trim())
        .filter(Boolean),
    ),
  ].sort((left, right) => left.localeCompare(right));

  const examOptions = (lectures, course) => [
    ...new Set(
      lectures
        .filter((lecture) => lecture.subject === course)
        .map((lecture) => Number(lecture.exam_number))
        .filter((exam) => Number.isInteger(exam) && exam > 0),
    ),
  ].sort((left, right) => left - right);

  const lectureOptions = (lectures, course, examNumber) => {
    const selectedExam = Number(examNumber);
    if (!course || !Number.isInteger(selectedExam) || selectedExam < 1) {
      return [];
    }
    return lectures
      .filter(
        (lecture) => (
          lecture.subject === course
          && Number(lecture.exam_number) === selectedExam
        ),
      )
      .sort(
        (left, right) => (
          Number(left.lecture_number) - Number(right.lecture_number)
          || String(left.topic).localeCompare(String(right.topic))
          || Number(left.id) - Number(right.id)
        ),
      );
  };

  const resolveProviderModel = (providerModels, provider) => {
    if (!providerModels || typeof providerModels !== "object") return "";
    const model = providerModels[String(provider || "")];
    return typeof model === "string" ? model : "";
  };

  const providerLabel = (provider) => ({
    anthropic: "Anthropic",
    openai: "OpenAI",
    gemini: "Gemini",
    openrouter: "OpenRouter",
  })[String(provider || "")] || "provider";

  const modelOptionValues = (models, currentModel) => {
    const list = Array.isArray(models) ? models.map((model) => String(model)) : [];
    const unique = [...new Set(list)];
    const current = String(currentModel || "").trim();
    if (current && !unique.includes(current)) {
      return [current, ...unique];
    }
    return unique;
  };

  const populateModelOptions = (documentRef, select, models, currentModel) => {
    const values = modelOptionValues(models, currentModel);
    select.replaceChildren();
    values.forEach((value) => {
      const option = documentRef.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    });
    const current = String(currentModel || "").trim();
    if (current && values.includes(current)) {
      select.value = current;
    } else if (values.length) {
      select.value = values[0];
    }
  };

  const loadModelOptions = async (
    documentRef,
    fetchImpl,
    select,
    provider,
    currentModel,
  ) => {
    let models = [];
    const status = documentRef.querySelector?.("[data-anki-model-status]");
    select.disabled = true;
    if (status) status.textContent = `Loading ${providerLabel(provider)} models…`;
    try {
      const result = await requestJson(
        documentRef,
        fetchImpl,
        `/api/settings/providers/${provider}/models`,
      );
      models = Array.isArray(result.models) ? result.models : [];
    } catch {
      models = [];
    }
    populateModelOptions(documentRef, select, models, currentModel);
    select.disabled = false;
    if (status) {
      status.textContent = models.length
        ? `${models.length} ${providerLabel(provider)} models available.`
        : `No ${providerLabel(provider)} models are available. Check the provider credential in Settings.`;
    }
  };

  const collectSourceRevisionIds = (form) => [
    ...form.querySelectorAll("input[name=source_revision_ids]"),
  ].map((input) => Number(input.value));

  const renderSourceChoices = (documentRef, lecture) => {
    const container = documentRef.querySelector("#anki-source-revisions");
    if (!container) return;
    container.replaceChildren();
    const ready = hasRequiredSources(lecture);
    container.dataset.ready = ready ? "true" : "false";
    const revisions = Array.isArray(lecture?.revisions)
      ? lecture.revisions
      : [];
    const statuses = sourceStatuses(lecture);
    ["slides", "transcripts", "summary"].forEach((kind) => {
      const revision = revisions.find((item) => item.kind === kind);
      const outline = kind === "summary" ? lecture?.outline : null;
      const state = statuses[kind];
      const label = element(documentRef, "label", "anki-source-option");
      label.classList.add(`is-${state}`);
      if (revision && state === "ready") {
        const input = documentRef.createElement("input");
        input.type = "hidden";
        input.name = "source_revision_ids";
        input.value = revision.id;
        label.append(input);
      }
      const copy = element(documentRef, "span");
      let detail = "Choose a lecture";
      if (state === "missing") detail = "Not found or not ready";
      if (revision && state === "ready") {
        detail = `Current revision · ${String(revision.source_sha256).slice(0, 10)}`;
      }
      if (outline && state === "ready") {
        detail = `Current output · ${String(outline.sha256).slice(0, 10)}`;
      }
      copy.append(
        element(documentRef, "strong", "", sourceLabel(kind)),
        element(documentRef, "small", "", detail),
      );
      label.append(copy);
      container.append(label);
    });
    if (lecture && !ready) {
      container.append(element(
        documentRef,
        "p",
        "sh-empty sh-empty__text",
        "Curation unlocks when current slides, transcript, and NotebookLM outline are all available.",
      ));
    }
    updateStartAvailability(container.closest("form"));
  };

  const lectureDisplayLabel = (lectures, lectureId) => {
    const lecture = resolveLecture(lectures || [], lectureId);
    if (!lecture) return "Lecture unavailable";
    return `${lecture.subject} Lecture ${String(lecture.lecture_number).padStart(2, "0")}`;
  };

  const jobRow = (documentRef, job, lectures = []) => {
    const row = element(documentRef, "div", "anki-job-row sh-row");
    row.dataset.jobId = job.id;
    const link = element(documentRef, "a", "anki-job-link");
    link.href = `/anki/jobs/${job.id}`;
    const dot = element(
      documentRef,
      "span",
      `status-dot sh-dot ${statusDotTone(job.state)} status-${job.state}`,
    );
    const description = element(documentRef, "span");
    description.append(
      element(
        documentRef,
        "strong",
        "",
        job.lecture_label || lectureDisplayLabel(lectures, job.lecture_id),
      ),
      element(documentRef, "small", "", job.target_deck),
    );
    const state = element(
      documentRef,
      "span",
      `status-pill sh-pill ${statusTone(job.state)} status-${job.state}`,
      readableState(job.state),
    );
    const time = element(
      documentRef,
      "time",
      "sh-mono",
      displayTimestamp(job.updated_at),
    );
    link.append(dot, description, state, time);
    row.append(link);
    if (canRetryCuration(job)) {
      const actions = element(documentRef, "span", "anki-job-actions");
      actions.setAttribute("aria-label", "Interrupted run actions");
      const retry = element(documentRef, "button", "anki-icon-button sh-iconbtn");
      retry.type = "button";
      retry.dataset.retryQueuedJob = "";
      retry.setAttribute("aria-label", "Retry interrupted run");
      retry.title = "Retry interrupted run";
      retry.append(element(documentRef, "span", "", "↻"));
      retry.firstChild.setAttribute("aria-hidden", "true");
      actions.append(retry);
      if (job.state === "failed") {
        const remove = element(
          documentRef,
          "button",
          "anki-icon-button sh-iconbtn sh-btn--danger is-danger",
        );
        remove.type = "button";
        remove.dataset.removeFailedJob = "";
        remove.setAttribute("aria-label", "Remove failed run");
        remove.title = "Remove failed run";
        remove.append(element(documentRef, "span", "", "×"));
        remove.firstChild.setAttribute("aria-hidden", "true");
        actions.append(remove);
      }
      row.append(actions);
    }
    return row;
  };

  const runFailedJobAction = (
    documentRef,
    fetchImpl,
    jobId,
    action,
  ) => {
    if (action === "retry") {
      return requestJson(
        documentRef,
        fetchImpl,
        `/api/anki/jobs/${jobId}/retry`,
        { method: "POST" },
      );
    }
    if (action === "remove") {
      return requestJson(
        documentRef,
        fetchImpl,
        `/api/anki/jobs/${jobId}/remove`,
        { method: "POST" },
      );
    }
    throw new Error("Unsupported failed-run action.");
  };

  const refreshJobs = async (documentRef, fetchImpl, lectures = []) => {
    const container = documentRef.querySelector("#anki-job-list");
    if (!container) return;
    const payload = await requestJson(
      documentRef,
      fetchImpl,
      "/api/anki/jobs",
    );
    container.replaceChildren();
    if (!payload.jobs.length) {
      const empty = element(documentRef, "div", "sh-empty anki-empty-compact");
      empty.append(
        element(documentRef, "h2", "sh-empty__title", "No curation runs yet"),
        element(
          documentRef,
          "p",
          "sh-empty__text",
          "Your first run will appear here and remain resumable if Study Hub restarts.",
        ),
      );
      container.append(empty);
      return;
    }
    payload.jobs.forEach((job) => container.append(jobRow(documentRef, job, lectures)));
  };

  const initializeHome = (documentRef, fetchImpl) => {
    const form = documentRef.querySelector("#anki-create-form");
    if (!form) return;
    const lectureId = form.elements.lecture_id;
    const targetTag = form.elements.target_tag;
    const targetDeck = form.elements.target_deck;
    const course = form.elements.course;
    const exam = form.elements.exam_number;
    const lectureSelect = form.elements.lecture_path_id;
    const provider = form.elements.provider;
    const model = form.elements.model;
    const deckChoice = form.querySelector("[data-deck-choice]");
    const addDeckButton = form.querySelector("[data-add-deck]");
    const deckList = form.querySelector("[data-deck-priority]");
    const deckInputs = form.querySelector("[data-deck-inputs]");
    const selectedLabel = documentRef.querySelector(
      "[data-selected-lecture]",
    );
    let lectures;
    let providerModels;
    try {
      lectures = parseLecturePayload(
        documentRef.querySelector("#anki-lecture-data")?.textContent,
      );
      providerModels = JSON.parse(
        documentRef.querySelector("#anki-provider-models")?.textContent
          || "{}",
      );
    } catch {
      lectures = [];
      providerModels = {};
      course.disabled = true;
      exam.disabled = true;
      lectureSelect.disabled = true;
      documentRef.querySelector("#anki-source-revisions").replaceChildren(
        element(
          documentRef,
          "p",
          "quiet-state",
          "Study Hub could not load the lecture source catalog. Reload the page.",
        ),
      );
    }

    const replaceOptions = (select, placeholder, options) => {
      select.replaceChildren();
      const empty = documentRef.createElement("option");
      empty.value = "";
      empty.textContent = placeholder;
      select.append(empty);
      options.forEach(({ value, label }) => {
        const option = documentRef.createElement("option");
        option.value = String(value);
        option.textContent = label;
        select.append(option);
      });
    };
    const clearLecture = () => {
      lectureId.value = "";
      targetTag.value = "";
      targetDeck.value = "";
      if (selectedLabel) {
        selectedLabel.textContent = "Choose a course, exam, and lecture.";
      }
      renderSourceChoices(documentRef, null);
    };
    replaceOptions(
      course,
      "Choose a course…",
      courseOptions(lectures).map((value) => ({ value, label: value })),
    );
    course.addEventListener("change", () => {
      clearLecture();
      replaceOptions(lectureSelect, "Choose an exam first", []);
      lectureSelect.disabled = true;
      const exams = examOptions(lectures, course.value);
      replaceOptions(
        exam,
        course.value ? "Choose an exam…" : "Choose a course first",
        exams.map((value) => ({ value, label: `Exam ${value}` })),
      );
      exam.disabled = !course.value || exams.length === 0;
    });
    exam.addEventListener("change", () => {
      clearLecture();
      const scopedLectures = lectureOptions(
        lectures,
        course.value,
        exam.value,
      );
      replaceOptions(
        lectureSelect,
        exam.value ? "Choose a lecture…" : "Choose an exam first",
        scopedLectures.map((lecture) => ({
          value: lecture.id,
          label: `Lecture ${String(lecture.lecture_number).padStart(2, "0")} — ${lecture.topic}`,
        })),
      );
      lectureSelect.disabled = !exam.value || scopedLectures.length === 0;
    });
    lectureSelect.addEventListener("change", () => {
      clearLecture();
      const lecture = lectureOptions(
        lectures,
        course.value,
        exam.value,
      ).find((item) => Number(item.id) === Number(lectureSelect.value));
      if (!lecture) return;
      lectureId.value = String(lecture.id);
      targetDeck.value = String(lecture.target_deck || "");
      targetTag.value = String(lecture.target_tag || "");
      if (selectedLabel) {
        selectedLabel.textContent = (
          `${lecture.subject} Lecture ${String(lecture.lecture_number).padStart(2, "0")} · `
          + `Exam ${lecture.exam_number} — ${lecture.topic}`
        );
      }
      renderSourceChoices(documentRef, lecture);
    });

    let deckPriorities = [];
    const renderDeckPriorities = () => {
      if (!deckList || !deckInputs) return;
      deckList.replaceChildren();
      deckInputs.replaceChildren();
      deckPriorities.forEach((deck, index) => {
        const row = element(documentRef, "li", "anki-deck-priority-item");
        row.append(element(documentRef, "span", "", `${index + 1}. ${deck}`));
        [["↑", -1], ["↓", 1]].forEach(([label, offset]) => {
          const button = element(documentRef, "button", "button secondary anki-compact-button", label);
          button.type = "button";
          button.disabled = index + offset < 0 || index + offset >= deckPriorities.length;
          button.addEventListener("click", () => {
            deckPriorities = moveDeckPriority(deckPriorities, index, offset);
            renderDeckPriorities();
          });
          row.append(button);
        });
        const remove = element(documentRef, "button", "button secondary anki-compact-button", "Remove");
        remove.type = "button";
        remove.addEventListener("click", () => {
          deckPriorities = deckPriorities.filter((_, itemIndex) => itemIndex !== index);
          renderDeckPriorities();
        });
        row.append(remove);
        deckList.append(row);
        const input = documentRef.createElement("input");
        input.type = "hidden";
        input.name = "deck_allowlist";
        input.value = deck;
        deckInputs.append(input);
      });
      updateStartAvailability(form);
    };
    const updateDeckAddAvailability = () => {
      if (addDeckButton) addDeckButton.disabled = !deckChoiceAvailable(deckChoice);
    };
    addDeckButton?.addEventListener("click", () => {
      deckPriorities = addDeckPriority(deckPriorities, deckChoice?.value);
      if (deckChoice) deckChoice.value = "";
      updateDeckAddAvailability();
      renderDeckPriorities();
    });
    deckChoice?.addEventListener("change", updateDeckAddAvailability);
    updateDeckAddAvailability();
    if (deckChoice) {
      const anking = [...deckChoice.options].find((option) => option.value === "AnKing Step Deck");
      if (anking) deckPriorities = addDeckPriority(deckPriorities, anking.value);
    }
    renderDeckPriorities();
    provider.addEventListener("change", () => {
      const savedModel = resolveProviderModel(
        providerModels,
        provider.value,
      );
      void loadModelOptions(
        documentRef,
        fetchImpl,
        model,
        provider.value,
        savedModel,
      );
    });
    void loadModelOptions(
      documentRef,
      fetchImpl,
      model,
      provider.value,
      model.value,
    );
    const profile = form.querySelector("[data-curation-profile]");
    const stagePanel = form.querySelector("[data-curation-stage-models]");
    const stageInputs = {
      s2: form.querySelector("[name=s2_model]"),
      s4: form.querySelector("[name=s4_model]"),
      s6: form.querySelector("[name=s6_model]"),
      s7: form.querySelector("[name=s7_model]"),
    };
    const updateProfile = () => {
      const selected = String(profile?.value || "balanced");
      const current = String(model.value || "").trim();
      Object.values(stageInputs).forEach((input) => {
        if (input && !input.value) input.value = current;
      });
      if (stagePanel) stagePanel.hidden = selected !== "custom";
      const estimate = form.querySelector("[data-curation-estimate]");
      if (estimate) estimate.textContent = selected === "max_quality"
        ? "Max quality: strong model at every LLM stage."
        : selected === "fast_cheap"
          ? "Fast / cheap: only fixture-passing S4/S6 models may be used as defaults."
          : selected === "custom"
            ? "Custom: S6 follows S4 unless explicitly unlocked by a validated configuration."
            : "Balanced profile. S6 matches classify; S4/S6 use non-thinking mode.";
    };
    try {
      const saved = JSON.parse(
        documentRef.querySelector("#anki-profile-default")?.textContent || "null",
      );
      if (saved && typeof saved === "object") {
        const profileName = String(saved.profile || "custom");
        if ([...profile.options].some((option) => option.value === profileName)) {
          profile.value = profileName;
        } else {
          profile.value = "custom";
        }
        [
          ["ledger_s2", stageInputs.s2],
          ["classify_s4", stageInputs.s4],
          ["residual_s6", stageInputs.s6],
          ["gap_fill_s7", stageInputs.s7],
        ].forEach(([key, input]) => {
          if (input && saved[key] && typeof saved[key].model === "string") {
            input.value = saved[key].model;
          }
        });
      }
    } catch {
      // An unavailable local default must never block a new curation run.
    }
    profile?.addEventListener("change", updateProfile);
    model.addEventListener("change", updateProfile);
    form.querySelector("[data-run-fixture]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const result = form.querySelector("[data-fixture-result]");
      button.disabled = true;
      if (result) result.textContent = "Running immutable Lecture07 fixture…";
      try {
        const value = await requestJson(documentRef, fetchImpl, "/api/anki/fixture-validation", {
          method: "POST",
          body: JSON.stringify({ provider: String(provider.value), model: String(stageInputs.s4?.value || model.value) }),
        });
        if (result) result.textContent = value.passed ? "Fixture validated ✓" : "Fixture failed — S4/S6 remain unavailable.";
      } catch (error) {
        if (result) result.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    updateProfile();
    clearLecture();

    const refresh = documentRef.querySelector("[data-refresh-jobs]");
    refresh?.addEventListener("click", async () => {
      refresh.disabled = true;
      const message = documentRef.querySelector("#anki-jobs-message");
      try {
        await refreshJobs(documentRef, fetchImpl, lectures);
        if (message) message.textContent = "";
      } catch (error) {
        if (message) message.textContent = error.message;
      } finally {
        refresh.disabled = false;
      }
    });

    const jobList = documentRef.querySelector("#anki-job-list");
    jobList?.addEventListener("click", async (event) => {
      const button = event.target.closest?.(
        "[data-retry-queued-job], [data-remove-failed-job]",
      );
      if (!button) return;
      const row = button.closest("[data-job-id]");
      if (!row) return;
      const removing = button.hasAttribute("data-remove-failed-job");
      if (
        removing
        && root.confirm
        && !root.confirm("Remove this failed curation run from the queue?")
      ) return;
      const message = documentRef.querySelector("#anki-jobs-message");
      button.disabled = true;
      if (message) {
        message.textContent = removing
          ? "Removing the failed run…"
          : "Retrying the interrupted stage…";
      }
      try {
        await runFailedJobAction(
          documentRef,
          fetchImpl,
          row.dataset.jobId,
          removing ? "remove" : "retry",
        );
        await refreshJobs(documentRef, fetchImpl, lectures);
        if (message) {
          message.textContent = removing
            ? "Failed run removed."
            : "Curation run returned to the queue.";
        }
      } catch (error) {
        if (message) message.textContent = error.message;
        button.disabled = false;
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
      const selectedSources = collectSourceRevisionIds(form);
      if (
        documentRef.querySelector("#anki-source-revisions")?.dataset.ready
        !== "true"
      ) {
        message.textContent = (
          "Current slides, transcript, and NotebookLM outline are required."
        );
        return;
      }
      const body = {
        contract_version: 1,
        lecture_id: Number(values.get("lecture_id")),
        block_id: null,
        source_revision_ids: selectedSources,
        deck_allowlist: values.getAll("deck_allowlist").map((value) => String(value).trim()).filter(Boolean),
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
        pipeline_contract_version: "card_centric_v2",
      };
      const selectedProfile = String(values.get("curation_profile") || "balanced");
      const selectedProvider = body.provider;
      const selectedModel = body.model;
      body.resolved_model_config = {
        profile: selectedProfile,
        ledger_s2: { provider: selectedProvider, model: String(values.get("s2_model") || selectedModel), thinking_mode: "default" },
        classify_s4: { provider: selectedProvider, model: String(values.get("s4_model") || selectedModel), thinking_mode: "disabled" },
        residual_s6: { provider: selectedProvider, model: String(values.get("s6_model") || selectedModel), thinking_mode: "disabled" },
        gap_fill_s7: { provider: selectedProvider, model: String(values.get("s7_model") || selectedModel), thinking_mode: "default" },
        fast_classify_s4b: { provider: "openai", model: "gpt-4o-mini", thinking_mode: "disabled" },
        residual_unlocked: false,
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
    const empty = element(documentRef, "div", "anki-group-empty sh-empty");
    empty.append(element(documentRef, "p", "sh-empty__text", message));
    return empty;
  };

  const tagEditor = (documentRef, candidate, policy) => {
    const wrapper = documentRef.createElement("details");
    wrapper.className = "anki-tag-editor";
    const disclosure = documentRef.createElement("summary");
    disclosure.textContent = "Tags";
    wrapper.append(disclosure);
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
    const card = element(documentRef, "article", "anki-match-card anki-studio-card");
    card.dataset.noteId = candidate.note_id;
    card.dataset.reviewKind = "existing";
    const tags = (candidate.note?.tags || []).map((tag) => tag.value || tag);
    card.dataset.searchText = [
      candidate.note_id,
      candidate.note?.text,
      candidate.note?.extra,
      ...tags,
    ].filter(Boolean).join(" ").toLocaleLowerCase();
    const header = element(documentRef, "div", "anki-card-choice");
    const label = documentRef.createElement("label");
    label.className = "anki-select-control sh-check";
    const checkbox = documentRef.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = candidate.selected;
    checkbox.dataset.candidateSelection = "";
    checkbox.value = candidate.note_id;
    label.append(
      checkbox,
      element(documentRef, "span", "", "Include in deck"),
    );
    const audit = candidateAudit(candidate);
    const score = element(
      documentRef,
      "span",
      "anki-confidence",
      audit.label,
    );
    const recovered = candidate.retrieval_pass === "pass_2_rescue"
      || candidate.provenance?.retrieval_pass === "pass_2_rescue";
    const origin = element(
      documentRef,
      "span",
      "status-pill sh-pill sh-pill--info",
      recovered ? "Recovered" : "Initial match",
    );
    header.append(label, origin, score);

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
    summary.textContent = candidate?.provenance?.audit
      ? "Audit verdict"
      : "Coverage assessment";
    const reason = element(documentRef, "p", "", audit.reason);
    const facts = element(documentRef, "dl", "anki-score-list");
    Object.entries({
      ...(audit.subject ? { primary_subject: audit.subject } : {}),
      ...(audit.support ? { source_support: audit.support } : {}),
      ...(audit.structureIssues.length
        ? { structure_issues: audit.structureIssues.join(", ") }
        : {}),
      ...(audit.fieldReviews.length
        ? { field_review: audit.fieldReviews.join(" · ") }
        : {}),
    }).forEach(([name, value]) => {
      const row = element(documentRef, "div");
      row.append(
        element(documentRef, "dt", "", readableState(name)),
        element(documentRef, "dd", "", value),
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

  const cardMediaUrl = (documentRef, cardId, media) => media.preview_url || (
    `/api/anki/jobs/${documentRef.querySelector("[data-anki-review]")?.dataset.jobId}`
      + `/gap-cards/${cardId}/media/${encodeURIComponent(media.filename)}`
  );

  const renderCardMedia = (documentRef, article, media = []) => {
    article.querySelectorAll("[data-card-media-gallery]").forEach((gallery) => {
      const removable = gallery.dataset.removable === "true";
      gallery.replaceChildren();
      gallery.hidden = !media.length;
      media.forEach((item) => {
        const figure = element(documentRef, "figure", "anki-card-media-item");
        const image = documentRef.createElement("img");
        image.src = cardMediaUrl(documentRef, article.dataset.cardId, item);
        image.alt = item.original_filename || "Card back image";
        image.loading = "lazy";
        figure.append(image);
        if (removable) {
          const remove = element(documentRef, "button", "anki-card-media-remove", "Remove");
          remove.type = "button";
          remove.dataset.removeCardMedia = item.filename;
          remove.setAttribute("aria-label", `Remove ${image.alt}`);
          figure.append(remove);
        }
        gallery.append(figure);
      });
    });
  };

  const generatedCard = (documentRef, card) => {
    const article = element(documentRef, "article", "anki-generated-card anki-studio-card");
    article.dataset.cardId = card.card_id;
    article.dataset.conceptId = card.concept_id;
    article.dataset.reviewKind = "generated";
    article.dataset.validationState = String(card.validation_state || "valid");
    article.dataset.overlap = String(
      card.validation_state === "overlap"
        || String(card.validation_state || "").startsWith("duplicate_"),
    );
    article.dataset.searchText = [card.card_id, card.text, card.extra]
      .filter(Boolean).join(" ").toLocaleLowerCase();
    const header = element(documentRef, "div", "anki-card-choice");
    const label = documentRef.createElement("label");
    label.className = "anki-select-control sh-check";
    const checkbox = documentRef.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = card.selected;
    checkbox.dataset.gapSelection = "";
    label.append(checkbox, element(documentRef, "span", "", "Include in deck"));
    const validation = element(
      documentRef,
      "span",
      `anki-validation is-${card.validation_state}`,
      readableState(card.validation_state),
    );
    const editButton = element(documentRef, "button", "anki-card-edit-button", "Edit");
    editButton.type = "button";
    editButton.setAttribute("aria-expanded", "false");
    const actions = element(documentRef, "div", "anki-card-actions");
    actions.append(validation, editButton);
    header.append(label, actions);

    const preview = element(documentRef, "div", "anki-card-preview");
    const previewQuestion = element(documentRef, "div", "anki-card-preview-row");
    const previewAnswer = element(documentRef, "div", "anki-card-preview-row");
    const previewQuestionText = element(documentRef, "p", "", card.text);
    const previewAnswerText = element(documentRef, "p", "", card.extra);
    const previewAnswerBody = element(documentRef, "div", "anki-card-answer");
    const previewMedia = element(documentRef, "div", "anki-card-media-gallery");
    previewMedia.dataset.cardMediaGallery = "";
    previewMedia.dataset.removable = "false";
    previewAnswerBody.append(previewAnswerText, previewMedia);
    previewQuestion.append(
      element(documentRef, "strong", "", "Q"),
      previewQuestionText,
    );
    previewAnswer.append(
      element(documentRef, "strong", "", "A"),
      previewAnswerBody,
    );
    preview.append(previewQuestion, previewAnswer);

    const editor = element(documentRef, "div", "anki-card-editor");
    editor.hidden = true;

    const textLabel = element(documentRef, "label", "anki-card-field");
    textLabel.append(element(documentRef, "span", "", "Question / cloze"));
    const text = documentRef.createElement("textarea");
    text.rows = 3;
    text.value = card.text;
    text.dataset.gapText = "";
    textLabel.append(text);

    const extraLabel = element(documentRef, "label", "anki-card-field");
    extraLabel.append(element(documentRef, "span", "", "Answer / extra"));
    const extra = documentRef.createElement("textarea");
    extra.rows = 3;
    extra.value = card.extra;
    extra.dataset.gapExtra = "";
    extraLabel.append(extra);
    const mediaEditor = element(documentRef, "div", "anki-card-media-editor");
    mediaEditor.append(
      element(documentRef, "strong", "", "Images on card back"),
      element(documentRef, "p", "", "Drop PNG, JPEG, or WebP images here. Up to 8 images, 10 MB each."),
    );
    const dropZone = documentRef.createElement("label");
    dropZone.className = "anki-card-media-dropzone";
    dropZone.dataset.cardMediaDropzone = "";
    dropZone.tabIndex = 0;
    dropZone.append(
      element(documentRef, "span", "", "Drop images here or choose files"),
    );
    const fileInput = documentRef.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/png,image/jpeg,image/webp";
    fileInput.multiple = true;
    fileInput.dataset.cardMediaInput = "";
    dropZone.append(fileInput);
    const editorMedia = element(documentRef, "div", "anki-card-media-gallery");
    editorMedia.dataset.cardMediaGallery = "";
    editorMedia.dataset.removable = "true";
    mediaEditor.append(dropZone, editorMedia);
    editor.append(textLabel, extraLabel, mediaEditor);

    editButton.addEventListener("click", () => {
      const editing = editor.hidden;
      editor.hidden = !editing;
      preview.hidden = editing;
      editButton.textContent = editing ? "Done" : "Edit";
      editButton.setAttribute("aria-expanded", String(editing));
      if (editing) text.focus?.();
    });
    text.addEventListener("input", () => {
      previewQuestionText.textContent = text.value;
    });
    extra.addEventListener("input", () => {
      previewAnswerText.textContent = extra.value;
    });

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
    article.append(header, preview, editor, citations);
    renderCardMedia(documentRef, article, card.media || []);
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
      element(documentRef, "span", `status-pill sh-pill ${statusTone(item.status)}`, readableState(item.status)),
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

  const matchesReviewSearch = (candidate, query) => {
    const needle = String(query || "").trim().toLocaleLowerCase();
    if (!needle) return true;
    const tags = (candidate.note?.tags || []).map((tag) => tag.value || tag);
    return [
      candidate.note_id,
      candidate.note?.text,
      candidate.note?.extra,
      ...tags,
    ].filter(Boolean).join(" ").toLocaleLowerCase().includes(needle);
  };

  const reviewViews = (review) => {
    const existing = [
      ...review.groups.pass_1_matches,
      ...review.groups.recovered_in_pass_2,
    ];
    return {
      active: "final",
      final: {
        existing: existing.filter((candidate) => candidate.selected),
        generated: review.groups.generated_cards.filter((card) => card.selected),
      },
      candidates: existing,
    };
  };

  const reviewWorkspaceCounts = (review) => {
    const groups = review?.groups || {};
    const existing = [
      ...(groups.pass_1_matches || []),
      ...(groups.recovered_in_pass_2 || []),
    ];
    const generated = groups.generated_cards || [];
    const selectedExisting = existing.filter((candidate) => candidate.selected).length;
    const selectedGenerated = generated.filter((card) => card.selected).length;
    const duplicateResolutions = review?.review_surface?.duplicate_resolutions || [];
    const overlapCards = generated.filter((card) => (
      card.validation_state === "overlap"
      || String(card.validation_state || "").startsWith("duplicate_")
    )).length;
    return {
      candidates: existing.length,
      selectedExisting,
      generated: generated.length,
      selectedGenerated,
      final: selectedExisting + selectedGenerated,
      excludedExisting: existing.length - selectedExisting,
      overlaps: Math.max(overlapCards, duplicateResolutions.length),
      gaps: (groups.unresolved || []).length,
      duplicateResolutions: duplicateResolutions.length,
    };
  };

  const setReviewView = (documentRef, view) => {
    const page = documentRef.querySelector("[data-anki-review]");
    if (!page) return;
    page.dataset.reviewView = view;
    documentRef.querySelectorAll("[data-review-view]").forEach((button) => {
      const active = button.dataset.reviewView === view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    documentRef.querySelectorAll("[data-workbench-view]").forEach((button) => {
      const active = button.dataset.workbenchView === view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    applyReviewFilters(documentRef);
  };

  const reviewCardVisible = ({ kind, view, selected, overlap, searchMatch }) => {
    if (!searchMatch) return false;
    if (kind === "existing") {
      return view === "candidates" || (view === "final" && selected);
    }
    return (view === "final" && selected)
      || view === "generated"
      || (view === "overlaps" && overlap);
  };

  const applyReviewFilters = (documentRef) => {
    const rootElement = documentRef.querySelector("[data-anki-review]");
    if (!rootElement) return;
    const view = rootElement.dataset.reviewView || "final";
    const query = String(documentRef.querySelector("[data-review-search]")?.value || "")
      .trim().toLocaleLowerCase();
    let selectedExisting = 0;
    let visibleExisting = 0;
    documentRef.querySelectorAll('[data-review-kind="existing"]').forEach((card) => {
      const selected = Boolean(card.querySelector("[data-candidate-selection]")?.checked);
      if (selected) selectedExisting += 1;
      const visible = reviewCardVisible({
        kind: "existing",
        view,
        selected,
        overlap: false,
        searchMatch: !query || card.dataset.searchText.includes(query),
      });
      card.hidden = !visible;
      if (visible) visibleExisting += 1;
    });
    let selectedGenerated = 0;
    let visibleGenerated = 0;
    documentRef.querySelectorAll('[data-review-kind="generated"]').forEach((card) => {
      const selected = Boolean(card.querySelector("[data-gap-selection]")?.checked);
      if (selected) selectedGenerated += 1;
      const visible = reviewCardVisible({
        kind: "generated",
        view,
        selected,
        overlap: card.dataset.overlap === "true",
        searchMatch: !query || card.dataset.searchText.includes(query),
      });
      card.hidden = !visible;
      if (visible) visibleGenerated += 1;
    });
    const candidateCount = documentRef.querySelectorAll('[data-review-kind="existing"]').length;
    const finalCount = selectedExisting + selectedGenerated;
    const finalNode = documentRef.querySelector("[data-count-final]");
    const candidateNode = documentRef.querySelector("[data-count-candidates]");
    if (finalNode) finalNode.textContent = finalCount;
    if (candidateNode) candidateNode.textContent = candidateCount;
    [
      ["[data-flow-existing]", selectedExisting],
      ["[data-flow-generated]", selectedGenerated],
      ["[data-flow-final]", finalCount],
      ["[data-tool-final]", finalCount],
      ["[data-deck-count]", finalCount],
    ].forEach(([selector, value]) => {
      const node = documentRef.querySelector(selector);
      if (node) node.textContent = value;
    });
    const existingSection = documentRef.querySelector("[data-review-existing]");
    if (existingSection) existingSection.hidden = visibleExisting === 0;
    const generatedSection = documentRef.querySelector("[data-review-generated]");
    if (generatedSection) generatedSection.hidden = visibleGenerated === 0;
    const unresolved = documentRef.querySelector("[data-review-unresolved]");
    if (unresolved) {
      unresolved.hidden = !["candidates", "gaps"].includes(view);
      unresolved.open = view === "gaps";
    }
    const diagnostics = documentRef.querySelector("[data-review-quality]");
    if (diagnostics && view === "overlaps") diagnostics.open = true;
    documentRef.querySelector("[data-existing-heading]").textContent = view === "final"
      ? "Existing notes to retag"
      : "Candidates";
    documentRef.querySelector("[data-existing-explainer]").textContent = view === "final"
      ? "Final selection"
      : "Initial + recovered";
    documentRef.querySelector("[data-existing-help]").textContent = view === "final"
      ? "Only cards currently proposed to receive the lecture tag are shown."
      : "This combines initial matches and missed-topic recovery. Change a selection to update Final immediately.";
  };

  const renderConceptGroups = (documentRef, concepts) => {
    const container = documentRef.querySelector("[data-group-concepts]");
    if (!container) return;
    container.replaceChildren();
    const items = Array.isArray(concepts) ? concepts : [];
    if (!items.length) {
      container.append(emptyGroup(documentRef, "No concept checklist is available."));
      return;
    }
    items.forEach((concept) => {
      const details = element(documentRef, "details", "anki-concept-review");
      const summary = element(documentRef, "summary", "", String(concept.concept_id || "Concept"));
      const counts = [
        ["YES", concept.yes],
        ["MAYBE", concept.maybe],
        ["Flagged", concept.flagged],
        ["Generated", concept.generated],
      ].map(([label, values]) => `${label}: ${Array.isArray(values) ? values.length : 0}`).join(" · ");
      summary.append(` — ${counts}`);
      details.append(summary);
      [
        ["YES", concept.yes, "note_id"],
        ["MAYBE", concept.maybe, "note_id"],
        ["Flagged", concept.flagged, "note_id"],
        ["Generated", concept.generated, "card_id"],
      ].forEach(([label, values, idKey]) => {
        if (!Array.isArray(values) || !values.length) return;
        const line = element(documentRef, "p", "field-help");
        line.textContent = `${label}: ${values.map((value) => value[idKey]).join(", ")}`;
        details.append(line);
      });
      container.append(details);
    });
  };

  const renderReview = (documentRef, review) => {
    const groups = review.groups;
    const workspace = reviewWorkspaceCounts(review);
    const convergence = convergenceDisplay(review.convergence);
    const reconciliation = reconciliationDisplay(review.reconciliation);
    renderConceptGroups(documentRef, review.concepts);
    renderReviewSurface(documentRef, review.review_surface || {});
    renderV3ReviewSurface(documentRef, review.review_surface || {});
    documentRef.querySelector("[data-count-convergence]").textContent =
      convergence.count;
    documentRef.querySelector("[data-label-convergence]").textContent =
      convergence.label;
    const convergenceWarning = documentRef.querySelector(
      "[data-convergence-warning]",
    );
    convergenceWarning.textContent = convergence.warning;
    convergenceWarning.hidden = !convergence.warning;
    documentRef.querySelector("[data-count-assertions]").textContent =
      reconciliation.checks;
    documentRef.querySelector("[data-count-audit-drop]").textContent =
      reconciliation.dropRate;
    documentRef.querySelector("[data-count-uncited]").textContent =
      reconciliation.uncitedCount;
    const reconciliationWarning = documentRef.querySelector(
      "[data-reconciliation-warning]",
    );
    reconciliationWarning.textContent = reconciliation.warning;
    reconciliationWarning.hidden = !reconciliation.warning;
    documentRef.querySelector("[data-count-pass1]").textContent =
      groups.pass_1_matches.length;
    documentRef.querySelector("[data-count-pass2]").textContent =
      groups.recovered_in_pass_2.length;
    documentRef.querySelector("[data-count-generated]").textContent =
      groups.generated_cards.length;
    documentRef.querySelector("[data-count-unresolved]").textContent =
      groups.unresolved.length;
    documentRef.querySelector("[data-count-unresolved-details]").textContent =
      groups.unresolved.length;
    [
      ["[data-flow-candidates]", workspace.candidates],
      ["[data-flow-existing]", workspace.selectedExisting],
      ["[data-flow-generated]", workspace.selectedGenerated],
      ["[data-flow-final]", workspace.final],
      ["[data-tool-final]", workspace.final],
      ["[data-deck-count]", workspace.final],
      ["[data-tool-candidates]", workspace.candidates],
      ["[data-tool-generated]", workspace.generated],
      ["[data-tool-overlaps]", workspace.overlaps],
      ["[data-tool-gaps]", workspace.gaps],
    ].forEach(([selector, value]) => {
      const node = documentRef.querySelector(selector);
      if (node) node.textContent = value;
    });
    const flowNote = documentRef.querySelector("[data-flow-note]");
    if (flowNote) {
      flowNote.textContent = `${workspace.excludedExisting} existing candidates were not selected. ${workspace.duplicateResolutions} generated ${workspace.duplicateResolutions === 1 ? "card was" : "cards were"} resolved as duplicates. Open Selection audit for the recorded reasons.`;
    }
    const diagnosticCount = documentRef.querySelector("[data-diagnostic-count]");
    if (diagnosticCount) {
      diagnosticCount.textContent = `${workspace.duplicateResolutions} duplicate ${workspace.duplicateResolutions === 1 ? "resolution" : "resolutions"}`;
    }
    setGroup(
      documentRef,
      "[data-group-existing]",
      [...groups.pass_1_matches, ...groups.recovered_in_pass_2],
      (candidate) => candidateCard(documentRef, candidate, review.tag_policy),
      "No existing-card candidates were retained.",
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
          + "[data-gap-text], [data-gap-extra], [data-tag-editor], "
          + "[data-card-media-input]",
      )
      .forEach((control) => {
        control.disabled = control.disabled || !editable;
      });
    documentRef.querySelector("[data-save-review]").disabled = !editable;
    documentRef.querySelector("[data-build-envelope]").disabled =
      !review.can_build_envelope;
    documentRef.querySelector("#anki-processing").hidden = true;
    documentRef.querySelector("#anki-review-content").hidden = false;
    const page = documentRef.querySelector("[data-anki-review]");
    page.dataset.reviewView = "final";
    documentRef.querySelectorAll(
      "[data-candidate-selection], [data-gap-selection]",
    ).forEach((input) => {
      input.addEventListener("change", () => applyReviewFilters(documentRef));
    });
    applyReviewFilters(documentRef);
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
    "converging_pass_3",
    "converging_pass_4",
    "converging_pass_5",
    "auditing_candidates",
    "recomputing_coverage",
    "deduping",
    "generating_gaps",
    "reconciling",
    "ready_for_review",
  ];

  const processingPercent = (state) => {
    const index = Math.max(stageOrder.indexOf(state), 0);
    return Math.max(
      6,
      Math.round(((index + 1) / stageOrder.length) * 100),
    );
  };

  const canRetryCuration = (job) => job.state === "failed" || job.can_retry === true;

  const renderProcessing = (documentRef, job) => {
    const percent = processingPercent(job.state);
    documentRef.querySelector("#anki-job-state").textContent =
      readableState(job.state);
    documentRef.querySelector("#anki-job-state").className =
      `status-pill sh-pill ${statusTone(job.state)} status-${job.state}`;
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
    const retry = documentRef.querySelector("[data-retry-job]");
    if (retry) retry.hidden = !canRetryCuration(job);
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
      card_id: card.dataset.cardId,
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
    let dialogConfirmed = false;
    const openApplyDialog = () => {
      dialog.showModal();
      (root.requestAnimationFrame || ((callback) => callback()))(() => dialog.classList.add("is-open"));
    };

    const uploadCardMedia = async (article, files) => {
      const accepted = [...files];
      if (!accepted.length) return;
      message.textContent = `Uploading ${accepted.length} card ${accepted.length === 1 ? "image" : "images"}…`;
      for (const file of accepted) {
        const form = new root.FormData();
        form.append("file", file);
        const payload = await requestForm(
          documentRef,
          fetchImpl,
          `/api/anki/jobs/${jobId}/gap-cards/${article.dataset.cardId}/media`,
          form,
        );
        if (Number.isInteger(payload.revision)) revision = payload.revision;
        renderCardMedia(documentRef, article, payload.media || []);
      }
      message.textContent = "Card-back images saved. No Anki changes were made.";
    };

    const removeCardMedia = async (article, filename) => {
      message.textContent = "Removing card image…";
      const payload = await requestForm(
        documentRef,
        fetchImpl,
        `/api/anki/jobs/${jobId}/gap-cards/${article.dataset.cardId}`
          + `/media/${encodeURIComponent(filename)}`,
        null,
        "DELETE",
      );
      if (Number.isInteger(payload.revision)) revision = payload.revision;
      renderCardMedia(documentRef, article, payload.media || []);
      message.textContent = "Card image removed. No Anki changes were made.";
    };

    if (dialog) {
      dialog.addEventListener("close", () => {
        if (!dialogConfirmed) {
          const saveButton = documentRef.querySelector("[data-save-review]");
          const buildButton = documentRef.querySelector(
            "[data-build-envelope]",
          );
          if (saveButton) saveButton.disabled = false;
          if (buildButton) buildButton.disabled = false;
        }
        dialogConfirmed = false;
      });
    }

    if (page.dataset.reviewListenersBound !== "true") {
      page.dataset.reviewListenersBound = "true";
      documentRef.querySelectorAll("[data-review-view]").forEach((button) => {
        button.addEventListener("click", () => {
          setReviewView(documentRef, button.dataset.reviewView);
        });
      });
      documentRef.querySelectorAll("[data-workbench-view]").forEach((button) => {
        button.addEventListener("click", () => {
          setReviewView(documentRef, button.dataset.workbenchView);
        });
      });
      documentRef.querySelector("[data-review-search]")?.addEventListener(
        "input",
        () => applyReviewFilters(documentRef),
      );
      page.addEventListener("change", (event) => {
        const input = event.target.closest?.("[data-card-media-input]");
        const article = input?.closest(".anki-generated-card");
        if (!input || !article) return;
        uploadCardMedia(article, input.files).catch((error) => {
          message.textContent = error.message;
        }).finally(() => { input.value = ""; });
      });
      page.addEventListener("dragover", (event) => {
        const zone = event.target.closest?.("[data-card-media-dropzone]");
        if (!zone) return;
        event.preventDefault();
        zone.classList.add("is-dragging");
      });
      page.addEventListener("dragleave", (event) => {
        event.target.closest?.("[data-card-media-dropzone]")
          ?.classList.remove("is-dragging");
      });
      page.addEventListener("drop", (event) => {
        const zone = event.target.closest?.("[data-card-media-dropzone]");
        const article = zone?.closest(".anki-generated-card");
        if (!zone || !article) return;
        event.preventDefault();
        zone.classList.remove("is-dragging");
        uploadCardMedia(article, event.dataTransfer?.files || []).catch((error) => {
          message.textContent = error.message;
        });
      });
      page.addEventListener("click", (event) => {
        const button = event.target.closest?.("[data-remove-card-media]");
        const article = button?.closest(".anki-generated-card");
        if (!button || !article) return;
        removeCardMedia(article, button.dataset.removeCardMedia).catch((error) => {
          message.textContent = error.message;
        });
      });
    }

    const basePollDelayMs = 2500;
    const maxPollDelayMs = 30000;
    let pollDelayMs = basePollDelayMs;

    const handlePollError = (error) => {
      const note = documentRef.querySelector("#anki-processing-note");
      if (note) {
        note.textContent = `${error.message} Retrying…`;
      }
      pollDelayMs = Math.min(pollDelayMs * 2, maxPollDelayMs);
      pollTimer = root.setTimeout(() => {
        refreshJob().catch(handlePollError);
      }, pollDelayMs);
    };

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
          openApplyDialog();
        }
        return;
      }
      if (!["failed", "canceled"].includes(job.state)) {
        pollDelayMs = basePollDelayMs;
        pollTimer = root.setTimeout(() => {
          refreshJob().catch(handlePollError);
        }, pollDelayMs);
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

    const acknowledgeOverflowIfNeeded = async () => {
      const selectedExisting = [...documentRef.querySelectorAll(
        "[data-candidate-selection]",
      )].filter((input) => input.checked).map((input) => Number(input.value));
      const selectedGenerated = [...documentRef.querySelectorAll(
        "[data-gap-selection]",
      )].filter((input) => input.checked).map((input) => String(
        input.closest(".anki-generated-card")?.dataset.cardId || "",
      )).filter(Boolean);
      if (selectedExisting.length + selectedGenerated.length <= 70) return null;
      if (!root.confirm(
        "This selection exceeds the 70-card soft cap. Confirm the exact mandatory, nonredundant coverage before freezing the apply plan.",
      )) {
        throw new Error("The over-soft-cap selection was not confirmed.");
      }
      return requestJson(
        documentRef,
        fetchImpl,
        `/api/anki/jobs/${jobId}/overflow-acknowledgement`,
        {
          method: "POST",
          body: JSON.stringify({
            contract_version: 1,
            review_revision: revision,
            selected_existing_note_ids: selectedExisting,
            selected_generated_card_ids: selectedGenerated,
          }),
        },
      );
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
          // This is intentionally requested after save: its signature binds
          // the current revision and exact persisted selection. Any reselection
          // or stale revision therefore requires a new server acknowledgement.
          const overflowAcknowledgement = await acknowledgeOverflowIfNeeded();
          const plan = await requestJson(
            documentRef,
            fetchImpl,
            `/api/anki/jobs/${jobId}/envelope`,
            {
              method: "POST",
              body: JSON.stringify({
                contract_version: 1,
                review_revision: revision,
                overflow_acknowledgement: overflowAcknowledgement,
              }),
            },
          );
          fillApplyPlan(documentRef, plan.summary);
          message.textContent =
            "Apply plan is frozen. Inspect the final counts before confirming.";
          openApplyDialog();
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
        const statusNode = dialog.querySelector("[data-dialog-status]");
        const errorNode = dialog.querySelector("[data-dialog-error]");
        button.disabled = true;
        if (errorNode) errorNode.textContent = "";
        if (statusNode) {
          statusNode.textContent =
            "Syncing first, then applying the frozen plan…";
        }
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
          dialogConfirmed = true;
          dialog.close();
          if (statusNode) statusNode.textContent = "";
          showRecovery(documentRef, result.recovery, result.apply_state);
          message.textContent = result.recovery.message;
          await refreshJob().catch(handlePollError);
        } catch (error) {
          if (statusNode) statusNode.textContent = "";
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

    documentRef.querySelector("[data-retry-job]")
      ?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        documentRef.querySelector("#anki-processing-note").textContent =
          "Retrying the interrupted stage…";
        try {
          await requestJson(
            documentRef,
            fetchImpl,
            `/api/anki/jobs/${jobId}/retry`,
            { method: "POST" },
          );
          await refreshJob();
        } catch (error) {
          documentRef.querySelector("#anki-processing-note").textContent =
            error.message;
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
    addDeckPriority,
    deckChoiceAvailable,
    canRetryCuration,
    candidateAudit,
    convergenceDisplay,
    collectReview,
    collectSourceRevisionIds,
    commaValues,
    csrfToken,
    courseOptions,
    editableTagPatch,
    examOptions,
    generatedCard,
    hasRequiredSources,
    initialize,
    initializeReview,
    jobRow,
    lectureDisplayLabel,
    lectureOptions,
    loadModelOptions,
    matchesReviewSearch,
    modelOptionValues,
    moveDeckPriority,
    parseLecturePayload,
    populateModelOptions,
    processingPercent,
    readableState,
    statusTone,
    reconciliationDisplay,
    reviewSurfaceDisplay,
    reviewSelectionDisplay,
    reviewSurfaceDetails,
    reviewCardVisible,
    renderReviewSurface,
    reviewWorkspaceCounts,
    reviewViews,
    renderV3ReviewSurface,
    renderProcessing,
    renderSourceChoices,
    resolveLecture,
    resolveProviderModel,
    runFailedJobAction,
    runWhenReady,
    sourceStatuses,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    runWhenReady(root.document, () => initialize(root.document));
  }
})(typeof globalThis === "undefined" ? this : globalThis);
