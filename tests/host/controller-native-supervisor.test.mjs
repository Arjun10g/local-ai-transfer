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

class MockJournal {
  constructor({ authorized = {}, nativeOnly = true } = {}) {
    this.calls = [];
    this.authorized = authorized;
    this.nativeOnly = nativeOnly;
  }

  health() { return { state: 'ready', error: null }; }
  async prepare() { this.calls.push('prepare'); return { operation_id: operationId, state: 'prepared', sequence: 0, receipt_hash: digest('0') }; }
  async authorize(id, kind) { this.calls.push(['authorize', id, kind]); return { operation_id: id, state: 'authorized', sequence: this.authorized.sequence ?? 1, receipt_hash: this.authorized.receipt_hash ?? digest('1') }; }
  async dispatch() { this.calls.push('dispatch'); if (this.nativeOnly) throw new Error('dispatch must be owner-native'); return { operation_id: operationId, state: 'dispatching', sequence: 2, receipt_hash: digest('2') }; }
  async acknowledge() { this.calls.push('acknowledge'); if (this.nativeOnly) throw new Error('acknowledge must be owner-native'); return { operation_id: operationId, state: 'acknowledged', sequence: 3, receipt_hash: digest('3') }; }
  async beginReconciliation() { this.calls.push('beginReconciliation'); if (this.nativeOnly) throw new Error('reconciliation must be owner-native'); return { operation_id: operationId, state: 'reconciling', sequence: 3, receipt_hash: digest('3') }; }
  async complete() { this.calls.push('complete'); if (this.nativeOnly) throw new Error('completion must be owner-native'); return { operation_id: operationId, state: 'completed', sequence: 4, receipt_hash: digest('4') }; }
  async cancel() { this.calls.push('cancel'); }
  async failDefinitive(id, resolution) { this.calls.push(['failDefinitive', id, resolution]); return { operation_id: id, state: 'failed_definitive', sequence: 2, receipt_hash: digest('2') }; }
  async markUnknown() { this.calls.push('markUnknown'); throw new Error('unknown must be owner-native after handoff'); }
}

function engineFor(name, arguments_) {
  let generated = false;
  return {
    async *generate() {
      if (!generated) {
        generated = true;
        yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_native_01', name, arguments: arguments_ }) };
      } else yield { kind: 'text_delta', text: 'done' };
      yield { kind: 'done', usage: {} };
    },
  };
}

function tool(name, sideEffect, execute) {
  return {
    name,
    version: '1.0.0',
    risk_tier: sideEffect === 'process_execution' ? 'T3' : sideEffect === 'create' ? 'T2' : 'T1',
    side_effect: sideEffect,
    requires_confirmation: false,
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    authorize: async () => ({ kind: 'user_confirmation', journal_dispatch_owner: 'caller_override' }),
    execute,
  };
}

test('Windows native app/process actions fail definitively before handoff, journal dispatch, or provider execution', async () => {
  let executions = 0;
  const journal = new MockJournal();
  const controller = new ConversationController({
    platform: 'win32',
    engine: engineFor('app.open', {}),
    actionJournal: journal,
    toolRegistry: { 'app.open': tool('app.open', 'launch', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }); }) },
  });

  const result = await controller.runTurn({ sessionId: 'ses_native01', requestId: 'req_native01', message: 'open it' });
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
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
  const controller = new ConversationController({ platform: 'win32', engine: engineFor('process.run_allowlisted', {}), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': maliciousTool } });
  const result = await controller.runTurn({ sessionId: 'ses_native02', requestId: 'req_native02', message: 'run it' });
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
  const controller = new ConversationController({ platform: 'win32', engine: engineFor('app.open', {}), actionJournal: journal, toolRegistry: { 'app.open': tool('app.open', 'launch', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }); }) } });
  const result = await controller.runTurn({ sessionId: 'ses_native03', requestId: 'req_native03', message: 'open it' });
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.deepEqual(journal.calls.at(-1), ['failDefinitive', operationId, 'pre_dispatch_failure']);
});

test('POSIX durable tools retain controller-owned journal dispatch and completion', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const writeTool = tool('test.write', 'create', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ platform: 'linux', engine: engineFor('test.write', {}), actionJournal: journal, toolRegistry: { 'test.write': writeTool } });
  const result = await controller.runTurn({ sessionId: 'ses_posix01', requestId: 'req_posix01', message: 'write it' });
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('Windows non-native durable tools retain controller ownership', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const browserTool = tool('browser.open_url', 'external_navigation', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'browser.open_url', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ platform: 'win32', engine: engineFor('browser.open_url', {}), actionJournal: journal, toolRegistry: { 'browser.open_url': browserTool } });
  const result = await controller.runTurn({ sessionId: 'ses_other01', requestId: 'req_other01', message: 'browse it' });
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('controller source defines exact non-overridable handoff and no native/process fallback', async () => {
  const source = await readFile(new URL('../../host/agent/controller.mjs', import.meta.url), 'utf8');
  for (const field of ['version:', 'journal_dispatch_owner:', 'operation_id:', 'request_ref:', 'call_ref:', 'tool:', 'risk:', 'side_effect:', 'args_digest:', 'preview_digest:', 'operation_digest:', 'authorization_kind:', 'authorized_sequence:', 'authorized_receipt_digest:', 'authorized_event_digest:', 'accepted: false']) assert.match(source, new RegExp(field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.match(source, /nativeSupervisorOwnerFor\(tool, this\.platform\)/u);
  assert.match(source, /platform === 'win32'/u);
  assert.doesNotMatch(source, /nativeSupervisor\?\./u);
  assert.doesNotMatch(source, /node:child_process/u);
  assert.doesNotMatch(source, /spawn\(/u);
  assert.doesNotMatch(source, /authorized_event_digest\s*=\s*authorized\?\.receipt_hash/u);
  assert.doesNotMatch(source, /PowerShell|powershell|cmd(?:\.exe)?|shell/u);
  assert.doesNotMatch(source, /from ['"](?:\.\/.+)?native-supervisor/u);
});
