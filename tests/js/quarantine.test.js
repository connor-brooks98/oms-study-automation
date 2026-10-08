const assert = require('node:assert/strict');
const test = require('node:test');
const { deleteUpload } = require('../../src/oms_hub/web/static/quarantine.js');

test('cancel names exact file and does not submit a deletion', async () => {
  let sent = false;
  let options;
  const result = await deleteUpload({
    item: { id: 'one', original_filename: 'A <draft>.pdf' }, token: 'csrf',
    confirm: async (value) => { options = value; return false; },
    fetchImpl: async () => { sent = true; },
  });
  assert.equal(result, null);
  assert.equal(sent, false);
  assert.equal(options.message, 'Delete “A <draft>.pdf”? Only this uploaded copy will be removed.');
});

test('delete submits authenticated decision and preserves cleanup pending response', async () => {
  let request;
  const result = await deleteUpload({
    item: { id: 'one/two', original_filename: 'A.pdf' }, token: 'csrf',
    confirm: async () => true,
    fetchImpl: async (url, options) => {
      request = { url, options };
      return { ok: true, json: async () => ({ state: 'discarded', cleanup_pending: true, message: 'Removed from uploads; file cleanup pending' }) };
    },
  });
  assert.equal(request.url, '/api/upload-items/one%2Ftwo/delete');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['X-CSRF-Token'], 'csrf');
  assert.equal(result.cleanup_pending, true);
});

test('stale assignment conflict remains visible instead of claiming deletion', async () => {
  await assert.rejects(deleteUpload({
    item: { id: 'one', original_filename: 'A.pdf' }, token: 'csrf',
    confirm: async () => true,
    fetchImpl: async () => ({ ok: false, json: async () => ({ detail: 'upload is no longer unmatched' }) }),
  }), /upload is no longer unmatched/);
});
