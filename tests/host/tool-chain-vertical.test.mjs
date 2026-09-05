import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';
import { createExternalToolRegistry } from '../../host/providers/index.mjs';
import { ConversationController, modelToolDefinitions } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { ActionJournal } from '../../host/agent/action-journal.mjs';

const auth = token => ({ authorization: `Bearer ${token}` });
const configuredWorkspace = (write = true) => ({ id: 'project', path: '/approved/workspace', read: true, write });
const configuredAction = { executable: '/approved/probe', args: [], workspace_id: 'project', cwd: '', parameters: {} };

function eventFromBlock(block) {
  const data = block.split('\n').find(line => line.startsWith('data: '));
  return data ? JSON.parse(data.slice(6)) : null;
}

function eventsFromText(text) { return text.split('\n\n').map(eventFromBlock).filter(Boolean); }

class SseReader {
  constructor(body) { this.reader = body.getReader(); this.decoder = new TextDecoder(); this.buffer = ''; this.events = []; this.done = false; }
  parse() {
    let boundary;
    while ((boundary = this.buffer.indexOf('\n\n')) >= 0) {
      const event = eventFromBlock(this.buffer.slice(0, boundary)); this.buffer = this.buffer.slice(boundary + 2); if (event) this.events.push(event);
    }
  }
  async until(predicate) {
    while (!this.events.some(predicate) && !this.done) {
      const part = await this.reader.read(); this.done = part.done; this.buffer += this.decoder.decode(part.value ?? new Uint8Array(), { stream: !part.done }); this.parse();
    }
    return this.events.find(predicate);
  }
  async finish() { await this.until(() => false); return this.events; }
}

test('production registries advertise only immutable configured capabilities', () => {
  const localDefault = createLocalToolRegistry({ platform: 'linux' });
  assert.deepEqual(Object.keys(localDefault), ['time.now', 'system.get_info']);
  assert.equal(Object.isFrozen(localDefault.capabilitySnapshot), true);
  assert.equal(Object.isFrozen(localDefault.capabilitySnapshot.tools), true);
  assert.equal(localDefault.capabilitySnapshot.tools['clipboard.read'].reason, 'platform_unsupported');
  assert.equal(localDefault.capabilitySnapshot.tools['app.open'].reason, 'allowlist_absent');
  assert.equal(localDefault.capabilitySnapshot.tools['browser.open_url'].reason, 'provider_disabled');
  assert.equal(localDefault.capabilitySnapshot.tools['process.run_allowlisted'].reason, 'provider_disabled');

  const linux = createLocalToolRegistry({
    platform: 'linux', workspaces: [configuredWorkspace()], applications: { probe: { executable: '/approved/app', args: [] } },
    networkProvider: 'browser_open', process_actions: { enabled: true, actions: { probe: configuredAction } }
  });
  for (const name of ['fs.list', 'fs.read_text', 'fs.search_text', 'fs.write_new', 'fs.apply_patch', 'app.open', 'browser.open_url', 'process.run_allowlisted']) assert.ok(Object.hasOwn(linux, name), name);
  assert.equal(Object.hasOwn(linux, 'clipboard.read'), false);

  const readOnlyProcess = createLocalToolRegistry({ platform: 'linux', workspaces: [configuredWorkspace(false)], process_actions: { enabled: true, actions: { probe: configuredAction } } });
  assert.equal(Object.hasOwn(readOnlyProcess, 'process.run_allowlisted'), false);
  assert.equal(Object.hasOwn(readOnlyProcess, 'fs.read_text'), true);
  assert.equal(Object.hasOwn(readOnlyProcess, 'fs.write_new'), false);
  assert.equal(readOnlyProcess.capabilitySnapshot.tools['process.run_allowlisted'].reason, 'actions_or_writable_workspace_unconfigured');
  const writeOnly = createLocalToolRegistry({ platform: 'linux', workspaces: [{ ...configuredWorkspace(), read: false }] });
  assert.equal(Object.hasOwn(writeOnly, 'fs.write_new'), true);
  assert.equal(Object.hasOwn(writeOnly, 'fs.apply_patch'), false, 'patch preview requires both read and write authorization');

  const windows = createLocalToolRegistry({
    platform: 'win32', workspaces: [configuredWorkspace()], applications: { probe: { executable: 'C:\\approved\\app.exe', args: [] } },
    networkProvider: 'browser_open', process_actions: { enabled: true, actions: { probe: { ...configuredAction, executable: 'C:\\approved\\probe.exe' } } }
  });
  assert.deepEqual(Object.keys(windows), ['time.now', 'system.get_info']);
  for (const name of ['clipboard.read', 'clipboard.write', 'app.open', 'browser.open_url', 'process.run_allowlisted']) assert.equal(windows.capabilitySnapshot.tools[name].reason, 'unsafe_subprocess_boundary', name);
  assert.equal(windows.capabilitySnapshot.tools['fs.read_text'].reason, 'platform_unsupported');

  const externalDefault = createExternalToolRegistry();
  assert.deepEqual(Object.keys(externalDefault), []);
  assert.equal(Object.isFrozen(externalDefault.capabilitySnapshot), true);
  assert.deepEqual(externalDefault.providerStatus(), { microsoft_graph: 'disabled', copilot: 'disabled', browser_actions: 'disabled' });

  const externalConfigured = createExternalToolRegistry({
    workspaceRoots: [configuredWorkspace()],
    config: {
      microsoft_graph: { enabled: true, tenant: 'organizations', client_id: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read', 'Mail.Read'] },
      copilot: { enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3' },
      browser_actions: { enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'] }
    }
  });
  assert.deepEqual(Object.keys(externalConfigured).sort(), ['browser.follow_link', 'browser.inspect_links', 'browser.inspect_page', 'browser.session_close', 'browser.session_start', 'coding.copilot_ask', 'mail.list_messages', 'mail.read_message'].sort());
  assert.equal(externalConfigured.providerAuthControl().microsoft_graph.configured, true);
  assert.equal(externalConfigured.providerAuthControl().microsoft_graph.status().state, 'idle');
  assert.deepEqual(externalConfigured.providerStatus(), { microsoft_graph: 'ready', copilot: 'ready', browser_actions: 'unverified' });
  assert.equal(Object.isFrozen(externalConfigured.capabilitySnapshot.providers.microsoft_graph.advertised_tools), true);
  assert.equal(Object.hasOwn(externalConfigured, 'mail.create_draft'), false, 'scope-insufficient Graph writes stay hidden');
  assert.equal(Object.hasOwn(externalConfigured, 'browser.fill_field'), false, 'browser mutations stay hidden without the safe-actions gate');

  const windowsExternal = createExternalToolRegistry({
    workspaceRoots: [{ ...configuredWorkspace(), path: 'C:\\approved\\workspace' }],
    config: {
      copilot: { enabled: true, executable: 'C:\\approved\\copilot.exe', allowlist: ['C:\\approved\\copilot.exe'], version: '1.2.3' },
      browser_actions: { enabled: true, executable: 'C:\\approved\\chrome.exe', allowlist: ['C:\\approved\\chrome.exe'] }
    },
    copilot: { platform: 'win32' }, browser: { platform: 'win32' }
  });
  assert.deepEqual(Object.keys(windowsExternal), [], 'Windows subprocess-backed external providers stay hidden until the native broker exists');
  assert.deepEqual(windowsExternal.providerStatus(), { microsoft_graph: 'disabled', copilot: 'unconfigured', browser_actions: 'unconfigured' });

  const mockedWindowsExternal = createExternalToolRegistry({
    copilot: { enabled: true, executable: 'C:\\approved\\copilot.exe', allowlist: ['C:\\approved\\copilot.exe'], version: '1.2.3', versionCheck: async () => true, readContext: async () => ({ text: '', files: [] }), platform: 'win32', testOnly: true },
    browser: { enabled: true, executable: 'C:\\approved\\chrome.exe', allowlist: ['C:\\approved\\chrome.exe'], platform: 'win32', testOnly: true }
  });
  assert.equal(Object.hasOwn(mockedWindowsExternal, 'coding.copilot_ask'), true, 'explicit test-only mocks retain a Windows unit-test seam');
  assert.equal(Object.hasOwn(mockedWindowsExternal, 'browser.session_start'), true, 'explicit test-only mocks retain a Windows unit-test seam');

  const modelNames = modelToolDefinitions(new Map(Object.entries({ ...localDefault, ...externalDefault }))).map(tool => tool.function.name);
  assert.deepEqual(modelNames, ['time.now', 'system.get_info']);
});

test('mocked HostServer drives validated read and confirmed mutation through model continuation', async t => {
  const observations = { advertised: [], readPreviews: 0, readExecutions: 0, draftPreviews: 0, draftExecutions: 0, authorization: null, continuations: [] };
  const readTool = {
    name: 'mail.read_message', description: 'Read one synthetic message.', risk_tier: 'T1', side_effect: 'read_mail', requires_confirmation: false, timeout_ms: 1000,
    parameters: { type: 'object', additionalProperties: false, required: ['message_id'], properties: { message_id: { type: 'string', maxLength: 64 } } },
    preview: async call => { observations.readPreviews += 1; assert.equal(typeof call.arguments.message_id, 'string'); return { provider: 'mock_mail', message_id: call.arguments.message_id }; },
    execute: async call => { observations.readExecutions += 1; assert.equal(call.authorization, undefined); return makeToolResult({ id: call.id, name: call.name, text: JSON.stringify({ subject: 'Synthetic', body: 'Meeting Friday' }) }); }
  };
  const draftTool = {
    name: 'mail.create_draft', description: 'Create one synthetic draft.', risk_tier: 'T2', side_effect: 'create_draft', requires_confirmation: true, timeout_ms: 1000,
    parameters: { type: 'object', additionalProperties: false, required: ['to', 'subject', 'body'], properties: { to: { type: 'string', maxLength: 320 }, subject: { type: 'string', maxLength: 998 }, body: { type: 'string', maxLength: 4096 } } },
    preview: async call => { observations.draftPreviews += 1; return { provider: 'mock_mail', recipients: [call.arguments.to], subject: call.arguments.subject, body_preview: call.arguments.body }; },
    confirmationRequired: (_call, { preview }) => { assert.equal(preview.subject, 'Friday follow-up'); return true; },
    execute: async call => { observations.draftExecutions += 1; observations.authorization = call.authorization; assert.deepEqual(call.authorization, { kind: 'user_confirmation' }); return makeToolResult({ id: call.id, name: call.name, text: JSON.stringify({ draft_id: 'draft-synthetic', created: true }) }); }
  };
  const engine = {
    async *generate({ messages, tools }) {
      observations.advertised.push(tools.map(tool => tool.function.name));
      const user = messages.find(message => message.role === 'user')?.content;
      const toolMessages = messages.filter(message => message.role === 'tool');
      if (user === 'invalid tool request') { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_invalid01', name: 'mail.read_message', arguments: { message_id: 42 } }) }; return; }
      if (toolMessages.length === 0) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_read01', name: 'mail.read_message', arguments: { message_id: 'message-1' } }) }; return; }
      observations.continuations.push(toolMessages.map(message => ({ name: message.name, content: JSON.parse(message.content) })));
      if (toolMessages.length === 1) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_draft01', name: 'mail.create_draft', arguments: { to: 'alice@example.com', subject: 'Friday follow-up', body: 'Meeting is Friday.' } }) }; return; }
      yield { kind: 'text_delta', text: 'Read the message; draft completion is awaiting reconciliation.' }; yield { kind: 'done', finish_reason: 'stop' };
    },
    async shutdown() {}
  };
  const rawJournalPath = await mkdtemp(join(tmpdir(), 'lae-vertical-journal-')); const journalPath = await realpath(rawJournalPath); await chmod(journalPath, 0o700); t.after(() => rm(journalPath, { recursive: true, force: true }));
  const actionJournal = await ActionJournal.open({ directory: journalPath, testOnly: true });
  const controller = new ConversationController({ engine, actionJournal, confirmationTimeoutMs: 1000, toolRegistry: { [readTool.name]: readTool, [draftTool.name]: draftTool } });
  const host = new HostServer({ controller, engine, actionJournal }); const address = await host.listen(0); t.after(() => host.close());
  const sessionResponse = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: '{}' });
  const session = await sessionResponse.json();
  const response = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ session_id: session.session_id, request_id: 'request_vertical01', message: 'read then draft' }) });
  assert.equal(response.status, 200); const stream = new SseReader(response.body);
  const confirmation = await stream.until(event => event.event === 'tool.confirmation_required');
  assert.ok(confirmation); assert.equal(confirmation.request_id, 'request_vertical01'); assert.equal(confirmation.session_id, session.session_id); assert.deepEqual(confirmation.data.call, { id: 'call_draft01', name: 'mail.create_draft' }); assert.deepEqual(confirmation.data.preview.recipients, ['alice@example.com']);
  const wrongRequest = await fetch(`${address.url}/api/tool-confirmations/${confirmation.data.confirmation_id}`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ approved: true, request_id: 'request_wrong01', call_id: confirmation.data.call.id }) });
  assert.equal(wrongRequest.status, 404); assert.deepEqual(await wrongRequest.json(), { accepted: false }); assert.equal(observations.draftExecutions, 0);
  const wrongCall = await fetch(`${address.url}/api/tool-confirmations/${confirmation.data.confirmation_id}`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ approved: true, request_id: confirmation.request_id, call_id: 'call_wrong01' }) });
  assert.equal(wrongCall.status, 404); assert.deepEqual(await wrongCall.json(), { accepted: false }); assert.equal(observations.draftExecutions, 0);
  const approved = await fetch(`${address.url}/api/tool-confirmations/${confirmation.data.confirmation_id}`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ approved: true, request_id: confirmation.request_id, call_id: confirmation.data.call.id }) });
  assert.equal(approved.status, 200); assert.deepEqual(await approved.json(), { accepted: true });
  const events = await stream.finish();

  assert.deepEqual(events.map(event => event.event), ['message.started', 'message.started', 'tool.proposed', 'tool.started', 'tool.completed', 'message.started', 'tool.proposed', 'tool.confirmation_required', 'tool.started', 'tool.completed', 'message.started', 'message.delta', 'message.completed', 'metrics.snapshot']);
  for (let index = 0; index < events.length; index++) { assert.equal(events[index].sequence, index); assert.equal(events[index].request_id, 'request_vertical01'); assert.equal(events[index].session_id, session.session_id); }
  const proposed = events.filter(event => event.event === 'tool.proposed'); const started = events.filter(event => event.event === 'tool.started'); const completed = events.filter(event => event.event === 'tool.completed');
  assert.deepEqual(proposed.map(event => event.data.call), [{ id: 'call_read01', name: 'mail.read_message' }, { id: 'call_draft01', name: 'mail.create_draft' }]);
  assert.deepEqual(started.map(event => event.data.call), proposed.map(event => event.data.call));
  assert.deepEqual(started.map(event => event.data.authorization), ['policy', 'user_confirmation']);
  assert.deepEqual(completed.map(event => ({ id: event.data.result.id, name: event.data.result.name })), proposed.map(event => event.data.call));
  assert.deepEqual(completed.map(event => event.data.result.status), ['ok', 'failed']);
  assert.equal(JSON.parse(completed[1].data.result.content[0].text).code, 'action_completion_unverified');
  assert.deepEqual({ readPreviews: observations.readPreviews, readExecutions: observations.readExecutions, draftPreviews: observations.draftPreviews, draftExecutions: observations.draftExecutions }, { readPreviews: 1, readExecutions: 1, draftPreviews: 1, draftExecutions: 1 });
  assert.deepEqual(observations.authorization, { kind: 'user_confirmation' });
  assert.deepEqual(observations.continuations.at(-1).map(item => item.name), ['mail.read_message', 'mail.create_draft']);
  assert.ok(observations.advertised.every(names => names.includes('mail.read_message') && names.includes('mail.create_draft')));

  const before = { previews: observations.readPreviews, executions: observations.readExecutions };
  const invalidSession = await (await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: '{}' })).json();
  const invalidResponse = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ session_id: invalidSession.session_id, request_id: 'request_invalid01', message: 'invalid tool request' }) });
  const invalidEvents = eventsFromText(await invalidResponse.text());
  assert.equal(invalidEvents.at(-1).event, 'request.failed'); assert.equal(invalidEvents.at(-1).data.code, 'invalid_tool_arguments');
  assert.equal(invalidEvents.some(event => event.event === 'tool.proposed'), false);
  assert.deepEqual({ previews: observations.readPreviews, executions: observations.readExecutions }, before, 'schema rejection occurs before preview or execution');
});
