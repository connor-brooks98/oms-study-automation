(function () {
  'use strict';

  function renderRequest(documentRef, container, row) {
    let article = Array.from(container.children).find(item => item.dataset.requestId === row.request_id);
    if (!article) {
      article = documentRef.createElement('article');
      article.className = 'sh-card chat-message';
      article.dataset.requestId = row.request_id;
      container.append(article);
    }
    const question = documentRef.createElement('h2');
    question.textContent = row.question;
    const answer = documentRef.createElement('p');
    answer.className = 'chat-answer';
    answer.textContent = row.answer ? row.answer.text : 'Answer pending…';
    const citations = documentRef.createElement('p');
    for (const citation of row.citations || []) {
      if (!/^\/study\/chat\/requests\/[a-f0-9-]{36}\/citations\/[a-f0-9]{64}$/.test(citation.url)) continue;
      const link = documentRef.createElement('a');
      link.href = citation.url;
      link.textContent = citation.label;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      citations.append(link, documentRef.createElement('br'));
    }
    article.replaceChildren(question, answer, citations);
  }

  function initialize(documentRef, fetchImpl) {
    const root = documentRef.querySelector('[data-study-chat]');
    if (!root) return null;
    const field = name => root.querySelector(`[data-chat-${name}]`);
    const form = field('form'), mode = field('mode'), sources = field('sources');
    const question = field('question'), status = field('status'), messages = field('messages');
    let conversationId = root.dataset.conversationId || null;
    let activeRequest = null, busy = false, pollTimer = null;
    let polling = false;

    function setBusy(value) {
      busy = value;
      for (const name of ['mode', 'sources', 'send', 'clear']) field(name).disabled = value;
      field('cancel').disabled = !value || !activeRequest;
      form.setAttribute('aria-busy', String(value));
    }
    function stopPolling() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
    }
    function remember() {
      if (typeof window === 'undefined') return;
      const url = new URL(window.location.href);
      if (conversationId) url.searchParams.set('conversation_id', conversationId);
      else url.searchParams.delete('conversation_id');
      window.history.replaceState(null, '', url);
    }
    async function api(path, body, method = 'POST') {
      const cookie = documentRef.cookie.split(';').map(item => item.trim())
        .find(item => item.startsWith('study_hub_csrf='));
      const headers = { 'X-CSRF-Token': cookie ? decodeURIComponent(cookie.slice(15)) : '' };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const response = await fetchImpl(`/study/chat${path}`, {
        method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) {
        const error = new Error('The request could not be completed. Check your access, selected sources and current request.');
        error.status = response.status;
        throw error;
      }
      return response.json();
    }
    function display(row) {
      renderRequest(documentRef, messages, row);
      if (['completed', 'failed', 'interrupted'].includes(row.state)) {
        stopPolling();
        activeRequest = null;
        setBusy(false);
        status.textContent = row.answer?.status === 'unavailable' ? row.answer.text :
          row.state === 'completed' ? 'Answer saved.' : 'Request stopped. No automatic retry was sent.';
      } else {
        status.textContent = row.provider_phase === 'completed' ? 'Checking the answer and its sources…' : 'Answer in progress…';
      }
    }
    function startPolling() {
      stopPolling();
      pollTimer = setInterval(async () => {
        if (!activeRequest || polling) return;
        polling = true;
        const identity = activeRequest;
        try {
          const row = await api(`/requests/${identity}`, undefined, 'GET');
          if (activeRequest === identity) display(row);
        } catch (_) {
          status.textContent = 'Waiting for saved request state. No automatic retry was sent.';
        } finally { polling = false; }
      }, 1500);
    }
    function scopeChanged(reset = true) {
      field('source-field').hidden = mode.value !== 'lecture';
      sources.required = mode.value === 'lecture';
      field('reference').hidden = mode.value !== 'medical_reference';
      if (reset && !busy) {
        conversationId = null;
        messages.replaceChildren();
        remember();
        status.textContent = 'The next question starts a new conversation in this mode and source scope.';
      }
    }
    async function send(event) {
      event?.preventDefault();
      if (busy || !question.value.trim()) return;
      const selected = mode.value === 'lecture' ? Array.from(sources.selectedOptions, item => Number(item.value)) : [];
      if (mode.value === 'lecture' && !selected.length) {
        status.textContent = 'Select at least one lecture source.';
        return;
      }
      setBusy(true);
      status.textContent = 'Preparing the question…';
      let identity = null;
      try {
        if (!conversationId) {
          conversationId = (await api('/conversations', { mode: mode.value, revision_ids: selected })).conversation_id;
          remember();
        }
        identity = globalThis.crypto.randomUUID();
        activeRequest = identity;
        field('cancel').disabled = false;
        const body = { request_id: activeRequest, conversation_id: conversationId, question: question.value };
        renderRequest(documentRef, messages, { ...body, state: 'pending', answer: null });
        startPolling();
        const row = await api('/answer', body);
        if (activeRequest !== identity) return;
        display(row);
        question.value = '';
        question.focus();
      } catch (error) {
        if (activeRequest !== identity) return;
        if ([400, 401, 403, 409, 422].includes(error.status)) {
          stopPolling();
          activeRequest = null;
          setBusy(false);
          status.textContent = error.message;
          return;
        }
        status.textContent = activeRequest ? 'The connection was interrupted. Checking the saved request; no retry was sent.' :
          'The conversation could not be created. Check your access and selected sources.';
        if (!activeRequest) setBusy(false);
      }
    }
    async function cancel() {
      if (!activeRequest) return;
      try {
        await api(`/requests/${activeRequest}/cancel`);
        status.textContent = 'Cancellation saved. Waiting for the request to stop…';
      } catch (_) {
        status.textContent = 'Could not confirm cancellation. Check the saved request before trying again.';
      }
    }
    async function clear() {
      if (busy) return;
      try {
        if (conversationId) await api(`/conversations/${conversationId}/clear`);
        conversationId = null;
        messages.replaceChildren();
        remember();
        status.textContent = 'Conversation cleared.';
      } catch (_) { status.textContent = 'The conversation could not be cleared.'; }
    }
    form.addEventListener('submit', send);
    mode.addEventListener('change', () => scopeChanged());
    sources.addEventListener('change', () => scopeChanged());
    field('cancel').addEventListener('click', cancel);
    field('clear').addEventListener('click', clear);
    question.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) return send(event);
      if (event.key === 'Escape' && activeRequest) { event.preventDefault(); return cancel(); }
    });
    scopeChanged(false);
    if (conversationId) setBusy(true);
    const ready = conversationId ? api(`/conversations/${conversationId}`, undefined, 'GET').then(data => {
      for (const row of data.requests) {
        renderRequest(documentRef, messages, row);
        if (['pending', 'running'].includes(row.state)) activeRequest = row.request_id;
      }
      if (activeRequest) { setBusy(true); startPolling(); }
      else setBusy(false);
    }).catch(() => {
      setBusy(false);
      status.textContent = 'This conversation could not be loaded.';
    }) : Promise.resolve();
    return { ready, dispose: stopPolling };
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { initialize, renderRequest };
  if (typeof document !== 'undefined') initialize(document, globalThis.fetch.bind(globalThis));
}());
