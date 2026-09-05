import { createHash, randomUUID } from 'node:crypto';
import { makeEvent } from './assistant-events.mjs';
import { makeToolResult, parseToolCall, validateToolResult, EnvelopeError } from './tool-envelope.mjs';
import { createActionBinding } from './action-journal.mjs';
import { readGraphAttestation, transferGraphAttestation } from '../providers/microsoft-graph.mjs';
import { browserSafeCompletionDigest, projectBrowserResult, readBrowserAttestation, transferBrowserAttestation } from '../providers/browser-actions.mjs';
import { isGraphReadTool, readGraphReadAttestation, transferGraphReadAttestation } from '../providers/microsoft-graph-reads.mjs';
import { copilotSafeCompletionDigest, readCopilotAttestation, transferCopilotAttestation } from '../providers/copilot-cli.mjs';
import { timeNowDefinition, timeNowTool } from '../tools/time-now.mjs';

export const STATES = Object.freeze(['IDLE', 'BUILDING_PROMPT', 'INFERENCING', 'TOOL_PROPOSED', 'WAITING_CONFIRMATION', 'TOOL_RUNNING', 'CONTINUING_MODEL', 'COMPLETED', 'CANCELLED', 'FAILED']);
const opaque = prefix => `${prefix}_${randomUUID().replaceAll('-', '')}`;
const CANCELLED_CONFIRMATION = Symbol('cancelled-confirmation');
const sessionIdPattern = /^[A-Za-z0-9_-]{8,96}$/;
const DURABLE_ACTION_EFFECTS = new Set(['create', 'replace', 'write_sensitive', 'launch', 'external_navigation', 'process_execution', 'cloud_inference', 'create_draft', 'send_mail', 'modify_mail', 'send_teams', 'browser_navigation', 'browser_input', 'browser_activation']);
const NON_ACTION_EFFECTS = new Set(['none', 'read_sensitive', 'read_mail', 'read_teams', 'external_query', 'browser_read', 'browser_close']);
const EFFECT_TIERS = Object.freeze({
  none: ['T0'], browser_close: ['T0'],
  read_sensitive: ['T1'], read_mail: ['T1'], read_teams: ['T1'], browser_read: ['T1'],
  external_query: ['T2'],
  launch: ['T1'], external_navigation: ['T1'], browser_navigation: ['T1', 'T2'],
  create: ['T2'], replace: ['T2'], write_sensitive: ['T2'], create_draft: ['T2'], modify_mail: ['T2'],
  process_execution: ['T3'], cloud_inference: ['T3'], send_mail: ['T3'], send_teams: ['T3'], browser_input: ['T3'], browser_activation: ['T3']
});
const RECONCILIATION_REQUIRED_EFFECTS = new Set(['create_draft', 'send_mail', 'modify_mail', 'send_teams', 'browser_navigation', 'browser_input', 'browser_activation', 'cloud_inference']);
const BROWSER_TOOL_NAMES = new Set(['browser.session_start', 'browser.inspect_links', 'browser.inspect_page', 'browser.follow_link', 'browser.fill_field', 'browser.activate_control', 'browser.session_close']);
const COPILOT_TOOL_NAMES = new Set(['coding.copilot_ask']);
const BROWSER_PROOFS = new Set(['session_started', 'navigation_verified', 'input_verified', 'activation_verified']);
const PRIVATE_JOURNAL_KEYS = new Set(['operation_id', 'operation_digest', 'arguments_digest', 'preview_digest', 'response_digest', 'resource_digest']);
const providerAttestationMatches = (result, expectedBinding, call) => {
  const browser = BROWSER_TOOL_NAMES.has(call?.name); const copilot = COPILOT_TOOL_NAMES.has(call?.name);
  const attestation = browser ? readBrowserAttestation(result) : copilot ? readCopilotAttestation(result) : readGraphAttestation(result);
  let payload = null; try { payload = JSON.parse(result?.content?.[0]?.text ?? ''); } catch {}
  const safeProofs = browser ? BROWSER_PROOFS : SAFE_RECONCILIATIONS;
  const providerPayloadValid = browser ? true : copilot ? payload?.provider === 'github_copilot' && payload?.state === 'ready' : payload?.provider_completion === 'verified' && payload?.state === 'completed' && payload?.completed === true;
  const safePayloadBound = browser ? attestation?.safe_payload_digest === browserSafeCompletionDigest(result) : copilot ? attestation?.safe_payload_digest === copilotSafeCompletionDigest(result) : true;
  return result?.status === 'ok' && payload && typeof payload === 'object' && providerPayloadValid && (copilot || payload.reconciliation === attestation?.proof) && attestation?.provider === (browser ? 'browser_actions' : copilot ? 'github_copilot' : 'microsoft_graph') && attestation.call_id === call.id && attestation.tool_name === call.name && attestation.operation_id === expectedBinding.id && attestation.operation_digest === expectedBinding.operationDigest && attestation.arguments_digest === expectedBinding.argumentsDigest && attestation.preview_digest === expectedBinding.previewDigest && (copilot || safeProofs.has(attestation.proof)) && safePayloadBound;
};
function stripPrivateJournalMetadata(value) {
  if (Array.isArray(value)) return value.map(stripPrivateJournalMetadata);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).filter(([key]) => !PRIVATE_JOURNAL_KEYS.has(key)).map(([key, item]) => [key, stripPrivateJournalMetadata(item)]));
}
function modelVisibleToolResult(result) {
  if (!result?.content || !Array.isArray(result.content)) return result;
  return { ...result, content: result.content.map(item => {
    if (item?.type !== 'text' || typeof item.text !== 'string') return item;
    try { return { ...item, text: JSON.stringify(stripPrivateJournalMetadata(JSON.parse(item.text))) }; } catch { return item; }
  }) };
}
function modelVisibleGraphReadResult(result, call) {
  const attestation = readGraphReadAttestation(result);
  const text = result?.content?.length === 1 && result.content[0]?.type === 'text' && typeof result.content[0].text === 'string' ? result.content[0].text : null;
  if (!attestation || attestation.provider !== 'microsoft_graph' || attestation.kind !== 'read' || attestation.call_id !== call.id || attestation.tool_name !== call.name || text === null || attestation.payload_digest !== digestEvidence(text)) {
    return makeToolResult({ id: result.id, name: result.name, status: 'failed', text: JSON.stringify({ provider: 'microsoft_graph', state: 'unverified', code: 'provider_read_unverified' }), durationMs: result.metadata?.duration_ms ?? 0 });
  }
  return result;
}
const SAFE_RECONCILIATIONS = new Set(['created_resource', 'unique_exact_draft', 'unique_sent_item', 'post_write_get_verified', 'timeout_get_verified', 'pre_read_already_desired']);
function modelVisibleReconciliationResult(result, controllerVerified, binding) {
  let payload;
  try { payload = JSON.parse(result?.content?.[0]?.text ?? ''); } catch { payload = null; }
  if (controllerVerified !== true || !payload || typeof payload !== 'object' || Array.isArray(payload)) return makeToolResult({ id: result.id, name: result.name, status: 'failed', text: JSON.stringify({ code: 'action_completion_unverified', state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified' }), durationMs: result.metadata?.duration_ms ?? 0 });
  const httpStatus = payload.http_status === null || (Number.isInteger(payload.http_status) && payload.http_status >= 200 && payload.http_status <= 599) ? payload.http_status : null;
  const privateValues = Object.values(binding ?? {}).filter(value => typeof value === 'string' && value.length > 0);
  const resourceId = typeof payload.resource_id === 'string' && payload.resource_id.length <= 512 && !/[\u0000-\u001f\u007f]/u.test(payload.resource_id) && !privateValues.some(value => payload.resource_id.toLocaleLowerCase('en-US').includes(value.toLocaleLowerCase('en-US'))) ? payload.resource_id : null;
  const reconciliation = SAFE_RECONCILIATIONS.has(payload.reconciliation) ? payload.reconciliation : null;
  return makeToolResult({ id: result.id, name: result.name, status: 'ok', text: JSON.stringify({ state: 'completed', provider_completion: 'verified', completion: 'provider_verified', accepted: true, completed: true, http_status: httpStatus, resource_id: resourceId, reconciliation }), durationMs: result.metadata?.duration_ms ?? 0 });
}
function modelVisibleCopilotResult(result, controllerVerified) {
  let payload = null;
  try { payload = JSON.parse(result?.content?.[0]?.text ?? ''); } catch {}
  if (controllerVerified !== true || !payload || typeof payload !== 'object' || Array.isArray(payload) || payload.provider !== 'github_copilot' || payload.state !== 'ready' || typeof payload.stdout !== 'string' || Buffer.byteLength(payload.stdout, 'utf8') > 65536 || !['ok', 'failed', 'cancelled'].includes(payload.exit_class) || typeof payload.truncated !== 'boolean' || !Number.isSafeInteger(payload.duration_ms) || payload.duration_ms < 0 || payload.duration_ms > 120000 || !Number.isSafeInteger(payload.egress_bytes) || payload.egress_bytes < 1 || payload.egress_bytes > 65536) return makeToolResult({ id: result?.id, name: result?.name, status: 'failed', text: JSON.stringify({ code: 'action_completion_unverified', state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified' }), durationMs: result?.metadata?.duration_ms ?? 0 });
  return makeToolResult({ id: result.id, name: result.name, status: 'ok', text: JSON.stringify({ provider: 'github_copilot', state: 'ready', completion: 'provider_verified', stdout: payload.stdout, exit_class: payload.exit_class, truncated: payload.truncated, duration_ms: payload.duration_ms, cli_version: typeof payload.cli_version === 'string' && payload.cli_version.length <= 128 ? payload.cli_version : 'unverified', egress_bytes: payload.egress_bytes }), durationMs: result.metadata?.duration_ms ?? 0 });
}
function digestEvidence(value) { return createHash('sha256').update(JSON.stringify(value)).digest('hex'); }

// The model sees only the OpenAI-compatible function schema. Execution and
// confirmation policy remain host-owned and never cross the native boundary.
const string = (max = 4096) => ({ type: 'string', maxLength: max });
const integer = (minimum, maximum) => ({ type: 'integer', minimum, maximum });
const descriptions = {
  'time.now': 'Return local wall-clock and UTC time.', 'system.get_info': 'Return bounded local runtime information.', 'clipboard.read': 'Read the local clipboard when supported.',
  'fs.list': 'List entries in an approved workspace directory.', 'fs.read_text': 'Read bounded UTF-8 text from an approved workspace file.', 'fs.search_text': 'Search literal text in approved workspace files.',
  'fs.write_new': 'Create a new file in an approved workspace.', 'fs.apply_patch': 'Apply a guarded replacement or patch to an approved workspace file.', 'clipboard.write': 'Write text to the local clipboard when supported.',
  'app.open': 'Open an allowlisted local application.', 'browser.open_url': 'Open an HTTPS URL after local policy checks.', 'process.run_allowlisted': 'Run one fixed operator-configured local process action.'
};
const parameterSchema = name => {
  const schemas = {
    'time.now': { properties: { format: { type: 'string', enum: ['local', 'utc', 'iso'] } } },
    'system.get_info': { properties: {} }, 'clipboard.read': { properties: {} },
    'fs.list': { properties: { workspace_id: string(64), path: string(), max_entries: integer(1, 500) }, required: ['workspace_id'] },
    'fs.read_text': { properties: { workspace_id: string(64), path: string(), offset_bytes: integer(0, 1048576), max_bytes: integer(1, 65536) }, required: ['workspace_id', 'path'] },
    'fs.search_text': { properties: { workspace_id: string(64), path: string(), query: string(4096), max_files: integer(1, 200), max_matches: integer(1, 500), max_depth: integer(0, 16) }, required: ['workspace_id', 'query'] },
    'fs.write_new': { properties: { workspace_id: string(64), path: string(), content: string(65536) }, required: ['workspace_id', 'path', 'content'] },
    'fs.apply_patch': { properties: { workspace_id: string(64), path: string(), base_sha256: { type: 'string', pattern: '^[a-fA-F0-9]{64}$' }, base_hash: { type: 'string', pattern: '^[a-fA-F0-9]{64}$' }, replacement: string(2097152), patch: string(2097152) }, required: ['workspace_id', 'path'], oneOf: [
      { required: ['base_sha256', 'replacement'], not: { anyOf: [{ required: ['base_hash'] }, { required: ['patch'] }] } },
      { required: ['base_sha256', 'patch'], not: { anyOf: [{ required: ['base_hash'] }, { required: ['replacement'] }] } },
      { required: ['base_hash', 'replacement'], not: { anyOf: [{ required: ['base_sha256'] }, { required: ['patch'] }] } },
      { required: ['base_hash', 'patch'], not: { anyOf: [{ required: ['base_sha256'] }, { required: ['replacement'] }] } }
    ] },
    'clipboard.write': { properties: { text: string(65536) }, required: ['text'] }, 'app.open': { properties: { app_id: string(64) }, required: ['app_id'] }, 'browser.open_url': { properties: { url: string(2048) }, required: ['url'] }, 'process.run_allowlisted': { properties: { action_id: string(64), parameters: { type: 'object', additionalProperties: true } }, required: ['action_id'] }
  };
  return { type: 'object', ...(schemas[name] ?? { properties: {} }), additionalProperties: false };
};
export const modelToolDefinitions = tools => [...tools.values()].map(tool => ({
  type: 'function', function: { name: tool.name, description: tool.description ?? descriptions[tool.name] ?? `Execute the local ${tool.name} operation.`, parameters: tool.parameters ?? parameterSchema(tool.name) }
}));

function schemaEqual(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function schemaMatches(value, schema) {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return false;
  if (schema.not && schemaMatches(value, schema.not)) return false;
  if (schema.oneOf && schema.oneOf.filter(item => schemaMatches(value, item)).length !== 1) return false;
  if (schema.anyOf && !schema.anyOf.some(item => schemaMatches(value, item))) return false;
  if (schema.enum && !schema.enum.some(item => schemaEqual(value, item))) return false;
  if (schema.type === 'object' || schema.required || schema.properties || schema.additionalProperties === false) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const properties = schema.properties ?? {};
    if (schema.additionalProperties === false && Object.keys(value).some(key => !Object.hasOwn(properties, key))) return false;
    if ((schema.required ?? []).some(key => !Object.hasOwn(value, key))) return false;
    return Object.entries(value).every(([key, item]) => !Object.hasOwn(properties, key) || schemaMatches(item, properties[key]));
  }
  if (schema.type === 'array') return Array.isArray(value) && (schema.maxItems === undefined || value.length <= schema.maxItems) && (!schema.items || value.every(item => schemaMatches(item, schema.items)));
  if (schema.type === 'string') return typeof value === 'string' && (schema.maxLength === undefined || value.length <= schema.maxLength) && (!schema.pattern || new RegExp(schema.pattern).test(value));
  if (schema.type === 'integer') return Number.isInteger(value) && (!('minimum' in schema) || value >= schema.minimum) && (!('maximum' in schema) || value <= schema.maximum);
  if (schema.type === 'number') return typeof value === 'number' && Number.isFinite(value) && (!('minimum' in schema) || value >= schema.minimum) && (!('maximum' in schema) || value <= schema.maximum);
  if (schema.type === 'boolean') return typeof value === 'boolean';
  return schema.type === undefined;
}
function validateToolArgumentShape(tool, call) {
  const schema = tool.parameters ?? parameterSchema(call.name);
  if (!schemaMatches(call.arguments, schema)) throw Object.assign(new Error('tool arguments do not match schema'), { code: 'invalid_tool_arguments' });
}
function publicToolCall(call) { return { id: call.id, name: call.name }; }

export function requiresDurableAction(tool) {
  const tier = tool?.risk_tier; const effect = tool?.side_effect;
  if (tier === 'T4') throw Object.assign(new Error('T4 tools are prohibited'), { code: 'action_journal_risk_prohibited' });
  if (!['T0', 'T1', 'T2', 'T3'].includes(tier)) throw Object.assign(new Error('tool risk tier is not classified'), { code: 'action_journal_classification_required' });
  if (typeof effect !== 'string' || !Object.hasOwn(EFFECT_TIERS, effect)) throw Object.assign(new Error('tool side effect is not classified'), { code: 'action_journal_classification_required' });
  if (!EFFECT_TIERS[effect].includes(tier)) throw Object.assign(new Error('tool risk and side effect do not match'), { code: 'action_journal_classification_required' });
  if (NON_ACTION_EFFECTS.has(effect)) return false;
  if (DURABLE_ACTION_EFFECTS.has(effect)) return true;
  if (tier === 'T2' || tier === 'T3') throw Object.assign(new Error('mutable tool must use a durable side effect'), { code: 'action_journal_classification_required' });
  return false;
}

function executionRegistryEntries(toolRegistry) {
  if (toolRegistry === undefined) return [];
  if (!toolRegistry || typeof toolRegistry !== 'object' || Array.isArray(toolRegistry)) throw new TypeError('toolRegistry must be an object');
  return Object.entries(toolRegistry).map(([name, tool]) => {
    if (!tool || typeof tool !== 'object' || Array.isArray(tool) || tool.name !== name || typeof tool.execute !== 'function') throw new TypeError(`toolRegistry entry ${name} must provide its matching name and execute function`);
    return [name, tool];
  });
}

async function invokeWithTimeout(tool, operation, call, signal) {
  // Tools must honor the supplied AbortSignal; the race bounds the controller
  // even when a misbehaving implementation cannot be interrupted immediately.
  const timeoutMs = tool.timeout_ms ?? 30000;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 600000) throw Object.assign(new Error('invalid_tool_timeout'), { code: 'invalid_tool_timeout' });
  const child = new AbortController(); const relay = () => child.abort(); signal?.addEventListener('abort', relay, { once: true });
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => { child.abort(); reject(Object.assign(new Error('tool execution timed out'), { code: 'tool_timeout' })); }, timeoutMs); });
  let cancelReject; const cancelled = signal ? new Promise((_, reject) => { cancelReject = () => reject(Object.assign(new Error('cancelled'), { code: 'cancelled' })); signal.addEventListener('abort', cancelReject, { once: true }); }) : null;
  try { return await Promise.race([operation({ ...call, signal: child.signal }), timeout, ...(signal ? [cancelled] : [])]); }
  finally { clearTimeout(timer); signal?.removeEventListener('abort', relay); if (signal && cancelReject) signal.removeEventListener('abort', cancelReject); }
}

export class ConversationController {
  constructor({ engine, maxToolCalls = 8, confirmationTimeoutMs = 30000, maxSessions = 4, maxHistoryMessages = 64, maxHistoryBytes = 262144, toolRegistry, actionJournal } = {}) {
    if (!engine?.generate) throw new TypeError('engine.generate is required');
    if (!Number.isInteger(maxSessions) || maxSessions < 1) throw new TypeError('maxSessions must be positive');
    if (!Number.isInteger(maxHistoryMessages) || maxHistoryMessages < 1 || !Number.isInteger(maxHistoryBytes) || maxHistoryBytes < 1024) throw new TypeError('history limits are invalid');
    if (actionJournal !== undefined && (!actionJournal || typeof actionJournal.health !== 'function' || typeof actionJournal.prepare !== 'function' || typeof actionJournal.authorize !== 'function' || typeof actionJournal.dispatch !== 'function' || typeof actionJournal.acknowledge !== 'function' || typeof actionJournal.beginReconciliation !== 'function' || typeof actionJournal.complete !== 'function' || typeof actionJournal.cancel !== 'function' || typeof actionJournal.failDefinitive !== 'function' || typeof actionJournal.markUnknown !== 'function')) throw new TypeError('actionJournal does not implement the durable transition contract');
    this.engine = engine; this.maxToolCalls = maxToolCalls; this.confirmationTimeoutMs = confirmationTimeoutMs; this.maxSessions = maxSessions; this.maxHistoryMessages = maxHistoryMessages; this.maxHistoryBytes = maxHistoryBytes; this.actionJournal = actionJournal; this.clock = 0;
    this.sessions = new Map(); this.active = null; this.pending = new Map();
    this.tools = new Map([[timeNowDefinition.name, { ...timeNowDefinition, execute: ({ id, arguments: args }) => timeNowTool({ id, arguments: args }) }], ...executionRegistryEntries(toolRegistry)]);
  }
  createSession(sessionId = opaque('ses')) {
    if (!sessionIdPattern.test(sessionId)) throw new Error('invalid session id');
    if (this.sessions.has(sessionId)) return this._touch(this.sessions.get(sessionId));
    if (this.sessions.size >= this.maxSessions) {
      const candidates = [...this.sessions.values()].filter(s => s.id !== this.active?.sessionId);
      if (!candidates.length) throw Object.assign(new Error('session_limit'), { code: 'session_limit' });
      candidates.sort((a, b) => a.last_used - b.last_used || a.id.localeCompare(b.id));
      this.sessions.delete(candidates[0].id);
    }
    const session = { id: sessionId, state: 'IDLE', history: [], history_bytes: 0, created_at: new Date().toISOString(), last_request_id: null, last_used: ++this.clock };
    this.sessions.set(sessionId, session); return session;
  }
  _touch(session) { session.last_used = ++this.clock; return session; }
  getSession(sessionId) { return this._touch(this.sessions.get(sessionId) ?? this.createSession(sessionId)); }
  _appendHistory(session, message) {
    session.history.push(message); session.history_bytes += Buffer.byteLength(JSON.stringify(message), 'utf8');
    while (session.history.length > this.maxHistoryMessages || session.history_bytes > this.maxHistoryBytes) { const removed = session.history.shift(); session.history_bytes -= Buffer.byteLength(JSON.stringify(removed), 'utf8'); }
  }
  resetSession(sessionId) { const session = this.sessions.get(sessionId); if (!session) return false; if (session.state !== 'IDLE' && session.state !== 'COMPLETED' && session.state !== 'FAILED' && session.state !== 'CANCELLED') throw new Error('session_busy'); session.history = []; session.history_bytes = 0; session.state = 'IDLE'; return true; }
  state(sessionId) { return this.getSession(sessionId).state; }
  _cancelPending(requestId) { const active = this.active; if (!active || active.requestId !== requestId || !active.confirmationId) return false; const item = this.pending.get(active.confirmationId); if (!item) return false; this.pending.delete(active.confirmationId); active.confirmationId = null; item.resolve(CANCELLED_CONFIRMATION); return true; }
  cancel(requestId) { if (this.active?.requestId !== requestId) return false; this.active.controller.abort(); this._cancelPending(requestId); this.engine.cancel?.(requestId); return true; }
  cancelActive() { return this.active ? this.cancel(this.active.requestId) : false; }
  confirm(confirmationId, approved, { requestId, callId } = {}) { const item = this.pending.get(confirmationId); if (!item || typeof approved !== 'boolean') return false; if (typeof requestId !== 'string' || typeof callId !== 'string' || requestId !== item.requestId || callId !== item.callId) return false; this.pending.delete(confirmationId); item.resolve(approved); return true; }
  emitFactory(requestId, sessionId, onEvent) { let sequence = 0; return (event, data) => { const output = makeEvent({ event, requestId, sessionId, sequence: sequence++, data }); onEvent?.(output); return output; }; }
  async runTurn({ sessionId, message, mode = 'normal', requestId = opaque('req'), signal, onEvent } = {}) {
    if (typeof message !== 'string' || !message.trim() || message.length > 32768) throw new Error('invalid_message');
    if (!/^[A-Za-z0-9_-]{8,96}$/.test(requestId)) throw Object.assign(new Error('invalid_request_id'), { code: 'invalid_request_id' });
    const session = this.getSession(sessionId); if (this.active) throw Object.assign(new Error('another_generation_active'), { code: 'busy' });
    if (!['normal', 'deep'].includes(mode)) throw new Error('invalid_mode');
    const controller = new AbortController();
    const relayAbort = () => { controller.abort(); this._cancelPending(requestId); }; signal?.addEventListener('abort', relayAbort, { once: true });
    this.active = { requestId, sessionId: session.id, controller, confirmationId: null };
    const emit = this.emitFactory(requestId, session.id, onEvent);
    session.last_request_id = requestId; this._appendHistory(session, { role: 'user', content: message }); session.state = 'BUILDING_PROMPT';
    let text = ''; let calls = 0; let activeJournalOperation = null;
    try {
      emit('message.started', { mode, state: session.state });
      while (true) {
        if (controller.signal.aborted) throw Object.assign(new Error('cancelled'), { code: 'cancelled' });
        session.state = calls ? 'CONTINUING_MODEL' : 'INFERENCING'; emit('message.started', { mode, state: session.state, continuation: calls > 0 });
        const journalReady = this.actionJournal?.health().state === 'ready';
        const tools = modelToolDefinitions(new Map([...this.tools].filter(([, tool]) => !requiresDurableAction(tool) || journalReady)));
        let callText = ''; let gotCall = false; let usage;
        for await (const frame of this.engine.generate({ requestId, sessionId: session.id, messages: session.history, tools, mode, signal: controller.signal })) {
          if (frame.kind === 'text_delta') { text += frame.text; emit('message.delta', { text: frame.text }); }
          else if (frame.kind === 'tool_call_chunk') { gotCall = true; callText += frame.text; if (Buffer.byteLength(callText) > 32768) throw new EnvelopeError('tool_call_too_large', 'tool call exceeds limit'); }
          else if (frame.kind === 'done') usage = frame.usage;
        }
        if (!gotCall) { this._appendHistory(session, { role: 'assistant', content: text }); session.state = 'COMPLETED'; emit('message.completed', { text, finish_reason: 'stop', usage: usage ?? { prompt_tokens: 0, completion_tokens: text.length }, state: session.state }); emit('metrics.snapshot', { tool_calls: calls, history_messages: session.history.length, history_bytes: session.history_bytes }); return { requestId, sessionId: session.id, state: session.state, text }; }
        if (text.trim()) throw new EnvelopeError('mixed_tool_call_output', 'tool call output cannot contain assistant text');
        calls++; if (calls > this.maxToolCalls) throw Object.assign(new Error('tool_call_limit_exceeded'), { code: 'tool_call_limit_exceeded' });
        const call = parseToolCall(callText); session.state = 'TOOL_PROPOSED';
        const tool = this.tools.get(call.name); if (!tool) throw Object.assign(new Error('unknown_tool'), { code: 'unknown_tool' });
        validateToolArgumentShape(tool, call);
        let preview;
        if (tool.preview) preview = await invokeWithTimeout(tool, tool.preview, call, controller.signal);
        emit('tool.proposed', { call: publicToolCall(call), ...(preview === undefined ? {} : { preview }) });
        let approved = true; let previewAccessDenied = false; let authorization = { kind: 'policy' };
        if (preview?.preview_authorization_required === true) {
          session.state = 'WAITING_CONFIRMATION'; const confirmationId = opaque('cnf'); this.active.confirmationId = confirmationId;
          emit('tool.confirmation_required', { confirmation_id: confirmationId, call: publicToolCall(call), preview, phase: 'preview_access', risk_tier: 'T1', expires_in_ms: this.confirmationTimeoutMs });
          approved = await new Promise(resolve => { const timer = setTimeout(() => { this.pending.delete(confirmationId); resolve(false); }, this.confirmationTimeoutMs); this.pending.set(confirmationId, { resolve: answer => { clearTimeout(timer); resolve(answer); }, requestId, sessionId: session.id, callId: call.id }); });
          this.active.confirmationId = null;
          if (approved === CANCELLED_CONFIRMATION) { if (activeJournalOperation) { await this.actionJournal.cancel(activeJournalOperation.id, 'request_cancelled'); activeJournalOperation = null; } throw Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
          if (!approved) { authorization = { kind: 'policy' }; previewAccessDenied = true; }
          else { preview = await invokeWithTimeout(tool, tool.preview, { ...call, authorization: { kind: 'user_confirmation' }, preview_authorized: true }, controller.signal); emit('tool.proposed', { call: publicToolCall(call), preview }); }
        }
        if (requiresDurableAction(tool)) {
          if (!this.actionJournal) throw Object.assign(new Error('durable action journal is not configured'), { code: 'action_journal_unavailable' });
          const binding = createActionBinding({ requestId, callId: call.id, toolName: call.name, arguments: call.arguments, preview });
          const receipt = await this.actionJournal.prepare({ requestId, callId: call.id, toolName: call.name, riskTier: tool.risk_tier, sideEffect: tool.side_effect, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest });
          activeJournalOperation = { id: receipt.operation_id, dispatched: false, reconcile: RECONCILIATION_REQUIRED_EFFECTS.has(tool.side_effect), operationDigest: binding.operationDigest, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest };
        }
        const requiresConfirmation = !previewAccessDenied && (typeof tool.confirmationRequired === 'function' ? await tool.confirmationRequired(call, { preview }) : Boolean(tool.requires_confirmation));
        if (requiresConfirmation) {
          session.state = 'WAITING_CONFIRMATION'; const confirmationId = opaque('cnf');
          this.active.confirmationId = confirmationId; emit('tool.confirmation_required', { confirmation_id: confirmationId, call: publicToolCall(call), preview, risk_tier: tool.risk_tier, expires_in_ms: this.confirmationTimeoutMs });
          approved = await new Promise(resolve => { const timer = setTimeout(() => { this.pending.delete(confirmationId); resolve(false); }, this.confirmationTimeoutMs); this.pending.set(confirmationId, { resolve: answer => { clearTimeout(timer); resolve(answer); }, requestId, sessionId: session.id, callId: call.id }); });
          this.active.confirmationId = null;
          if (approved === CANCELLED_CONFIRMATION) { if (activeJournalOperation) { await this.actionJournal.cancel(activeJournalOperation.id, 'request_cancelled'); activeJournalOperation = null; } throw Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
          if (approved) authorization = { kind: 'user_confirmation' };
        } else { const autoAuthorization = tool.authorize ? await invokeWithTimeout(tool, tool.authorize, { ...call, preview }, controller.signal) : null; if (autoAuthorization && typeof autoAuthorization === 'object') authorization = autoAuthorization; }
        if (activeJournalOperation && !approved) { await this.actionJournal.cancel(activeJournalOperation.id); activeJournalOperation = null; }
        else if (activeJournalOperation) {
          if (controller.signal.aborted) { await this.actionJournal.cancel(activeJournalOperation.id, 'request_cancelled'); activeJournalOperation = null; throw Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
          await this.actionJournal.authorize(activeJournalOperation.id, authorization.kind); await this.actionJournal.dispatch(activeJournalOperation.id); activeJournalOperation.dispatched = true;
        }
        session.state = 'TOOL_RUNNING'; emit('tool.started', { call: publicToolCall(call), approved, authorization: authorization.kind });
        let result;
        if (!approved) result = makeToolResult({ id: call.id, name: call.name, status: 'denied', text: 'User denied this action.' });
        else {
          if (controller.signal.aborted) throw Object.assign(new Error('cancelled'), { code: 'cancelled' });
          // `policy` is host-internal bookkeeping, not a model/provider
          // authorization object. Only pass concrete user/grant proof across
          // the provider boundary.
          // Every durable action receives the host-only journal binding. The
          // Copilot ACP egress path is not reconcilable, but it still needs a
          // durable operation guard; providers must never infer authority from
          // model-visible arguments.
          const internal = activeJournalOperation ? { journal_binding: { operation_id: activeJournalOperation.id, operation_digest: activeJournalOperation.operationDigest, arguments_digest: activeJournalOperation.argumentsDigest, preview_digest: activeJournalOperation.previewDigest } } : undefined;
          result = await invokeWithTimeout(tool, tool.execute, { ...call, ...(authorization.kind === 'policy' ? {} : { authorization }), ...(internal ? { internal } : {}) }, controller.signal);
        }
        const hostResult = result;
        try { result = validateToolResult(result); } catch { throw Object.assign(new Error('invalid_tool_result'), { code: 'invalid_tool_result' }); }
        if (result.id !== call.id || result.name !== call.name) throw Object.assign(new Error('tool_result_mismatch'), { code: 'tool_result_mismatch' });
        transferGraphAttestation(hostResult, result); if (BROWSER_TOOL_NAMES.has(call.name)) transferBrowserAttestation(hostResult, result);
        transferGraphReadAttestation(hostResult, result);
        if (COPILOT_TOOL_NAMES.has(call.name)) transferCopilotAttestation(hostResult, result);
        let strictModelResult = false; let controllerVerified = false; let modelBinding = null;
        if (activeJournalOperation) {
          strictModelResult = activeJournalOperation.reconcile === true;
          modelBinding = activeJournalOperation;
          if (result.status === 'ok') {
            await this.actionJournal.acknowledge(activeJournalOperation.id);
            if (activeJournalOperation.reconcile && !providerAttestationMatches(result, activeJournalOperation, call)) {
              await this.actionJournal.beginReconciliation(activeJournalOperation.id);
              const responseDigest = digestEvidence({ status: result.status, content: result.content.map(item => ({ type: item.type, text_digest: digestEvidence(item.text) })) });
              result = makeToolResult({ id: call.id, name: call.name, status: 'failed', text: JSON.stringify({ code: 'action_completion_unverified', operation_id: activeJournalOperation.id, state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified', evidence: { operation_digest: activeJournalOperation.operationDigest, preview_digest: activeJournalOperation.previewDigest, resource_digest: null, response_digest: responseDigest, arguments_digest: activeJournalOperation.argumentsDigest } }) });
            } else {
              await this.actionJournal.complete(activeJournalOperation.id);
              controllerVerified = activeJournalOperation.reconcile === true;
            }
          }
          else await this.actionJournal.markUnknown(activeJournalOperation.id);
          activeJournalOperation = null;
        }
        const modelResult = BROWSER_TOOL_NAMES.has(call.name) ? projectBrowserResult(result, { controllerVerified, reconciliationRequired: strictModelResult }) : strictModelResult && COPILOT_TOOL_NAMES.has(call.name) ? modelVisibleCopilotResult(result, controllerVerified) : strictModelResult ? modelVisibleReconciliationResult(result, controllerVerified, modelBinding) : isGraphReadTool(call.name) ? modelVisibleGraphReadResult(result, call) : modelVisibleToolResult(result);
        emit('tool.completed', { result: modelResult });
        this._appendHistory(session, { role: 'assistant', content: callText });
        this._appendHistory(session, { role: 'tool', name: call.name, tool_call_id: call.id, content: modelResult.content[0]?.text ?? '' });
        session.state = 'CONTINUING_MODEL'; text = '';
      }
    } catch (caught) {
      let error = caught;
      if (activeJournalOperation) {
        try { if (activeJournalOperation.dispatched) await this.actionJournal.markUnknown(activeJournalOperation.id); else await this.actionJournal.failDefinitive(activeJournalOperation.id); }
        catch (journalError) { error = journalError; }
        activeJournalOperation = null;
      }
      const cancelled = error?.code === 'cancelled' || controller.signal.aborted;
      session.state = cancelled ? 'CANCELLED' : 'FAILED';
      emit(cancelled ? 'request.cancelled' : 'request.failed', { code: cancelled ? 'cancelled' : (error.code ?? 'request_failed'), message: cancelled ? 'Request cancelled.' : 'Request failed.' });
      return { requestId, sessionId: session.id, state: session.state, error: cancelled ? 'cancelled' : (error.code ?? 'request_failed') };
    } finally { signal?.removeEventListener('abort', relayAbort); if (this.active?.requestId === requestId) this.active = null; }
  }
}
