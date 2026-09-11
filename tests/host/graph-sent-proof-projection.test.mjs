import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ActionJournal } from '../../host/agent/action-journal.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { createMicrosoftGraphTools, MicrosoftGraphProvider } from '../../host/providers/microsoft-graph.mjs';

// The Sent Items proof for `mail.send_draft` is the only completion path that
// operation has: Graph answers `POST /me/messages/{id}/send` with a bodyless
// `202 Accepted`, so nothing else can attest the send. These tests model the
// provider honestly: `internetMessageHeaders` is a single-message projection
// and is therefore ABSENT from every `/messages` collection listing, whatever
// `$select` asks for. Against that model the pre-repair single-collection-query
// proof can never match its marker.

const DRAFT_ID = 'draft-sent-proof';
const SENT_ID = 'sent-sent-proof';
const SUBJECT = 'Quarterly update';
const BODY = 'Body text';
const RECIPIENT = 'alice@example.com';
const MARKER = `act_${'1'.repeat(32)}:${'2'.repeat(64)}`;
const FOREIGN_MARKER = `act_${'1'.repeat(32)}:${'2'.repeat(64)}:${'3'.repeat(64)}`;
const LIST_SELECT = 'id,sentDateTime';
// Every fixture drives the provider from an injected clock so the bounded
// deterministic send window and the tool budget are exact, never wall-clock
// dependent.
const FIXED_NOW_MS = Date.UTC(2026, 8, 11, 12, 0, 0);

const value = result => JSON.parse(result.content[0].text);
const nowUtc = () => new Date(FIXED_NOW_MS).toISOString();

async function directory(t) {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'lae-graph-sent-')));
  await chmod(path, 0o700); t.after(() => rm(path, { recursive: true, force: true })); return path;
}

const draftBody = ({ marker = MARKER } = {}) => ({
  id: DRAFT_ID, subject: SUBJECT, body: { contentType: 'Text', content: BODY },
  toRecipients: [{ emailAddress: { address: RECIPIENT } }], ccRecipients: [],
  internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }],
  '@odata.etag': 'etag-sent-proof', changeKey: 'change-sent-proof',
});

// A full single-message Sent Items projection: this is the ONLY shape that ever
// carries `internetMessageHeaders`.
const sentBody = ({ id = SENT_ID, marker = MARKER, sentDateTime = nowUtc(), subject = SUBJECT, body = BODY, headers } = {}) => ({
  id, subject, body: { contentType: 'Text', content: body },
  toRecipients: [{ emailAddress: { address: RECIPIENT } }], ccRecipients: [],
  ...(headers === null ? {} : { internetMessageHeaders: headers ?? [{ name: 'x-lae-operation', value: marker }] }),
  sentDateTime, changeKey: 'change-sent-item',
});

// One `mail.send_draft` transport. `sentPage` supplies the Sent Items proof ID
// page exactly as Graph would answer it (ids and `sentDateTime` only); `items`
// supplies the per-message projections returned by the explicit GETs.
function sendFixture({ sentPage = null, items = [], nextLink = false, listFault = null, draftMarker = MARKER, clockStepMs = 0 } = {}) {
  const requests = []; let dispatched = false; let sends = 0; let listPages = 0; let itemGets = 0; let clock = FIXED_NOW_MS;
  const byId = new Map(items.map(item => [item.id, item]));
  const page = sentPage ?? items.map(item => ({ id: item.id, sentDateTime: item.sentDateTime }));
  const transport = { request: async request => {
    clock += clockStepMs;
    requests.push({ method: request.method, path: request.path, query: request.query, headers: request.headers });
    if (request.method === 'POST' && request.path.endsWith('/send')) { request.onDispatch?.(); dispatched = true; sends += 1; return { status: 202, body: {} }; }
    if (request.path === `/v1.0/me/messages/${DRAFT_ID}`) return dispatched ? { status: 404, body: {} } : { status: 200, body: draftBody({ marker: draftMarker }) };
    if (request.path === '/v1.0/me/mailFolders/sentitems/messages') {
      // The pre-write baseline snapshot is a different query from the proof ID
      // page; only the proof page is under test here.
      if (request.query.$select === LIST_SELECT) return { status: 200, body: { value: [] } };
      listPages += 1;
      if (listFault) return listFault();
      return { status: 200, body: { value: page, ...(nextLink ? { '@odata.nextLink': 'opaque-next-page' } : {}) } };
    }
    if (request.path.startsWith('/v1.0/me/messages/')) {
      itemGets += 1; const id = decodeURIComponent(request.path.slice('/v1.0/me/messages/'.length));
      const item = byId.get(id); return item ? { status: 200, body: item } : { status: 404, body: {} };
    }
    throw new Error(`unexpected injected request: ${request.method} ${request.path}`);
  } };
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport, now: () => clock });
  return { provider, requests, counts: () => ({ sends, listPages, itemGets }), clock: () => clock };
}

// Direct tool invocation: preview, authorize the private preview read, execute.
async function runSend(fixture, id = 'call_sent_proof') {
  const tool = createMicrosoftGraphTools(fixture.provider)['mail.send_draft'];
  const request = { id, name: 'mail.send_draft', arguments: { draft_id: DRAFT_ID } };
  await tool.preview(request);
  await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } });
  return tool.execute({ ...request, authorization: { kind: 'user_confirmation' } });
}

// The same send driven through the real controller and a real durable journal,
// so every case can assert the persisted record state, not just the payload.
async function runSendThroughController(t, fixture) {
  const journal = await ActionJournal.open({ directory: await directory(t), testOnly: true });
  const events = [];
  const engine = { async *generate({ messages }) {
    if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_sent_turn', name: 'mail.send_draft', arguments: { draft_id: DRAFT_ID } }) }; return; }
    yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' };
  } };
  const controller = new ConversationController({ engine, actionJournal: journal, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
  const pending = controller.runTurn({ sessionId: 'ses_sentproof1', requestId: 'req_sentproof1', message: 'send it', onEvent: event => events.push(event) });
  for (let phase = 1; phase <= 2; phase += 1) {
    while (events.filter(event => event.event === 'tool.confirmation_required').length < phase) await new Promise(resolve => setImmediate(resolve));
    const confirmation = events.filter(event => event.event === 'tool.confirmation_required')[phase - 1];
    assert.equal(controller.confirm(confirmation.data.confirmation_id, true, { requestId: 'req_sentproof1', callId: 'call_sent_turn' }), true);
  }
  const turn = await pending;
  const records = (await journal.summary()).records;
  return { turn, events, record: records[0] ?? null };
}

test('Sent Items proof lists IDs then GETs each message with an explicit header projection', async () => {
  const sent = sentBody();
  const fixture = sendFixture({ items: [sent] });
  const output = await runSend(fixture);
  const payload = value(output);
  assert.equal(output.status, 'ok');
  assert.equal(payload.state, 'completed');
  assert.equal(payload.provider_completion, 'verified');
  assert.equal(payload.reconciliation, 'unique_sent_item');
  assert.equal(payload.resource_id, SENT_ID);

  // This is the repair, asserted as a request shape. Pre-repair the proof was a
  // single `$top=50` collection query whose `$select` asked for
  // `internetMessageHeaders`, and there was no per-message GET at all.
  const sentReads = fixture.requests.filter(request => request.path === '/v1.0/me/mailFolders/sentitems/messages');
  assert.equal(sentReads.length, 2);
  const [baseline, proofPage] = sentReads;
  assert.deepEqual(baseline.query, { '$top': 50, '$orderby': 'sentDateTime desc', '$select': LIST_SELECT });
  assert.deepEqual(proofPage.query, { '$top': 20, '$orderby': 'sentDateTime desc', '$select': 'id' });
  assert.equal(proofPage.query.$select.includes('internetMessageHeaders'), false, 'a collection query must never be asked for a single-message projection');
  const proofGet = fixture.requests.find(request => request.path === `/v1.0/me/messages/${SENT_ID}`);
  assert.equal(proofGet.method, 'GET');
  assert.equal(proofGet.query.$select, 'id,subject,body,toRecipients,ccRecipients,internetMessageHeaders,changeKey,sentDateTime');
  assert.equal(proofGet.headers.Prefer, 'outlook.body-content-type="text"');
  assert.deepEqual(fixture.counts(), { sends: 1, listPages: 1, itemGets: 1 });
});

test('the Sent Items proof request count is one list page plus at most the shared candidate cap', async () => {
  // 20 candidate IDs, exactly one of which is the real send. The bound is
  // 1 + 20; a 21st candidate would mean the cap is not being applied.
  const ids = Array.from({ length: 20 }, (_, index) => `sent-bound-${index}`);
  const items = ids.map(id => sentBody({ id, marker: id === ids[7] ? MARKER : `act_${'9'.repeat(32)}:${'8'.repeat(64)}` }));
  const fixture = sendFixture({ items });
  const payload = value(await runSend(fixture, 'call_sent_bound'));
  assert.equal(payload.reconciliation, 'unique_sent_item');
  assert.equal(payload.resource_id, ids[7]);
  const counts = fixture.counts();
  assert.equal(counts.listPages, 1);
  assert.equal(counts.itemGets, 20);
  assert.ok(counts.listPages + counts.itemGets <= 21, `expected at most 1 list page + 20 GETs, saw ${counts.listPages + counts.itemGets}`);
});

test('every hostile Sent Items proof response fails closed and leaves the record reconciling', async t => {
  const cases = [
    ['transport throw on the proof page', { listFault: () => { throw Object.assign(new Error('socket reset'), { code: 'ECONNRESET' }); }, items: [sentBody()] }, 'sent_collection_unavailable'],
    ['http 500 on the proof page', { listFault: () => ({ status: 500, body: {} }), items: [sentBody()] }, 'sent_collection_unavailable'],
    ['malformed collection value', { sentPage: undefined, listFault: () => ({ status: 200, body: { value: 'not-an-array' } }), items: [sentBody()] }, 'sent_collection_unavailable'],
    ['paginated proof page', { items: [sentBody()], nextLink: true }, 'sent_collection_truncated'],
    ['over-cap proof page', { sentPage: Array.from({ length: 21 }, (_, index) => ({ id: `sent-over-${index}`, sentDateTime: nowUtc() })), items: [sentBody()] }, 'sent_collection_truncated'],
    ['duplicate IDs in the proof page', { sentPage: [{ id: SENT_ID, sentDateTime: nowUtc() }, { id: SENT_ID, sentDateTime: nowUtc() }], items: [sentBody()] }, 'sent_collection_unavailable'],
    ['unusable ID in the proof page', { sentPage: [{ id: '' }], items: [sentBody()] }, 'sent_collection_unavailable'],
    ['id mismatch on the exact GET', { sentPage: [{ id: SENT_ID, sentDateTime: nowUtc() }], items: [sentBody({ id: 'sent-other-id' })] }, 'sent_collection_unavailable'],
    ['two exact matches', { items: [sentBody(), sentBody({ id: 'sent-duplicate' })] }, 'multiple_matching_sent_items'],
    ['duplicated operation header', { items: [sentBody({ headers: [{ name: 'x-lae-operation', value: MARKER }, { name: 'X-LAE-OPERATION', value: MARKER }] })] }, 'sent_collection_unavailable'],
    ['missing marker on the sent item', { items: [sentBody({ headers: null })] }, 'sent_item_not_found'],
    ['empty header collection on the sent item', { items: [sentBody({ headers: [] })] }, 'sent_item_not_found'],
    ['marker bound to a foreign account', { items: [sentBody({ marker: FOREIGN_MARKER })] }, 'sent_item_not_found'],
    ['marker matches but content does not', { items: [sentBody({ body: 'tampered body' })] }, 'sent_item_not_found'],
    ['match predates the pre-send snapshot', { items: [sentBody({ sentDateTime: '2020-01-01T00:00:00Z' })] }, 'sent_item_not_found'],
    ['undated match', { sentPage: [{ id: SENT_ID, sentDateTime: nowUtc() }], items: [sentBody({ sentDateTime: '2026-02-30T00:00:00Z' })] }, 'sent_collection_unavailable'],
    ['no sent item at all', { items: [] }, 'sent_item_not_found'],
  ];
  for (const [label, options, expected] of cases) {
    const direct = sendFixture(options);
    const payload = value(await runSend(direct, `call_sent_${cases.findIndex(entry => entry[0] === label)}`));
    assert.equal(payload.reconciliation, expected, label);
    assert.equal(payload.state, 'reconciling', label);
    assert.equal(payload.completed, false, label);
    assert.equal(payload.provider_completion, 'unverified', label);
    assert.equal(payload.completion, 'manual_required', label);
    assert.equal(direct.counts().sends, 1, label);

    // The same case through the real controller and a real durable journal.
    // `reconciling` is recoverable; `unknown_manual` and `completed` are the
    // two states this seam must never reach on hostile evidence.
    const live = sendFixture(options);
    const { turn, record } = await runSendThroughController(t, live);
    assert.equal(turn.state, 'COMPLETED', label);
    assert.equal(record.state, 'reconciling', label);
    assert.equal(record.tool_name, 'mail.send_draft', label);
  }
});

test('a proof walk that runs out of tool budget reports a typed inconclusive result', async t => {
  // The injected clock advances with every provider request, so the 10 s
  // `mail.send_draft` budget is consumed deterministically without real time.
  const items = Array.from({ length: 20 }, (_, index) => sentBody({ id: `sent-budget-${index}`, marker: `act_${'9'.repeat(32)}:${'8'.repeat(64)}` }));
  const budgeted = () => sendFixture({ items, clockStepMs: 900 });
  const direct = budgeted();
  const output = await runSend(direct, 'call_sent_budget');
  const payload = value(output);
  assert.equal(output.status, 'ok', 'a budget overrun is data, not a thrown tool failure');
  assert.equal(payload.reconciliation, 'sent_proof_budget_exhausted');
  assert.equal(payload.state, 'reconciling');
  assert.equal(payload.completed, false);
  const counts = direct.counts();
  assert.equal(counts.listPages, 1);
  assert.ok(counts.itemGets > 0 && counts.itemGets < 20, `expected a partial bounded walk, saw ${counts.itemGets}`);
  assert.ok(direct.clock() - FIXED_NOW_MS < 10000, 'the proof walk must stop inside the 10 s mail.send_draft budget');

  const { turn, record } = await runSendThroughController(t, budgeted());
  assert.equal(turn.state, 'COMPLETED');
  assert.equal(record.state, 'reconciling');
});

test('operator cancellation during the proof walk propagates instead of being swallowed', async () => {
  // Ambiguity is reported as `reconciling` data, but an operator decision is
  // not ambiguity: it must surface as a cancellation, exactly as the draft
  // seam already does.
  const items = Array.from({ length: 4 }, (_, index) => sentBody({ id: `sent-cancel-${index}`, marker: `act_${'9'.repeat(32)}:${'8'.repeat(64)}` }));
  const fixture = sendFixture({ items });
  const operator = new AbortController();
  const originalRequest = fixture.provider.transport.request;
  fixture.provider.transport.request = async request => {
    if (request.path.startsWith('/v1.0/me/messages/sent-cancel-')) operator.abort();
    return originalRequest(request);
  };
  const tool = createMicrosoftGraphTools(fixture.provider)['mail.send_draft'];
  const request = { id: 'call_sent_cancel', name: 'mail.send_draft', arguments: { draft_id: DRAFT_ID } };
  await tool.preview(request);
  await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } });
  const payload = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, signal: operator.signal }));
  assert.equal(payload.code, 'provider_cancelled');
  assert.equal(payload.completed, undefined);
  assert.ok(fixture.counts().itemGets < 4, `cancellation must stop the walk, saw ${fixture.counts().itemGets} GETs`);
});

test('Sent Items proof refuses to spend a request without a usable marker or binding', async () => {
  const provider = sendFixture({ items: [sentBody()] }).provider;
  const baseline = { values: null, truncated: false, exhausted: false };
  const digest = 'a'.repeat(64);
  const ids = new Set();
  assert.deepEqual(await provider.listSentForDigest(digest, ids, 0, null), baseline);
  assert.deepEqual(await provider.listSentForDigest(digest, ids, 0, ''), baseline);
  assert.deepEqual(await provider.listSentForDigest(digest, ids, 0, 'not-a-marker'), baseline);
  assert.deepEqual(await provider.listSentForDigest(digest, ids, 0, `${MARKER}:trailing`), baseline);
  assert.deepEqual(await provider.listSentForDigest('short-digest', ids, 0, MARKER), baseline);
  assert.deepEqual(await provider.listSentForDigest(digest, undefined, 0, MARKER), baseline);
  assert.deepEqual(await provider.listSentForDigest(digest, ['array-not-a-set'], 0, MARKER), baseline);
  assert.deepEqual(await provider.listSentForDigest(digest, ids, Number.NaN, MARKER), baseline);
  // A refused precondition must cost zero provider requests.
  assert.deepEqual(await provider.collectMailProof({ folder: 'sentitems', listQuery: { '$select': 'id' }, select: 'id', marker: null, matches: () => true }), baseline);
});
