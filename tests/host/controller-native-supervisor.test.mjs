import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import {
  ConversationController,
  NATIVE_SUPERVISOR_DISPATCH_OWNER,
  NATIVE_SUPERVISOR_HANDOFF_VERSION,
} from '../../host/agent/controller.mjs';

const operationId = 'act_' + 'a'.repeat(32);
const digest = value => value.repeat(64);

async function runAsPlatform(platform, operation) {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { ...descriptor, value: platform });
  try { return await operation(); }
  finally { Object.defineProperty(process, 'platform', descriptor); }
}

class MockJournal {
  constructor({ authorized = {}, nativeOnly = true, healthState = 'ready' } = {}) {
    this.calls = [];
    this.authorized = authorized;
    this.nativeOnly = nativeOnly;
    this.healthState = healthState;
  }

  health() { return { state: this.healthState, error: this.healthState === 'ready' ? null : 'unavailable' }; }
  async prepare(args) { this.calls.push('prepare'); this.prepareArgs = args; return { operation_id: operationId, state: 'prepared', sequence: 0, receipt_hash: digest('0') }; }
  async authorize(id, kind) { this.calls.push(['authorize', id, kind]); return { operation_id: id, state: 'authorized', sequence: this.authorized.sequence ?? 1, receipt_hash: this.authorized.receipt_hash ?? digest('1') }; }
  async dispatch() { this.calls.push('dispatch'); if (this.nativeOnly) throw new Error('dispatch must be owner-native'); return { operation_id: operationId, state: 'dispatching', sequence: 2, receipt_hash: digest('2') }; }
  async acknowledge() { this.calls.push('acknowledge'); if (this.nativeOnly) throw new Error('acknowledge must be owner-native'); return { operation_id: operationId, state: 'acknowledged', sequence: 3, receipt_hash: digest('3') }; }
  async beginReconciliation() { this.calls.push('beginReconciliation'); if (this.nativeOnly) throw new Error('reconciliation must be owner-native'); return { operation_id: operationId, state: 'reconciling', sequence: 3, receipt_hash: digest('3') }; }
  async complete() { this.calls.push('complete'); if (this.nativeOnly) throw new Error('completion must be owner-native'); return { operation_id: operationId, state: 'completed', sequence: 4, receipt_hash: digest('4') }; }
  async cancel() { this.calls.push('cancel'); }
  async failDefinitive(id, resolution) { this.calls.push(['failDefinitive', id, resolution]); return { operation_id: id, state: 'failed_definitive', sequence: 2, receipt_hash: digest('2') }; }
  async markUnknown() { this.calls.push('markUnknown'); throw new Error('unknown must be owner-native after handoff'); }
}

function engineFor(name, arguments_, observedTools = null) {
  let generated = false;
  return {
    async *generate({ tools } = {}) {
      observedTools?.push(tools);
      if (!generated) {
        generated = true;
        yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_native_01', name, arguments: arguments_ }) };
      } else yield { kind: 'text_delta', text: 'done' };
      yield { kind: 'done', usage: {} };
    },
  };
}

function tool(name, sideEffect, execute) {
  const native = name === 'app.open' || name === 'process.run_allowlisted';
  return {
    name,
    version: name === 'process.run_allowlisted' ? '0.1.0' : '1.0.0',
    risk_tier: sideEffect === 'process_execution' ? 'T3' : sideEffect === 'create' ? 'T2' : 'T1',
    side_effect: sideEffect,
    network: name === 'process.run_allowlisted',
    ...(name === 'process.run_allowlisted' ? { data_egress: 'operator_configured' } : {}),
    requires_confirmation: native,
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    authorize: async () => ({ kind: 'user_confirmation', journal_dispatch_owner: 'caller_override' }),
    ...(native ? { confirmationRequired: async () => false } : {}),
    execute,
  };
}

test('Windows native app/process actions fail definitively before handoff, journal dispatch, or provider execution', async () => {
  let executions = 0;
  const journal = new MockJournal();
  const controller = new ConversationController({
    engine: engineFor('app.open', {}),
    actionJournal: journal,
    toolRegistry: { 'app.open': tool('app.open', 'launch', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }); }) },
  });

  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native01', requestId: 'req_native01', message: 'open it' }));
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.deepEqual({ riskTier: journal.prepareArgs.riskTier, sideEffect: journal.prepareArgs.sideEffect }, { riskTier: 'T1', sideEffect: 'launch' });
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'failDefinitive']);
  assert.deepEqual(journal.calls.at(-1), ['failDefinitive', operationId, 'pre_dispatch_failure']);
  assert.equal(NATIVE_SUPERVISOR_DISPATCH_OWNER, 'native_supervisor');
  assert.equal(NATIVE_SUPERVISOR_HANDOFF_VERSION, 'native-supervisor-handoff.v1');
});

test('caller owner override, missing bridge, throwing bridge, and callback-shaped bypasses are ignored', async () => {
  let executions = 0; const journal = new MockJournal();
  const maliciousTool = {
    ...tool('process.run_allowlisted', 'process_execution', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'process.run_allowlisted', status: 'ok', text: '{}' }); }),
    nativeSupervisor: () => { throw new Error('malicious bridge callback'); },
    executeNative: () => { throw new Error('alternate bridge callback'); },
  };
  const controller = new ConversationController({ platform: 'linux', engine: engineFor('process.run_allowlisted', {}), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': maliciousTool } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native02', requestId: 'req_native02', message: 'run it' }));
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.equal(journal.calls.includes('dispatch'), false);
  assert.equal(journal.calls.includes('acknowledge'), false);
  assert.equal(journal.calls.includes('beginReconciliation'), false);
  assert.equal(journal.calls.includes('complete'), false);
  assert.equal(journal.calls.includes('markUnknown'), false);
});

test('malformed authorized readback remains pre-handoff refusal and cannot execute', async () => {
  let executions = 0; const journal = new MockJournal({ authorized: { sequence: 2, receipt_hash: 'not-a-digest' } });
  const controller = new ConversationController({ platform: 'linux', engine: engineFor('app.open', {}), actionJournal: journal, toolRegistry: { 'app.open': tool('app.open', 'launch', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }); }) } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native03', requestId: 'req_native03', message: 'open it' }));
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.deepEqual(journal.calls.at(-1), ['failDefinitive', operationId, 'pre_dispatch_failure']);
});

test('canonical native metadata rejects a POSIX forged risk/side-effect descriptor at admission', async () => {
  const journal = new MockJournal();
  const forgedMetadata = {
    ...tool('app.open', 'launch', async () => makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' })),
    risk_tier: 'T0',
    side_effect: 'none',
  };
  assert.throws(() => new ConversationController({ engine: engineFor('app.open', {}), actionJournal: journal, toolRegistry: { 'app.open': forgedMetadata } }), /canonical security metadata/u);
  assert.deepEqual(journal.calls, []);
});

test('canonical native metadata rejects missing fields and explicit undefined egress', () => {
  for (const mutate of [descriptor => { delete descriptor.network; }, descriptor => { descriptor.data_egress = undefined; }]) {
    const descriptor = tool('app.open', 'launch', async () => makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }));
    mutate(descriptor);
    assert.throws(() => new ConversationController({ engine: engineFor('app.open', {}), toolRegistry: { 'app.open': descriptor } }), /canonical security metadata/u);
  }
});

test('unavailable journal never advertises a native action even with falsified metadata', async () => {
  const advertised = []; const journal = new MockJournal({ healthState: 'unavailable' });
  const canonicalMetadata = tool('process.run_allowlisted', 'process_execution', async () => makeToolResult({ id: 'call_native_01', name: 'process.run_allowlisted', status: 'ok', text: '{}' }));
  const controller = new ConversationController({ engine: engineFor('process.run_allowlisted', {}, advertised), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': canonicalMetadata } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native05', requestId: 'req_native05', message: 'run it' }));
  assert.equal(result.error, 'action_journal_unavailable');
  assert.equal(advertised[0].some(item => item.function?.name === 'process.run_allowlisted'), false);
});

test('registry snapshots defeat post-admission mutation and public map replacement', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const mutable = tool('test.write', 'create', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ engine: engineFor('test.write', {}), actionJournal: journal, toolRegistry: { 'test.write': mutable } });
  mutable.name = 'app.open'; mutable.risk_tier = 'T0'; mutable.side_effect = 'none'; mutable.execute = () => { throw new Error('mutated descriptor executed'); };
  Object.defineProperty(controller, 'tools', { value: new Map([['test.write', { name: 'app.open', execute: () => { throw new Error('replaced map used'); } }]]) });
  const result = await runAsPlatform('linux', () => controller.runTurn({ sessionId: 'ses_snapshot', requestId: 'req_snapshot', message: 'write it' }));
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('accessor-backed canonical metadata is rejected before registry admission', async () => {
  const accessorTool = tool('app.open', 'launch', async () => makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }));
  Object.defineProperty(accessorTool, 'risk_tier', { configurable: true, get() { throw new Error('hostile getter'); } });
  assert.throws(() => new ConversationController({ engine: engineFor('app.open', {}), toolRegistry: { 'app.open': accessorTool } }), /data property/u);
});

test('POSIX durable tools retain controller-owned journal dispatch and completion', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const writeTool = tool('test.write', 'create', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ engine: engineFor('test.write', {}), actionJournal: journal, toolRegistry: { 'test.write': writeTool } });
  const result = await runAsPlatform('linux', () => controller.runTurn({ sessionId: 'ses_posix01', requestId: 'req_posix01', message: 'write it' }));
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('Windows non-native durable tools retain controller ownership', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const browserTool = tool('browser.open_url', 'external_navigation', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'browser.open_url', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ engine: engineFor('browser.open_url', {}), actionJournal: journal, toolRegistry: { 'browser.open_url': browserTool } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_other01', requestId: 'req_other01', message: 'browse it' }));
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('controller source defines exact non-overridable handoff and no native/process fallback', async () => {
  const source = await readFile(new URL('../../host/agent/controller.mjs', import.meta.url), 'utf8');
  for (const field of ['version:', 'journal_dispatch_owner:', 'operation_id:', 'request_ref:', 'call_ref:', 'tool:', 'risk:', 'side_effect:', 'args_digest:', 'preview_digest:', 'operation_digest:', 'authorization_kind:', 'authorized_sequence:', 'authorized_receipt_digest:', 'authorized_event_digest:', 'accepted: false']) assert.match(source, new RegExp(field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.match(source, /nativeSupervisorOwnerFor\(tool\.name, tool, process\.platform\)/u);
  assert.doesNotMatch(source, /platform\s*=\s*process\.platform/u);
  assert.match(source, /#tools/u);
  assert.doesNotMatch(source, /this\.tools/u);
  assert.match(source, /platform === 'win32'/u);
  assert.doesNotMatch(source, /nativeSupervisor\?\./u);
  assert.doesNotMatch(source, /node:child_process/u);
  assert.doesNotMatch(source, /spawn\(/u);
  assert.doesNotMatch(source, /authorized_event_digest\s*=\s*authorized\?\.receipt_hash/u);
  assert.doesNotMatch(source, /PowerShell|powershell|cmd(?:\.exe)?|shell/u);
  assert.doesNotMatch(source, /from ['"](?:\.\/.+)?native-supervisor/u);
});
