import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CopilotCliProvider, createCopilotVersionCheck, parseCopilotAcpFrame, readCopilotAttestation } from '../../host/providers/copilot-cli.mjs';

const call = (arguments_ = {}) => ({ id: 'call_acp_harden', name: 'coding.copilot_ask', arguments: { prompt: 'hello', workspace_id: 'private', context_paths: [], ...arguments_ } });

test('Copilot production refuses ambient cwd and permits only explicit reviewed cwd', async t => {
  const ambient = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true });
  assert.equal(ambient.state(), 'unconfigured'); assert.equal(ambient.cwd, undefined);
  const root = await mkdtemp(join(tmpdir(), 'lae-copilot-cwd-')); t.after(() => rm(root, { recursive: true, force: true }));
  const executable = join(root, 'copilot'); await writeFile(executable, 'fixture');
  const provider = new CopilotCliProvider({ enabled: true, executable, allowlist: [executable], version: '1.2.3', versionCheck: async () => true, cwd: root });
  assert.equal(provider.state(), 'ready');
  const preview = await provider.preview(call()); assert.equal(preview.workspace_id, 'private');
  const denied = await provider.execute({ ...call(), authorization: { kind: 'user_confirmation' } });
  assert.equal(JSON.parse(denied.content[0].text).code, 'provider_permission_insufficient');
});

test('Copilot environment is an explicit minimal allowlist with no ambient credential variables', () => {
  const provider = new CopilotCliProvider({ testOnly: true, environment: { PATH: '/safe', SystemRoot: 'C:\\Windows', WINDIR: 'C:\\Windows', SECRET_TOKEN: 'secret', COPILOT_HOME: 'credential-store' } });
  assert.deepEqual(provider.safeEnvironment(), { PATH: '/safe', SystemRoot: 'C:\\Windows', WINDIR: 'C:\\Windows' });
});

test('executable identity replacement between preview and dispatch fails before spawn', async () => {
  let identityCalls = 0; let spawned = 0;
  const provider = new CopilotCliProvider({ testOnly: true, enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => ({ text: '', files: [] }), executableIdentity: async value => ({ canonicalPath: value, dev: 1, ino: ++identityCalls, size: 1, mtimeMs: 1, nlink: 1 }), cwdIdentity: async value => ({ canonicalPath: value, dev: 1, ino: 9, size: 0, mtimeMs: 1, nlink: 1 }), spawn: () => { spawned += 1; throw new Error('must not spawn'); } });
  const request = call(); await provider.preview(request);
  const result = await provider.execute({ ...request, authorization: { kind: 'user_confirmation' } });
  assert.equal(JSON.parse(result.content[0].text).code, 'copilot_policy_denied'); assert.equal(spawned, 0);
});

test('ACP frames reject duplicate/unknown/deep/oversized data and unsafe correlations', () => {
  assert.deepEqual(parseCopilotAcpFrame('{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}').result, { protocolVersion: 1 });
  for (const frame of [
    '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"protocolVersion":1}}',
    '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1},"extra":true}',
    JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { sessionId: 's', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'ok' }, extra: true } } }),
    JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { sessionId: 's', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'x'.repeat(20000) } } } }),
    JSON.stringify({ jsonrpc: '2.0', method: 'unknown', params: {} }),
    `${'['.repeat(10)}0${']'.repeat(10)}`,
    'x'.repeat(65537)
  ]) assert.throws(() => parseCopilotAcpFrame(frame));
});

test('ACP session updates require exact session correlation and known update shape', () => {
  assert.throws(() => parseCopilotAcpFrame(JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { sessionId: 's', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'html', text: 'no' } } } })));
  assert.throws(() => parseCopilotAcpFrame(JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { sessionId: 's', update: { sessionUpdate: 'unknown' } } })));
});

test('ACP command and permission payloads are strict bounded objects', () => {
  const command = { jsonrpc: '2.0', method: 'session/update', params: { sessionId: 's', update: { sessionUpdate: 'available_commands_update', availableCommands: [{ name: 'help', description: 'bounded' }] } } };
  assert.equal(parseCopilotAcpFrame(JSON.stringify(command)).params.update.availableCommands[0].name, 'help');
  const permission = { jsonrpc: '2.0', id: 2, method: 'session/request_permission', params: { options: [{ optionId: 'allow_once', name: 'Allow once', kind: 'allow_once' }] } };
  assert.equal(parseCopilotAcpFrame(JSON.stringify(permission)).params.options[0].optionId, 'allow_once');
  for (const invalid of [
    { ...command, params: { ...command.params, update: { ...command.params.update, availableCommands: [{ description: 'missing name' }] } } },
    { ...permission, params: { options: [{ optionId: 'x', name: 'x', kind: 'x', extra: true }] } },
    { ...permission, params: { options: [{ optionId: 'x', name: 'x' }] } },
  ]) assert.throws(() => parseCopilotAcpFrame(JSON.stringify(invalid)));
});

test('production version probe and ACP spawn carry explicit cwd and never ambient env', async () => {
  let versionOptions; let acpOptions; let acpArgs;
  const versionChild = { stdout: { on(_event, handler) { queueMicrotask(() => { handler(Buffer.from('v1.2.3')); }); } }, once(event, handler) { if (event === 'close') queueMicrotask(() => handler(0)); }, kill() {} };
  const check = createCopilotVersionCheck({ expectedVersion: '1.2.3', spawn(_exe, _args, options) { versionOptions = options; return versionChild; } });
  assert.equal(await check('/approved/copilot', undefined, '/reviewed/workspace'), true); assert.equal(versionOptions.cwd, '/reviewed/workspace'); assert.deepEqual(versionOptions.env, {}); assert.equal('SECRET_TOKEN' in versionOptions.env, false);
  const provider = new CopilotCliProvider({ testOnly: true, enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3', cwd: '/reviewed/workspace', versionCheck: async () => true, readContext: async () => ({ text: '', files: [] }), spawn(_exe, args, options) {
    acpArgs = args; acpOptions = options; const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.exitCode = null; child.signalCode = null; child.stdin = { write(line) { const request = JSON.parse(line); queueMicrotask(() => { if (request.method === 'initialize') child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { protocolVersion: 1 } })}\n`)); else if (request.method === 'session/new') child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { sessionId: 'session-1' } })}\n`)); else if (request.method === 'session/prompt') { child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { sessionId: 'session-1', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'ok' } } } })}\n`)); child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { stopReason: 'end_turn' } })}\n`)); child.exitCode = 0; child.emit('close', 0); } }); }, end() {} }; child.kill = () => { child.exitCode = 0; child.emit('close', 0); }; return child;
  } });
  const request = call(); await provider.preview(request); const binding = { operation_id: 'act_' + '1'.repeat(32), operation_digest: 'a'.repeat(64), arguments_digest: 'b'.repeat(64), preview_digest: 'c'.repeat(64) }; const output = await provider.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } }); assert.equal(JSON.parse(output.content[0].text).stdout, 'ok'); assert.ok(readCopilotAttestation(output)); assert.equal(acpOptions.cwd, '/reviewed/workspace'); assert.deepEqual(acpOptions.env, {}); assert.equal(acpArgs.includes('ok'), false); assert.equal(acpArgs.includes('--acp'), true);
});

test('production filesystem identity is non-authorizing without a broker-issued handle', async t => {
  const root = await mkdtemp(join(tmpdir(), 'lae-copilot-private-')); t.after(() => rm(root, { recursive: true, force: true }));
  const executable = join(root, 'copilot'); await writeFile(executable, 'fixture');
  const provider = new CopilotCliProvider({ enabled: true, executable, allowlist: [executable], versionCheck: async () => true, cwd: root });
  await provider.preview(call());
  const output = await provider.execute({ ...call(), authorization: { kind: 'user_confirmation' }, internal: { journal_binding: { operation_id: 'act_' + 'a'.repeat(32), operation_digest: 'a'.repeat(64), arguments_digest: 'b'.repeat(64), preview_digest: 'c'.repeat(64) } } });
  assert.equal(JSON.parse(output.content[0].text).code, 'copilot_policy_denied');
});

test('ACP cleanup ambiguity fails closed after cancellation/timeout', async () => {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = { write() {}, end() {} }; child.exitCode = null; child.signalCode = null;
  const provider = new CopilotCliProvider({ testOnly: true, enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], cwd: '/reviewed/workspace', versionCheck: async () => true, readContext: async () => ({ text: '', files: [] }), timeoutMs: 100, spawn: () => child, killProcess: async () => { throw new Error('tree state unknown'); } });
  const output = await provider.runAcp({ id: 'cleanup_call', name: 'coding.copilot_ask' }, 'hello');
  assert.equal(JSON.parse(output.content[0].text).code, 'provider_failed');
});
