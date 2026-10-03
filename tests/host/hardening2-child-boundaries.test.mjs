import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { tmpdir } from 'node:os';
import { BrowserActionProvider, browserEnvironment, createBrowserActionTools } from '../../host/providers/browser-actions.mjs';
import { killCopilotProcessTree } from '../../host/providers/copilot-cli.mjs';
import { createSystemTools, posixUrlOpener } from '../../host/tools/local/system-tools.mjs';
import { NativeEngineClient } from '../../host/engine/native-engine-client.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';

// Child-process boundaries, round two: what the automated browser inherits,
// how Windows cleanup and the URL opener find their executables, and that a
// usage figure the engine never sent is not reported as if it had.

// A host environment full of things no child should see.  Lower-case and
// mixed-case spellings matter because Windows treats them as the same name.
const HOSTILE = Object.freeze({
  LAE_ENGINE_TOKEN: 'engine-secret', lae_action_journal_fd: '7', LAE_ACTION_JOURNAL_DIR: '/var/lae',
  GITHUB_TOKEN: 'ghp_secret', GH_TOKEN: 'gh-secret', OPENAI_API_KEY: 'sk-secret', AWS_SECRET_ACCESS_KEY: 'aws-secret', HF_TOKEN: 'hf-secret',
  HTTPS_PROXY: 'http://evil:8080', http_proxy: 'http://evil:8080', NODE_OPTIONS: '--require /tmp/evil.js', LD_PRELOAD: '/tmp/evil.so', DYLD_INSERT_LIBRARIES: '/tmp/evil.dylib',
  PATH: '/tmp/planted:/usr/bin', Path: 'C:\\planted;C:\\Windows\\System32', PSModulePath: 'C:\\planted',
  ELECTRON_RUN_AS_NODE: '1', CHROME_LOG_FILE: '/tmp/leak.log'
});
const WINDOWS_ALLOWED = new Set(['SystemRoot', 'windir', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA']);
const POSIX_ALLOWED = new Set(['PATH', 'HOME', 'TMPDIR', 'XDG_RUNTIME_DIR', 'XAUTHORITY', 'DISPLAY', 'WAYLAND_DISPLAY', 'LANG', 'LC_ALL', 'LC_CTYPE']);
const SECRET_VALUES = Object.values(HOSTILE);

function assertMinimal(env, allowed, label) {
  for (const key of Object.keys(env)) assert.ok(allowed.has(key), `${label}: ${key} is outside the allowlist`);
  for (const key of Object.keys(env)) assert.ok(!/^LAE_/iu.test(key) && !/TOKEN|SECRET|KEY|PROXY|PRELOAD|NODE_OPTIONS/iu.test(key), `${label}: ${key} reached the child`);
  for (const value of Object.values(env)) assert.ok(!SECRET_VALUES.includes(value), `${label}: inherited value ${value} reached the child`);
}

// ---- 1. the automated browser gets an explicit environment ------------------

test('browserEnvironment passes only validated Windows locations and never PATH or secrets', () => {
  const env = browserEnvironment({ ...HOSTILE, SystemRoot: 'D:\\Windows', TEMP: 'C:\\Users\\Op\\AppData\\Local\\Temp', tmp: 'C:\\Users\\Op\\AppData\\Local\\Temp', USERPROFILE: 'C:\\Users\\Op', APPDATA: '\\\\attacker\\share\\roaming', LOCALAPPDATA: 'C:\\Users\\Op\\..\\Other' }, 'win32');
  assertMinimal(env, WINDOWS_ALLOWED, 'win32');
  assert.deepEqual(env, { SystemRoot: 'D:\\Windows', windir: 'D:\\Windows', TEMP: 'C:\\Users\\Op\\AppData\\Local\\Temp', TMP: 'C:\\Users\\Op\\AppData\\Local\\Temp', USERPROFILE: 'C:\\Users\\Op' }, 'UNC and `..` locations are dropped, not passed');
  // A hostile SystemRoot never becomes the child's: the constant root is used.
  for (const SystemRoot of ['\\\\attacker\\share', '..\\planted', 'planted', '\\\\?\\C:\\Windows', 'C:\\Win\u0000dows']) assert.equal(browserEnvironment({ SystemRoot }, 'win32').SystemRoot, 'C:\\Windows', SystemRoot);
});

test('browserEnvironment pins PATH to system directories on POSIX and drops everything else', () => {
  for (const platform of ['darwin', 'linux']) {
    const env = browserEnvironment({ ...HOSTILE, HOME: '/home/op', TMPDIR: '/tmp/op', DISPLAY: ':0', LANG: 'en_GB.UTF-8', XAUTHORITY: 'relative/.Xauthority', XDG_RUNTIME_DIR: '/run/user/1000/../0' }, platform);
    assertMinimal(env, POSIX_ALLOWED, platform);
    assert.deepEqual(env, { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: '/home/op', TMPDIR: '/tmp/op', DISPLAY: ':0', LANG: 'en_GB.UTF-8' }, platform);
  }
});

function browserHarness({ platform, environment, onTaskkill }) {
  const launched = []; const killers = [];
  const cdp = { async connect() {}, async navigate() {}, async inspect() { return { url: 'https://example.com/', title: 'Example', text: '', links: [], controls: [] }; }, close() {} };
  const provider = new BrowserActionProvider({
    enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], platform, environment,
    mkdtempImpl: async () => path.join(tmpdir(), `lae-browser-hardening2-${platform}`), rmImpl: async () => {},
    spawn: (executable, args, options) => { const child = new EventEmitter(); child.pid = 4242; child.exitCode = null; child.kill = () => {}; launched.push({ executable, args, options, child }); return child; },
    taskkillSpawn: (executable, args, options) => { killers.push({ executable, args, options }); const killer = new EventEmitter(); queueMicrotask(() => { onTaskkill?.(launched.at(-1).child); killer.emit('close', 0); }); return killer; },
    proxyFactory: async () => ({ port: 43123, close: async () => {} }), waitDevtoolsPortImpl: async () => ({ port: 43124 }),
    resolve: async () => ['93.184.216.34'], cdpFactory: async () => cdp
  });
  return { provider, tools: createBrowserActionTools(provider), launched, killers };
}
const browserCall = (name, args, id) => ({ id, name, arguments: args });
const payload = result => JSON.parse(result.content?.[0]?.text ?? result.text ?? result.content);

test('the browser process is launched with the minimal environment, not the host one', async t => {
  for (const [platform, extra, allowed] of [['win32', { SystemRoot: 'C:\\Windows', USERPROFILE: 'C:\\Users\\Op' }, WINDOWS_ALLOWED], ['linux', { HOME: '/home/op', DISPLAY: ':0' }, POSIX_ALLOWED], ['darwin', { HOME: '/Users/op' }, POSIX_ALLOWED]]) {
    const { provider, tools, launched } = browserHarness({ platform, environment: { ...HOSTILE, ...extra }, onTaskkill: child => { child.exitCode = 0; } }); t.after(() => provider.shutdown());
    const start = browserCall('browser.session_start', { url: 'https://example.com/' }, `start_${platform}`);
    await tools['browser.session_start'].preview(start);
    const started = await tools['browser.session_start'].execute({ ...start, authorization: { kind: 'user_confirmation' } });
    assert.equal(started.status, 'ok', JSON.stringify(started));
    assert.equal(launched.length, 1); const { options } = launched[0];
    assert.equal(options.shell, false);
    assert.ok(options.env && typeof options.env === 'object', `${platform}: an explicit env is always passed, so nothing is inherited`);
    assertMinimal(options.env, allowed, platform);
    for (const [key, value] of Object.entries(extra)) assert.equal(options.env[key], value, `${platform}: ${key} kept`);
    if (platform === 'win32') assert.equal(Object.keys(options.env).some(key => key.toUpperCase() === 'PATH'), false, 'no PATH on Windows');
  }
});

// ---- 2. Windows cleanup runs taskkill.exe from a validated System32 ---------

const system32Taskkill = root => `${root}\\System32\\taskkill.exe`;

test('browser session cleanup on Windows spawns taskkill.exe by its absolute System32 path', async t => {
  for (const [SystemRoot, root] of [['D:\\Windows', 'D:\\Windows'], ['\\\\attacker\\share', 'C:\\Windows'], ['.\\planted', 'C:\\Windows'], [undefined, 'C:\\Windows']]) {
    const environment = SystemRoot === undefined ? { PATH: 'C:\\planted' } : { SystemRoot, PATH: 'C:\\planted' };
    const { provider, tools, killers } = browserHarness({ platform: 'win32', environment, onTaskkill: child => { child.exitCode = 0; } }); t.after(() => provider.shutdown());
    const start = browserCall('browser.session_start', { url: 'https://example.com/' }, 'start_kill');
    await tools['browser.session_start'].preview(start);
    const started = payload(await tools['browser.session_start'].execute({ ...start, authorization: { kind: 'user_confirmation' } }));
    await tools['browser.session_close'].execute(browserCall('browser.session_close', { browser_session_id: started.browser_session_id }, 'close_kill'));
    assert.equal(killers.length, 1, String(SystemRoot));
    assert.ok(path.win32.isAbsolute(killers[0].executable), killers[0].executable);
    assert.equal(killers[0].executable, system32Taskkill(root), `SystemRoot ${SystemRoot}`);
    assert.deepEqual(killers[0].args, ['/PID', '4242', '/T', '/F']); assert.equal(killers[0].options.shell, false);
    assert.deepEqual(killers[0].options.env, { SystemRoot: root, windir: root }, 'taskkill inherits nothing else from the host');
  }
});

test('Copilot process-tree cleanup on Windows spawns taskkill.exe by its absolute System32 path', async () => {
  for (const [SystemRoot, root] of [['E:\\WinNT', 'E:\\WinNT'], ['\\\\attacker\\share', 'C:\\Windows'], ['C:\\Windows\\..\\Users\\Op', 'C:\\Windows'], ['planted', 'C:\\Windows']]) {
    const calls = []; const child = new EventEmitter(); child.pid = 4321; child.exitCode = null; child.signalCode = null;
    await killCopilotProcessTree(child, { platform: 'win32', graceMs: 100, environment: { SystemRoot, Path: 'C:\\planted' }, spawn: (executable, args, options) => { calls.push({ executable, args, options }); const killer = new EventEmitter(); killer.exitCode = 0; killer.signalCode = null; queueMicrotask(() => { child.exitCode = 1; child.treeReaped = true; child.emit('close', 1); killer.emit('close', 0); }); return killer; } });
    assert.ok(path.win32.isAbsolute(calls[0].executable), calls[0].executable);
    assert.equal(calls[0].executable, system32Taskkill(root), SystemRoot);
    assert.deepEqual(calls[0].options.env, { SystemRoot: root, windir: root }, 'taskkill inherits nothing else from the host');
  }
});

// ---- 3. browser.open_url on macOS/Linux never searches PATH -----------------

const statOf = ({ uid = 0, mode = 0o100755, file = true } = {}) => ({ isFile: () => file, uid, mode });

test('posixUrlOpener uses /usr/bin/open on macOS and a root-owned xdg-open from fixed directories only', async () => {
  assert.equal(await posixUrlOpener('darwin', async () => { throw new Error('macOS does not stat'); }), '/usr/bin/open');
  const fixed = files => async candidate => { if (!(candidate in files)) throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' }); return files[candidate]; };
  assert.equal(await posixUrlOpener('linux', fixed({ '/usr/bin/xdg-open': statOf() })), '/usr/bin/xdg-open');
  assert.equal(await posixUrlOpener('linux', fixed({ '/bin/xdg-open': statOf() })), '/bin/xdg-open');
  // A writable or user-owned opener in /usr/bin is skipped, never trusted.
  assert.equal(await posixUrlOpener('linux', fixed({ '/usr/bin/xdg-open': statOf({ mode: 0o100777 }), '/bin/xdg-open': statOf() })), '/bin/xdg-open');
  for (const files of [{}, { '/usr/bin/xdg-open': statOf({ uid: 1000 }) }, { '/usr/bin/xdg-open': statOf({ mode: 0o100775 }) }, { '/usr/bin/xdg-open': statOf({ mode: 0o100644 }) }, { '/usr/bin/xdg-open': statOf({ file: false }) }, { '/usr/local/bin/xdg-open': statOf(), '/home/op/bin/xdg-open': statOf() }]) {
    await assert.rejects(() => posixUrlOpener('linux', fixed(files)), error => error.code === 'browser_open_unavailable', JSON.stringify(files));
  }
});

test('browser.open_url spawns the absolute opener path and refuses when none is trusted', async () => {
  const urlCall = { id: 'call_open', name: 'browser.open_url', arguments: { url: 'https://example.com/docs' } };
  const run = async (platform, statOpener) => {
    const spawned = [];
    const spawn = (executable, args, options) => { spawned.push({ executable, args, options }); const child = new EventEmitter(); child.pid = 77; queueMicrotask(() => child.emit('spawn')); return child; };
    const tools = createSystemTools({ platform, networkProvider: 'browser_open', environment: { PATH: '/tmp/planted:/usr/bin' }, spawn, statOpener });
    return { result: JSON.parse((await tools['browser.open_url'].execute(urlCall)).content?.[0]?.text ?? '{}'), spawned };
  };
  const mac = await run('darwin');
  assert.equal(mac.spawned[0].executable, '/usr/bin/open'); assert.deepEqual(mac.spawned[0].args, ['https://example.com/docs']); assert.equal(mac.result.opened, true);
  const linux = await run('linux', async candidate => { if (candidate === '/usr/bin/xdg-open') return statOf(); throw new Error('ENOENT'); });
  assert.equal(linux.spawned[0].executable, '/usr/bin/xdg-open'); assert.equal(linux.result.opened, true);
  const refused = await run('linux', async () => { throw new Error('ENOENT'); });
  assert.equal(refused.spawned.length, 0, 'nothing is spawned without a trusted opener'); assert.equal(refused.result.code, 'browser_open_unavailable');
});

// ---- 4. usage the engine did not send is not reported -----------------------

test('engine usage omitted on the final frame reaches message.completed as missing, and nothing is learned', async t => {
  const server = http.createServer(async (request, response) => {
    for await (const chunk of request) void chunk;
    if (request.url === '/v1/sessions') { response.writeHead(201, { 'content-type': 'application/json' }); response.end(JSON.stringify({ id: 'sess-1' })); return; }
    if (request.url.startsWith('/v1/cancel/')) { response.writeHead(200, { 'content-type': 'application/json' }); response.end('{"cancelled":true}'); return; }
    response.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': 'req-1' });
    const frame = body => `data: ${JSON.stringify(body)}\n\n`;
    response.end(frame({ id: 'x', choices: [{ delta: { content: 'hel' } }] }) + frame({ id: 'x', choices: [{ delta: { content: 'lo' } }] }) + frame({ id: 'x', choices: [{ delta: {}, finish_reason: 'stop' }] }) + 'data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  const engine = new NativeEngineClient({ endpoint: `http://127.0.0.1:${server.address().port}`, token: 'hardening2-test-token', model: 'fixture', backend: 'fixture-cpu' }); t.after(() => engine.shutdown());
  const controller = new ConversationController({ engine }); const events = [];
  await controller.runTurn({ sessionId: 'ses_hardening2', requestId: 'req_hardening2', message: 'w'.repeat(3000), onEvent: event => events.push(event) });
  const completed = events.find(event => event.event === 'message.completed');
  assert.ok(completed, JSON.stringify(events.map(event => event.event)));
  assert.equal(completed.data.text, 'hello');
  assert.deepEqual(completed.data.usage, {}, 'a frame tally is not presented as engine-reported usage');
  assert.equal(controller.sessions.get('ses_hardening2').bytes_per_token ?? null, null);
});
