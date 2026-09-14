const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.textContent = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, value) { this.events[key] = value; }
}
function setup(payload, ok = true) {
  const calls = [];
  const window = {};
  vm.runInNewContext(fs.readFileSync('src/oms_hub/web/static/quiz_sources.js', 'utf8'), {
    window, document: {createElement: (tag) => new Element(tag)},
    fetch: async (...args) => { calls.push(args); return {ok, json: async () => payload}; },
  });
  const container = new Element('div');
  const details = window.StudyHubQuizSources.mount(container, {sessionId: 'session', attemptId: 'attempt'});
  return {calls, details, body: details.children[1]};
}
test('sources load only on expansion and text is escaped by DOM textContent', async () => {
  const {calls, details, body} = setup({available: true, sources: [{
    title: '<script>title</script>', locator: 'Slide 2', excerpt: '<img onerror=bad>',
    original_url: '/study/sessions/session/sources/attempt/0/original', link_label: 'Download source',
  }]});
  assert.equal(calls.length, 0);
  details.open = true;
  await details.events.toggle();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(body.children[0].children[1].textContent, '<img onerror=bad>');
  assert.equal(body.children[0].children[2].rel, 'noopener');
  await details.events.toggle();
  assert.equal(calls.length, 1);
});
test('external and sibling-attempt links are never rendered', async () => {
  for (const url of ['javascript:alert(1)', 'https://example.com', '/study/sessions/session/sources/other/0/original']) {
    const {details, body} = setup({available: true, sources: [{title:'Source', locator:'Block 1', excerpt:'Text', original_url:url}]});
    details.open = true;
    await details.events.toggle();
    assert.equal(body.children[0].children.length, 2);
  }
});
test('unavailable and failed responses have honest messages without changing answers', async () => {
  for (const ok of [true, false]) {
    const {details, body} = setup({available: false, message:'Source preview unavailable for this quiz.'}, ok);
    details.open = true;
    await details.events.toggle();
    assert.match(body.textContent, /Source preview unavailable/);
  }
});
