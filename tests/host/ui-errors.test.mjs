import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describeError, isCancellation } from '../../ui/errors.js';

const root = new URL('../../', import.meta.url);

async function sources(dir, extension) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await sources(path, extension));
    else if (entry.name.endsWith(extension)) out.push(await readFile(path, 'utf8'));
  }
  return out;
}

test('suggested wording reaches the user for the demo-critical codes', () => {
  assert.equal(describeError('busy').message, 'The engine is still finishing an earlier request (about a minute).');
  for (const code of ['not_ready', 'http_503', 'engine_not_ready']) assert.equal(describeError(code).message, 'The model is still loading or has stopped.');
  assert.deepEqual([describeError('engine_timeout').message, describeError('engine_timeout').action], ['That took too long.', 'Try a shorter message or press Reset.']);
  for (const code of ['invalid_request', 'engine_request_too_large', 'context_overflow']) assert.equal(describeError(code).message, 'This conversation has grown too long.');
  for (const code of ['malformed_tool_call', 'invalid_tool_arguments', 'unknown_tool']) assert.equal(describeError(code).message, 'The assistant made a bad tool request.');
  assert.equal(describeError('network_error').message, 'Lost contact with the BMO host.');
  assert.match(describeError('network_error').action, /window still open/);
});

test('HTTP statuses without a body code still map to plain English', () => {
  assert.equal(describeError(null, { status: 409 }).message, describeError('busy').message);
  assert.equal(describeError(undefined, { status: 503 }).message, describeError('not_ready').message);
  assert.equal(describeError(null, { status: 401 }).code, 'http_401');
  assert.equal(describeError(null, { status: 401 }).known, true);
  assert.equal(describeError(null, { status: 500 }).known, true);
  assert.equal(describeError(null).code, 'request_failed');
});

test('unknown or hostile codes fall back to a generic sentence and never echo markup', () => {
  const unknown = describeError('totally_new_code');
  assert.equal(unknown.known, false); assert.equal(unknown.code, 'totally_new_code'); assert.match(unknown.message, /unexpected/);
  for (const hostile of ['<script>alert(1)</script>', 'BUSY', 'x'.repeat(500), 42, {}, '__proto__ ']) {
    const result = describeError(hostile);
    assert.equal(result.code, 'request_failed'); assert.equal(result.known, true);
    assert.doesNotMatch(result.message + result.action, /[<>]/);
  }
  assert.equal(describeError('constructor').known, false); assert.equal(describeError('tostring').known, false);
});

test('cancellation is recognised and is not phrased as a failure', () => {
  assert.equal(isCancellation('cancelled'), true); assert.equal(isCancellation('request_cancelled'), true); assert.equal(isCancellation('busy'), false);
  assert.equal(describeError('cancelled').message, 'Stopped.');
});

test('every error code the host, tools, and native HTTP server can emit has a specific message', async () => {
  const codes = new Set(['http_503', 'http_500', 'http_409', 'http_400', 'http_401', 'session_busy', 'request_failed', 'invalid_chat_request']);
  for (const text of await sources(fileURLToPath(new URL('host/', root)), '.mjs')) {
    for (const match of text.matchAll(/(?:code: ?|[A-Z][A-Za-z]*Error\(|error: ?)'([a-z][a-z0-9_]+)'/g)) codes.add(match[1]);
  }
  const server = await readFile(new URL('native/server/http_server.cpp', root), 'utf8');
  for (const match of server.matchAll(/fail\(\d+, "([a-z_]+)"\)/g)) codes.add(match[1]);
  for (const match of server.matchAll(/fail\([^;]*\? "([a-z_]+)" : "([a-z_]+)"\)/g)) { codes.add(match[1]); codes.add(match[2]); }
  assert.ok(codes.size > 100, `scan found only ${codes.size} codes`);
  const unmapped = [...codes].filter(code => !describeError(code).known).sort();
  assert.deepEqual(unmapped, [], `add these codes to ui/errors.js: ${unmapped.join(', ')}`);
  for (const code of codes) { const { message, action } = describeError(code); assert.ok(message.length > 5 && !message.includes('_'), `${code} message reads like a code: ${message}`); assert.equal(typeof action, 'string'); }
});
