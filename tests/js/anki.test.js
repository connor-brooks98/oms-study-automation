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

test("only failed curation jobs expose pipeline retry", () => {
  assert.equal(anki.canRetryCuration("failed"), true);
  assert.equal(anki.canRetryCuration("judging_pass_1"), false);
  assert.equal(anki.canRetryCuration("complete"), false);
});

test("audit and coverage recompute advance processing progress", () => {
  const judged = anki.processingPercent("judging_pass_2");
  const passThree = anki.processingPercent("converging_pass_3");
  const passFour = anki.processingPercent("converging_pass_4");
  const passFive = anki.processingPercent("converging_pass_5");
  const audited = anki.processingPercent("auditing_candidates");
  const recomputed = anki.processingPercent("recomputing_coverage");
  const deduped = anki.processingPercent("deduping");

  assert.ok(judged < passThree);
  assert.ok(passThree < passFour);
  assert.ok(passFour < passFive);
  assert.ok(passFive < audited);
  assert.ok(audited < recomputed);
  assert.ok(recomputed < deduped);
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
