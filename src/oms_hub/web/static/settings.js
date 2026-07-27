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
    const payload = await response.json();
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
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Study Hub rejected the request.");
    }
    return payload;
  };

  const renderGoogleStatus = (card, status) => {
    const badge = card.querySelector("[data-google-badge]");
    const message = card.querySelector("[data-google-status]");
    const connected = status.state === "connected";
    const connecting = Boolean(status.connecting);

    badge.textContent = connecting ? "Connecting" : connected ? "Connected" : "Not connected";
    badge.classList.toggle("is-configured", connected);
    const surfaces = new Map(
      (status.surfaces || []).map((surface) => [surface.name, surface.state]),
    );
    card.querySelectorAll("[data-google-surface]").forEach((surface) => {
      const state = surfaces.get(surface.dataset.googleSurface) || "disconnected";
      surface.textContent = state.replaceAll("_", " ");
      surface.className = `status-pill status-${state}`;
    });

    if (connecting) {
      message.textContent = "Finish signing in using the browser window on this Study Hub device.";
    } else if (connected) {
      message.textContent = status.account_email
        ? `Google is connected as ${status.account_email}.`
        : "Google is connected.";
    } else if (status.message) {
      message.textContent = status.message;
    }
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
      card.querySelector("[data-save-prompt]").addEventListener("click", async () => {
        try {
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

    const googleCard = documentRef.querySelector("[data-google-card]");
    if (googleCard) {
      const connectButton = googleCard.querySelector("[data-google-connect]");
      const testButton = googleCard.querySelector("[data-google-test]");
      const saveClientButton = googleCard.querySelector("[data-google-save-client]");
      const clientInput = googleCard.querySelector("[data-google-oauth-client]");
      const message = googleCard.querySelector("[data-google-status]");
      let googlePollTimer;

      const refreshGoogle = () => getJson(fetchImpl, "/settings/google/status")
        .then((status) => {
          renderGoogleStatus(googleCard, status);
          if (status.state === "connecting") {
            googlePollTimer = root.setTimeout(refreshGoogle, 2000);
          }
        })
        .catch((error) => {
          message.textContent = error.message;
        });
      void refreshGoogle();

      saveClientButton.addEventListener("click", async () => {
        const selected = clientInput.files[0];
        if (!selected) {
          message.textContent = "Choose the OAuth client JSON file first.";
          return;
        }
        saveClientButton.disabled = true;
        const form = new FormData();
        form.append("client_file", selected);
        try {
          const response = await fetchImpl("/settings/google/oauth-client", {
            method: "POST",
            headers: { "X-CSRF-Token": token() },
            body: form,
            cache: "no-store",
          });
          const result = await response.json();
          if (!response.ok) {
            throw new Error(result.detail || "Study Hub rejected the client file.");
          }
          message.textContent = "OAuth client file saved securely.";
        } catch (error) {
          message.textContent = error.message;
        } finally {
          saveClientButton.disabled = false;
        }
      });

      connectButton.addEventListener("click", async () => {
        connectButton.disabled = true;
        try {
          const status = await postJson(
            fetchImpl,
            "/settings/google/connect",
            {},
            token(),
          );
          renderGoogleStatus(googleCard, status);
          root.clearTimeout(googlePollTimer);
          googlePollTimer = root.setTimeout(refreshGoogle, 1500);
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
            "/settings/google/test",
            {},
            token(),
          );
          renderGoogleStatus(googleCard, status);
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
  };

  const api = {
    csrfToken,
    diagnosticLines,
    getJson,
    initialize,
    postJson,
    renderGoogleStatus,
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
