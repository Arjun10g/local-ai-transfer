// The bridge's loopback client and host discovery: what leaves the bridge, where it goes,
// and how strictly host.json is trusted. Checklist ids in [brackets].

import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtemp, rm, symlink, writeFile, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { HostClient, HostError, MAX_RESPONSE_BYTES, NOT_RUNNING_MESSAGE, defaultStateDir, parseHostJson, readHostInfo } from '../../host/mcp/host-client.mjs';
import { TEST_KEY, makeStateDir, proofFor, startBridge, startFakeHost, structured, CopilotCliLikeClient } from './mcp-fake-host.mjs';

// A raw listener that proves its identity correctly (so the bridge proceeds to the keyed
// request under test) and hands every other request to `handler`.
async function rawServer(t, handler) {
  let port;
  const server = http.createServer((req, res) => {
    if (req.url !== '/api/delegate/handshake') return handler(req, res);
    let raw = ''; req.on('data', chunk => { raw += chunk; });
    req.on('end', () => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ proof: proofFor(TEST_KEY, JSON.parse(raw).nonce, port, process.pid) })); });
  });
  const sockets = new Set();
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { for (const socket of sockets) socket.destroy(); return new Promise(resolve => server.close(resolve)); });
  port = server.address().port;
  return port;
}
const clientFor = (port, options = {}) => new HostClient({ key: TEST_KEY, discover: async () => ({ ok: true, port, pid: process.pid }), ...options });

test('requests carry the bearer key and a pinned loopback Host, never an Origin, cookie or proxy hop', async t => {
  const host = await startFakeHost();
  t.after(() => host.close());
  let requestOptions;
  const spy = (options, callback) => { requestOptions = options; return http.request(options, callback); };
  const client = new HostClient({ key: TEST_KEY, discover: host.discover, request: spy });
  await client.health();
  const job = await client.startJob({ task: 't', allow_files: false, caller: { client: 'c', name: 'n' } });
  await client.getJob(job.job_id, 0);
  await client.cancelJob(job.job_id);
  const [handshake, ...keyed] = host.requests;
  assert.equal(handshake.url, '/api/delegate/handshake');
  assert.equal(handshake.headers.authorization, undefined);
  for (const request of keyed) {
    assert.equal(request.headers.authorization, `Bearer ${TEST_KEY}`);
    assert.equal(request.headers.host, `127.0.0.1:${host.port}`);
    assert.equal(request.headers.origin, undefined);
    assert.equal(request.headers.cookie, undefined);
    assert.equal(request.headers.referer, undefined);
  }
  assert.equal(requestOptions.host, '127.0.0.1');
  assert.equal(requestOptions.family, 4);
  assert.ok(requestOptions.agent instanceof http.Agent && requestOptions.agent !== http.globalAgent);
  assert.deepEqual(host.requests.map(request => `${request.method} ${request.url}`), ['POST /api/delegate/handshake', 'GET /api/delegate/health', 'POST /api/delegate/jobs', `GET /api/delegate/jobs/${job.job_id}?wait=0`, `POST /api/delegate/jobs/${job.job_id}/cancel`]);
});

test('redirects are refused, never followed (the bearer key must not travel to a second listener)', async t => {
  let secondHit = 0;
  const second = await rawServer(t, (req, res) => { secondHit += 1; res.end('{}'); });
  const first = await rawServer(t, (req, res) => { res.writeHead(307, { location: `http://127.0.0.1:${second}/api/delegate/health` }); res.end(); });
  await assert.rejects(() => clientFor(first).health(), error => error.code === 'host_protocol_error' && /redirect/.test(error.message));
  assert.equal(secondHit, 0);
});

test('responses above 256 KiB are cut off; malformed JSON and odd shapes are protocol errors', async t => {
  const huge = await rawServer(t, (req, res) => { res.writeHead(200); res.end(`{"ready":true,"engine":"${'x'.repeat(MAX_RESPONSE_BYTES)}"}`); });
  await assert.rejects(() => clientFor(huge).health(), error => error.code === 'host_protocol_error' && /too large/.test(error.message));
  const junk = await rawServer(t, (req, res) => { res.writeHead(200); res.end('not json'); });
  await assert.rejects(() => clientFor(junk).health(), error => error.code === 'host_protocol_error');
  const badJob = await rawServer(t, (req, res) => { res.writeHead(202); res.end(JSON.stringify({ job_id: '../../evil', status: 'queued' })); });
  await assert.rejects(() => clientFor(badJob).startJob({ task: 'x' }), error => error.code === 'host_protocol_error');
  const badStatus = await rawServer(t, (req, res) => { res.writeHead(202); res.end(JSON.stringify({ job_id: 'job_12345678', status: 'completed', answer: 'instant?' })); });
  await assert.rejects(() => clientFor(badStatus).startJob({ task: 'x' }), error => error.code === 'host_protocol_error');
  const noAnswer = await rawServer(t, (req, res) => { res.writeHead(200); res.end(JSON.stringify({ job_id: 'job_12345678', status: 'completed' })); });
  await assert.rejects(() => clientFor(noAnswer).getJob('job_12345678', 0), error => error.code === 'host_protocol_error');
});

test('per-request timeout and abort tear the connection down; error text never names the port', async t => {
  let closed = 0;
  const port = await rawServer(t, (req, res) => { res.on('close', () => { closed += 1; }); });
  const started = Date.now();
  await assert.rejects(() => clientFor(port).health({ timeoutMs: 150 }), error => error.code === 'host_timeout' && !error.message.includes(String(port)));
  assert.ok(Date.now() - started < 1000);
  const controller = new AbortController();
  const pending = clientFor(port).health({ timeoutMs: 10_000, signal: controller.signal });
  setTimeout(() => controller.abort('cancelled'), 50);
  await assert.rejects(pending, error => error.code === 'aborted');
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(closed, 2);
});

test('connection refused means BMO is not running; other socket errors are generic and port-free', async t => {
  const probe = http.createServer(); await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve)); const dead = probe.address().port; await new Promise(resolve => probe.close(resolve));
  await assert.rejects(() => clientFor(dead).health(), error => error.code === 'bmo_not_running' && error.message === NOT_RUNNING_MESSAGE);
  const reset = await rawServer(t, req => req.socket.destroy());
  await assert.rejects(() => clientFor(reset).health(), error => error.code === 'host_unreachable' && !error.message.includes(String(reset)));
});

test('job ids are validated before any URL is built (no traversal or query injection)', async t => {
  const host = await startFakeHost();
  t.after(() => host.close());
  const client = new HostClient({ key: TEST_KEY, discover: host.discover });
  for (const bad of ['../../health', 'job_12345678?wait=999', 'job_12345678/cancel', 'job 12345678', 'a'.repeat(65), 'short', 42, null]) {
    await assert.rejects(() => client.getJob(bad, 0), error => error instanceof HostError && error.code === 'invalid_job_id', String(bad));
    await assert.rejects(() => client.cancelJob(bad), error => error.code === 'invalid_job_id');
  }
  assert.equal(host.requests.length, 0);
  await assert.rejects(() => client.getJob('job_12345678', 99), error => error.code === 'unknown_job');
  assert.equal(host.keyed()[0].url, '/api/delegate/jobs/job_12345678?wait=15');
});

test('a 401 from the host is reported as a rejected key without echoing it', async t => {
  const port = await rawServer(t, (req, res) => { res.writeHead(401, { 'content-type': 'application/json' }); res.end('{"error":"unauthorized"}'); });
  const error = await clientFor(port).health().catch(failure => failure);
  assert.equal(error.code, 'unauthorized');
  assert.ok(!error.message.includes(TEST_KEY));
  assert.match(error.message, /Start-BMO\.ps1 -ShowDelegateKey/);
});

test('state directory per platform, with an absolute BMO_STATE_DIR override', () => {
  assert.equal(defaultStateDir({ platform: 'win32', env: { LOCALAPPDATA: 'C:\\Users\\u\\AppData\\Local' }, homedir: 'C:\\Users\\u' }), 'C:\\Users\\u\\AppData\\Local\\BMO');
  assert.equal(defaultStateDir({ platform: 'win32', env: {}, homedir: 'C:\\Users\\u' }), 'C:\\Users\\u\\AppData\\Local\\BMO');
  assert.equal(defaultStateDir({ platform: 'darwin', env: {}, homedir: '/Users/u' }), '/Users/u/Library/Application Support/BMO');
  assert.equal(defaultStateDir({ platform: 'linux', env: {}, homedir: '/home/u' }), '/home/u/.local/state/bmo');
  assert.equal(defaultStateDir({ platform: 'linux', env: { XDG_STATE_HOME: '/xdg' }, homedir: '/home/u' }), '/xdg/bmo');
  assert.equal(defaultStateDir({ platform: 'linux', env: { XDG_STATE_HOME: 'relative' }, homedir: '/home/u' }), '/home/u/.local/state/bmo');
  assert.equal(defaultStateDir({ platform: 'linux', env: { BMO_STATE_DIR: '/custom' }, homedir: '/home/u' }), '/custom');
  assert.equal(defaultStateDir({ platform: 'linux', env: { BMO_STATE_DIR: 'relative/dir' }, homedir: '/home/u' }), '/home/u/.local/state/bmo');
});

test('host.json content is parsed strictly', () => {
  const good = { version: 1, port: 50123, pid: 1234, started_at: '2026-10-03T10:00:00Z' };
  assert.deepEqual(parseHostJson(JSON.stringify(good)), { port: 50123, pid: 1234, startedAt: good.started_at });
  assert.deepEqual(parseHostJson(`\uFEFF${JSON.stringify(good)}`), { port: 50123, pid: 1234, startedAt: good.started_at });
  for (const bad of [{ ...good, version: 2 }, { ...good, port: 80 }, { ...good, port: 70000 }, { ...good, port: '50123' }, { ...good, pid: 0 }, { ...good, pid: 1.5 }, { ...good, started_at: 'yesterday' }, { ...good, extra: true }, { version: 1, port: 50123, pid: 1 }, [good]]) {
    assert.equal(parseHostJson(JSON.stringify(bad)), null, JSON.stringify(bad));
  }
  assert.equal(parseHostJson('{nope'), null);
});

test('[C24] host.json is trusted only when it is ours, private, small, not a symlink, and names a live pid', { skip: process.platform === 'win32' && 'POSIX ownership/mode checks' }, async t => {
  const port = 50123;
  const ok = await makeStateDir(t, { port });
  const info = await readHostInfo({ stateDir: ok });
  assert.deepEqual({ ...info, startedAt: undefined }, { ok: true, port, pid: process.pid, startedAt: undefined });
  assert.ok(Number.isFinite(Date.parse(info.startedAt)));
  assert.equal((await readHostInfo({ stateDir: join(ok, 'missing-dir') })).reason, 'missing');
  assert.equal((await readHostInfo({ stateDir: await makeStateDir(t, { port, mode: 0o666 }) })).reason, 'insecure');
  assert.equal((await readHostInfo({ stateDir: await makeStateDir(t, { port, mode: 0o620 }) })).reason, 'insecure');
  assert.equal((await readHostInfo({ stateDir: ok, getuid: () => 4242 })).reason, 'insecure');
  assert.equal((await readHostInfo({ stateDir: ok, isAlive: () => false })).reason, 'stale');
  assert.equal((await readHostInfo({ stateDir: await makeStateDir(t, { port, raw: `${JSON.stringify({ version: 1, port, pid: process.pid, started_at: new Date().toISOString() })}${' '.repeat(1100)}` }) })).reason, 'invalid');
  assert.equal((await readHostInfo({ stateDir: await makeStateDir(t, { port, raw: '{"version":1}' }) })).reason, 'invalid');
  const linked = await mkdtemp(join(tmpdir(), 'bmo-mcp-link-'));
  t.after(() => rm(linked, { recursive: true, force: true }));
  await symlink(join(ok, 'host.json'), join(linked, 'host.json'));
  assert.equal((await readHostInfo({ stateDir: linked })).reason, 'insecure');
  const dirInstead = await mkdtemp(join(tmpdir(), 'bmo-mcp-dir-'));
  t.after(() => rm(dirInstead, { recursive: true, force: true }));
  await (await import('node:fs/promises')).mkdir(join(dirInstead, 'host.json'));
  assert.notEqual((await readHostInfo({ stateDir: dirInstead })).ok, true);
});

test('a dead pid in host.json is detected with a real process probe', async t => {
  const { spawnSync } = await import('node:child_process');
  const child = spawnSync(process.execPath, ['-e', 'process.stdout.write(String(process.pid))']);
  const deadPid = Number(child.stdout.toString());
  const dir = await makeStateDir(t, { port: 50123, pid: deadPid });
  assert.equal((await readHostInfo({ stateDir: dir })).reason, 'stale');
});

test('stale host.json reaches the caller as "BMO is not running" without paths, pids or ports', async t => {
  const dir = await makeStateDir(t, { port: 50999, pid: 999_999 });
  const { client } = startBridge(t, { ClientClass: CopilotCliLikeClient, discover: () => readHostInfo({ stateDir: dir, isAlive: () => false }) });
  const reply = await client.callTool('bmo_health', {});
  assert.equal(reply.result.isError, true);
  assert.equal(structured(reply).error.message, NOT_RUNNING_MESSAGE);
  const text = JSON.stringify(reply);
  for (const leak of [dir, '50999', '999999', 'host.json']) assert.ok(!text.includes(leak), leak);
});

test('an unwritable or partially written host.json never throws out of discovery', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'bmo-mcp-partial-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await writeFile(join(dir, 'host.json'), '{"version":1,"port":501');
  await chmod(join(dir, 'host.json'), 0o600);
  assert.equal((await readHostInfo({ stateDir: dir })).ok, false);
});
