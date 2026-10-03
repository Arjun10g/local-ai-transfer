import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { ConversationController } from '../../host/agent/controller.mjs';
import { FixtureEngineClient } from '../../host/engine/fixture-engine.mjs';
import { DEFAULT_BOOTSTRAP_TTL_SECONDS, HostServer, bootstrapTtlMs } from '../../host/server/host-server.mjs';

const auth = token => ({ authorization: `Bearer ${token}`, 'content-type': 'application/json' });
const uiDir = new URL('../../ui/', import.meta.url);
const EXPECTED_TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

async function fixtureHost(t, options = {}) {
  const engine = options.engine ?? new FixtureEngineClient(); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine });
  const address = await host.listen(0); t.after(() => host.close()); return { host, address, controller };
}

test('every UI file is served from the allowlist with its content type and CSP, and nothing else is', async t => {
  const { address } = await fixtureHost(t);
  const files = (await readdir(uiDir)).filter(name => /\.(?:html|js|css)$/.test(name));
  assert.ok(['errors.js', 'markdown.js', 'transcript.js', 'tool-labels.js', 'cards.js'].every(name => files.includes(name)));
  for (const name of files) {
    const response = await fetch(`${address.url}/${name}`);
    assert.equal(response.status, 200, `${name} is not served; add it to ASSETS in host-server.mjs`);
    assert.equal(response.headers.get('content-type'), EXPECTED_TYPES[name.slice(name.lastIndexOf('.'))], name);
    assert.match(response.headers.get('content-security-policy'), /default-src 'self'; script-src 'self'/, name);
    assert.equal(response.headers.get('cache-control'), 'no-store', name);
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff', name);
    assert.equal(await response.text(), await readFile(new URL(name, uiDir), 'utf8'), name);
  }
  // Paths outside the allowlist fall through to the authenticated API and are refused.
  for (const path of ['/ui/app.js', '/errors.js?v=1', '/markdown.mjs', '/transcript.js/', '/..%2Fpackage.json', '/package.json', '/host/server/host-server.mjs', '/ERRORS.JS', '/errors.js%00']) {
    const response = await fetch(`${address.url}${path}`); assert.equal(response.status, 401, path);
  }
  const authorized = await fetch(`${address.url}/package.json`, { headers: auth(address.token) }); assert.equal(authorized.status, 404);
});

test('the page loads only same-origin module scripts that are on the allowlist', async t => {
  const { address } = await fixtureHost(t);
  const html = await (await fetch(`${address.url}/`)).text();
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/u, 'inline scripts are blocked by CSP');
  assert.doesNotMatch(html, /\sstyle=|\son[a-z]+=/u, 'inline styles/handlers are blocked by CSP');
  const scripts = [...html.matchAll(/<script[^>]*src="([^"]+)"/gu)].map(match => match[1]);
  assert.deepEqual(scripts, ['/app.js']);
  for (const name of ['app.js', 'errors.js', 'markdown.js', 'transcript.js', 'tool-labels.js', 'cards.js']) {
    const source = await (await fetch(`${address.url}/${name}`)).text();
    for (const [, target] of source.matchAll(/^import [^'"]*['"]([^'"]+)['"]/gmu)) {
      assert.match(target, /^\.\/[a-z][a-z-]*\.js$/u, `${name} imports ${target}`);
      assert.equal((await fetch(`${address.url}/${target.slice(2)}`)).status, 200, `${name} imports unserved ${target}`);
    }
    assert.doesNotMatch(source, /https?:\/\/(?!127\.0\.0\.1)[a-z0-9-]+\./iu, `${name} references a remote origin`);
  }
});

test('a concurrent chat gets 409 busy and a mid-generation reset gets 409 session_busy', async t => {
  let release; const gate = new Promise(resolve => { release = resolve; });
  const engine = { async health() { return { ready: true, backend: 'fixture' }; }, cancel() {}, async *generate() { await gate; yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const { address } = await fixtureHost(t, { engine });
  const first = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ session_id: 'ses_uibusy1', message: 'slow one', request_id: 'req_uibusy1' }) });
  assert.equal(first.status, 200);
  const second = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ session_id: 'ses_uibusy2', message: 'second', request_id: 'req_uibusy2' }) });
  assert.equal(second.status, 409); assert.deepEqual(await second.json(), { error: 'busy' });
  const reset = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ session_id: 'ses_uibusy1', reset: true }) });
  assert.equal(reset.status, 409); assert.deepEqual(await reset.json(), { error: 'session_busy' });
  release(); const stream = await first.text(); assert.match(stream, /event: message\.completed/);
  const after = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ session_id: 'ses_uibusy1', reset: true }) });
  assert.equal(after.status, 201); assert.equal((await after.json()).state, 'IDLE');
});

test('after the one-time nonce is spent, a reload reuses the tab-scoped bearer and session', async t => {
  const { host, address } = await fixtureHost(t);
  const nonce = new URLSearchParams(new URL(address.bootstrap_url).hash.slice(1)).get('bootstrap');
  const bootstrap = () => fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json' }, body: JSON.stringify({ nonce }) });
  const exchanged = await bootstrap(); assert.equal(exchanged.status, 200); const { token } = await exchanged.json();
  const created = await (await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: auth(token), body: '{}' })).json();
  const chat = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: auth(token), body: JSON.stringify({ session_id: created.session_id, message: 'hello', request_id: 'req_uireload' }) });
  assert.match(await chat.text(), /event: message\.completed/);
  // Reload: the fragment is gone and the nonce cannot be replayed...
  assert.equal((await bootstrap()).status, 410); assert.equal(host.address().bootstrap_url, null);
  // ...but the stored bearer still authenticates and resumes the same server session.
  assert.equal((await fetch(`${address.url}/api/status`, { headers: auth(token) })).status, 200);
  const resumed = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: auth(token), body: JSON.stringify({ session_id: created.session_id }) });
  assert.equal(resumed.status, 201); assert.deepEqual(await resumed.json(), { session_id: created.session_id, state: 'COMPLETED' });
  // A stale bearer from an earlier host process is refused, which the page shows as "relaunch".
  assert.equal((await fetch(`${address.url}/api/status`, { headers: auth('A'.repeat(43)) })).status, 401);
});

test('the page keeps the bearer in sessionStorage only and strips the nonce before any request', async () => {
  const app = await readFile(new URL('app.js', uiDir), 'utf8');
  const strip = app.indexOf('history.replaceState(null, document.title, `${location.pathname}${location.search}`)');
  assert.ok(strip > 0 && strip < app.indexOf('fetch(') && strip < app.indexOf("fetch('/bootstrap'"));
  assert.match(app, /const TOKEN_KEY = 'bmo\.host_token'/);
  assert.match(app, /sessionStorage\.setItem\(key, value\)/); assert.match(app, /sessionSet\(TOKEN_KEY, token\)/); assert.match(app, /sessionSet\(TOKEN_KEY, null\)/);
  // Nothing is ever written to localStorage (see ui-transcript.test.mjs for the purge).
  assert.doesNotMatch(app, /localStorage\.setItem|localSet\(/u); assert.doesNotMatch(app, /document\.cookie/);
  assert.doesNotMatch(app, /console\.(?:log|info|debug)/);
});

test('the chat body accepts the optional tools switch the UI sends and rejects junk', async t => {
  const seen = [];
  const engine = { async health() { return { ready: true, backend: 'fixture' }; }, cancel() {}, async *generate({ tools }) { seen.push(tools.length); yield { kind: 'text_delta', text: 'ok' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const { address } = await fixtureHost(t, { engine });
  const chat = body => fetch(`${address.url}/api/chat`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ session_id: 'ses_uitools', message: 'hi', ...body }) });
  const off = await chat({ request_id: 'req_uitools1', tools: 'off' }); assert.equal(off.status, 200); assert.match(await off.text(), /event: message\.completed/);
  const auto = await chat({ request_id: 'req_uitools2' }); assert.equal(auto.status, 200); await auto.text();
  assert.equal(seen[0], 0); assert.ok(seen[1] > 0, 'default turn still offers tools');
  const explicit = await chat({ request_id: 'req_uitools5', tools: 'auto' }); assert.equal(explicit.status, 200); await explicit.text(); assert.ok(seen[2] > 0);
  for (const [id, tools] of [['req_uitools3', 'none'], ['req_uitools4', false], ['req_uitools6', 'OFF'], ['req_uitools7', null], ['req_uitools8', ['off']]]) { const response = await chat({ request_id: id, tools }); assert.equal(response.status, 400, JSON.stringify(tools)); assert.deepEqual(await response.json(), { error: 'invalid_chat_request' }); }
});

test('launch nonce lifetime defaults to 180 s and accepts only a validated override', () => {
  assert.equal(DEFAULT_BOOTSTRAP_TTL_SECONDS, 180);
  assert.equal(bootstrapTtlMs({}), 180_000); assert.equal(bootstrapTtlMs(undefined), 180_000);
  assert.equal(bootstrapTtlMs({ LAE_BOOTSTRAP_TTL_SECONDS: '30' }), 30_000); assert.equal(bootstrapTtlMs({ LAE_BOOTSTRAP_TTL_SECONDS: '600' }), 600_000); assert.equal(bootstrapTtlMs({ LAE_BOOTSTRAP_TTL_SECONDS: '240' }), 240_000);
  for (const bad of ['29', '601', '0', '-60', '90.5', '1e2', ' 120', '120s', '0x78', '', 'Infinity', '00120', '9999999']) assert.equal(bootstrapTtlMs({ LAE_BOOTSTRAP_TTL_SECONDS: bad }), 180_000, JSON.stringify(bad));
  assert.equal(bootstrapTtlMs({ LAE_BOOTSTRAP_TTL_SECONDS: 120 }), 180_000, 'only the string an environment provides is read');
});

test('the host applies the lifetime from the environment and refuses the nonce once it has expired', async t => {
  const previous = process.env.LAE_BOOTSTRAP_TTL_SECONDS;
  t.after(() => { if (previous === undefined) delete process.env.LAE_BOOTSTRAP_TTL_SECONDS; else process.env.LAE_BOOTSTRAP_TTL_SECONDS = previous; });
  const lifetime = async value => { if (value === undefined) delete process.env.LAE_BOOTSTRAP_TTL_SECONDS; else process.env.LAE_BOOTSTRAP_TTL_SECONDS = value; const { host } = await fixtureHost(t); return host.bootstrapExpiresAt - Date.now(); };
  const near = (actual, expected) => assert.ok(actual > expected - 5000 && actual <= expected, `${actual} vs ${expected}`);
  near(await lifetime(undefined), 180_000); near(await lifetime('45'), 45_000); near(await lifetime('nonsense'), 180_000);

  delete process.env.LAE_BOOTSTRAP_TTL_SECONDS;
  const { host, address } = await fixtureHost(t);
  const nonce = new URLSearchParams(new URL(address.bootstrap_url).hash.slice(1)).get('bootstrap');
  host.bootstrapExpiresAt = Date.now() - 1;
  const expired = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json' }, body: JSON.stringify({ nonce }) });
  assert.equal(expired.status, 410); assert.deepEqual(await expired.json(), { error: 'bootstrap_unavailable' });
});

test('long messages reach the controller, which gives the clear byte-limit error', async t => {
  const lengths = [];
  const engine = { async health() { return { ready: true, backend: 'fixture' }; }, cancel() {}, async *generate({ messages }) { lengths.push(messages.at(-1).content.length); yield { kind: 'text_delta', text: 'ok' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const { address } = await fixtureHost(t, { engine });
  const chat = (id, message) => fetch(`${address.url}/api/chat`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ session_id: 'ses_uilong1', request_id: id, message }) });
  // 20,000 characters pass the HTTP parser (formerly a bare 400 invalid_json)
  // and reach the controller; whether they then fit the 8K-token context is
  // the controller's decision, reported in plain words on the stream.
  const twenty = await chat('req_uilong1', 'a'.repeat(20000)); assert.equal(twenty.status, 200);
  const stream = await twenty.text(); assert.match(stream, /event: message\.started/); assert.match(stream, /event: (?:message\.completed|request\.failed)/); assert.doesNotMatch(stream, /invalid_json/);
  const forty = await chat('req_uilong2', 'b'.repeat(40000)); assert.equal(forty.status, 400); assert.deepEqual(await forty.json(), { error: 'invalid_message_too_large' });
  const wide = await chat('req_uilong3', '\u4e2d'.repeat(13334)); assert.equal(wide.status, 400); assert.deepEqual(await wide.json(), { error: 'invalid_message_too_large' }, '40,002 UTF-8 bytes in 13,334 characters');
  const oversized = await chat('req_uilong4', 'c'.repeat(70000)); assert.equal(oversized.status, 413, 'the body bound still applies');
  assert.ok(lengths.length <= 1 && lengths.every(length => length === 20000));
});

test('a late answer to an expired confirmation gets 410 confirmation_expired, a foreign one 404', async t => {
  const engine = { async health() { return { ready: true, backend: 'fixture' }; }, cancel() {}, async *generate({ messages }) { if (!messages.some(m => m.role === 'tool')) { yield { kind: 'tool_call_chunk', text: '{"id":"call_uiexp01","name":"test.confirm","arguments":{}}' }; return; } yield { kind: 'text_delta', text: 'after' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const controller = new ConversationController({ engine, confirmationTimeoutMs: 50, toolRegistry: { 'test.confirm': { name: 'test.confirm', risk_tier: 'T1', side_effect: 'read_sensitive', requires_confirmation: true, execute: async () => { throw new Error('must not execute'); } } } });
  const host = new HostServer({ controller, engine }); const address = await host.listen(0); t.after(() => host.close());
  const events = []; await controller.runTurn({ sessionId: 'ses_uiexp01', requestId: 'req_uiexp01', message: 'do it', onEvent: event => events.push(event) });
  const required = events.find(event => event.event === 'tool.confirmation_required'); assert.ok(required);
  const answer = (id, body) => fetch(`${address.url}/api/tool-confirmations/${id}`, { method: 'POST', headers: auth(address.token), body: JSON.stringify(body) });
  const late = await answer(required.data.confirmation_id, { approved: true, request_id: 'req_uiexp01', call_id: 'call_uiexp01' });
  assert.equal(late.status, 410); assert.deepEqual(await late.json(), { accepted: false, error: 'confirmation_expired' });
  const wrongCall = await answer(required.data.confirmation_id, { approved: true, request_id: 'req_uiexp01', call_id: 'call_other01' }); assert.equal(wrongCall.status, 404);
  const unknown = await answer('cnf_unknown123', { approved: false, request_id: 'req_uiexp01', call_id: 'call_uiexp01' }); assert.equal(unknown.status, 404); assert.deepEqual(await unknown.json(), { accepted: false });
});
