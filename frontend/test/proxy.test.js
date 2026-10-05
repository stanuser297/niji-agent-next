import test from 'node:test';
import assert from 'node:assert/strict';
import { proxy } from '../lib/proxy.js';

function responseRecorder() {
  return {
    headers: {}, statusCode: 200, body: undefined,
    setHeader(name, value) { this.headers[name.toLowerCase()] = value; },
    status(code) { this.statusCode = code; return this; },
    json(value) { this.body = value; return this; },
    send(value) { this.body = value; return this; },
  };
}

function upstream({ status = 200, contentType = 'application/json', body = '{}' } = {}) {
  const bytes = typeof body === 'string' ? Buffer.from(body) : Buffer.from(body);
  return {
    status,
    headers: new Headers({ 'content-type': contentType }),
    async text() { return bytes.toString('utf8'); },
    async arrayBuffer() { return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); },
  };
}

test('capabilities are forwarded only to the explicit Niji endpoint with the bearer token', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => { request = { url: String(url), options }; return upstream(); };
  try {
    const res = responseRecorder();
    await proxy({ method: 'GET', headers: { authorization: 'Bearer session-token' } }, res, 'capabilities');
    assert.equal(request.url, 'https://niji-cloud-api-production.up.railway.app/v1/capabilities');
    assert.equal(request.options.headers.Authorization, 'Bearer session-token');
    assert.equal(res.statusCode, 200);
  } finally { globalThis.fetch = originalFetch; }
});

test('account-data deletion forwards explicit confirmation and no request body', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => { request = { url: String(url), options }; return upstream(); };
  try {
    const res = responseRecorder();
    await proxy({ method: 'DELETE', headers: {
      authorization: 'Bearer session-token', 'x-confirm-data-deletion': 'delete',
    } }, res, 'account/data');
    assert.equal(request.url, 'https://niji-cloud-api-production.up.railway.app/v1/account/data');
    assert.equal(request.options.method, 'DELETE');
    assert.equal(request.options.headers['X-Confirm-Data-Deletion'], 'delete');
    assert.equal(request.options.body, undefined);
  } finally { globalThis.fetch = originalFetch; }
});

test('artifact downloads preserve binary bytes and safe download headers', async () => {
  const originalFetch = globalThis.fetch;
  const data = Buffer.from([0, 1, 2, 255]);
  globalThis.fetch = async () => {
    const result = upstream({ contentType: 'application/octet-stream', body: data });
    result.headers.set('content-disposition', 'attachment; filename=download');
    result.headers.set('etag', '"abc123"');
    return result;
  };
  try {
    const res = responseRecorder();
    await proxy({ method: 'GET', headers: { authorization: 'Bearer session-token' } }, res, 'runs/run-123/artifacts/file-456');
    assert.deepEqual(res.body, data);
    assert.equal(res.headers['content-disposition'], 'attachment; filename=download');
    assert.equal(res.headers.etag, '"abc123"');
    assert.equal(res.headers['x-content-type-options'], 'nosniff');
  } finally { globalThis.fetch = originalFetch; }
});

test('proxy rejects routes outside the allowlist without calling upstream', async () => {
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = async () => { called = true; return upstream(); };
  try {
    const res = responseRecorder();
    await proxy({ method: 'GET', headers: {} }, res, 'runs/../account/data');
    assert.equal(res.statusCode, 404);
    assert.equal(called, false);
  } finally { globalThis.fetch = originalFetch; }
});
