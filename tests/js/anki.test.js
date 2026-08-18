const test = require("node:test");
const assert = require("node:assert/strict");

const anki = require("../../src/oms_hub/web/static/anki.js");

const lectures = [{
  id: 42,
  subject: "Heme Lymph",
  exam_number: 1,
  lecture_number: 4,
  topic: 'Anemia "I"',
  target_tag:
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I",
  revisions: [{
    id: 7,
    kind: "slides",
    source_sha256: "a".repeat(64),
  }, {
    id: 8,
    kind: "transcripts",
    source_sha256: "b".repeat(64),
  }],
  outline: { id: 9, kind: "summary", sha256: "c".repeat(64) },
  source_ready: true,
}, {
  id: 55,
  subject: "Heme Lymph",
  exam_number: 3,
  lecture_number: 22,
  topic: "Hemolytic Anemias",
  target_tag:
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block3::Lec22_Hemolytic_Anemias",
  revisions: [],
}, {
  id: 77,
  subject: "Neuro",
  exam_number: 2,
  lecture_number: 8,
  topic: "Brainstem",
  target_tag:
    "AnkiHub_Optional::LMU_OMS_II::Neuro::Block2::Lec8_Brainstem",
  revisions: [],
}];

test("lecture payload preserves quoted topics and current revisions", () => {
  const parsed = anki.parseLecturePayload(JSON.stringify(lectures));

  assert.deepEqual(parsed, lectures);
});

test("lecture selection resolves sources and editable tag by numeric id", () => {
  const selected = anki.resolveLecture(lectures, "42");

  assert.equal(selected.id, 42);
  assert.equal(selected.revisions[0].kind, "slides");
  assert.equal(
    selected.target_tag,
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I",
  );
  assert.equal(anki.resolveLecture(lectures, "999"), null);
});

test("curation requires slides, transcript, and NotebookLM outline", () => {
  assert.equal(anki.hasRequiredSources(lectures[0]), true);
  assert.equal(anki.hasRequiredSources(lectures[1]), false);
  assert.equal(anki.hasRequiredSources({
    revisions: [{ kind: "slides" }, { kind: "transcripts" }],
    outline: null,
  }), false);
});

test("lecture source state always includes slides transcript and outline", () => {
  assert.deepEqual(anki.sourceStatuses(null), {
    slides: "neutral",
    transcripts: "neutral",
    summary: "neutral",
  });
  assert.deepEqual(anki.sourceStatuses({
    source_status: { slides: true, transcripts: false, summary: true },
  }), {
    slides: "ready",
    transcripts: "missing",
    summary: "ready",
  });
});

test("ordered deck helpers preserve selection priority", () => {
  assert.deepEqual(
    anki.addDeckPriority(["AnKing"], "Sketchy"),
    ["AnKing", "Sketchy"],
  );
  assert.deepEqual(
    anki.addDeckPriority(["AnKing"], "anking"),
    ["AnKing"],
  );
  assert.deepEqual(
    anki.moveDeckPriority(["AnKing", "Sketchy"], 1, -1),
    ["Sketchy", "AnKing"],
  );
});

test("review defaults to only selected final changes and combines candidates", () => {
  const review = {
    groups: {
      pass_1_matches: [{ note_id: 1, selected: true }, { note_id: 2, selected: false }],
      recovered_in_pass_2: [{ note_id: 3, selected: true }],
      generated_cards: [{ card_id: "g1", selected: true }, { card_id: "g2", selected: false }],
    },
  };
  const views = anki.reviewViews(review);
  assert.equal(views.active, "final");
  assert.deepEqual(views.final.existing.map((item) => item.note_id), [1, 3]);
  assert.deepEqual(views.final.generated.map((item) => item.card_id), ["g1"]);
  assert.deepEqual(views.candidates.map((item) => item.note_id), [1, 2, 3]);
});

test("review surface keeps below-floor guidance quality-first", () => {
  const display = anki.reviewSurfaceDisplay({
    selection: {
      selected_count: 59,
      warning_floor: 60,
      ordinary_target: 65,
      soft_cap: 70,
      below_warning_floor: true,
      selection_metadata_state: "incomplete",
    },
  });

  assert.equal(display.visible, true);
  assert.match(display.sizing, /60 is a warning floor/);
  assert.match(display.belowFloorWarning, /do not add weak, redundant, or ungrounded cards/);
  assert.equal(display.selectionMetadataState, "incomplete");
});

test("review surface exposes only complete current marginal and overflow reasons", () => {
  const complete = anki.reviewSelectionDisplay({
    selection: {
      selection_metadata_state: "complete",
      overflow_acknowledgement: { signed: true },
      selection_metadata: [{
        identity: "note:66",
        selected_position: 66,
        tier: "T2",
        marginal_value_reason: "only_valid_required_fact",
      }, {
        identity: "note:71",
        selected_position: 71,
        tier: "T1",
        mandatory: true,
        overflow_reason: "required fact",
      }],
    },
  });

  assert.deepEqual(complete.selectionReasons, [
    "Card 66 (note:66, T2): marginal value — Only Valid Required Fact",
    "Card 71 (note:71, T1): mandatory yes; overflow reason — required fact; signed acknowledgement recorded.",
  ]);
  assert.equal(complete.metadataNotice, "");

  const incomplete = anki.reviewSelectionDisplay({
    selection: {
      selection_metadata_state: "incomplete",
      selection_metadata: [{ selected_position: 71, overflow_reason: "stale" }],
    },
  });
  assert.deepEqual(incomplete.selectionReasons, []);
  assert.match(incomplete.metadataNotice, /incomplete or unavailable/);
});

test("review surface preserves evidence-quality and duplicate resolution details", () => {
  const details = anki.reviewSurfaceDetails({
    evidence_quality: [{ identity: "note:42", evidence_quality: "primary_source" }],
    duplicate_resolutions: [{ card_id: "CC-1", duplicate_of_existing_note_id: 42 }],
    selection: { overflow_acknowledgement: { signed: false, state: "pending" } },
  });

  assert.deepEqual(details.evidenceQuality, [
    { identity: "note:42", evidence_quality: "primary_source" },
  ]);
  assert.deepEqual(details.duplicates, [{ card_id: "CC-1", duplicate_of_existing_note_id: 42 }]);
  assert.deepEqual(details.acknowledgement, { signed: false, state: "pending" });
});

test("candidate search includes note id text extra and hidden tags", () => {
  const candidate = {
    note_id: 42,
    note: {
      text: "Iron deficiency",
      extra: "Low ferritin",
      tags: [{ value: "Heme::Anemia" }],
    },
  };
  assert.equal(anki.matchesReviewSearch(candidate, "ferritin"), true);
  assert.equal(anki.matchesReviewSearch(candidate, "heme::anemia"), true);
  assert.equal(anki.matchesReviewSearch(candidate, "42"), true);
  assert.equal(anki.matchesReviewSearch(candidate, "brainstem"), false);
});

test("dependent selector options stay inside the selected pathway", () => {
  assert.deepEqual(anki.courseOptions(lectures), ["Heme Lymph", "Neuro"]);
  assert.deepEqual(anki.examOptions(lectures, "Heme Lymph"), [1, 3]);
  assert.deepEqual(
    anki.lectureOptions(lectures, "Heme Lymph", "3").map((item) => item.id),
    [55],
  );
  assert.deepEqual(anki.examOptions(lectures, "Unknown"), []);
  assert.deepEqual(anki.lectureOptions(lectures, "Heme Lymph", ""), []);
});

test("provider changes resolve that provider's saved model", () => {
  const models = {
    anthropic: "claude-sonnet-4-6",
    openai: "gpt-5.2",
  };

  assert.equal(
    anki.resolveProviderModel(models, "anthropic"),
    "claude-sonnet-4-6",
  );
  assert.equal(anki.resolveProviderModel(models, "gemini"), "");
});

test("modelOptionValues inserts the current model when missing from the fetched list", () => {
  assert.deepEqual(
    anki.modelOptionValues(["gpt-4o", "gpt-4o-mini"], "custom/my-model"),
    ["custom/my-model", "gpt-4o", "gpt-4o-mini"],
  );
  assert.deepEqual(
    anki.modelOptionValues(["gpt-4o", "gpt-4o-mini", "gpt-4o"], "gpt-4o-mini"),
    ["gpt-4o", "gpt-4o-mini"],
  );
});

class FakeOption {
  constructor() {
    this.value = "";
    this.textContent = "";
  }
}

class FakeSelect {
  constructor() {
    this.children = [];
    this.value = "";
  }

  replaceChildren() {
    this.children = [];
  }

  append(node) {
    this.children.push(node);
  }
}

const fakeSelectDocument = { createElement: () => new FakeOption() };

test("populateModelOptions keeps the saved model selected after populating fetched options", () => {
  const select = new FakeSelect();

  anki.populateModelOptions(
    fakeSelectDocument,
    select,
    ["gpt-4o", "gpt-4o-mini"],
    "custom/my-model",
  );

  assert.deepEqual(
    select.children.map((option) => option.value),
    ["custom/my-model", "gpt-4o", "gpt-4o-mini"],
  );
  assert.equal(select.value, "custom/my-model");
});

test("model dropdown repopulates from the settings models endpoint when the provider changes", async () => {
  const select = new FakeSelect();
  const requestedUrls = [];
  const modelsByProvider = {
    "/api/settings/providers/anthropic/models": ["claude-sonnet-5", "claude-opus-5"],
    "/api/settings/providers/gemini/models": ["gemini-3.6-flash", "gemini-3.6-pro"],
  };
  const fetchImpl = async (url) => {
    requestedUrls.push(url);
    return {
      ok: true,
      async json() {
        return { models: modelsByProvider[url] || [], source: "live" };
      },
    };
  };
  const documentRef = {
    cookie: "study_hub_csrf=test-token",
    createElement: () => new FakeOption(),
  };

  await anki.loadModelOptions(
    documentRef,
    fetchImpl,
    select,
    "anthropic",
    "claude-sonnet-5",
  );

  assert.equal(requestedUrls[0], "/api/settings/providers/anthropic/models");
  assert.deepEqual(
    select.children.map((option) => option.value),
    ["claude-sonnet-5", "claude-opus-5"],
  );
  assert.equal(select.value, "claude-sonnet-5");

  await anki.loadModelOptions(
    documentRef,
    fetchImpl,
    select,
    "gemini",
    "",
  );

  assert.equal(requestedUrls[1], "/api/settings/providers/gemini/models");
  assert.deepEqual(
    select.children.map((option) => option.value),
    ["gemini-3.6-flash", "gemini-3.6-pro"],
  );
  assert.equal(select.value, "gemini-3.6-flash");
});

// -- Minimal fake DOM sufficient to drive renderSourceChoices --

class FakeClassList {
  constructor() {
    this.tokens = new Set();
  }

  add(name) {
    this.tokens.add(name);
  }

  contains(name) {
    return this.tokens.has(name);
  }
}

class FakeSourceNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.classList = new FakeClassList();
    this.textContent = "";
    this.value = "";
    this.name = "";
    this.type = "";
  }

  append(...nodes) {
    nodes.forEach((node) => this.children.push(node));
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  closest() {
    return null;
  }

  querySelectorAll(selector) {
    const match = /^input\[name=([\w-]+)\]$/.exec(selector);
    if (!match) return [];
    const [, name] = match;
    const found = [];
    const walk = (node) => {
      node.children.forEach((child) => {
        if (child.tagName === "input" && child.name === name) found.push(child);
        walk(child);
      });
    };
    walk(this);
    return found;
  }
}

class FakeSourceDocument {
  constructor() {
    this.registry = {};
  }

  register(id, node) {
    this.registry[id] = node;
  }

  createElement(tagName) {
    return new FakeSourceNode(tagName);
  }

  querySelector(selector) {
    if (selector.startsWith("#")) return this.registry[selector.slice(1)] || null;
    return null;
  }
}

const buildSourceContainer = () => {
  const documentRef = new FakeSourceDocument();
  const container = documentRef.createElement("div");
  documentRef.register("anki-source-revisions", container);
  return { documentRef, container };
};

test("v3 renderer displays bounded approval evidence without enabling envelopes", () => {
  const documentRef = new FakeSourceDocument();
  const host = documentRef.createElement("section");
  const buildEnvelope = { disabled: true };
  documentRef.register("review-quality", host);
  const originalQuery = documentRef.querySelector.bind(documentRef);
  documentRef.querySelector = (selector) => (
    selector === "[data-review-quality]" ? host
      : selector === "[data-build-envelope]" ? buildEnvelope
        : originalQuery(selector)
  );

  anki.renderReviewSurface(documentRef, {});
  assert.equal(host.hidden, true, "legacy rendering hides an empty quality host");

  anki.renderV3ReviewSurface(documentRef, {
    v3: {
      approval_only: true,
      reason: "approval only",
      policy: { sha256: "a".repeat(64), enforcement: { present: true, tier: "professor" } },
      scope: {
        fact_ids: ["fact-a"],
        degraded_mode: "none",
        sources: [{ source_id: "slides" }],
      },
      retrieval: { exact_only_fact_ids: ["fact-a"], polluted_fact_ids: ["fact-b"] },
      evidence: {
        grounding: "cited",
        degraded_mode: "none",
        generated_grounding: [{ card_id: "card-1", fact_id: "fact-a", evidence_ids: ["e1"] }],
      },
      classification: { tiers: ["cheap", "thorough"], escalations: [{ reason: "low" }] },
      selected_existing_note_ids: [7],
      selected_generated_card_ids: ["card-1"],
      resolution: {
        duplicate_fact_ids: ["fact-c"],
        unresolved: [{ fact_id: "fact-d", state: "unresolved", reason: "needs review" }],
      },
      cost: {
        calls: [{
          stage: "R7",
          call_id: "call-1",
          modality: "structured",
          model: "fixture",
          predicted: { microusd: 1 },
          reserved: { microusd: 2 },
          observed: { microusd: 1 },
          observed_estimated: true,
        }],
      },
    },
  });

  const lines = host.children[0].children.map((node) => node.textContent).join("\\n");
  ["Policy:", "Scope:", "Sources:", "Retrieval:", "Evidence:", "Classification:", "Resolution:", "Cost calls:"]
    .forEach((section) => assert.match(lines, new RegExp(section)));
  assert.match(lines, /Selected existing: 7/);
  assert.match(lines, /Selected generated: card-1/);
  assert.match(lines, /Grounding: card-1 \/ fact-a; evidence e1/);
  assert.match(lines, /Unresolved: fact-d \/ unresolved; needs review/);
  assert.match(lines, /Cost: R7 \/ call-1; structured \/ fixture; predicted 1, reserved 2, observed 1 \(estimated\)/);
  assert.equal(host.hidden, false, "v3 rendering reopens the host after legacy rendering");
  assert.equal(buildEnvelope.disabled, true);

  anki.renderV3ReviewSurface(documentRef, { selection: { selected_count: 1 } });
  assert.equal(host.children.length, 1, "legacy review surfaces do not gain v3 details");
});

test("renderSourceChoices emits hidden inputs for ready slides and transcript sources", () => {
  const { documentRef, container } = buildSourceContainer();

  anki.renderSourceChoices(documentRef, lectures[0]);

  const cards = container.children.filter((node) => node.tagName === "label");
  assert.equal(cards.length, 3);
  cards.forEach((card) => {
    assert.equal(card.className, "anki-source-option");
    assert.equal(card.classList.contains("is-ready"), true);
  });

  const hiddenInputs = container.querySelectorAll("input[name=source_revision_ids]");
  assert.deepEqual(hiddenInputs.map((input) => input.type), ["hidden", "hidden"]);
  assert.deepEqual(
    hiddenInputs.map((input) => Number(input.value)).sort((left, right) => left - right),
    [7, 8],
  );
});

test("renderSourceChoices marks missing sources red and emits no hidden inputs", () => {
  const { documentRef, container } = buildSourceContainer();

  anki.renderSourceChoices(documentRef, lectures[1]);

  const cards = container.children.filter((node) => node.tagName === "label");
  assert.equal(cards.length, 3);
  cards.forEach((card) => {
    assert.equal(card.classList.contains("is-missing"), true);
  });
  assert.equal(container.querySelectorAll("input[name=source_revision_ids]").length, 0);
});

test("renderSourceChoices shows neutral cards when no lecture is selected", () => {
  const { documentRef, container } = buildSourceContainer();

  anki.renderSourceChoices(documentRef, null);

  const cards = container.children.filter((node) => node.tagName === "label");
  assert.equal(cards.length, 3);
  cards.forEach((card) => {
    assert.equal(card.classList.contains("is-neutral"), true);
  });
  assert.equal(container.querySelectorAll("input[name=source_revision_ids]").length, 0);
});

test("collectSourceRevisionIds reads the hidden inputs rendered for ready sources", () => {
  const { documentRef, container } = buildSourceContainer();
  anki.renderSourceChoices(documentRef, lectures[0]);

  const form = documentRef.createElement("form");
  form.append(container);

  assert.deepEqual(
    anki.collectSourceRevisionIds(form).sort((left, right) => left - right),
    [7, 8],
  );
});

test("failed and explicitly held curation jobs expose pipeline retry", () => {
  assert.equal(anki.canRetryCuration({ state: "failed" }), true);
  assert.equal(
    anki.canRetryCuration({ state: "ready_for_review", can_retry: true }),
    true,
  );
  assert.equal(anki.canRetryCuration({ state: "judging_pass_1" }), false);
  assert.equal(anki.canRetryCuration({ state: "complete" }), false);
});

test("audit and coverage recompute advance processing progress", () => {
  const judged = anki.processingPercent("judging_pass_2");
  const passThree = anki.processingPercent("converging_pass_3");
  const passFour = anki.processingPercent("converging_pass_4");
  const passFive = anki.processingPercent("converging_pass_5");
  const audited = anki.processingPercent("auditing_candidates");
  const recomputed = anki.processingPercent("recomputing_coverage");
  const deduped = anki.processingPercent("deduping");
  const generated = anki.processingPercent("generating_gaps");
  const reconciled = anki.processingPercent("reconciling");
  const review = anki.processingPercent("ready_for_review");

  assert.ok(judged < passThree);
  assert.ok(passThree < passFour);
  assert.ok(passFour < passFive);
  assert.ok(passFive < audited);
  assert.ok(audited < recomputed);
  assert.ok(recomputed < deduped);
  assert.ok(deduped < generated);
  assert.ok(generated < reconciled);
  assert.ok(reconciled < review);
});

test("reconciliation display summarizes checks and warnings", () => {
  const display = anki.reconciliationDisplay({
    passed: ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A10"],
    failed: [],
    warned: [
      { assertion_id: "A9", message: "One or more source passages are uncited" },
      { assertion_id: "A11", message: "The run used a stale prompt checkout" },
    ],
    metrics: {
      audit_drop_rate: 0.27,
      uncited_passage_ids: ["SLD:07:0004", "TRX:07:0012"],
    },
  });

  assert.deepEqual(display, {
    checks: "9 / 11",
    dropRate: "27%",
    warning: "A9: One or more source passages are uncited · A11: The run used a stale prompt checkout",
    uncitedCount: 2,
  });
});

test("candidate review uses the blind audit instead of fake confidence", () => {
  const review = anki.candidateAudit({
    verdict: "drop",
    reason: "Different disease",
    provenance: {
      audit: {
        verdict: "drop",
        primary_subject: "hemophilia A",
        support: "none",
        reason: "Different disease",
        structure_issue: ["context_trap"],
      },
    },
  });

  assert.deepEqual(review, {
    label: "Audit: Drop",
    reason: "Different disease",
    subject: "hemophilia A",
    support: "None",
    structureIssues: ["Context Trap"],
  });
});

test("convergence summary surfaces pass count and manual review state", () => {
  assert.deepEqual(anki.convergenceDisplay({
    passes_run: 5,
    concepts_converged: 31,
    concepts_total: 33,
    needs_manual_review: true,
    manual_review_concept_ids: ["C17", "C28"],
  }), {
    count: "31 / 33",
    label: "Manual review · 5 passes",
    warning: "2 concepts were still growing after pass 5.",
  });
});

test("generated-card review edits use stable card identity", () => {
  const generated = {
    dataset: {
      cardId: "00000000-0000-0000-0000-000000000102",
      conceptId: "C01",
    },
    querySelector(selector) {
      return {
        "[data-gap-text]": { value: "{{c1::second}}" },
        "[data-gap-extra]": { value: "Split card" },
        "[data-gap-selection]": { checked: true },
      }[selector];
    },
  };
  const documentRef = {
    querySelectorAll(selector) {
      return selector === ".anki-generated-card" ? [generated] : [];
    },
  };

  const review = anki.collectReview(documentRef, 3);

  assert.deepEqual(review.gap_edits, [{
    contract_version: 1,
    card_id: "00000000-0000-0000-0000-000000000102",
    concept_id: "C01",
    text: "{{c1::second}}",
    extra: "Split card",
    selected: true,
  }]);
});

test("failed-run actions use CSRF-protected retry and remove endpoints", async () => {
  const requests = [];
  const documentRef = { cookie: "study_hub_csrf=test-token" };
  const fetchImpl = async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      async json() {
        return { job_id: "job-1" };
      },
    };
  };

  await anki.runFailedJobAction(
    documentRef,
    fetchImpl,
    "job-1",
    "retry",
  );
  await anki.runFailedJobAction(
    documentRef,
    fetchImpl,
    "job-1",
    "remove",
  );

  assert.deepEqual(
    requests.map(({ url, options }) => ({
      url,
      method: options.method,
      csrf: options.headers["X-CSRF-Token"],
    })),
    [
      {
        url: "/api/anki/jobs/job-1/retry",
        method: "POST",
        csrf: "test-token",
      },
      {
        url: "/api/anki/jobs/job-1/remove",
        method: "POST",
        csrf: "test-token",
      },
    ],
  );
});

test("failed jobs expose retry and removal while retry holds expose retry only", () => {
  class Node {
    constructor() {
      this.dataset = {};
      this.children = [];
      this.firstChild = null;
    }

    append(...children) {
      this.children.push(...children);
      this.firstChild = this.children[0] || null;
    }

    setAttribute() {}
  }

  const row = anki.jobRow(
    { createElement: () => new Node() },
    {
      id: 7,
      state: "failed",
      can_retry: true,
      lecture_id: 2,
      target_deck: "Study Hub::Neuro",
      updated_at: "2026-08-07T14:00:00",
    },
  );
  const actions = row.children[1];
  const [retry, remove] = actions.children;

  assert.equal(retry.className, "anki-icon-button sh-iconbtn");
  assert.equal(
    remove.className,
    "anki-icon-button sh-iconbtn sh-btn--danger is-danger",
  );
  assert.equal(retry.dataset.retryQueuedJob, "");
  assert.equal(remove.dataset.removeFailedJob, "");

  const heldRow = anki.jobRow(
    { createElement: () => new Node() },
    {
      id: 8,
      state: "ready_for_review",
      can_retry: true,
      lecture_id: 2,
      target_deck: "Study Hub::Neuro",
      updated_at: "2026-08-07T14:00:00",
    },
  );
  const heldActions = heldRow.children[1];

  assert.equal(heldActions.children.length, 1);
  assert.equal(heldActions.children[0].className, "anki-icon-button sh-iconbtn");
  assert.equal(heldActions.children[0].dataset.retryQueuedJob, "");
  assert.equal(heldActions.children[0].dataset.removeFailedJob, undefined);
});

// -- Minimal fake DOM sufficient to drive initializeReview --

class FakeReviewElement {
  constructor(tag = "div") {
    this.tagName = tag;
    this.dataset = {};
    this.style = {};
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this._children = {};
    this._listeners = {};
  }

  addEventListener(type, handler) {
    (this._listeners[type] ||= []).push(handler);
  }

  dispatchClose() {
    (this._listeners.close || []).forEach((handler) => handler());
  }

  close() {
    this.dispatchClose();
  }

  showModal() {}

  querySelector(selector) {
    return this._children[selector] || null;
  }
}

class FakeReviewDocument {
  constructor(registry) {
    this.registry = registry;
    this.cookie = "study_hub_csrf=test-token";
  }

  querySelector(selector) {
    return this.registry[selector] || null;
  }

  querySelectorAll(selector) {
    return this.registry[`${selector}[]`] || [];
  }
}

const buildReviewDom = () => {
  const page = new FakeReviewElement("article");
  page.dataset.jobId = "job-1";
  const message = new FakeReviewElement("p");
  const dialog = new FakeReviewElement("dialog");
  const dialogError = new FakeReviewElement("p");
  const dialogStatus = new FakeReviewElement("p");
  dialog._children["[data-dialog-error]"] = dialogError;
  dialog._children["[data-dialog-status]"] = dialogStatus;
  const saveButton = new FakeReviewElement("button");
  const buildButton = new FakeReviewElement("button");
  const confirmButton = new FakeReviewElement("button");
  const processingLabel = new FakeReviewElement("strong");
  const processingCount = new FakeReviewElement("span");
  const processingProgress = new FakeReviewElement("span");
  const processingNote = new FakeReviewElement("p");
  const jobState = new FakeReviewElement("span");

  const registry = {
    "[data-anki-review]": page,
    "#anki-review-message": message,
    "#anki-apply-dialog": dialog,
    "[data-save-review]": saveButton,
    "[data-build-envelope]": buildButton,
    "[data-confirm-apply]": confirmButton,
    "#anki-job-state": jobState,
    "#anki-processing-label": processingLabel,
    "#anki-processing-count": processingCount,
    "#anki-processing-progress": processingProgress,
    "#anki-processing-note": processingNote,
  };

  return {
    documentRef: new FakeReviewDocument(registry),
    dialog,
    dialogError,
    dialogStatus,
    saveButton,
    buildButton,
    confirmButton,
    processingNote,
  };
};

// The real setTimeout/clearTimeout, saved before any monkeypatching below,
// used only to yield to pending microtasks between async steps.
const realSetTimeout = global.setTimeout;
const flushMicrotasks = async (times = 6) => {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => realSetTimeout(resolve, 0));
  }
};

test("closing the apply dialog without confirming re-enables save/build-envelope buttons", async () => {
  const { documentRef, dialog, saveButton, buildButton } = buildReviewDom();
  const fetchImpl = async () => {
    throw new Error("network unavailable");
  };

  anki.initializeReview(documentRef, fetchImpl);
  await flushMicrotasks();

  // Simulate the state right after "Review apply plan" opened the dialog.
  saveButton.disabled = true;
  buildButton.disabled = true;

  // User pressed Escape / "Go back" — the dialog closes without the
  // confirm-apply handler ever running.
  dialog.close();

  assert.equal(saveButton.disabled, false);
  assert.equal(buildButton.disabled, false);
});

test("confirm-apply writes progress to the status node and errors to the alert node", async () => {
  const { documentRef, dialogError, dialogStatus, confirmButton } =
    buildReviewDom();
  const fetchImpl = async (url) => {
    if (String(url).endsWith("/apply")) {
      return {
        ok: false,
        async json() {
          return { detail: "Apply was rejected." };
        },
      };
    }
    throw new Error("not needed for this test");
  };

  anki.initializeReview(documentRef, fetchImpl);
  await flushMicrotasks();

  const [confirmHandler] = confirmButton._listeners.click;
  const handlerPromise = confirmHandler({ currentTarget: confirmButton });
  // Immediately after the click, before the failing fetch resolves, the
  // status node (not the alert node) should carry the in-progress copy.
  assert.match(dialogStatus.textContent, /Syncing/);
  assert.equal(dialogError.textContent, "");

  await handlerPromise;

  // Once the request fails, the status line is cleared and the error goes
  // to the dedicated alert node instead.
  assert.equal(dialogStatus.textContent, "");
  assert.equal(dialogError.textContent, "Apply was rejected.");
  assert.equal(confirmButton.disabled, false);
});

test("closing the apply dialog after a successful confirm leaves the buttons disabled", async () => {
  const { documentRef, dialog, saveButton, buildButton, confirmButton } =
    buildReviewDom();
  const fetchImpl = async (url) => {
    if (String(url).endsWith("/apply")) {
      return {
        ok: true,
        async json() {
          return {
            recovery: { kind: "complete", message: "Applied." },
            apply_state: "complete",
          };
        },
      };
    }
    throw new Error("not needed for this test");
  };

  anki.initializeReview(documentRef, fetchImpl);
  await flushMicrotasks();

  saveButton.disabled = true;
  buildButton.disabled = true;

  const [confirmHandler] = confirmButton._listeners.click;
  await confirmHandler({ currentTarget: confirmButton });
  // The handler itself calls dialog.close() on success, which fires the
  // "close" listener registered by initializeReview.

  assert.equal(saveButton.disabled, true);
  assert.equal(buildButton.disabled, true);
});

test("a failed job poll surfaces the error and retries with doubling backoff", async () => {
  const { documentRef, processingNote } = buildReviewDom();
  const scheduled = [];
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  global.setTimeout = (fn, delay) => {
    const entry = { fn, delay };
    scheduled.push(entry);
    return entry;
  };
  global.clearTimeout = (entry) => {
    const index = scheduled.indexOf(entry);
    if (index !== -1) scheduled.splice(index, 1);
  };

  let callCount = 0;
  const okJob = () => ({
    ok: true,
    async json() {
      return {
        state: "judging_pass_1",
        recovery: { kind: "pending" },
        error: null,
      };
    },
  });
  const failedJob = () => ({
    ok: false,
    async json() {
      return { detail: "Network hiccup" };
    },
  });
  const fetchImpl = async () => {
    callCount += 1;
    if (callCount === 1) return okJob();
    if (callCount === 2) return failedJob();
    if (callCount === 3) return failedJob();
    return okJob();
  };

  try {
    anki.initializeReview(documentRef, fetchImpl);
    await flushMicrotasks();

    assert.equal(scheduled.length, 1, "expected the healthy poll to be scheduled");
    assert.equal(scheduled[0].delay, 2500);

    // Fire the scheduled poll; it fails.
    const first = scheduled.shift();
    first.fn();
    await flushMicrotasks();

    assert.match(processingNote.textContent, /Network hiccup/);
    assert.match(processingNote.textContent, /Retrying/);
    assert.equal(scheduled.length, 1, "expected a retry to be scheduled after failure");
    assert.equal(scheduled[0].delay, 5000, "delay should double after one failure");

    // Fire again; it fails a second time and should double again.
    const second = scheduled.shift();
    second.fn();
    await flushMicrotasks();

    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 10000, "delay should double again after a second failure");
  } finally {
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
  }
});
