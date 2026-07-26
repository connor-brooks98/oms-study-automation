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
    initialize,
    postJson,
    testPresentation,
    togglePassword,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", () => {
      initialize(root.document, root.fetch.bind(root));
    });
  }
})(typeof globalThis === "undefined" ? this : globalThis);
