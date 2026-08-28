const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const review = require("../../src/oms_hub/web/static/studio_quiz_review.js");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.open = false;
    this.parentElement = null;
    this.classList = {
      add: (name) => { if (!this.className.split(" ").includes(name)) this.className = `${this.className} ${name}`.trim(); },
      toggle: (name, enabled) => {
        const names = this.className.split(" ").filter(Boolean).filter((item) => item !== name);
        if (enabled) names.push(name);
        this.className = names.join(" ");
      },
    };
  }

  append(...items) {
    items.forEach((item) => { if (item && typeof item === "object") item.parentElement = this; });
    this.children.push(...items);
  }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  insertBefore(item, before) {
    item.parentElement = this;
    const index = this.children.indexOf(before);
    if (index < 0) this.children.push(item);
    else this.children.splice(index, 0, item);
  }
  setAttribute(name, value) { this[name] = value; }
  focus() { documentRef.activeElement = this; }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
  }
  closest(selector) {
    let element = this;
    while (element) {
      if (Element.matches(element, selector)) return element;
      element = element.parentElement;
    }
    return null;
  }
  static matches(element, selector) {
    if (!element?.dataset) return false;
    if (selector === ".studio-review-choice") return element.className.split(" ").includes("studio-review-choice");
    if (selector === "details") return element.tagName === "details";
    if (selector === "summary") return element.tagName === "summary";
    if (selector === 'input[name="choice"]') return element.tagName === "input" && element.name === "choice";
    if (selector === 'input[name="correct_index"]') return element.tagName === "input" && element.name === "correct_index";
    if (selector === "[data-choices]") return element.dataset.choices === "true";
    if (selector === "[data-add-choice]") return element.dataset.addChoice === "true";
    if (selector === "[data-remove-choice]") return element.dataset.removeChoice === "true";
    if (selector === "[data-acknowledge-run-diagnostic]") return Boolean(element.dataset.acknowledgeRunDiagnostic);
    if (selector === "[data-image-override]") return Boolean(element.dataset.imageOverride);
    if (selector === "[data-image-upload]") return Boolean(element.dataset.imageUpload);
    if (selector === "[data-review-tab]") return Boolean(element.dataset.reviewTab);
    if (selector === "[data-review-panel]") return Boolean(element.dataset.reviewPanel);
    if (selector === "[data-question-message]") return element.dataset.questionMessage === "true";
    if (selector === "details[data-state-key]") return element.tagName === "details" && Boolean(element.dataset.stateKey);
    if (selector === "[data-state-key]") return Boolean(element.dataset.stateKey);
    if (selector === "[data-focus-key]") return Boolean(element.dataset.focusKey);
    if (selector === "[data-question-id]") return Boolean(element.dataset.questionId);
    return false;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = (element) => {
      if (Element.matches(element, selector)) found.push(element);
      element?.children?.forEach(visit);
    };
    this.children.forEach(visit);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const documentRef = {
  activeElement: null,
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => ({ textContent: text }),
};

const question = (id, stem) => ({
  id, original_identifier: id, provenance: "source", confidence: 1,
  source_refs: [], stem, choices: ["A", "B"], correct_index: 0,
  rationale: "Because", topic: null, area: null, learning_objective: null,
  verification_required: false, verified_at: null, candidates: [], selected_candidate_id: null,
  image_required: false, image_not_needed: false, image_attached: false,
});

const reviewPage = () => {
  const page = new Element("main");
  const blockers = new Element("section");
  const publish = new Element("button");
  const preview = new Element("a");
  const questions = new Element("section");
  const controls = new Map([
    ["[data-review-blockers]", blockers], ["[data-publish-quiz]", publish],
    ["[data-preview-link]", preview], ["[data-review-questions]", questions],
  ]);
  page.querySelector = (selector) => controls.get(selector) || Element.prototype.querySelector.call(page, selector);
  page.append(blockers, publish, preview, questions);
  return { page, blockers, publish, questions };
};

test("publish gating follows authoritative blocker state", () => {
  assert.equal(review.canPublish([]), true);
  assert.equal(review.canPublish(["q1: answer is missing"]), false);
  assert.equal(review.blockersText([]), "Ready for preview and publication.");
  assert.match(review.blockersText(["first", "second"]), /first\nsecond/);
});

test("run diagnostics render once and only overridable blockers can be acknowledged", () => {
  const { page, blockers, publish } = reviewPage();
  review.render(documentRef, page, {
    blockers: ["Count needs review", "OCR unavailable"],
    issues: [],
    run_diagnostics: [
      {
        code: "incomplete-sequential-question-extraction",
        message: "Count needs review",
        severity: "blocker",
        overridable: true,
        acknowledged: false,
      },
      {
        code: "parser-blocker",
        message: "OCR unavailable",
        severity: "blocker",
        overridable: false,
        acknowledged: false,
      },
    ],
    preview_url: null,
    questions: [],
  });

  const acknowledgements = blockers.querySelectorAll("[data-acknowledge-run-diagnostic]");
  assert.equal(acknowledgements.length, 1);
  assert.equal(
    acknowledgements[0].dataset.acknowledgeRunDiagnostic,
    "incomplete-sequential-question-extraction",
  );
  assert.equal(publish.disabled, true);
  assert.equal(
    blockers.children.filter((item) => item.className.includes("studio-review-issue-group sh-card")).length,
    2,
  );
});

test("acknowledged run diagnostic has no acknowledgement control", () => {
  const { page, blockers, publish } = reviewPage();
  review.render(documentRef, page, {
    blockers: [],
    issues: [],
    run_diagnostics: [{
      code: "incomplete-sequential-question-extraction",
      message: "Count reviewed",
      severity: "blocker",
      overridable: true,
      acknowledged: true,
    }],
    preview_url: null,
    questions: [],
  });

  assert.equal(blockers.querySelectorAll("[data-acknowledge-run-diagnostic]").length, 0);
  assert.equal(publish.disabled, false);
});

test("issue helpers retain roles, groups, and stable question anchors", () => {
  const issues = [
    { question_id: "question/1", type: "answer", message: "missing", role: "err" },
    { question_id: "question-2", type: "draft_diagnostic", message: "uncertain", role: "warn" },
    { question_id: "question-3", type: "answer", message: "outside range", role: "err" },
  ];
  assert.equal(review.questionAnchor("question/1"), "question-10-7175657374696f6e2f31");
  assert.equal(review.issueSummary(issues), "3 issues · 2 blocking issues · 1 warning");
  assert.deepEqual(review.groupIssues(issues).map((group) => [group.type, group.issues.length]), [
    ["answer", 2],
    ["draft_diagnostic", 1],
  ]);
});

test("question anchors are stable, fragment-safe, and injective for allowed identifiers", () => {
  const identifiers = ["question/1", "question%2F1", "question 1", "質問 1"];
  const anchors = identifiers.map((identifier) => review.questionAnchor(identifier));
  assert.equal(new Set(anchors).size, identifiers.length);
  assert.ok(anchors.every((anchor) => /^question-[0-9]+-[0-9a-f]+$/.test(anchor)));
  assert.equal(review.questionAnchor("質問 1"), review.questionAnchor("質問 1"));
});

test("no-candidate empty state is limited to unresolved image review", () => {
  assert.equal(review.hasImageReviewIssues([]), false);
  assert.equal(review.hasImageReviewIssues([{ type: "answer", code: "missing_answer" }]), false);
  assert.equal(review.hasImageReviewIssues([{ type: "image", code: "required_image_unresolved" }]), true);
  assert.equal(review.hasImageReviewIssues([{ type: "draft_diagnostic", code: "required_image_unresolved" }]), true);
  assert.equal(review.shouldRenderNoCandidateEmpty({
    issues: [{ type: "answer", code: "missing_answer" }],
    questions: [{ candidates: [] }],
  }), false);
  assert.equal(review.shouldRenderNoCandidateEmpty({
    issues: [{ type: "image", code: "required_image_unresolved" }],
    questions: [{ candidates: [] }],
  }), true);
});

test("edit and candidate payload helpers normalize only supported values", () => {
  assert.deepEqual(
    review.normalizedEditPayload({
      stem: "  Stem  ",
      choices: [" A ", "", "B"],
      correct_index: "1",
      rationale: " because ",
      topic: " ",
      area: "Neuro",
      learning_objective: "Recall",
    }),
    {
      stem: "Stem",
      choices: ["A", "B"],
      correct_index: 1,
      rationale: "because",
      topic: null,
      area: "Neuro",
      learning_objective: "Recall",
    },
  );
  assert.deepEqual(review.candidateSelectionPayload("candidate-1"), {
    image_candidate_id: "candidate-1",
  });
  assert.equal(
    review.candidateSelectionUrl("run / 1", "question/1"),
    "/studio/runs/run%20%2F%201/questions/question%2F1/image-selection",
  );
});

test("review UI uses DOM text nodes rather than untrusted HTML injection", () => {
  const source = fs.readFileSync(
    "src/oms_hub/web/static/studio_quiz_review.js",
    "utf8",
  );
  const template = fs.readFileSync(
    "src/oms_hub/web/templates/studio_quiz_review.html",
    "utf8",
  );
  assert.equal(source.includes("innerHTML"), false);
  assert.match(source, /textContent/);
  assert.match(source, /X-CSRF-Token/);
  assert.match(source, /"PATCH"/);
  assert.match(source, /Provide an answer rationale before saving\./);
  assert.match(template, /studio_quiz_review\.js\?v=[0-9.]+/);
});

test("issue disclosures and keyed focus survive a clean review refresh", () => {
  const { page, blockers } = reviewPage();
  const payload = {
    blockers: ["q1: missing answer"], issues: [{ question_id: "q1", display_label: "Q1", type: "answer", message: "missing", role: "err" }],
    preview_url: null, questions: [question("q1", "Stem")],
  };
  review.render(documentRef, page, payload);
  const details = blockers.querySelectorAll("details[data-state-key]")[0];
  const summary = details.children[0];
  details.open = true;
  summary.focus();

  review.render(documentRef, page, payload);

  const restored = blockers.querySelectorAll("details[data-state-key]")[0];
  assert.equal(restored.open, true);
  assert.equal(documentRef.activeElement, restored.children[0]);
});

test("render-state recovery uses the page when the keyed control became disabled", () => {
  const page = new Element("main");
  const candidate = new Element("button");
  candidate.dataset.focusKey = "question:q1:candidate:candidate-1";
  candidate.disabled = true;
  page.append(candidate);
  documentRef.activeElement = null;

  review.restoreRenderState(page, {
    openKeys: new Set(),
    focusKey: candidate.dataset.focusKey,
  });

  assert.equal(documentRef.activeElement, page);
});

test("removing a review choice retains dirty state and moves focus before refresh", () => {
  const card = { dataset: {}, focus() { documentRef.activeElement = card; } };
  const fallback = { focus() { documentRef.activeElement = fallback; } };
  const first = { querySelector: () => fallback };
  const removed = { removed: false, remove() { this.removed = true; } };
  const third = { querySelector: () => fallback };
  const group = {
    querySelectorAll() {
      return removed.removed ? [first, third] : [first, removed, third];
    },
    querySelector: () => null,
  };
  const remove = {
    closest(selector) {
      if (selector === ".studio-review-choice") return removed;
      if (selector === "[data-choices]") return group;
      if (selector === "[data-question-id]") return card;
      return null;
    },
  };

  assert.equal(review.removeChoiceRow(remove), true);
  assert.equal(removed.removed, true);
  assert.equal(card.dataset.dirty, "true");
  assert.equal(documentRef.activeElement, fallback);
});

test("removing a choice reindexes the retained correct answer and future choice keys", () => {
  const { page, questions } = reviewPage();
  const payload = question("q1", "Stem");
  payload.choices = ["A", "B", "C"];
  payload.correct_index = 1;
  review.render(documentRef, page, {
    blockers: [], issues: [], preview_url: null, questions: [payload],
  });
  const card = questions.querySelector("[data-question-id]");
  const group = card.querySelector("[data-choices]");
  const firstRemove = group.querySelectorAll(".studio-review-choice")[0]
    .querySelector("[data-remove-choice]");

  assert.equal(review.removeChoiceRow(firstRemove), true);
  let rows = group.querySelectorAll(".studio-review-choice");
  let choices = rows.map((row) => row.querySelector('input[name="choice"]'));
  let correct = rows.map((row) => row.querySelector('input[name="correct_index"]'));
  assert.deepEqual(choices.map((choice) => choice.value), ["B", "C"]);
  assert.deepEqual(correct.map((radio) => radio.value), ["0", "1"]);
  assert.equal(correct[0].checked, true);
  assert.deepEqual(review.normalizedEditPayload({
    stem: "Stem", choices: choices.map((choice) => choice.value),
    correct_index: correct.find((radio) => radio.checked).value, rationale: "Because",
  }), {
    stem: "Stem", choices: ["B", "C"], correct_index: 0, rationale: "Because",
  });

  const add = group.querySelector("[data-add-choice]");
  group.insertBefore(review.choiceRow(documentRef, "D", rows.length, -1, "q1"), add);
  review.reindexChoiceRows(group, "q1");
  rows = group.querySelectorAll(".studio-review-choice");
  const focusKeys = group.querySelectorAll("[data-focus-key]").map((element) => element.dataset.focusKey);
  const stateKeys = group.querySelectorAll("[data-state-key]").map((element) => element.dataset.stateKey);
  assert.equal(new Set(focusKeys).size, focusKeys.length);
  assert.equal(new Set(stateKeys).size, stateKeys.length);
  assert.equal(rows[2].querySelector('input[name="choice"]')["aria-label"], "Choice 3");
  assert.equal(rows[2].querySelector('input[name="correct_index"]').value, "2");
});

test("a successful q2-style authoritative refresh retains an unrelated dirty q1 editor", () => {
  const { page, questions } = reviewPage();
  const initial = {
    blockers: [], issues: [], preview_url: null,
    questions: [question("q1", "Local stem"), question("q2", "Old clean stem")],
  };
  review.render(documentRef, page, initial);
  const [dirtyCard, cleanCard] = questions.querySelectorAll("[data-question-id]");
  const dirtyStem = dirtyCard.querySelectorAll("[data-focus-key]")
    .find((field) => field.dataset.focusKey === "question:q1:stem");
  dirtyCard.dataset.dirty = "true";
  dirtyStem.value = "Unsaved local edit";

  review.render(documentRef, page, {
    blockers: [], issues: [], preview_url: null,
    questions: [question("q1", "Server replacement"), question("q2", "New clean stem")],
  });

  const [afterDirty, afterClean] = questions.querySelectorAll("[data-question-id]");
  assert.equal(afterDirty, dirtyCard);
  assert.equal(afterDirty.querySelectorAll("[data-focus-key]")
    .find((field) => field.dataset.focusKey === "question:q1:stem").value, "Unsaved local edit");
  assert.notEqual(afterClean, cleanCard);
});

test("a successful question save applies authoritative blocker and answer state", () => {
  const { page, publish, questions } = reviewPage();
  const initial = question("q1", "Stem");
  initial.correct_index = null;
  review.render(documentRef, page, {
    blockers: ["q1: answer is missing"],
    issues: [{ question_id: "q1", display_label: "Q1", type: "answer", message: "answer is missing", role: "err" }],
    preview_url: null,
    questions: [initial],
  });
  const dirtyCard = questions.querySelector("[data-question-id]");
  dirtyCard.dataset.dirty = "true";
  const form = { closest: () => dirtyCard };
  const saved = question("q1", "Stem");
  saved.choices = ["A", "B", "C"];
  saved.correct_index = 2;

  const status = review.applyQuestionSave(documentRef, page, form, {
    blockers: ["q1: required image is unresolved"],
    issues: [{ question_id: "q1", message: "required image is unresolved", role: "err" }],
    preview_url: null,
    questions: [saved],
  });

  const savedCard = questions.querySelector("[data-question-id]");
  const correct = savedCard.querySelectorAll('input[name="correct_index"]');
  assert.notEqual(savedCard, dirtyCard);
  assert.equal(correct[2].checked, true);
  assert.equal(publish.disabled, true);
  assert.equal(status.dataset.questionMessage, "true");
  assert.equal(savedCard.children.some((item) => item.textContent.includes("required image is unresolved")), true);
});

test("hard run diagnostics suppress the contradictory ready banner", () => {
  const { page, blockers } = reviewPage();
  review.render(documentRef, page, {
    blockers: ["OCR unavailable"], issues: [], preview_url: null,
    run_diagnostics: [{ message: "OCR unavailable", severity: "blocker", overridable: false, acknowledged: false }],
    questions: [question("q1", "Stem")],
  });
  assert.equal(blockers.children.some((item) => item.textContent === "Ready for preview and publication."), false);
  assert.equal(blockers.children.some((item) => item.children?.some(
    (child) => child.textContent === "OCR unavailable",
  )), true);
});

test("typed review-artifact envelopes retain recovery guidance with safe detail fallback", () => {
  assert.equal(
    review.reviewErrorMessage({
      error: {
        code: "review_artifact_unavailable",
        message: "Review data is unavailable.",
        recovery: "Re-run the source import, then refresh this page.",
      },
    }, "Fallback."),
    "Review data is unavailable. Re-run the source import, then refresh this page.",
  );
  assert.equal(
    review.reviewErrorMessage({ detail: "Legacy detail." }, "Fallback."),
    "Legacy detail.",
  );
});

test("candidate selection and choice removal controls have stable focus keys", () => {
  const { page, questions } = reviewPage();
  const item = question("q1", "Stem");
  item.candidates = [{
    candidate_id: "candidate-1", preview_url: "/candidate.png", source_title: "Slides",
    origin: "embedded", locator: "slide 1",
  }];
  review.render(documentRef, page, { blockers: [], issues: [], preview_url: null, questions: [item] });

  const keys = questions.querySelectorAll("[data-focus-key]").map((element) => element.dataset.focusKey);
  assert.ok(keys.includes("question:q1:candidate:candidate-1"));
  assert.ok(keys.includes("question:q1:choice:0:remove"));
});

test("image requirements can be marked unnecessary and restored", () => {
  const { page, questions } = reviewPage();
  const item = question("q1", "Stem");
  item.image_required = true;
  item.image_not_needed = false;
  review.render(documentRef, page, {
    blockers: ["q1: required image is unresolved"],
    issues: [{ question_id: "q1", message: "required image is unresolved", role: "err" }],
    preview_url: null,
    questions: [item],
  });

  let toggle = questions.querySelector("[data-image-override]");
  assert.equal(questions.querySelectorAll("[data-image-upload]").length, 1);
  assert.equal(toggle.textContent, "No image needed");
  assert.equal(toggle.dataset.imageNotNeeded, "false");

  item.image_not_needed = true;
  review.render(documentRef, page, {
    blockers: [], issues: [], preview_url: null, questions: [item],
  });
  toggle = questions.querySelector("[data-image-override]");
  assert.equal(toggle.textContent, "Require image");
  assert.equal(toggle.dataset.imageNotNeeded, "true");
});

test("blocking questions and ready questions render in editable tabs", () => {
  const { page, questions } = reviewPage();
  review.render(documentRef, page, {
    blockers: ["q1: answer is missing"],
    issues: [{ question_id: "q1", message: "answer is missing", role: "err" }],
    preview_url: null,
    questions: [question("q1", "Blocked"), question("q2", "Ready")],
  });

  const tabs = questions.querySelectorAll("[data-review-tab]");
  const panels = questions.querySelectorAll("[data-review-panel]");
  assert.deepEqual(tabs.map((tab) => tab.textContent), ["Needs review (1)", "Ready (1)"]);
  assert.equal(panels[0].querySelector("[data-question-id]").dataset.questionId, "q1");
  assert.equal(panels[1].querySelector("[data-question-id]").dataset.questionId, "q2");
  assert.equal(questions.querySelectorAll("[data-image-upload]").length, 2);
  assert.equal(panels[0].hidden, false);
  assert.equal(panels[1].hidden, true);
  assert.equal(tabs[0]["aria-controls"], panels[0].id);
  assert.equal(tabs[1]["aria-controls"], panels[1].id);
  assert.equal(panels[0]["aria-labelledby"], tabs[0].id);
  assert.equal(panels[1]["aria-labelledby"], tabs[1].id);
  assert.equal(tabs[0].tabIndex, 0);
  assert.equal(tabs[1].tabIndex, -1);

  review.setReviewTab(questions, "ready");
  assert.equal(panels[0].hidden, true);
  assert.equal(panels[1].hidden, false);
  assert.equal(tabs[0].tabIndex, -1);
  assert.equal(tabs[1].tabIndex, 0);
  assert.equal(review.moveReviewTab(tabs[1], "ArrowRight"), true);
  assert.equal(documentRef.activeElement, tabs[0]);
  assert.equal(review.moveReviewTab(tabs[0], "End"), true);
  assert.equal(documentRef.activeElement, tabs[1]);

  review.render(documentRef, page, {
    blockers: [], issues: [], preview_url: "/preview",
    questions: [question("q1", "Fixed"), question("q2", "Ready")],
  });
  const updatedTabs = questions.querySelectorAll("[data-review-tab]");
  const updatedPanels = questions.querySelectorAll("[data-review-panel]");
  assert.deepEqual(updatedTabs.map((tab) => tab.textContent), ["Needs review (0)", "Ready (2)"]);
  assert.equal(updatedPanels[1].querySelectorAll("[data-question-id]").length, 2);
  assert.equal(updatedTabs[1]["aria-selected"], "true");
  assert.equal(updatedPanels[0].hidden, true);
  assert.equal(updatedPanels[1].hidden, false);
});
