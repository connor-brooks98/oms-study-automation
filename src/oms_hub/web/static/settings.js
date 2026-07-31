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

  const togglePassword = (input, button, providerLabel) => {
    const revealing = input.type === "password";
    input.type = revealing ? "text" : "password";
    button.textContent = revealing ? "Hide" : "Show";
    button.setAttribute(
      "aria-label",
      `${revealing ? "Hide" : "Show"} ${providerLabel} credential`,
    );
  };

  const testPresentation = (state) => {
    if (state === "testing") {
      return { label: "Testing…", className: "is-testing" };
    }
    if (state === "connected") {
      return { label: "Connected", className: "is-connected" };
    }
    if (state === "failed") {
      return { label: "Connection failed", className: "is-failed" };
    }
    return { label: "Test connection", className: "" };
  };

  const sourceLabels = {
    study_hub: "Study Hub issue",
    network: "Network issue",
    provider_authentication: "Provider authentication issue",
    provider_model: "Provider model issue",
    provider_quota: "Provider quota issue",
    provider_service: "Provider service issue",
  };

  const diagnosticLines = (diagnostic, correlationId) => {
    if (!diagnostic) return [];
    const lines = [
      sourceLabels[diagnostic.source] || "Connection issue",
      diagnostic.message,
    ];
    if (diagnostic.http_status) lines.push(`HTTP ${diagnostic.http_status}`);
    if (diagnostic.next_action) lines.push(diagnostic.next_action);
    if (correlationId) lines.push(`Study Hub reference: ${correlationId}`);
    return lines;
  };

  const postJson = async (fetchImpl, url, body, token) => {
    const response = await fetchImpl(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": token,
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "Study Hub rejected the request.");
    }
    return payload;
  };

  const getJson = async (fetchImpl, url) => {
    const response = await fetchImpl(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "Study Hub rejected the request.");
    }
    return payload;
  };

  const renderNotebookStatus = (card, status) => {
    const badge = card.querySelector("[data-notebook-badge]");
    const message = card.querySelector("[data-notebook-status]");
    const connected = status.state === "connected";
    const connecting = status.state === "connecting";

    badge.textContent = connecting ? "Connecting" : connected ? "Connected" : "Not connected";
    badge.classList.toggle("is-configured", connected);

    if (connecting) {
      message.textContent = status.message
        || "Finish signing in using the browser window on this Study Hub device.";
    } else if (connected) {
      message.textContent = "Gemini Notebook is connected.";
    } else if (status.message) {
      message.textContent = status.message;
    }
  };

  const promptPathAction = (value) => (
    String(value || "").trim() ? "save" : "select"
  );

  const setTestState = (button, state) => {
    const presentation = testPresentation(state);
    button.classList.remove("is-testing", "is-connected", "is-failed");
    if (presentation.className) button.classList.add(presentation.className);
    button.textContent = presentation.label;
  };

  const renderDiagnostic = (container, diagnostic, correlationId) => {
    container.replaceChildren();
    const lines = diagnosticLines(diagnostic, correlationId);
    lines.forEach((line, index) => {
      const paragraph = container.ownerDocument.createElement("p");
      paragraph.textContent = line;
      if (index === 0) paragraph.className = "diagnostic-title";
      container.append(paragraph);
    });
    container.hidden = lines.length === 0;
  };

  const runWhenReady = (documentRef, callback) => {
    if (documentRef.readyState === "loading") {
      documentRef.addEventListener("DOMContentLoaded", callback, { once: true });
      return;
    }
    callback();
  };

  const initialize = (documentRef, fetchImpl) => {
    const token = () => csrfToken(documentRef);
    documentRef.querySelectorAll("[data-provider-card]").forEach((card) => {
      const provider = card.dataset.provider;
      const providerLabel = card.querySelector("h3").textContent.trim();
      const credential = card.querySelector("[data-credential-input]");
      const toggle = card.querySelector("[data-toggle-password]");
      const configured = card.querySelector("[data-configured-state]");
      const message = card.querySelector("[data-provider-message]");
      const diagnostic = card.querySelector("[data-diagnostic]");
      const testButton = card.querySelector("[data-test-connection]");
      const model = card.querySelector("[data-model-input]");

      toggle.addEventListener("click", () => {
        togglePassword(credential, toggle, providerLabel);
      });

      card.querySelector("[data-save-credential]").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        message.textContent = "Saving credential…";
        try {
          const result = await postJson(
            fetchImpl,
            `/settings/ai/${provider}/credential`,
            { credential: credential.value },
            token(),
          );
          credential.value = "";
          credential.type = "password";
          toggle.textContent = "Show";
          configured.textContent = result.configured ? "Configured" : "Not configured";
          configured.classList.toggle("is-configured", result.configured);
          message.textContent = result.configured
            ? "Credential saved securely."
            : "No credential is configured.";
          setTestState(testButton, "neutral");
          renderDiagnostic(diagnostic, null, null);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });

      card.querySelector("[data-save-model]").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        message.textContent = "Saving model…";
        try {
          const result = await postJson(
            fetchImpl,
            `/settings/ai/${provider}/model`,
            { model: model.value },
            token(),
          );
          model.value = result.model;
          message.textContent = "Model saved.";
          setTestState(testButton, "neutral");
          renderDiagnostic(diagnostic, null, null);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });

      testButton.addEventListener("click", async () => {
        testButton.disabled = true;
        setTestState(testButton, "testing");
        message.textContent = "Testing the selected model with a small request…";
        renderDiagnostic(diagnostic, null, null);
        try {
          const result = await postJson(
            fetchImpl,
            `/settings/ai/${provider}/test`,
            {},
            token(),
          );
          setTestState(testButton, result.state);
          message.textContent = result.state === "connected"
            ? "Provider and model are ready."
            : "The connection test found a problem.";
          renderDiagnostic(
            diagnostic,
            result.diagnostic,
            result.correlation_id,
          );
        } catch (error) {
          setTestState(testButton, "failed");
          message.textContent = error.message;
        } finally {
          testButton.disabled = false;
        }
      });
    });

    documentRef.querySelectorAll("[data-prompt-card]").forEach((card) => {
      const kind = card.dataset.prompt;
      const input = card.querySelector("[data-prompt-path]");
      const message = card.querySelector("[data-prompt-message]");
      const pathButton = card.querySelector("[data-save-prompt]");
      const updatePromptAction = () => {
        pathButton.textContent = promptPathAction(input.value) === "select"
          ? "Select Path"
          : "Save Path";
      };
      input.addEventListener("input", updatePromptAction);
      pathButton.addEventListener("click", async () => {
        try {
          if (promptPathAction(input.value) === "select") {
            message.textContent = "Choose the Obsidian prompt on the NUC…";
            const result = await postJson(
              fetchImpl,
              `/settings/generation/prompts/${kind}/select`,
              {},
              token(),
            );
            if (result.selected) {
              input.value = result.path;
              updatePromptAction();
              message.textContent = "Path selected. Click Save Path to keep it.";
            } else {
              message.textContent = "No prompt file was selected.";
            }
            return;
          }
          await postJson(
            fetchImpl,
            `/settings/generation/prompts/${kind}`,
            { path: input.value },
            token(),
          );
          message.textContent = "Prompt path saved.";
        } catch (error) {
          message.textContent = error.message;
        }
      });
      card.querySelector("[data-test-prompt]").addEventListener("click", async () => {
        try {
          const result = await postJson(
            fetchImpl,
            `/settings/generation/prompts/${kind}/test`,
            {},
            token(),
          );
          message.textContent = result.state === "valid"
            ? "Prompt file is ready."
            : result.message;
        } catch (error) {
          message.textContent = error.message;
        }
      });
    });

    const notebookCard = documentRef.querySelector("[data-notebook-card]");
    if (notebookCard) {
      const connectButton = notebookCard.querySelector("[data-notebook-connect]");
      const testButton = notebookCard.querySelector("[data-notebook-test]");
      const message = notebookCard.querySelector("[data-notebook-status]");
      let notebookPollTimer;

      const refreshNotebook = () => getJson(fetchImpl, "/settings/notebook/status")
        .then((status) => {
          renderNotebookStatus(notebookCard, status);
          if (status.state === "connecting") {
            notebookPollTimer = root.setTimeout(refreshNotebook, 2000);
          }
        })
        .catch((error) => {
          message.textContent = error.message;
        });
      void refreshNotebook();

      connectButton.addEventListener("click", async () => {
        connectButton.disabled = true;
        try {
          const status = await postJson(
            fetchImpl,
            "/settings/notebook/connect",
            {},
            token(),
          );
          renderNotebookStatus(notebookCard, status);
          root.clearTimeout(notebookPollTimer);
          notebookPollTimer = root.setTimeout(refreshNotebook, 1500);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          connectButton.disabled = false;
        }
      });

      testButton.addEventListener("click", async () => {
        testButton.disabled = true;
        try {
          const status = await postJson(
            fetchImpl,
            "/settings/notebook/test",
            {},
            token(),
          );
          renderNotebookStatus(notebookCard, status);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          testButton.disabled = false;
        }
      });
    }

    const active = documentRef.querySelector("[data-active-provider]");
    const saveActive = documentRef.querySelector("[data-save-active]");
    const activeMessage = documentRef.querySelector("[data-active-message]");
    if (active && saveActive) {
      saveActive.addEventListener("click", async () => {
        saveActive.disabled = true;
        activeMessage.textContent = "Updating active provider…";
        try {
          const result = await postJson(
            fetchImpl,
            "/settings/ai/active",
            { provider: active.value },
            token(),
          );
          active.value = result.provider;
          activeMessage.textContent = "New transcripts will use this provider.";
        } catch (error) {
          activeMessage.textContent = error.message;
        } finally {
          saveActive.disabled = false;
        }
      });
    }

    const trackerInput = documentRef.querySelector("[data-tracker-input]");
    const trackerZone = documentRef.querySelector("[data-tracker-dropzone]");
    if (trackerInput && trackerZone) {
      const selection = trackerZone.querySelector("[data-tracker-selection]");
      const showTracker = () => {
        selection.textContent = trackerInput.files?.[0]?.name || "XLSX only";
      };
      trackerZone.querySelector("[data-tracker-browse]").addEventListener(
        "click",
        () => trackerInput.click(),
      );
      trackerInput.addEventListener("change", showTracker);
      ["dragenter", "dragover"].forEach((name) => trackerZone.addEventListener(
        name,
        (event) => { event.preventDefault(); trackerZone.classList.add("is-dragging"); },
      ));
      ["dragleave", "drop"].forEach((name) => trackerZone.addEventListener(
        name,
        (event) => { event.preventDefault(); trackerZone.classList.remove("is-dragging"); },
      ));
      trackerZone.addEventListener("drop", (event) => {
        trackerInput.files = event.dataTransfer.files;
        showTracker();
      });
    }
  };

  const api = {
    csrfToken,
    diagnosticLines,
    getJson,
    initialize,
    postJson,
    promptPathAction,
    renderNotebookStatus,
    runWhenReady,
    testPresentation,
    togglePassword,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root.document) {
    runWhenReady(root.document, () => {
      initialize(root.document, root.fetch.bind(root));
    });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
