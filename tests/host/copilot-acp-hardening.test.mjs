import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CopilotCliProvider, parseCopilotAcpFrame } from '../../host/providers/copilot-cli.mjs';

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
