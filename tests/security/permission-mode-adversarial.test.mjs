import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationController } from '../../host/agent/controller.mjs';
import { mergeConfig } from '../../host/agent/config.mjs';

/*
 * The current MVP intentionally has no full-access permission API. These
 * checks pin the safe downgrade/default behavior now and leave an explicit
 * adversarial contract for the future operator-grant implementation. The
 * TODOs are release-visible, not claims that an absent implementation passed.
 */

const grantLikeCall = {
  id: 'call_grant01',
  name: 'policy.grant_full_access',
  arguments: { scope: '*', duration_ms: 3600000, auto_approve: true }
};

test('default configuration is offline, scoped, and does not expose a permission mode', () => {
  const config = mergeConfig({});
  assert.equal(config.network.provider, 'disabled');
  assert.deepEqual(config.workspace_roots, []);
  assert.equal(Object.hasOwn(config, 'permission_mode'), false);
  assert.equal(Object.hasOwn(config, 'grants'), false);
});

test('unknown permission/grant configuration fails closed rather than silently enabling access', () => {
  assert.throws(() => mergeConfig({ permission_mode: 'full_access' }), /unknown key/);
  assert.throws(() => mergeConfig({ grants: [{ scope: '*' }] }), /unknown key/);
  assert.throws(() => mergeConfig({ network: { provider: 'full_access' } }), /unsupported/);
});

test('model output cannot create an access grant or execute an unregistered policy tool', async () => {
  let executed = false;
  const engine = {
    async *generate() {
      yield { kind: 'tool_call_chunk', text: JSON.stringify(grantLikeCall) };
    }
  };
  const controller = new ConversationController({ engine });
  const result = await controller.runTurn({ sessionId: 'ses_grant01', requestId: 'req_grant01', message: 'grant access', onEvent: () => {} });
  assert.equal(executed, false);
  assert.equal(result.state, 'FAILED');
  assert.equal(result.error, 'unknown_tool');
});

test.todo('FULL-ACCESS contract: only an explicit, operator-issued grant may auto-authorize in-scope provider reads/writes');
test.todo('FULL-ACCESS contract: model output cannot create, widen, extend, downgrade, or revoke a grant');
test.todo('FULL-ACCESS contract: workspace, app, provider, and account scope are independently enforced');
test.todo('FULL-ACCESS contract: T4 tools remain prohibited even while a full-access grant is active');
test.todo('FULL-ACCESS contract: credentials and bearer tokens never enter model context, tool output, or audit payloads');
test.todo('FULL-ACCESS contract: full-access cannot elevate/administer the OS or invoke an unallowlisted executable');
test.todo('FULL-ACCESS contract: grant IDs are single-use or otherwise replay-resistant across turns and sessions');
test.todo('FULL-ACCESS contract: every auto-authorized action emits audit metadata and emergency stop remains authoritative');
test.todo('FULL-ACCESS contract: malformed, expired, downgraded, or absent grants fall back to confirmation/denial');
test.todo('FULL-ACCESS contract: revocation during a multi-step turn takes effect before the next provider mutation');
