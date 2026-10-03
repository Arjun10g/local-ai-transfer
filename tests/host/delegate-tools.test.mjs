import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { ConversationController, DELEGATE_TOOL_NAMES } from '../../host/agent/controller.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { createDelegateToolRegistry } from '../../host/delegate/tools.mjs';
import { ScriptEngine, setup, tempDir } from './delegate-helpers.mjs';

// A registry holding everything a delegated job must never touch, each with
// an execute spy, beside the legitimately read-only tools.
function hostileRegistry(ran) {
  const tool = (name, meta) => ({ name, version: '1.0.0', network: false, timeout_ms: 1000, ...meta, execute: async call => { ran.push(name); return makeToolResult({ id: call.id, name, text: '{"ok":true}' }); } });
  return {
    'system.get_info': tool('system.get_info', { risk_tier: 'T0', side_effect: 'none', requires_confirmation: false }),
    'fs.read_text': tool('fs.read_text', { risk_tier: 'T0', side_effect: 'none', requires_confirmation: false }),
    'fs.list': tool('fs.list', { risk_tier: 'T0', side_effect: 'none', requires_confirmation: false }),
    'fs.search_text': tool('fs.search_text', { risk_tier: 'T0', side_effect: 'none', requires_confirmation: false }),
    'fs.write_new': tool('fs.write_new', { risk_tier: 'T2', side_effect: 'create', requires_confirmation: true }),
    'clipboard.read': tool('clipboard.read', { risk_tier: 'T1', side_effect: 'read_sensitive', requires_confirmation: false }),
    'clipboard.write': tool('clipboard.write', { risk_tier: 'T2', side_effect: 'write_sensitive', requires_confirmation: true }),
    'process.run_allowlisted': tool('process.run_allowlisted', { version: '0.1.0', risk_tier: 'T3', side_effect: 'process_execution', network: true, data_egress: 'operator_configured', requires_confirmation: true }),
    'browser.open_url': tool('browser.open_url', { risk_tier: 'T1', side_effect: 'external_navigation', network: true, data_egress: 'external_destination', requires_confirmation: true }),
    'app.open': tool('app.open', { risk_tier: 'T1', side_effect: 'launch', requires_confirmation: true }),
  };
}
const journal = { health: () => ({ state: 'ready', error: null }), prepare: async () => ({ operation_id: `act_${'0'.repeat(32)}` }), authorize: async () => ({}), dispatch: async () => {}, acknowledge: async () => {}, beginReconciliation: async () => {}, complete: async () => {}, cancel: async () => {}, failDefinitive: async () => {}, markUnknown: async () => {} };

test('tools: delegate offers only the read-only set, and the fs trio only with a scope', async () => {
  const engine = new ScriptEngine(); const ran = [];
  // The journal is ready, so in `auto` mode every one of these would be offered.
  const controller = new ConversationController({ engine, toolRegistry: hostileRegistry(ran), actionJournal: journal });
  await controller.runTurn({ sessionId: 'ses_auto_0001', message: 'hi', requestId: 'req_auto_0001' });
  assert.ok(engine.calls[0].toolNames.includes('fs.write_new') && engine.calls[0].toolNames.includes('process.run_allowlisted'), 'the registry really is hostile');
  await controller.runTurn({ sessionId: 'ses_dlg_00001', message: 'hi', requestId: 'req_dlg_00001', tools: 'delegate', delegateScope: { workspaces: [] } });
  assert.deepEqual(engine.calls[1].toolNames.sort(), ['system.get_info', 'time.now']);
  await controller.runTurn({ sessionId: 'ses_dlg_00002', message: 'hi', requestId: 'req_dlg_00002', tools: 'delegate', delegateScope: { workspaces: ['shared'] } });
  assert.deepEqual(engine.calls[2].toolNames.sort(), ['fs.list', 'fs.read_text', 'fs.search_text', 'system.get_info', 'time.now']);
  assert.deepEqual([...DELEGATE_TOOL_NAMES].sort(), ['fs.list', 'fs.read_text', 'fs.search_text', 'system.get_info', 'time.now']);
});

test('a model that calls a write, process, clipboard, browser, or app tool in delegate mode fails closed', async () => {
  for (const [name, args] of [['fs.write_new', { workspace_id: 'shared', path: 'x.txt', content: 'x' }], ['process.run_allowlisted', { action_id: 'a' }], ['clipboard.write', { text: 'x' }], ['clipboard.read', {}], ['browser.open_url', { url: 'https://example.com' }], ['app.open', { app_id: 'a' }]]) {
    const engine = new ScriptEngine(); const ran = []; const events = [];
    const controller = new ConversationController({ engine, toolRegistry: hostileRegistry(ran), actionJournal: journal });
    engine.plan.push({ call: { name, arguments: args } });
    const result = await controller.runTurn({ sessionId: 'ses_dlg_00003', message: 'do it', requestId: 'req_dlg_00003', tools: 'delegate', delegateScope: { workspaces: ['shared'] }, onEvent: event => events.push(event.event) });
    assert.equal(result.error, 'tool_not_offered', name);
    assert.deepEqual(ran, [], `${name} never executed`);
    assert.equal(events.includes('tool.confirmation_required'), false, `${name} never asked anyone`);
    assert.equal(events.includes('tool.started'), false);
  }
});

test('a file tool outside the scope, or with no scope, fails closed before it runs', async () => {
  const engine = new ScriptEngine(); const ran = [];
  const controller = new ConversationController({ engine, toolRegistry: hostileRegistry(ran) });
  engine.plan.push({ call: { name: 'fs.read_text', arguments: { workspace_id: 'private', path: 'secret.txt' } } });
  const outside = await controller.runTurn({ sessionId: 'ses_dlg_00004', message: 'read', requestId: 'req_dlg_00004', tools: 'delegate', delegateScope: { workspaces: ['shared'] } });
  assert.equal(outside.error, 'workspace_not_offered');
  engine.plan.push({ call: { name: 'fs.read_text', arguments: { workspace_id: 'shared', path: 'a.txt' } } });
  const noScope = await controller.runTurn({ sessionId: 'ses_dlg_00005', message: 'read', requestId: 'req_dlg_00005', tools: 'delegate', delegateScope: { workspaces: [] } });
  assert.equal(noScope.error, 'tool_not_offered');
  assert.deepEqual(ran, []);
  engine.plan.push({ call: { name: 'fs.read_text', arguments: { workspace_id: 'shared', path: 'a.txt' } } }, { text: 'done' });
  const inside = await controller.runTurn({ sessionId: 'ses_dlg_00006', message: 'read', requestId: 'req_dlg_00006', tools: 'delegate', delegateScope: { workspaces: ['shared'] } });
  assert.equal(inside.state, 'COMPLETED'); assert.deepEqual(ran, ['fs.read_text']);
});

test('delegate mode refuses a confirmation-requiring read tool and an unscoped or misused mode', async () => {
  const engine = new ScriptEngine(); const ran = [];
  const registry = hostileRegistry(ran);
  registry['system.get_info'] = { ...registry['system.get_info'], requires_confirmation: true };
  const controller = new ConversationController({ engine, toolRegistry: registry });
  await controller.runTurn({ sessionId: 'ses_dlg_00007', message: 'hi', requestId: 'req_dlg_00007', tools: 'delegate', delegateScope: { workspaces: [] } });
  assert.deepEqual(engine.calls[0].toolNames, ['time.now'], 'a tool that wants a click is not offered');
  engine.plan.push({ call: { name: 'system.get_info', arguments: {} } });
  const asked = await controller.runTurn({ sessionId: 'ses_dlg_00008', message: 'hi', requestId: 'req_dlg_00008', tools: 'delegate', delegateScope: { workspaces: [] } });
  assert.equal(asked.error, 'tool_not_offered'); assert.deepEqual(ran, []);
  await assert.rejects(controller.runTurn({ sessionId: 'ses_dlg_00009', message: 'hi', requestId: 'req_dlg_00009', tools: 'delegate' }), { code: 'invalid_delegate_scope' });
  await assert.rejects(controller.runTurn({ sessionId: 'ses_dlg_00009', message: 'hi', requestId: 'req_dlg_00010', tools: 'auto', delegateScope: { workspaces: [] } }), { code: 'invalid_delegate_scope' });
  await assert.rejects(controller.runTurn({ sessionId: 'ses_dlg_00009', message: 'hi', requestId: 'req_dlg_00011', tools: 'delegate', delegateScope: { workspaces: ['../x'] } }), { code: 'invalid_delegate_scope' });
});

test('the delegate registry holds read-only tools over flagged folders only', async t => {
  const dir = await tempDir(t);
  const roots = [{ id: 'shared', path: join(dir, 'shared'), delegate: true }, { id: 'private', path: join(dir, 'private'), write: true }, { id: 'notread', path: join(dir, 'n'), read: false }];
  for (const name of ['shared', 'private', 'n']) await mkdir(join(dir, name));
  const flagged = createDelegateToolRegistry({ workspaceRoots: roots, platform: 'linux' });
  assert.deepEqual(Object.keys(flagged.registry).sort(), ['fs.list', 'fs.read_text', 'fs.search_text', 'system.get_info']);
  assert.deepEqual(flagged.workspaceIds, ['shared']);
  const none = createDelegateToolRegistry({ workspaceRoots: roots.map(({ delegate, ...rest }) => rest), platform: 'linux' });
  assert.deepEqual(Object.keys(none.registry), ['system.get_info']); assert.deepEqual(none.workspaceIds, []);
});

test('end to end: allow_files reads a flagged folder; an unflagged folder or a write fails the job', async t => {
  const dir = await tempDir(t);
  await mkdir(join(dir, 'shared')); await mkdir(join(dir, 'private'));
  await writeFile(join(dir, 'shared', 'note.txt'), 'meeting at noon');
  await writeFile(join(dir, 'private', 'secret.txt'), 'PRIVATE-DO-NOT-LEAK');
  const env = await setup(t, { workspaceRoots: [{ id: 'shared', path: join(dir, 'shared'), delegate: true }, { id: 'private', path: join(dir, 'private'), write: true }] });
  const run = async (extra, ...steps) => {
    const id = (await env.submit(extra)).json.job_id;
    env.engine.plan.push(...steps);
    await env.ui('POST', `/api/delegation/jobs/${id}/decision`, { approved: true });
    return env.until(id, job => ['completed', 'failed'].includes(job.status));
  };
  let toolResult = null;
  const ok = await run({ allow_files: true }, { call: { name: 'fs.read_text', arguments: { workspace_id: 'shared', path: 'note.txt' } } }, args => { toolResult = args.messages.at(-1).content; return { text: 'It says the meeting is at noon.' }; });
  assert.equal(ok.status, 'completed'); assert.match(toolResult, /meeting at noon/);
  const offered = env.engine.calls.at(-2);
  assert.deepEqual(offered.toolNames.sort(), ['fs.list', 'fs.read_text', 'fs.search_text', 'system.get_info', 'time.now']);
  const userMessage = offered.messages.find(message => message.role === 'user').content;
  assert.match(userMessage, /workspace_id\): shared\./); assert.doesNotMatch(userMessage, /private/);
  const leak = await run({ allow_files: true }, { call: { name: 'fs.read_text', arguments: { workspace_id: 'private', path: 'secret.txt' } } });
  assert.equal(leak.status, 'failed'); assert.equal(leak.error.code, 'tool_not_offered');
  const write = await run({ allow_files: true }, { call: { name: 'fs.write_new', arguments: { workspace_id: 'shared', path: 'x.txt', content: 'x' } } });
  assert.equal(write.status, 'failed'); assert.equal(write.error.code, 'tool_not_offered');
  const noFiles = await run({}, { call: { name: 'fs.list', arguments: { workspace_id: 'shared' } } });
  assert.equal(noFiles.status, 'failed'); assert.deepEqual(env.engine.calls.at(-1).toolNames.sort(), ['system.get_info', 'time.now']);
  for (const response of env.responses) assert.equal(response.text.includes('PRIVATE-DO-NOT-LEAK'), false);
});

test('allow_files with no flagged folder: the card says so and no file tool is offered', async t => {
  const env = await setup(t);
  const id = (await env.submit({ allow_files: true })).json.job_id;
  const card = (await env.ui('GET', '/api/delegation')).json.pending[0];
  assert.equal(card.files_requested, true); assert.equal(card.allow_files, false); assert.deepEqual(card.workspaces, []);
  await env.ui('POST', `/api/delegation/jobs/${id}/decision`, { approved: true });
  await env.until(id, job => job.status === 'completed');
  assert.deepEqual(env.engine.calls.at(-1).toolNames.sort(), ['system.get_info', 'time.now']);
});
