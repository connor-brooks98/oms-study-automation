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
    provider_request: "Provider request issue",
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

  const postJson = async (fetchImpl, url, body, token, method = "POST") => {
    const response = await fetchImpl(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": token,
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
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
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(payload.detail || "Study Hub rejected the request.");
    }
    return payload;
  };

  const CUSTOM_MODEL_VALUE = "__custom__";

  const modelOptionValues = (models, currentModel) => {
    const list = Array.isArray(models) ? models.map((model) => String(model)) : [];
    const unique = [...new Set(list)];
    const current = String(currentModel || "").trim();
    if (current && !unique.includes(current)) {
      return [current, ...unique];
    }
    return unique;
  };

  const syncCustomModelVisibility = (select, customInput) => {
    customInput.hidden = select.value !== CUSTOM_MODEL_VALUE;
  };

  const populateModelSelect = (documentRef, select, customInput, models, currentModel) => {
    const values = modelOptionValues(models, currentModel);
    select.replaceChildren();
    values.forEach((value) => {
      const option = documentRef.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    });
    const customOption = documentRef.createElement("option");
    customOption.value = CUSTOM_MODEL_VALUE;
    customOption.textContent = "Custom model ID…";
    select.append(customOption);

    const current = String(currentModel || "").trim();
    if (current && values.includes(current)) {
      select.value = current;
    } else if (values.length) {
      select.value = values[0];
    } else {
      select.value = CUSTOM_MODEL_VALUE;
      customInput.value = current;
    }
    syncCustomModelVisibility(select, customInput);
  };

  const resolvedModelValue = (select, customInput) => (
    select.value === CUSTOM_MODEL_VALUE ? customInput.value.trim() : select.value
  );

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

  const promptRoutes = (kind) => ({
    select: `/settings/generation/prompts/${kind}/select`,
    save: `/settings/generation/prompts/${kind}`,
    test: `/settings/generation/prompts/${kind}/test`,
  });

  const catalogMessage = (result) => {
    const count = Number(result?.choice_count || 0);
    const issues = Array.isArray(result?.issues) ? result.issues : [];
    const choiceText = `${count} prompt ${count === 1 ? "choice is" : "choices are"} ready.`;
    if (!issues.length) return choiceText;
    const warning = issues.map((item) => item.message).join(" · ");
    return `${choiceText} ${issues.length} ${issues.length === 1 ? "warning" : "warnings"}: ${warning}`;
  };

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
      const model = card.querySelector("[data-model-select]");
      const customModel = card.querySelector("[data-model-custom]");
      const initialModel = model.value;
      let fetchedModels = [];

      const refreshModelOptions = (currentModel) => {
        populateModelSelect(documentRef, model, customModel, fetchedModels, currentModel);
      };

      model.addEventListener("change", () => syncCustomModelVisibility(model, customModel));

      void getJson(fetchImpl, `/api/settings/providers/${provider}/models`)
        .then((result) => {
          fetchedModels = Array.isArray(result.models) ? result.models : [];
          refreshModelOptions(initialModel);
        })
        .catch(() => {
          refreshModelOptions(initialModel);
        });

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
            { model: resolvedModelValue(model, customModel) },
            token(),
          );
          refreshModelOptions(result.model);
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

    documentRef.querySelectorAll("[data-assignment-row]").forEach((row) => {
      const task = row.dataset.task;
      const providerSelect = row.querySelector("[data-assignment-provider]");
      const modelSelect = row.querySelector("[data-assignment-model]");
      const customModel = row.querySelector("[data-assignment-custom]");
      const saveButton = row.querySelector("[data-save-assignment]");
      const keyState = row.querySelector("[data-assignment-key]");
      const message = row.querySelector("[data-assignment-message]");
      const gate = row.querySelector("[data-openrouter-gate]");
      const initialModel = modelSelect.value;
      let fetchedModels = [];

      const controls = [providerSelect, modelSelect, customModel, saveButton, gate]
        .filter(Boolean);
      const setBusy = (busy) => {
        controls.forEach((control) => {
          control.disabled = busy;
        });
      };

      const refreshModelOptions = (currentModel) => {
        populateModelSelect(documentRef, modelSelect, customModel, fetchedModels, currentModel);
      };

      const loadModelsForProvider = async (provider, currentModel) => {
        try {
          const result = await getJson(fetchImpl, `/api/settings/providers/${provider}/models`);
          fetchedModels = Array.isArray(result.models) ? result.models : [];
        } catch (error) {
          fetchedModels = [];
          message.textContent = error.message;
        }
        refreshModelOptions(currentModel);
      };

      modelSelect.addEventListener("change", () => {
        syncCustomModelVisibility(modelSelect, customModel);
      });

      providerSelect.addEventListener("change", () => {
        void loadModelsForProvider(providerSelect.value, "");
      });

      saveButton.addEventListener("click", async () => {
        setBusy(true);
        message.textContent = "Saving assignment…";
        try {
          const result = await postJson(
            fetchImpl,
            `/api/settings/task-assignments/${task}`,
            {
              provider: providerSelect.value,
              model: resolvedModelValue(modelSelect, customModel),
            },
            token(),
            "PUT",
          );
          keyState.textContent = result.key_configured
            ? "Key configured"
            : "Key not configured";
          keyState.classList.toggle("is-configured", result.key_configured);
          refreshModelOptions(result.model);
          message.textContent = "Assignment saved.";
        } catch (error) {
          message.textContent = error.message;
        } finally {
          setBusy(false);
        }
      });

      if (gate) {
        gate.addEventListener("change", async () => {
          try {
            const result = await postJson(
              fetchImpl,
              "/settings/ai/openrouter/gate",
              { enabled: gate.checked },
              token(),
            );
            gate.checked = result.enabled;
            message.textContent = result.enabled
              ? "Publication will wait for medical review."
              : "Medical review gate disabled.";
          } catch (error) {
            gate.checked = !gate.checked;
            message.textContent = error.message;
          }
        });
      }

      void loadModelsForProvider(providerSelect.value, initialModel);
    });

    documentRef.querySelectorAll("[data-prompt-card]").forEach((card) => {
      const kind = card.dataset.prompt;
      const routes = promptRoutes(kind);
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
        pathButton.disabled = true;
        try {
          if (promptPathAction(input.value) === "select") {
            message.textContent = "Choose the Obsidian prompt on the NUC…";
            const result = await postJson(
              fetchImpl,
              routes.select,
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
            routes.save,
            { path: input.value },
            token(),
          );
          message.textContent = "Prompt path saved.";
        } catch (error) {
          message.textContent = error.message;
        } finally {
          pathButton.disabled = false;
        }
      });
      const testPromptButton = card.querySelector("[data-test-prompt]");
      testPromptButton.addEventListener("click", async () => {
        testPromptButton.disabled = true;
        try {
          const result = await postJson(
            fetchImpl,
            routes.test,
            {},
            token(),
          );
          message.textContent = result.state === "valid"
            ? "Prompt file is ready."
            : result.message;
        } catch (error) {
          message.textContent = error.message;
        } finally {
          testPromptButton.disabled = false;
        }
      });
    });

    const ankiPromptCard = documentRef.querySelector("[data-anki-prompt-directory]");
    if (ankiPromptCard) {
      const input = ankiPromptCard.querySelector("[data-anki-prompt-directory-path]");
      const action = ankiPromptCard.querySelector("[data-save-anki-prompt-directory]");
      const testButton = ankiPromptCard.querySelector("[data-test-anki-prompt-directory]");
      const message = ankiPromptCard.querySelector("[data-anki-prompt-directory-message]");
      const updateAction = () => {
        action.textContent = promptPathAction(input.value) === "select"
          ? "Select Folder"
          : "Save Path";
      };
      input.addEventListener("input", updateAction);
      action.addEventListener("click", async () => {
        action.disabled = true;
        try {
          if (promptPathAction(input.value) === "select") {
            const result = await postJson(
              fetchImpl,
              "/settings/anki/prompts/directory/select",
              {},
              token(),
            );
            if (!result.selected) {
              message.textContent = "No prompt folder was selected.";
              return;
            }
            input.value = result.path;
            updateAction();
            message.textContent = "Folder selected. Click Save Path to keep it.";
            return;
          }
          await postJson(
            fetchImpl,
            "/settings/anki/prompts/directory",
            { path: input.value },
            token(),
          );
          message.textContent = "Anki prompt directory saved.";
        } catch (error) {
          message.textContent = error.message;
        } finally {
          action.disabled = false;
        }
      });
      testButton.addEventListener("click", async () => {
        testButton.disabled = true;
        try {
          const result = await postJson(
            fetchImpl,
            "/settings/anki/prompts/directory/test",
            {},
            token(),
          );
          message.textContent = catalogMessage(result);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          testButton.disabled = false;
        }
      });
    }

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

  };

  const api = {
    catalogMessage,
    csrfToken,
    diagnosticLines,
    getJson,
    initialize,
    modelOptionValues,
    populateModelSelect,
    postJson,
    promptPathAction,
    promptRoutes,
    renderNotebookStatus,
    resolvedModelValue,
    runWhenReady,
    syncCustomModelVisibility,
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
