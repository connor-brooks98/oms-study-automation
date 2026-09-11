const test = require('node:test');
const assert = require('node:assert/strict');
const chat = require('../../src/oms_hub/web/static/study_chat.js');

function element() {
  return { children: [], dataset: {}, value: '', disabled: false, hidden: false,
    textContent: '', listeners: {}, selectedOptions: [],
    addEventListener(type, handler) { this.listeners[type] = handler; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    focus() {}, setAttribute() {},
  };
}

function page() {
  const controls = Object.fromEntries(['form', 'mode', 'sources', 'source-field', 'reference',
    'question', 'send', 'cancel', 'clear', 'status', 'messages'].map(key => [`[data-chat-${key}]`, element()]));
  controls['[data-chat-mode]'].value = 'general';
  const root = element();
  root.querySelector = selector => controls[selector];
  const documentRef = { cookie: 'study_hub_csrf=safe-token', createElement: element,
    querySelector: () => root };
  return { controls, root, documentRef };
}

test('renders model markup as text and only accepts server citation paths', () => {
  const { documentRef, controls } = page();
  const container = controls['[data-chat-messages]'];
  chat.renderRequest(documentRef, container, { request_id: 'r1', question: '<img src=x>',
    answer: { text: '<script>steal()</script>' }, state: 'completed', citations: [
      { label: '<b>source</b>', url: 'javascript:steal()' },
      { label: 'source', url: '/study/chat/requests/123/citations/abc' },
    ] });
  const text = JSON.stringify(container.children);
  assert.match(text, /<script>steal/);
  assert.doesNotMatch(text, /"innerHTML"/);
  assert.doesNotMatch(text, /javascript:/);
});

test('keyboard sends one request with CSRF and Escape cancels without retry', async () => {
  const { documentRef, controls } = page();
  const calls = [];
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const fetchImpl = async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith('/conversations')) return { ok: true, json: async () => ({ conversation_id: 'cid' }) };
    if (url.endsWith('/answer')) return pending;
    return { ok: true, json: async () => ({ state: 'interrupted' }) };
  };
  const controller = chat.initialize(documentRef, fetchImpl);
  controls['[data-chat-question]'].value = 'hello';
  const event = { key: 'Enter', ctrlKey: true, preventDefault() {} };
  const submit = controls['[data-chat-question]'].listeners.keydown(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(([url]) => url.endsWith('/answer')).length, 1);
  assert.equal(calls[1][1].headers['X-CSRF-Token'], 'safe-token');
  assert.equal(JSON.parse(calls[1][1].body).owner_id, undefined);
  await controls['[data-chat-question]'].listeners.keydown({ key: 'Escape', preventDefault() {} });
  assert.equal(calls.filter(([url]) => url.endsWith('/cancel')).length, 1);
  finish({ ok: true, json: async () => ({ request_id: 'r', question: 'hello', state: 'interrupted',
    answer: { text: 'Interrupted' }, citations: [] }) });
  await submit;
  controller.dispose();
  assert.equal(calls.filter(([url]) => url.endsWith('/answer')).length, 1);
});

test('source mode changes clear local context and medical mode is explicit', () => {
  const { documentRef, controls } = page();
  const controller = chat.initialize(documentRef, async () => { throw Error('unexpected fetch'); });
  controls['[data-chat-mode]'].value = 'medical_reference';
  controls['[data-chat-mode]'].listeners.change();
  assert.equal(controls['[data-chat-reference]'].hidden, false);
  assert.equal(controls['[data-chat-source-field]'].hidden, true);
  controller.dispose();
});

test('a rejected unstarted answer unlocks controls without polling forever or resending', async () => {
  const { documentRef, controls } = page();
  let calls = 0;
  const controller = chat.initialize(documentRef, async url => {
    calls++;
    if (url.endsWith('/conversations')) return { ok: true, json: async () => ({ conversation_id: 'cid' }) };
    return { ok: false, status: 409 };
  });
  controls['[data-chat-question]'].value = 'hello';
  await controls['[data-chat-form]'].listeners.submit({ preventDefault() {} });
  assert.equal(controls['[data-chat-send]'].disabled, false);
  assert.equal(calls, 2);
  controller.dispose();
});

test('reload waits for durable history and never resends an active request', async () => {
  const { documentRef, controls, root } = page();
  root.dataset.conversationId = 'cid';
  let calls = 0;
  const controller = chat.initialize(documentRef, async () => {
    calls++;
    return { ok: true, json: async () => ({ requests: [
      { request_id: 'rid', question: 'saved', state: 'running', answer: null },
    ] }) };
  });
  assert.equal(controls['[data-chat-send]'].disabled, true);
  await controller.ready;
  assert.equal(controls['[data-chat-cancel]'].disabled, false);
  assert.equal(calls, 1);
  controller.dispose();
});

for (const outcome of ['success', 'rejected', 'network error']) {
  test(`delayed A POST ${outcome} cannot disturb B after polling completes A`, async t => {
    const timers = new Map();
    let timerId = 0;
    t.mock.method(globalThis, 'setInterval', callback => {
      timers.set(++timerId, callback);
      return timerId;
    });
    t.mock.method(globalThis, 'clearInterval', id => timers.delete(id));
    const { documentRef, controls } = page();
    const posts = [], cancelled = [];
    const response = body => ({ ok: true, json: async () => body });
    const fetchImpl = async (url, options) => {
      if (url.endsWith('/conversations')) return response({ conversation_id: 'cid' });
      if (url.endsWith('/answer')) {
        return new Promise((resolve, reject) => posts.push({ body: JSON.parse(options.body), resolve, reject }));
      }
      if (url.endsWith('/cancel')) { cancelled.push(url); return response({ state: 'interrupted' }); }
      return response({ ...posts[0].body, state: 'completed', answer: { text: 'Answer A' }, citations: [] });
    };
    const controller = chat.initialize(documentRef, fetchImpl);
    try {
      const submit = controls['[data-chat-form]'].listeners.submit;
      controls['[data-chat-question]'].value = 'Question A';
      const sendingA = submit({ preventDefault() {} });
      await new Promise(resolve => setImmediate(resolve));
      await [...timers.values()][0]();
      assert.equal(controls['[data-chat-send]'].disabled, false);

      controls['[data-chat-question]'].value = 'Question B';
      const sendingB = submit({ preventDefault() {} });
      await new Promise(resolve => setImmediate(resolve));
      const bStatus = controls['[data-chat-status]'].textContent;
      const bTimer = [...timers.keys()][0];
      if (outcome === 'success') posts[0].resolve(response({ ...posts[0].body, state: 'completed', answer: { text: 'Late A' } }));
      else if (outcome === 'rejected') posts[0].resolve({ ok: false, status: 409 });
      else posts[0].reject(new Error('delayed connection failure'));
      await sendingA;

      assert.equal(controls['[data-chat-send]'].disabled, true);
      assert.equal(controls['[data-chat-cancel]'].disabled, false);
      assert.equal(controls['[data-chat-question]'].value, 'Question B');
      assert.equal(controls['[data-chat-status]'].textContent, bStatus);
      assert.deepEqual([...timers.keys()], [bTimer]);
      assert.equal(controls['[data-chat-messages]'].children[0].children[1].textContent, 'Answer A');
      await controls['[data-chat-cancel]'].listeners.click();
      assert.deepEqual(cancelled, [`/study/chat/requests/${posts[1].body.request_id}/cancel`]);

      posts[1].resolve(response({ ...posts[1].body, state: 'interrupted', answer: { text: 'Stopped B' } }));
      await sendingB;
      assert.equal(posts.length, 2);
    } finally { controller.dispose(); }
  });
}
