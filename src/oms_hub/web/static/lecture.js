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

  const render = (card, status) => {
    const message = card.querySelector("[data-generation-message]");
    const active = ["queued", "running"].includes(status.state);
    const completeMessage = card.dataset.kind === "quiz"
      ? "1 Study Hub quiz is ready."
      : "Lecture outline PDF is ready.";
    const labels = {
      queued: "Queued on the Study Hub device.",
      running: `Working: ${(status.stage || "starting").replaceAll("_", " ")}.`,
      paused: status.message || "Reconnect Google, then try again.",
      failed: status.message || "Generation stopped. You can retry.",
      complete: completeMessage,
      ready: "Ready to generate.",
    };
    message.textContent = labels[status.state] || "Status updated.";
    const generate = card.querySelector("[data-generate]");
    generate.disabled = active;
    if (status.url) {
      const actions = card.querySelector(".file-actions");
      let link = card.querySelector("[data-generation-link]");
      if (!link) {
        link = card.ownerDocument.createElement("a");
        link.dataset.generationLink = "";
        actions.prepend(link);
      }
      link.href = status.url;
      if (card.dataset.kind === "quiz") {
        link.className = "button primary sh-btn sh-btn--primary";
        link.textContent = "Take Lecture Quiz";
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } else {
        link.className = "button secondary sh-btn sh-btn--secondary";
        link.textContent = "Open Lecture Outline";
        actions.classList.remove("lecture-card-actions--single");
        let download = card.querySelector("[data-generation-download]");
        if (!download) {
          download = card.ownerDocument.createElement("a");
          download.className = "button primary sh-btn sh-btn--primary";
          download.dataset.generationDownload = "";
          actions.append(download);
        }
        download.href = `${status.url}/download`;
        download.textContent = "Download Lecture Outline";
      }
      card.classList.add("ready");
      card.classList.remove("missing");
      if (!generate.className.split(" ").includes("lecture-regenerate")) {
        generate.className = "lecture-regenerate sh-iconbtn";
        generate.textContent = "↻";
        const label = `Regenerate lecture ${card.dataset.kind}`;
        generate.setAttribute("aria-label", label);
        generate.title = label;
        card.append(generate);
      }
    }
    return active;
  };

  const runWhenReady = (documentRef, callback) => {
    if (documentRef.readyState === "loading") {
      documentRef.addEventListener("DOMContentLoaded", callback, { once: true });
      return;
    }
    callback();
  };

  const formatCompletedOn = (value) => {
    if (!value) return "Not completed";
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return String(value);
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    }).format(new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  };

  const localDateValue = (value = new Date()) => {
    const pad = (part) => String(part).padStart(2, "0");
    return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
  };

  const initializePassTracker = (documentRef, fetchImpl, lectureId) => {
    const rows = [...documentRef.querySelectorAll("[data-pass-row]")];
    const passCount = documentRef.querySelector("[data-pass-count]");
    const addPass = documentRef.querySelector("[data-add-pass]");
    const feedback = documentRef.querySelector("[data-pass-feedback]");
    if (!rows.length) return;

    const announce = (message) => {
      if (feedback) feedback.textContent = message;
    };
    const updateSummary = () => {
      const completed = rows.filter(
        (row) => row.querySelector("[data-pass-complete]").checked,
      ).length;
      if (passCount) {
        passCount.textContent = `${completed}/${rows.length}`;
        passCount.classList.remove("sh-pill--ok", "sh-pill--info", "t-number");
        passCount.classList.add(completed === rows.length ? "sh-pill--ok" : "sh-pill--info");
        void passCount.offsetWidth;
        passCount.classList.add("t-number");
      }
      const saving = rows.some((row) => (
        row.querySelector("[data-pass-complete]").disabled
        || row.querySelector("[data-pass-resource]").disabled
        || (
          row.querySelector("[data-pass-complete]").checked
          && row.querySelector("[data-pass-date]").disabled
        )
      ));
      if (addPass) addPass.disabled = completed !== rows.length || saving;
    };
    const renderCompletion = (row, payload) => {
      const checkbox = row.querySelector("[data-pass-complete]");
      const date = row.querySelector("[data-pass-date]");
      checkbox.checked = Boolean(payload.completed_on);
      date.value = payload.completed_on || "";
      date.disabled = !checkbox.checked;
      row.classList[checkbox.checked ? "add" : "remove"]("is-complete");
      updateSummary();
    };
    const renderResource = (row, payload) => {
      if (payload.resource !== undefined) {
        row.querySelector("[data-pass-resource]").value = payload.resource || "";
      }
    };
    const patchPass = async (position, body) => {
      const response = await fetchImpl(
        `/api/lectures/${lectureId}/passes/${position}`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken(documentRef),
          },
          body: JSON.stringify(body),
          cache: "no-store",
        },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Pass update failed.");
      return payload;
    };
    const ensureResourceOption = (name) => {
      documentRef.querySelectorAll("[data-pass-resource]").forEach((select) => {
        const exists = [...select.options].some((option) => (
          String(option.value).toLowerCase() === String(name).toLowerCase()
        ));
        if (!exists) {
          const option = documentRef.createElement("option");
          option.value = name;
          option.textContent = name;
          const other = [...select.options].find((option) => option.value === "Other");
          if (other) select.insertBefore(option, other);
          else select.append(option);
        }
      });
    };

    rows.forEach((row) => {
      const position = row.dataset.passPosition;
      const checkbox = row.querySelector("[data-pass-complete]");
      const date = row.querySelector("[data-pass-date]");
      const resource = row.querySelector("[data-pass-resource]");
      const custom = row.querySelector("[data-pass-resource-custom]");
      const customInput = row.querySelector("[data-pass-resource-name]");
      const addResource = row.querySelector("[data-add-pass-resource]");
      let savedCompletedOn = date.value;
      let savedResource = resource.value;
      const hideCustomResource = () => {
        if (!custom) return;
        custom.hidden = true;
        if (customInput) customInput.value = "";
      };
      const showCustomResource = () => {
        custom.hidden = false;
        customInput.focus();
      };
      if (resource.value === "Other") showCustomResource();

      checkbox.addEventListener("change", async () => {
        const previousCompleted = !checkbox.checked;
        const previousCompletedOn = savedCompletedOn;
        const completedOn = checkbox.checked ? localDateValue() : null;
        if (checkbox.checked) {
          date.value = completedOn;
        }
        date.disabled = true;
        checkbox.disabled = true;
        updateSummary();
        try {
          const payload = await patchPass(
            position,
            checkbox.checked ? { completed_on: completedOn } : { completed: false },
          );
          renderCompletion(row, payload);
          savedCompletedOn = date.value;
          announce(`Pass ${position} saved.`);
        } catch (error) {
          checkbox.checked = previousCompleted;
          date.value = previousCompletedOn;
          date.disabled = !previousCompleted;
          updateSummary();
          announce(`Pass ${position} update failed: ${error.message}`);
        } finally {
          checkbox.disabled = false;
          updateSummary();
        }
      });

      date.addEventListener("change", async () => {
        if (!checkbox.checked || date.disabled) return;
        const previousCompletedOn = savedCompletedOn;
        date.disabled = true;
        updateSummary();
        try {
          const payload = await patchPass(position, { completed_on: date.value });
          renderCompletion(row, payload);
          savedCompletedOn = date.value;
          announce(`Pass ${position} date saved.`);
        } catch (error) {
          date.value = previousCompletedOn;
          date.disabled = false;
          announce(`Pass ${position} date update failed: ${error.message}`);
        } finally {
          date.disabled = !checkbox.checked;
          updateSummary();
        }
      });

      resource.addEventListener("change", async () => {
        if (resource.value === "Other") {
          showCustomResource();
          return;
        }
        hideCustomResource();
        resource.disabled = true;
        updateSummary();
        try {
          const payload = await patchPass(position, { resource: resource.value });
          renderResource(row, payload);
          savedResource = resource.value;
          announce(`Pass ${position} resource saved.`);
        } catch (error) {
          resource.value = savedResource;
          announce(`Pass ${position} resource update failed: ${error.message}`);
        } finally {
          resource.disabled = false;
          updateSummary();
        }
      });

      addResource?.addEventListener("click", async () => {
        const name = customInput.value.trim();
        if (!name) {
          announce(`Pass ${position} resource name is required.`);
          customInput.focus();
          return;
        }
        checkbox.disabled = true;
        resource.disabled = true;
        customInput.disabled = true;
        addResource.disabled = true;
        updateSummary();
        try {
          const payload = await patchPass(position, { resource: name });
          ensureResourceOption(payload.resource);
          renderResource(row, payload);
          savedResource = resource.value;
          hideCustomResource();
          announce(`Pass ${position} resource saved.`);
        } catch (error) {
          resource.value = savedResource;
          announce(`Pass ${position} resource update failed: ${error.message}`);
        } finally {
          checkbox.disabled = false;
          resource.disabled = false;
          customInput.disabled = false;
          addResource.disabled = false;
          updateSummary();
        }
      });
    });

    updateSummary();
    addPass?.addEventListener("click", async () => {
      if (addPass.disabled) return;
      addPass.disabled = true;
      try {
        const response = await fetchImpl(`/api/lectures/${lectureId}/passes`, {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken(documentRef) },
          cache: "no-store",
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "Pass could not be added.");
        root.location.reload();
      } catch (error) {
        updateSummary();
        announce(`Add pass failed: ${error.message}`);
      }
    });
  };

  const initialize = (documentRef, fetchImpl = root.fetch.bind(root)) => {
    const match = root.location.pathname.match(/^\/lectures\/(\d+)/);
    if (!match) return;
    const lectureId = match[1];
    initializePassTracker(documentRef, fetchImpl, lectureId);
    let pollTimer;
    const basePollDelayMs = 2500;
    const maxPollDelayMs = 30000;
    let pollDelayMs = basePollDelayMs;

    const scheduleRefresh = (delay) => {
      pollTimer = root.setTimeout(() => {
        refresh().catch(handlePollError);
      }, delay);
    };

    const handlePollError = (error) => {
      documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
        const message = card.querySelector("[data-generation-message]");
        if (message) message.textContent = `${error.message} Retrying…`;
      });
      pollDelayMs = Math.min(pollDelayMs * 2, maxPollDelayMs);
      scheduleRefresh(pollDelayMs);
    };

    const refresh = async () => {
      const response = await fetchImpl(`/lectures/${lectureId}/generation-status`, {
        cache: "no-store",
      });
      const payload = await response.json();
      let active = false;
      documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
        active = render(card, payload[card.dataset.kind]) || active;
      });
      if (active) {
        pollDelayMs = basePollDelayMs;
        scheduleRefresh(pollDelayMs);
      }
    };

    documentRef.querySelectorAll("[data-generation-card]").forEach((card) => {
      card.querySelector("[data-generate]").addEventListener("click", async () => {
        const button = card.querySelector("[data-generate]");
        const message = card.querySelector("[data-generation-message]");
        button.disabled = true;
        message.textContent = "Adding this generation to the queue…";
        try {
          const response = await fetchImpl(
            `/lectures/${lectureId}/${card.dataset.kind}`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken(documentRef),
              },
              body: "{}",
              cache: "no-store",
            },
          );
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.detail || "Generation could not start.");
          }
          render(card, payload);
          root.clearTimeout(pollTimer);
          pollDelayMs = basePollDelayMs;
          scheduleRefresh(1000);
        } catch (error) {
          message.textContent = error.message;
          button.disabled = false;
        }
      });
    });
    void refresh().catch(handlePollError);
  };

  const api = { csrfToken, formatCompletedOn, initialize, localDateValue, render, runWhenReady };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    runWhenReady(
      root.document,
      () => initialize(root.document),
    );
  }
})(typeof globalThis === "undefined" ? this : globalThis);
