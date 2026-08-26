(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ObjectiveReview = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function escapeHTML(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function statusTone(status) {
    if (status === "approved") return "sh-pill--ok";
    if (status === "pending") return "sh-pill--warn";
    if (status === "merged") return "sh-pill--info";
    return "sh-pill--bare";
  }

  async function responseJSON(response) {
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
    return payload;
  }

  class ObjectiveReviewClient {
    constructor(fetchImpl, csrfToken) {
      this.fetch = fetchImpl;
      this.csrfToken = csrfToken;
    }

    list() {
      return this.fetch("/api/v1/objectives").then(responseJSON);
    }

    mutate(path, body) {
      const options = {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": this.csrfToken,
        },
      };
      if (body !== undefined) options.body = JSON.stringify(body);
      return this.fetch(path, options).then(responseJSON);
    }

    extract(sourceRevisionIds) {
      return this.mutate("/api/v1/objectives/extract", {
        source_revision_ids: sourceRevisionIds,
      });
    }

    approve(objectiveId) {
      return this.mutate(`/api/v1/objectives/${encodeURIComponent(objectiveId)}/approve`);
    }

    merge(objectiveId, targetObjectiveId) {
      return this.mutate(`/api/v1/objectives/${encodeURIComponent(objectiveId)}/merge`, {
        target_objective_id: targetObjectiveId,
      });
    }

    retire(objectiveId) {
      return this.mutate(`/api/v1/objectives/${encodeURIComponent(objectiveId)}/retire`);
    }

    previewEvidence(evidenceId) {
      return this.fetch(`/api/v1/knowledge/evidence/${encodeURIComponent(evidenceId)}`)
        .then(responseJSON);
    }
  }

  class PendingActions {
    constructor() {
      this.actions = new Set();
    }

    has(key) {
      return this.actions.has(key);
    }

    async run(key, action) {
      if (this.actions.has(key)) throw new Error(`${key} is already pending`);
      this.actions.add(key);
      try {
        return await action();
      } finally {
        this.actions.delete(key);
      }
    }
  }

  function objectiveCard(objective) {
    const id = escapeHTML(objective.objective_id);
    const evidence = (objective.evidence_ids || []).map((evidenceId) => (
      `<li><button class="sh-btn sh-btn--secondary" type="button" `
      + `data-preview-evidence="${escapeHTML(evidenceId)}">`
      + `${escapeHTML(evidenceId)}</button></li>`
    )).join("");
    let actions = "";
    if (objective.status === "pending") actions = (
      `<form class="objective-review__actions" data-objective-form data-objective-id="${id}">`
      + '<button class="sh-btn sh-btn--primary" name="action" value="approve">Approve</button>'
      + '<label>Merge into <input class="sh-input" name="target_objective_id"></label>'
      + '<button class="sh-btn sh-btn--secondary" name="action" value="merge">Merge</button>'
      + '<button class="sh-btn sh-btn--secondary" name="action" value="retire">Retire</button>'
      + "</form>"
    );
    else if (objective.status === "approved") actions = (
      `<form class="objective-review__actions" data-objective-form data-objective-id="${id}">`
      + '<button class="sh-btn sh-btn--secondary" name="action" value="retire">Retire</button>'
      + "</form>"
    );
    return `<article class="objective-review__card sh-row" data-objective-id="${id}">`
      + '<div class="objective-review__summary">'
      + `<span class="sh-pill ${statusTone(objective.status)}">${escapeHTML(objective.status)}</span>`
      + `<h3>${escapeHTML(objective.observable_verb)} ${escapeHTML(objective.concept)}</h3>`
      + `<p>${escapeHTML(objective.description)}</p></div>`
      + '<details class="objective-review__evidence"><summary>'
      + '<span class="sh-disclose" aria-hidden="true">▸</span> Evidence</summary>'
      + `<ul>${evidence}</ul><pre data-evidence-preview aria-live="polite"></pre></details>`
      + '<p data-objective-status aria-live="polite"></p>' + actions
      + "</article>";
  }

  function initialize(host, options = {}) {
    if (!host) return null;
    const client = options.client || new ObjectiveReviewClient(
      options.fetchImpl || globalThis.fetch.bind(globalThis),
      options.csrfToken || "",
    );
    const pending = options.pending || new PendingActions();

    async function refresh() {
      const payload = await client.list();
      host.innerHTML = payload.objectives.map(objectiveCard).join("");
    }

    async function load() {
      try {
        await refresh();
      } catch (error) {
        host.innerHTML = '<div class="sh-validation" role="alert" aria-live="assertive">'
          + `${escapeHTML(error.message)} `
          + '<button class="sh-btn sh-btn--secondary" type="button" '
          + "data-objective-retry>Retry</button></div>";
      }
    }

    host.addEventListener("click", async (event) => {
      const retry = event.target.closest?.("[data-objective-retry]");
      if (retry) {
        await load();
        return;
      }
      const button = event.target.closest?.("[data-preview-evidence]");
      if (!button) return;
      const preview = button.closest("details")?.querySelector("[data-evidence-preview]");
      try {
        const payload = await pending.run(`evidence:${button.dataset.previewEvidence}`, () => (
          client.previewEvidence(button.dataset.previewEvidence)
        ));
        if (preview) preview.textContent = payload.excerpt || "Preview unavailable";
      } catch (error) {
        if (preview) {
          preview.textContent = error.message;
          preview.setAttribute("role", "alert");
          preview.setAttribute("aria-live", "assertive");
        }
      }
    });

    host.addEventListener("submit", async (event) => {
      const form = event.target.closest?.("[data-objective-form]");
      if (!form) return;
      event.preventDefault();
      const submitter = event.submitter;
      const action = submitter?.value;
      const objectiveId = form.dataset.objectiveId;
      const key = `${action}:${objectiveId}`;
      form.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      try {
        await pending.run(key, () => {
          if (action === "approve") return client.approve(objectiveId);
          if (action === "merge") {
            return client.merge(objectiveId, form.elements.target_objective_id.value);
          }
          return client.retire(objectiveId);
        });
        await refresh();
      } catch (error) {
        const status = form.closest("[data-objective-id]")?.querySelector("[data-objective-status]");
        if (status) status.textContent = error.message;
      } finally {
        form.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      }
    });

    const ready = load();
    return { client, pending, ready, refresh: load };
  }

  return {
    ObjectiveReviewClient,
    PendingActions,
    escapeHTML,
    initialize,
    objectiveCard,
    statusTone,
  };
});
