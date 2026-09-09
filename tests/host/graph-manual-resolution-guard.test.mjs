import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ActionJournal, createActionBinding } from '../../host/agent/action-journal.mjs';
import { HostServer } from '../../host/server/host-server.mjs';

async function directory(t) {
  const raw = await mkdtemp(join(tmpdir(), 'lae-graph-resolution-'));
  const path = await realpath(raw);
  await chmod(path, 0o700);
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}

function options(path) {
  let id = 0;
  let tick = 0;
  return {
    directory: path,
    testOnly: true,
    idFactory: () => `act_${(++id).toString(16).padStart(32, '0')}`,
    now: () => new Date(Date.UTC(2026, 0, 1, 0, 0, tick++)).toISOString(),
  };
}

async function unknownOperation(journal, { toolName = 'mail.create_draft', sideEffect = 'create_draft' } = {}) {
  const requestId = `request_${toolName.replaceAll('.', '_')}`;
  const callId = `call_${toolName.replaceAll('.', '_')}`;
  const args = toolName === 'mail.create_draft'
    ? { to: ['alice@example.com'], subject: 'bounded', body: 'bounded' }
    : { path: 'bounded.txt', content: 'bounded' };
  const preview = { summary: 'bounded' };
  const binding = createActionBinding({ requestId, callId, toolName, arguments: args, preview });
  const input = { requestId, callId, toolName, riskTier: toolName === 'teams.send_message' ? 'T3' : 'T2', sideEffect, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest };
  const receipt = await journal.prepare(input);
  await journal.authorize(receipt.operation_id, 'user_confirmation');
  await journal.dispatch(receipt.operation_id);
  await journal.markUnknown(receipt.operation_id);
  return { receipt, input };
}

test('Graph mutation ambiguity rejects direct manual completion and definitive failure without appending', async t => {
  const journal = await ActionJournal.open(options(await directory(t)));
  const { receipt } = await unknownOperation(journal);
  const before = await journal.detail(receipt.operation_id);
  for (const resolution of ['completed', 'failed_definitive']) {
    await assert.rejects(journal.resolve(receipt.operation_id, resolution), error => error.code === 'action_journal_provider_proof_required');
    const after = await journal.detail(receipt.operation_id);
    assert.equal(after.state, 'unknown_manual');
    assert.equal(after.events.length, before.events.length);
  }
});

test('Graph manual-resolution API fails closed, leaves ambiguity active, and cannot replay', async t => {
  const journal = await ActionJournal.open(options(await directory(t)));
  const { receipt, input } = await unknownOperation(journal, { toolName: 'teams.send_message', sideEffect: 'send_teams' });
  let providerExecutions = 0;
  const host = new HostServer({
    controller: { actionJournal: journal, cancelActive() {} },
    actionJournal: journal,
    providerShutdown: async () => { providerExecutions += 1; },
  });
  const address = await host.listen(0);
  t.after(() => host.close());
  const headers = { authorization: `Bearer ${address.token}`, 'content-type': 'application/json' };
  for (const resolution of ['completed', 'failed_definitive']) {
    const response = await fetch(`${address.url}/api/action-journal/${receipt.operation_id}/resolve`, { method: 'POST', headers, body: JSON.stringify({ resolution }) });
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { error: 'action_journal_provider_proof_required' });
  }
  assert.equal(providerExecutions, 0);
  const detail = await journal.detail(receipt.operation_id);
  assert.equal(detail.state, 'unknown_manual');
  assert.equal(detail.events.length, 4);
  await assert.rejects(journal.prepare(input), error => error.code === 'action_journal_duplicate_active');
});

test('non-Graph manual operator resolution remains available and does not execute a provider', async t => {
  const journal = await ActionJournal.open(options(await directory(t)));
  const { receipt } = await unknownOperation(journal, { toolName: 'fs.write_new', sideEffect: 'create' });
  let executions = 0;
  const resolved = await journal.resolve(receipt.operation_id, 'failed_definitive');
  assert.equal(resolved.state, 'failed_definitive');
  assert.equal(executions, 0);
});
