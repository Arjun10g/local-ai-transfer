import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { chmod, mkdtemp, realpath, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ProcessRunProvider, createProcessRunTools, terminateProcessTree } from '../../host/tools/local/process-run.mjs';
import { WorkspacePolicy } from '../../host/tools/local/workspace-policy.mjs';
import { OperatorGrantControl, OperatorGrantStore } from '../../host/providers/operator-grants.mjs';
import { mergeConfig } from '../../host/agent/config.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { ActionJournal } from '../../host/agent/action-journal.mjs';

const call = (arguments_, id = 'process_call') => ({ id, name: 'process.run_allowlisted', arguments: arguments_ });
const value = result => JSON.parse(result.content[0].text);
const action = overrides => ({ executable: '/approved/probe', args: ['--name', '{name}'], workspace_id: 'project', cwd: '', parameters: { name: { type: 'string', max_length: 64, argv_kind: 'identifier' }, input: { type: 'string', max_length: 128 } }, stdin_parameter: 'input', timeout_ms: 1000, ...overrides });

class FakeChild extends EventEmitter {
  constructor({ output = true, overflow = false, stderrOverflow = false } = {}) { super(); this.stdout = new EventEmitter(); this.stderr = new EventEmitter(); this.stdin = { end: text => { this.input = text; if (output) queueMicrotask(() => { if (overflow) this.stdout.emit('data', Buffer.alloc(32769, 65)); else { this.stdout.emit('data', Buffer.from([0xf0])); this.stdout.emit('data', Buffer.from([0x9f, 0x98, 0x80])); } if (stderrOverflow) this.stderr.emit('data', Buffer.alloc(32769, 66)); this.emit('close', 0); }); } }; this.input = null; }
  kill() { this.emit('close', null); }
}

async function setup({ spawn, killProcess, taskkillSpawn, resolveExecutable, statExecutable, statResolvedExecutable, grantControl, actions = { probe: action({}) }, workspace = {} } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'lae-process-')); const policy = new WorkspacePolicy([{ id: 'project', path: root, read: workspace.read ?? true, write: workspace.write ?? true }]); const provider = new ProcessRunProvider({ enabled: true, actions, workspacePolicy: policy, grantControl, spawn, killProcess, taskkillSpawn, resolveExecutable: resolveExecutable ?? (async value => value), statExecutable: statExecutable ?? (async () => ({ isSymbolicLink: () => false })), statResolvedExecutable: statResolvedExecutable ?? (async () => ({ isFile: () => true, mode: 0o755 })), environment: { PATH: '/secret', SystemRoot: '/windows' } }); return { provider, root, tools: createProcessRunTools(provider) };
}
async function journal(t) { const path = await realpath(await mkdtemp(join(tmpdir(), 'lae-process-journal-'))); await chmod(path, 0o700); t.after(() => rm(path, { recursive: true, force: true })); return ActionJournal.open({ directory: path }); }

test('process.run_allowlisted uses fixed argv, workspace cwd, minimal env, bounded UTF-8, and stdin only when declared', async () => {
  let launched; const setupValue = await setup({ spawn: (executable, args, options) => { launched = { executable, args, options }; const child = new FakeChild(); return child; } }); const request = call({ action_id: 'probe', parameters: { name: 'safe', input: 'literal input' } }); const preview = await setupValue.tools.preview(request); assert.equal(preview.action_id, 'probe'); assert.equal(await setupValue.tools.confirmationRequired(request), true); const output = value(await setupValue.tools.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.stdout, '😀'); assert.deepEqual(launched.args, ['--name', 'safe']); assert.equal(launched.options.shell, false); assert.equal(launched.options.env.PATH, undefined); assert.equal(launched.options.env.SystemRoot, '/windows'); assert.equal(launched.options.cwd, await realpath(setupValue.root)); assert.equal(launched.options.detached, true);
});

test('process action config and calls reject interpreters, option injection, traversal, unknown fields, and read-only cwd', async () => {
  for (const executable of ['/bin/sh', '/usr/bin/python3', '/usr/bin/node', 'probe', '\\\\server\\probe']) await assert.rejects(() => setup({ actions: { probe: action({ executable }) } }), /not permitted|absolute/);
  const configured = await setup({ spawn: () => new FakeChild() }); let index = 0; for (const bad of [{ action_id: 'probe', parameters: { name: '--help' } }, { action_id: 'probe', parameters: { name: '../escape' } }, { action_id: 'probe', parameters: { name: 'ok', unknown: true } }, { action_id: 'probe', parameters: {} }]) { const request = call(bad, `bad_${index++}`); await assert.rejects(() => configured.tools.preview(request), error => error.code === 'invalid_tool_arguments'); }
  const readonly = await setup({ workspace: { write: false } }); await assert.rejects(() => readonly.tools.preview(call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'readonly_call')), error => error.code === 'provider_failed' || error.code === 'write_not_allowed');
});

test('process action validates scalar argv kinds and rejects unsafe config before launch', async () => {
  const scalarAction = action({ args: ['--count', '{count}', '--enabled', '{enabled}'], parameters: { count: { type: 'integer', minimum: 0, maximum: 10, argv_kind: 'scalar' }, enabled: { type: 'boolean', argv_kind: 'scalar' } }, stdin_parameter: undefined });
  let launched;
  const configured = await setup({ actions: { scalar: scalarAction }, spawn: (executable, args) => { launched = { executable, args }; return new FakeChild(); } });
  const request = call({ action_id: 'scalar', parameters: { count: 3, enabled: true } }, 'scalar_call'); await configured.tools.preview(request); const output = value(await configured.tools.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.stdout, '😀'); assert.deepEqual(launched.args, ['--count', '3', '--enabled', 'true']);
  assert.equal(configured.tools.parameters.oneOf[0].properties.parameters.properties.count.minimum, 0);
  await assert.rejects(() => configured.tools.preview(call({ action_id: 'scalar', parameters: { count: -1, enabled: true } }, 'negative_scalar_call')), error => error.code === 'invalid_tool_arguments');
  for (const executable of ['/bin/sh', '/usr/bin/powershell_ise', '/usr/bin/rundll32', '/tmp/helper.cmd', '/tmp/helper.ps1', '/tmp/helper.js']) assert.throws(() => mergeConfig({ process_actions: { enabled: true, actions: { bad: action({ executable }) } } }), /executable invalid|not permitted/);
  assert.throws(() => mergeConfig({ process_actions: { enabled: true, actions: { bad: action({ parameters: { count: { type: 'integer', argv_kind: 'identifier' } }, args: ['{count}'] }) } } }), /argv|parameter/);
  assert.throws(() => mergeConfig({ process_actions: { enabled: true, actions: { constructor: action({}) } } }), /process action/);
});

test('process schema is exact per configured action and controller rejects undeclared parameters', async t => {
  const configured = await setup({ spawn: () => new FakeChild() }); const schema = configured.tools.parameters;
  assert.equal(schema.additionalProperties, false); assert.deepEqual(schema.properties.action_id.enum, ['probe']); assert.equal(schema.oneOf.length, 1); assert.equal(schema.oneOf[0].properties.parameters.additionalProperties, false); assert.deepEqual(schema.oneOf[0].required, ['action_id', 'parameters']);
  let advertised;
  const engine = { async *generate({ tools }) { advertised = tools.find(item => item.function.name === 'process.run_allowlisted'); yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'process_schema_call', name: 'process.run_allowlisted', arguments: { action_id: 'probe', parameters: { name: 'safe', unknown: true } } }) }; } };
  const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: { 'process.run_allowlisted': configured.tools } }); const output = await controller.runTurn({ sessionId: 'process_schema_session', requestId: 'process_schema_request', message: 'run it' }); assert.equal(output.error, 'invalid_tool_arguments'); assert.deepEqual(advertised.function.parameters, schema);
});

test('process termination uses an injectable bounded Windows tree-kill path', async () => {
  const child = new FakeChild({ output: false }); child.pid = 42; child.exitCode = null; let launched;
  await terminateProcessTree(child, undefined, 'win32', (executable, args, options) => { launched = { executable, args, options }; const killer = new EventEmitter(); queueMicrotask(() => { child.exitCode = 1; child.emit('close', 1); killer.emit('close', 0); }); return killer; });
  assert.deepEqual(launched.args, ['/PID', '42', '/T', '/F']); assert.equal(launched.executable, 'taskkill.exe'); assert.equal(launched.options.shell, false);
});

test('process action binds grant generation and is at-most-once across timeout/overflow/replay', async () => {
  const store = new OperatorGrantStore(); const control = new OperatorGrantControl({ store, bindings: [{ capability: 'local.process:probe', provider: 'local_process', accountFingerprint: 'local_host', scope: 'probe', label: 'Probe' }] }); control.grant('local.process:probe', 60000); let dispatches = 0; const configured = await setup({ grantControl: control, spawn: () => { dispatches += 1; return new FakeChild(); } }); const request = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'grant_call'); await configured.tools.preview(request); assert.equal(await configured.tools.confirmationRequired(request), false); const authorization = await configured.tools.authorize(request); assert.equal(authorization.kind, 'operator_grant'); const output = value(await configured.tools.execute({ ...request, authorization })); assert.equal(output.stdout, '😀'); assert.equal(dispatches, 1); assert.equal(value(await configured.tools.execute({ ...request, authorization })).code, 'process_already_attempted');
  let overflowDispatches = 0; const overflow = await setup({ spawn: () => { overflowDispatches += 1; return new FakeChild({ overflow: true }); } }); const overflowCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'overflow_call'); await overflow.tools.preview(overflowCall); const overflowResult = value(await overflow.tools.execute({ ...overflowCall, authorization: { kind: 'user_confirmation' } })); assert.equal(overflowResult.code, 'provider_response_too_large'); assert.equal(overflowDispatches, 1); assert.equal(value(await overflow.tools.execute({ ...overflowCall, authorization: { kind: 'user_confirmation' } })).code, 'process_already_attempted');
});

test('process cancellation, timeout, spawn failure, child failure, and stderr overflow are typed and cleaned before return', async () => {
  let kills = 0; const timeout = await setup({ actions: { probe: action({ timeout_ms: 100 }) }, killProcess: async child => { await new Promise(resolve => setTimeout(resolve, 15)); kills += 1; child.kill(); }, spawn: () => new FakeChild({ output: false }) }); const timeoutCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'timeout_call'); await timeout.tools.preview(timeoutCall); const started = Date.now(); assert.equal(value(await timeout.tools.execute({ ...timeoutCall, authorization: { kind: 'user_confirmation' } })).code, 'provider_timeout'); assert.ok(Date.now() - started >= 15); assert.equal(kills, 1);
  const cancel = await setup({ killProcess: async child => { kills += 1; child.kill(); }, spawn: () => new FakeChild({ output: false }) }); const controller = new AbortController(); const cancelCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'cancel_call'); await cancel.tools.preview(cancelCall); const pending = cancel.tools.execute({ ...cancelCall, authorization: { kind: 'user_confirmation' }, signal: controller.signal }); controller.abort(); assert.equal(value(await pending).code, 'provider_cancelled');
  const spawnError = await setup({ spawn: () => { throw new Error('not disclosed'); } }); const spawnCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'spawn_error_call'); await spawnError.tools.preview(spawnCall); assert.equal(value(await spawnError.tools.execute({ ...spawnCall, authorization: { kind: 'user_confirmation' } })).code, 'provider_failed');
  const childError = await setup({ spawn: () => { const child = new FakeChild({ output: false }); queueMicrotask(() => child.emit('error', new Error('not disclosed'))); return child; } }); const childCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'child_error_call'); await childError.tools.preview(childCall); assert.equal(value(await childError.tools.execute({ ...childCall, authorization: { kind: 'user_confirmation' } })).code, 'provider_failed');
  const stderr = await setup({ spawn: () => new FakeChild({ stderrOverflow: true }) }); const stderrCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'stderr_overflow_call'); await stderr.tools.preview(stderrCall); assert.equal(value(await stderr.tools.execute({ ...stderrCall, authorization: { kind: 'user_confirmation' } })).code, 'provider_response_too_large');
});

test('operator grant revocation aborts an in-flight process and prevents success', async () => {
  const store = new OperatorGrantStore(); const control = new OperatorGrantControl({ store, bindings: [{ capability: 'local.process:probe', provider: 'local_process', accountFingerprint: 'local_host', scope: 'probe', label: 'Probe' }] }); control.grant('local.process:probe', 60000); let killed = 0; const configured = await setup({ grantControl: control, spawn: () => new FakeChild({ output: false }), killProcess: async child => { killed += 1; child.kill(); } }); const request = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'revoke_process_call'); await configured.tools.preview(request); const authorization = await configured.tools.authorize(request); const pending = configured.tools.execute({ ...request, authorization }); await new Promise(resolve => setTimeout(resolve, 25)); control.revoke('local.process:probe'); assert.equal(value(await pending).code, 'provider_permission_revoked'); assert.equal(killed, 1);
});

test('process executable preview requires a regular non-symlink and binds its canonical path before dispatch', async () => {
  const symlink = await setup({ statExecutable: async () => ({ isSymbolicLink: () => true }) });
  await assert.rejects(() => symlink.tools.preview(call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'symlink_executable')), error => error.code === 'provider_executable_invalid');
  const directory = await setup({ statResolvedExecutable: async () => ({ isFile: () => false }) });
  await assert.rejects(() => directory.tools.preview(call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'directory_executable')), error => error.code === 'provider_executable_invalid');
  const missing = await setup({ resolveExecutable: async () => { throw new Error('missing'); } });
  await assert.rejects(() => missing.tools.preview(call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'missing_executable')), error => error.code === 'provider_executable_invalid');

  let launched;
  const canonical = await setup({ resolveExecutable: async () => '/canonical/probe', spawn: (executable, args) => { launched = { executable, args }; return new FakeChild(); } });
  const canonicalCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'canonical_executable');
  const preview = await canonical.tools.preview(canonicalCall);
  assert.equal(preview.executable, '/canonical/probe');
  assert.equal(value(await canonical.tools.execute({ ...canonicalCall, authorization: { kind: 'user_confirmation' } })).stdout, '😀');
  assert.equal(launched.executable, '/canonical/probe');

  let resolutions = 0; let dispatches = 0;
  const changed = await setup({ resolveExecutable: async () => (++resolutions === 1 ? '/canonical/probe' : '/replaced/probe'), spawn: () => { dispatches += 1; return new FakeChild(); } });
  const changedCall = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, 'changed_executable');
  await changed.tools.preview(changedCall);
  assert.equal(value(await changed.tools.execute({ ...changedCall, authorization: { kind: 'user_confirmation' } })).code, 'provider_permission_insufficient');
  assert.equal(dispatches, 0);
});

test('process executable authorization is an exact, typed object', async () => {
  const invalid = [null, {}, { kind: 'policy' }, { kind: 'user_confirmation', extra: true }, { kind: 'operator_grant' }, { kind: 'operator_grant', generation: 'ABCDEF0123456789ABCDEF0123456789' }, { kind: 'operator_grant', generation: '00000000000000000000000000000000', extra: true }];
  const configured = await setup({ spawn: () => new FakeChild() });
  for (const [index, authorization] of invalid.entries()) {
    const request = call({ action_id: 'probe', parameters: { name: 'safe', input: '' } }, `invalid_authorization_${index}`);
    await configured.tools.preview(request);
    assert.equal(value(await configured.tools.execute({ ...request, authorization })).code, 'provider_permission_insufficient');
  }
});

test('process timeout limits stay in parity across runtime config and JSON schema', async () => {
  const schema = JSON.parse(await readFile(new URL('../../contracts/config-schema/v0.1.0.json', import.meta.url), 'utf8'));
  const timeoutSchema = schema.properties.process_actions.properties.actions.additionalProperties.properties.timeout_ms;
  assert.equal(timeoutSchema.minimum, 100);
  assert.equal(timeoutSchema.maximum, 110000);
  assert.throws(() => mergeConfig({ process_actions: { enabled: true, actions: { probe: action({ timeout_ms: 110001 }) } } }), /timeout/);
  assert.doesNotThrow(() => mergeConfig({ process_actions: { enabled: true, actions: { probe: action({ timeout_ms: 110000 }) } } }));
});
