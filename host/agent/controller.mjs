import { createHash, randomUUID } from 'node:crypto';
import { types as utilTypes } from 'node:util';
import { makeEvent } from './assistant-events.mjs';
import { makeToolResult, parseToolCall, coerceBooleanArguments, validateToolResult, EnvelopeError } from './tool-envelope.mjs';
import { ACTION_JOURNAL_HEALTH_ERRORS, createActionBinding } from './action-journal.mjs';
import { readGraphAttestation, readGraphRestartAttestation, readGraphRestartControl, transferGraphAttestation } from '../providers/microsoft-graph.mjs';
import { browserSafeCompletionDigest, projectBrowserResult, readBrowserAttestation, transferBrowserAttestation } from '../providers/browser-actions.mjs';
import { isGraphReadTool, readGraphReadAttestation, transferGraphReadAttestation } from '../providers/microsoft-graph-reads.mjs';
import { copilotSafeCompletionDigest, readCopilotAttestation, transferCopilotAttestation } from '../providers/copilot-cli.mjs';
import { timeNowDefinition, timeNowTool } from '../tools/time-now.mjs';
import { CONTEXT_DEFAULTS, ContextBudgetError, droppableHeadLength, fitHistory, historyBudget, messageTokens, messagesTokens, isContextOverflowError, isElidedToolResult, learnBytesPerToken, observedBytesPerToken, penalizeBytesPerToken, utf8Bytes } from './context-budget.mjs';
import { RecallArchive, recallMessage, recallOptions, recallQueries } from './memory-recall.mjs';
import { buildExcerpt, isRecallMessage, memoryOptions, noteByteLimit, noteMessage, planCompaction, removedBy, sanitizeNote, summaryRequest } from './memory-note.mjs';
import { ENGINE_MAX_MESSAGE_BYTES, toolResultByteCap, truncateUtf8 } from './tool-result-cap.mjs';
import { displayDiff, maskCredentialText, summarizeToolArguments } from './argument-summary.mjs';

// The stored-history accounting the hard byte bound has always used.
const storedBytes = message => Buffer.byteLength(JSON.stringify(message), 'utf8');
// After cancelling an unfinished memory summary, how long a new turn waits for
// that engine call to wind down before sending its own (the engine runs one
// generation at a time and answers 409 busy to a second).
const MEMORY_CANCEL_GRACE_MS = 30000;
// Memory outcomes are reported in `metrics.snapshot` (memory_note.code),
// never as a request error: a failed summary does not fail the turn.  They
// ride on their own property so they never enter the request-error space.
const memoryFailure = outcome => Object.assign(new Error(outcome), { memoryOutcome: outcome });
const settleWithin = (promise, ms) => new Promise(resolve => { const timer = setTimeout(resolve, ms); promise.then(() => { clearTimeout(timer); resolve(); }, () => { clearTimeout(timer); resolve(); }); });
const newMemoryState = (epoch = 0) => ({ archive: null, last_user: null, note: null, backlog: [], backlog_bytes: 0, epoch, ready: null, outcome: null, summaries: 0, failures: 0, cancelled: 0, lost_messages: 0 });
// Long enough to read a confirmation card aloud during a narrated demo; the
// host config can shorten or lengthen it (host.confirmation_timeout_ms).
export const DEFAULT_CONFIRMATION_TIMEOUT_MS = 120000;
// `auto` offers every available tool; `off` sends an empty tool list, which
// drops the ~1,200+ token tool preamble from the prompt for plain chat.
// `delegate` is for jobs a coding assistant hands this laptop (host/delegate):
// read-only tools only, and the fs trio only over the folders the job's scope
// names.  It is never reachable from the chat route, which accepts auto/off.
export const TOOL_MODES = Object.freeze(['auto', 'off', 'delegate']);
// What a delegated job may ever be offered.  The answer leaves the laptop for
// a cloud caller, so nothing that writes, launches, runs, reads the clipboard,
// browses, or calls a provider is on this list, whatever the registry holds.
export const DELEGATE_TOOL_NAMES = Object.freeze(['time.now', 'system.get_info', 'fs.list', 'fs.read_text', 'fs.search_text']);
const DELEGATE_FILE_TOOLS = new Set(['fs.list', 'fs.read_text', 'fs.search_text']);
// Beyond its name, a delegated tool must be declared inert: T0, no side
// effect, no network, no confirmation, no preview/authorize hooks.  A tool
// that would need the operator's click cannot run in a job nobody watches.
function delegateEligible(name, tool, scope) {
  if (!DELEGATE_TOOL_NAMES.includes(name) || (DELEGATE_FILE_TOOLS.has(name) && !scope.workspaces.length)) return false;
  return tool?.risk_tier === 'T0' && tool.side_effect === 'none' && tool.network !== true && tool.requires_confirmation !== true && typeof tool.confirmationRequired !== 'function' && typeof tool.preview !== 'function' && typeof tool.authorize !== 'function';
}
function delegateScopeOf(scope) {
  if (!scope || typeof scope !== 'object' || Array.isArray(scope) || !Array.isArray(scope.workspaces) || scope.workspaces.length > 16 || scope.workspaces.some(id => typeof id !== 'string' || !/^[A-Za-z0-9_.-]{1,64}$/u.test(id))) throw Object.assign(new Error('invalid_delegate_scope'), { code: 'invalid_delegate_scope' });
  return Object.freeze({ workspaces: Object.freeze([...scope.workspaces]) });
}
const EXPIRED_CONFIRMATION = Symbol('expired-confirmation');
// `length` means the answer hit the max_tokens cap, which the UI turns into a
// "Continue" offer; anything unknown is reported as a normal stop.
const FINISH_REASONS = new Set(['stop', 'length']);
// Only counts the engine actually reported.  A missing field stays missing:
// an invented `prompt_tokens: 0` or a character count would read as a
// tokenizer measurement to anything downstream.
function reportedUsage(usage) {
  const out = {};
  for (const key of ['prompt_tokens', 'completion_tokens']) if (Number.isSafeInteger(usage?.[key]) && usage[key] >= 0) out[key] = usage[key];
  return out;
}
// Late clicks on an expired card are answered from this bounded memory, so
// the UI can say "expired" instead of "unknown confirmation".
const MAX_EXPIRED_CONFIRMATIONS = 16;
// Appended to a partial answer kept in history, so the model knows the text
// it sees was cut off rather than finished.
// Fraction of a hard history bound that a trim drops down to (see _enforceHistoryBounds).
const HISTORY_LOW_WATER = 0.75;
const INTERRUPTED_ANSWER_MARKER = '\n\n[This answer was interrupted before it finished.]';
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
// Windows process/application mutation belongs to the future native
// supervisor.  This is derived from the trusted tool definition and platform,
// never from model arguments, caller metadata, or a provider response.
export const NATIVE_SUPERVISOR_DISPATCH_OWNER = 'native_supervisor';
export const NATIVE_SUPERVISOR_HANDOFF_VERSION = 'native-supervisor-handoff.v1';
const NATIVE_SUPERVISOR_TOOL_NAMES = new Set(['app.open', 'process.run_allowlisted']);
const JOURNAL_RECEIPT_DIGEST = /^[a-f0-9]{64}$/u;
const JOURNAL_OPERATION_ID = /^act_[a-f0-9]{32}$/u;
const MAX_GRAPH_RESTART_CANDIDATES = 8;
const GRAPH_RESTART_TIMEOUT_MS = 30000;
// Bounded, lowercase, metadata-only failure identifier. Only a journal-owned
// code is surfaced as-is; every other error — including a typed code from some
// other subsystem — collapses to one explicit code. No message, path,
// argument, or content ever reaches this value.
const RESTART_FAILURE_CODE = /^action_journal_[a-z0-9_]{1,48}$/u;
const restartFailureCode = error => typeof error?.code === 'string' && RESTART_FAILURE_CODE.test(error.code) ? error.code : 'action_journal_complete_failed';
const restartUnavailable = code => Object.freeze({ state: 'unavailable', examined: 0, completed: 0, blocked: 0, code });
const exactAuthorizationReadback = (receipt, operationId) => receipt?.operation_id === operationId && receipt?.state === 'authorized' && receipt?.sequence === 1 && JOURNAL_RECEIPT_DIGEST.test(receipt?.receipt_hash ?? '');
const nativeSupervisorOwnerFor = (toolName, tool, platform) => platform === 'win32' && NATIVE_SUPERVISOR_TOOL_NAMES.has(toolName) && tool?.name === toolName
  ? NATIVE_SUPERVISOR_DISPATCH_OWNER : null;
const NATIVE_CANONICAL_METADATA = Object.freeze({
  'app.open': Object.freeze({ version: '1.0.0', risk_tier: 'T1', side_effect: 'launch', network: false, data_egress: null, requires_confirmation: true }),
  'process.run_allowlisted': Object.freeze({ version: '0.1.0', risk_tier: 'T3', side_effect: 'process_execution', network: true, data_egress: 'operator_configured', requires_confirmation: true }),
});
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
  'app.open': 'Open an allowlisted local application.', 'browser.open_url': 'Open an HTTPS URL in the user’s browser and stop there. Navigation only: it returns no session id, and the page cannot afterwards be read or interacted with.', 'process.run_allowlisted': 'Run one fixed operator-configured local process action.'
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
// The display copy rides beside `call`, not inside it: `call` stays the bare
// {id, name} the UI and tests key on, and nothing here can reach execution.
function argumentsSummaryField(call, preview) {
  let summary = null;
  try { summary = summarizeToolArguments(call.name, call.arguments, preview); } catch { summary = null; }
  return summary ? { arguments_summary: summary } : {};
}
// An oversized raw result would fail envelope validation (65,536 chars) and
// end the turn. Unattested results are cut here instead; private journal keys
// are stripped from the WHOLE value first, because a truncated JSON text can
// no longer be parsed to strip them later.
// Every text part of a result, in order, joined by one newline: what the
// model sees and what history stores.  Deterministic, so a stored result is
// byte-stable and later prompts stay strict extensions of earlier ones.
function toolResultText(result) {
  return (Array.isArray(result?.content) ? result.content : []).filter(item => item?.type === 'text' && typeof item.text === 'string').map(item => item.text).join('\n');
}
// The bound is on the joined text (what the model will see), so a result
// split into several parts is cut once, with a marker that counts every
// part, instead of each part separately.  A malformed item is left for
// envelope validation to reject.
function boundRawToolResult(result, maxBytes) {
  if (!result || typeof result !== 'object' || !Array.isArray(result.content) || result.content.length > 16) return result;
  if (!result.content.every(item => item?.type === 'text' && typeof item.text === 'string')) return result;
  if (utf8Bytes(toolResultText(result)) <= maxBytes) return result;
  const parts = result.content.map(item => { try { return JSON.stringify(stripPrivateJournalMetadata(JSON.parse(item.text))); } catch { return item.text; } });
  return { ...result, content: [{ type: 'text', text: truncateUtf8(parts.join('\n'), maxBytes).text }], metadata: result.metadata && typeof result.metadata === 'object' ? { ...result.metadata, truncated: true } : result.metadata };
}
// The model-visible result is what enters history, so it is the one held to
// the per-result cap; the flag the envelope already carries says it was cut.
// The cap applies to the joined text, not only the first part, so a result
// split into several parts can neither slip past the cap nor lose its later
// parts.  A single-part result is cut exactly as before.
function capModelResult(result, maxBytes) {
  const text = toolResultText(result);
  if (utf8Bytes(text) <= maxBytes) return result;
  return validateToolResult({ ...result, content: [{ type: 'text', text: truncateUtf8(text, maxBytes).text }], metadata: { ...result.metadata, truncated: true } });
}
// The preview as the UI receives it.  The fs.apply_patch diff carries up to
// 8 KB of the old and new file text, and a process preview carries the real
// argv; either may hold a credential the operator would not want on a shared
// screen.  Credential-shaped values are masked in this display copy only:
// the action binding, confirmationRequired, authorize and execute all keep
// the real preview and arguments.  The diff is also re-classified by
// structure (see displayDiff) and sanitized.
function displayPreview(preview, call) {
  if (!preview || typeof preview !== 'object' || Array.isArray(preview)) return preview;
  let view = preview;
  if (typeof preview.diff === 'string') {
    const args = call?.arguments ?? {};
    view = { ...view, ...displayDiff(preview.diff, { path: preview.path, oldBytes: preview.old_bytes, replacement: args.replacement ?? args.patch }) };
  }
  if (Array.isArray(preview.argv)) view = { ...view, argv: preview.argv.map(value => (typeof value === 'string' ? maskCredentialText(value).text : value)) };
  return view;
}

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

const JOURNAL_FAILURE_CODES = new Set(ACTION_JOURNAL_HEALTH_ERRORS);
const JOURNAL_METHODS = Object.freeze(['health', 'prepare', 'authorize', 'dispatch', 'acknowledge', 'beginReconciliation', 'complete', 'cancel', 'failDefinitive', 'markUnknown']);
const primordialBind = Function.prototype.bind;
const primordialReflectApply = Reflect.apply;
function snapshotJournalMethods(journal) {
  if (!journal || typeof journal !== 'object' || utilTypes.isProxy(journal)) throw new TypeError('actionJournal does not implement the durable transition contract');
  const snapshot = Object.create(null);
  try {
    for (const name of JOURNAL_METHODS) {
      let current = journal; let descriptor;
      while (current) {
        if (utilTypes.isProxy(current)) throw new TypeError('actionJournal does not implement the durable transition contract');
        descriptor = Object.getOwnPropertyDescriptor(current, name);
        if (descriptor) break;
        current = Object.getPrototypeOf(current);
      }
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || typeof descriptor.value !== 'function' || utilTypes.isProxy(descriptor.value) || descriptor.get !== undefined || descriptor.set !== undefined) throw new TypeError('actionJournal does not implement the durable transition contract');
      const bound = primordialReflectApply(primordialBind, descriptor.value, [journal]);
      if (typeof bound !== 'function' || utilTypes.isProxy(bound)) throw new TypeError('actionJournal does not implement the durable transition contract');
      snapshot[name] = bound;
    }
    let current = journal; let descriptor;
    while (current) {
      if (utilTypes.isProxy(current)) throw new TypeError('actionJournal does not implement the durable transition contract');
      descriptor = Object.getOwnPropertyDescriptor(current, 'summary');
      if (descriptor) break;
      current = Object.getPrototypeOf(current);
    }
    if (descriptor) {
      if (!Object.hasOwn(descriptor, 'value') || typeof descriptor.value !== 'function' || utilTypes.isProxy(descriptor.value) || descriptor.get !== undefined || descriptor.set !== undefined) throw new TypeError('actionJournal does not implement the durable transition contract');
      const bound = primordialReflectApply(primordialBind, descriptor.value, [journal]);
      if (typeof bound !== 'function' || utilTypes.isProxy(bound)) throw new TypeError('actionJournal does not implement the durable transition contract');
      snapshot.summary = bound;
    } else snapshot.summary = null;
  } catch { throw new TypeError('actionJournal does not implement the durable transition contract'); }
  return Object.freeze(snapshot);
}
const PUBLIC_RECORD_KEYS = Object.freeze(['operation_id', 'tool_name', 'risk_tier', 'side_effect', 'state', 'arguments_digest', 'preview_digest', 'operation_digest', 'created_at_utc', 'updated_at_utc', 'receipt_hash']);
function safeRestartRecords(summary) {
  if (!summary || typeof summary !== 'object' || Array.isArray(summary) || utilTypes.isProxy(summary) || ![Object.prototype, null].includes(Object.getPrototypeOf(summary))) return [];
  const recordsDescriptor = Object.getOwnPropertyDescriptor(summary, 'records');
  if (!recordsDescriptor || !Object.hasOwn(recordsDescriptor, 'value') || recordsDescriptor.get !== undefined || recordsDescriptor.set !== undefined) return [];
  const records = recordsDescriptor.value;
  if (!Array.isArray(records) || utilTypes.isProxy(records) || records.length > MAX_GRAPH_RESTART_CANDIDATES) return [];
  const candidates = [];
  for (const record of records) {
    if (!record || typeof record !== 'object' || Array.isArray(record) || utilTypes.isProxy(record) || ![Object.prototype, null].includes(Object.getPrototypeOf(record))) continue;
    let descriptors;
    try { descriptors = Object.getOwnPropertyDescriptors(record); } catch { continue; }
    if (Object.keys(descriptors).length !== PUBLIC_RECORD_KEYS.length || !PUBLIC_RECORD_KEYS.every(key => Object.hasOwn(descriptors, key) && Object.hasOwn(descriptors[key], 'value') && descriptors[key].get === undefined && descriptors[key].set === undefined)) continue;
    const value = Object.fromEntries(PUBLIC_RECORD_KEYS.map(key => [key, descriptors[key].value]));
    if (!JOURNAL_OPERATION_ID.test(value.operation_id ?? '') || value.tool_name !== 'mail.create_draft' || value.state !== 'acknowledged' || !JOURNAL_RECEIPT_DIGEST.test(value.arguments_digest ?? '') || !JOURNAL_RECEIPT_DIGEST.test(value.operation_digest ?? '')) continue;
    candidates.push(Object.freeze({ operation_id: value.operation_id, tool_name: value.tool_name, state: value.state, arguments_digest: value.arguments_digest, operation_digest: value.operation_digest }));
  }
  return candidates;
}
function journalStatus(journal) {
  try {
    if (!journal) return { ready: false, code: 'action_journal_unavailable' };
    const health = journal.health();
    if (!health || typeof health !== 'object' || Array.isArray(health) || utilTypes.isProxy(health) || Object.getPrototypeOf(health) !== Object.prototype) return { ready: false, code: 'action_journal_unavailable' };
    const keys = Reflect.ownKeys(health); if (keys.length !== 2 || !keys.includes('state') || !keys.includes('error')) return { ready: false, code: 'action_journal_unavailable' };
    const stateDescriptor = Object.getOwnPropertyDescriptor(health, 'state'); const errorDescriptor = Object.getOwnPropertyDescriptor(health, 'error');
    if (!stateDescriptor || !errorDescriptor || !Object.hasOwn(stateDescriptor, 'value') || !Object.hasOwn(errorDescriptor, 'value') || stateDescriptor.get !== undefined || stateDescriptor.set !== undefined || errorDescriptor.get !== undefined || errorDescriptor.set !== undefined) return { ready: false, code: 'action_journal_unavailable' };
    const state = stateDescriptor.value; const error = errorDescriptor.value;
    if (typeof state !== 'string') return { ready: false, code: 'action_journal_unavailable' };
    if (state === 'ready') return { ready: error === null, code: error === null ? null : 'action_journal_unavailable' };
    return { ready: false, code: typeof error === 'string' && JOURNAL_FAILURE_CODES.has(error) ? error : 'action_journal_unavailable' };
  } catch { return { ready: false, code: 'action_journal_unavailable' }; }
}

const TOOL_DESCRIPTOR_FIELDS = Object.freeze(['name', 'version', 'description', 'risk_tier', 'side_effect', 'network', 'data_egress', 'requires_confirmation', 'timeout_ms', 'output_limit', 'parameters', 'input_schema', 'authorize', 'preview', 'confirmationRequired', 'execute']);
const MISSING_TOOL_PROPERTY = Symbol('missing-tool-property');
function snapshotToolValue(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object') return value;
  if (seen.has(value)) throw new TypeError('tool descriptor contains a cycle');
  seen.add(value);
  let snapshot;
  if (Array.isArray(value)) snapshot = value.map(item => snapshotToolValue(item, seen));
  else {
    snapshot = Object.create(Object.getPrototypeOf(value) === null ? null : Object.prototype);
    for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(value))) {
      if (!Object.hasOwn(descriptor, 'value')) throw new TypeError(`tool descriptor field ${key} must be a data property`);
      Object.defineProperty(snapshot, key, { value: snapshotToolValue(descriptor.value, seen), enumerable: descriptor.enumerable, writable: false, configurable: false });
    }
  }
  seen.delete(value);
  return Object.freeze(snapshot);
}
function readToolDataProperty(tool, key, required = false) {
  let descriptor;
  try { descriptor = Object.getOwnPropertyDescriptor(tool, key); } catch { throw new TypeError(`tool descriptor field ${key} is unavailable`); }
  if (!descriptor) { if (required) throw new TypeError(`tool descriptor field ${key} is required`); return MISSING_TOOL_PROPERTY; }
  if (!Object.hasOwn(descriptor, 'value')) throw new TypeError(`tool descriptor field ${key} must be a data property`);
  return descriptor.value;
}
function snapshotToolDescriptor(name, tool) {
  const snapshot = Object.create(null);
  for (const key of TOOL_DESCRIPTOR_FIELDS) {
    const value = readToolDataProperty(tool, key, ['name', 'execute'].includes(key));
    if (value !== MISSING_TOOL_PROPERTY) snapshot[key] = ['parameters', 'input_schema'].includes(key) ? snapshotToolValue(value) : value;
  }
  if (snapshot.name !== name) throw new TypeError(`tool registry key ${name} does not match its immutable name`);
  const canonical = NATIVE_CANONICAL_METADATA[name];
  if (canonical && Object.entries(canonical).some(([key, value]) => value === null ? Object.hasOwn(snapshot, key) : snapshot[key] !== value)) throw new TypeError(`canonical security metadata for ${name} is invalid`);
  return Object.freeze(snapshot);
}
function executionRegistrySnapshot(toolRegistry) {
  if (toolRegistry === undefined) return { entries: [], graphRestartControls: new Map() };
  if (!toolRegistry || typeof toolRegistry !== 'object' || Array.isArray(toolRegistry)) throw new TypeError('toolRegistry must be an object');
  const graphRestartControls = new Map();
  const entries = Object.entries(toolRegistry).map(([name, tool]) => {
    if (!tool || typeof tool !== 'object' || Array.isArray(tool)) throw new TypeError(`toolRegistry entry ${name} must be an object`);
    const restartControl = readGraphRestartControl(tool); if (restartControl) graphRestartControls.set(name, restartControl);
    const snapshot = snapshotToolDescriptor(name, tool);
    if (typeof snapshot.execute !== 'function') throw new TypeError(`toolRegistry entry ${name} must provide its matching execute function`);
    return [name, snapshot];
  });
  return { entries, graphRestartControls };
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
  #tools;
  #journalMethods;
  #graphRestartControls;
  #restartReconciliationPromise;
  // The one memory summary in flight, controller-wide: the engine serves one
  // generation at a time, whichever session it belongs to.
  #memoryJob = null;
  // `contextTokens` must match the engine's `--context`; `maxOutputTokens`
  // defaults to the engine client's per-request `max_tokens`, which the engine
  // reserves out of the same window.
  constructor({ engine, maxToolCalls = 8, confirmationTimeoutMs = DEFAULT_CONFIRMATION_TIMEOUT_MS, maxSessions = 4, maxHistoryMessages = 64, maxHistoryBytes = 262144, contextTokens = CONTEXT_DEFAULTS.contextTokens, maxOutputTokens = engine?.maxTokens ?? CONTEXT_DEFAULTS.maxOutputTokens, toolRegistry, actionJournal, memory } = {}) {
    if (!engine?.generate) throw new TypeError('engine.generate is required');
    if (!Number.isInteger(maxSessions) || maxSessions < 1) throw new TypeError('maxSessions must be positive');
    if (!Number.isInteger(maxHistoryMessages) || maxHistoryMessages < 1 || !Number.isInteger(maxHistoryBytes) || maxHistoryBytes < 1024) throw new TypeError('history limits are invalid');
    // 512 is the smallest window the host launcher accepts; such a window
    // only fits plain chat (tools: 'off'), which the budget then enforces.
    if (!Number.isInteger(contextTokens) || contextTokens < 512 || contextTokens > 16384 || !Number.isInteger(maxOutputTokens) || maxOutputTokens < 1 || maxOutputTokens * 4 > contextTokens) throw new TypeError('context limits are invalid');
    if (!Number.isInteger(confirmationTimeoutMs) || confirmationTimeoutMs < 1 || confirmationTimeoutMs > 3600000) throw new TypeError('confirmationTimeoutMs is invalid');
    this.contextTokens = contextTokens; this.maxOutputTokens = maxOutputTokens;
    // Off unless asked for: see memory-note.mjs.
    this.memory = memoryOptions(memory);
    // Validated now, so a bad recall bound fails at start-up, not mid-conversation.
    this.recallBounds = recallOptions(this.memory);
    this.#journalMethods = actionJournal === undefined ? null : snapshotJournalMethods(actionJournal);
    this.engine = engine; this.maxToolCalls = maxToolCalls; this.confirmationTimeoutMs = confirmationTimeoutMs; this.maxSessions = maxSessions; this.maxHistoryMessages = maxHistoryMessages; this.maxHistoryBytes = maxHistoryBytes; this.actionJournal = actionJournal; this.clock = 0;
    this.sessions = new Map(); this.active = null; this.pending = new Map(); this.expiredConfirmations = new Map();
    const registry = executionRegistrySnapshot(toolRegistry); this.#graphRestartControls = registry.graphRestartControls; this.#restartReconciliationPromise = null;
    this.#tools = new Map([[timeNowDefinition.name, { ...timeNowDefinition, execute: ({ id, arguments: args }) => timeNowTool({ id, arguments: args }) }], ...registry.entries]);
  }
  reconcileRestartActions({ signal } = {}) {
    if (this.#restartReconciliationPromise) return this.#restartReconciliationPromise;
    this.#restartReconciliationPromise = this.#reconcileRestartActions(signal).finally(() => { this.#restartReconciliationPromise = null; });
    return this.#restartReconciliationPromise;
  }
  async #reconcileRestartActions(signal) {
    const health = journalStatus(this.#journalMethods);
    if (!health.ready || typeof this.#journalMethods?.summary !== 'function') return restartUnavailable(health.code ?? 'action_journal_unavailable');
    let summary;
    try { summary = await this.#journalMethods.summary({ limit: MAX_GRAPH_RESTART_CANDIDATES, state: 'acknowledged' }); } catch { return restartUnavailable('action_journal_summary_failed'); }
    const candidates = safeRestartRecords(summary); let examined = 0; let completed = 0; let blocked = 0; let code = null;
    const deadline = AbortSignal.timeout(GRAPH_RESTART_TIMEOUT_MS); const proofSignal = signal ? AbortSignal.any([signal, deadline]) : deadline;
    for (const candidate of candidates) {
      if (proofSignal.aborted) break;
      const control = this.#graphRestartControls.get(candidate.tool_name); if (!control) continue;
      examined += 1;
      let attestation;
      try { attestation = readGraphRestartAttestation(await control.reconcile(candidate, proofSignal)); } catch { continue; }
      if (!attestation || attestation.provider !== 'microsoft_graph' || attestation.tool_name !== candidate.tool_name || attestation.state !== 'completed' || attestation.proof !== 'restart_unique_exact_draft' || attestation.operation_id !== candidate.operation_id || attestation.operation_digest !== candidate.operation_digest || attestation.arguments_digest !== candidate.arguments_digest || !JOURNAL_RECEIPT_DIGEST.test(attestation.account_fingerprint ?? '')) continue;
      // A `complete()` rejection is exactly the signal that the durable
      // journal blocked itself or that a transition raced.  The record keeps
      // its current durable state and stays eligible for a later pass, and the
      // failure is surfaced as a typed metadata-only count instead of being
      // discarded.  Nothing else is attempted on that record.
      try { await this.#journalMethods.complete(candidate.operation_id); completed += 1; }
      catch (error) { blocked += 1; code ??= restartFailureCode(error); }
    }
    return Object.freeze({ state: blocked > 0 ? 'degraded' : 'completed', examined, completed, blocked, code });
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
    const session = { id: sessionId, state: 'IDLE', history: [], history_bytes: 0, bytes_per_token: null, context_compactions: 0, memory: newMemoryState(), created_at: new Date().toISOString(), last_request_id: null, last_used: ++this.clock };
    this.sessions.set(sessionId, session); return session;
  }
  _touch(session) { session.last_used = ++this.clock; return session; }
  getSession(sessionId) { return this._touch(this.sessions.get(sessionId) ?? this.createSession(sessionId)); }
  // Hard storage bounds, enforced a whole oldest turn at a time: shifting off a
  // single message could strand a tool result without its call or leave no
  // user message, both of which the engine rejects.  The latest turn is never
  // split, so it may alone exceed a bound; the token budget then decides.
  _appendHistory(session, message) {
    session.history.push(message); session.history_bytes += storedBytes(message);
    this._enforceHistoryBounds(session);
  }
  // The pinned memory note is one more message on the wire, and the engine
  // refuses more than 64, so it takes one slot of the message bound.
  _enforceHistoryBounds(session) {
    const maxMessages = this.maxHistoryMessages - (session.memory?.note ? 1 : 0);
    const overMessages = session.history.length > maxMessages;
    const overBytes = session.history_bytes > this.maxHistoryBytes;
    if (!overMessages && !overBytes) return;
    // Trim to a LOW-WATER mark, not merely back under the cap. The engine reuses
    // the retained prompt only while each prompt strictly extends the last, and
    // dropping the oldest turn changes the prompt's start. Trimming exactly to the
    // cap would drop one turn on EVERY following turn (a short chat sits at the cap
    // forever), so every turn would re-read the whole history. Dropping to 75% once
    // buys many stable turns before the next trim.
    const lowMessages = overMessages ? Math.max(1, Math.floor(maxMessages * HISTORY_LOW_WATER)) : maxMessages;
    const lowBytes = overBytes ? Math.floor(this.maxHistoryBytes * HISTORY_LOW_WATER) : this.maxHistoryBytes;
    while (session.history.length > lowMessages || session.history_bytes > lowBytes) {
      const length = droppableHeadLength(session.history); if (!length) break;
      const removed = session.history.splice(0, length);
      for (const message of removed) session.history_bytes -= storedBytes(message);
      this._toBacklog(session, removed);
    }
  }
  _memoryState(session) { session.memory ??= newMemoryState(); return session.memory; }
  // What the engine is sent: the pinned note (when there is one) and then the
  // history.  The note is rebuilt from the same stored text every time, so it
  // is byte-identical between compactions and later prompts still extend
  // earlier ones.
  _promptMessages(session) { const note = session.memory?.note; return note ? [noteMessage(note), ...session.history] : session.history; }
  _noteTokens(session, bytesPerToken) { const note = session.memory?.note; return note ? messageTokens(noteMessage(note), bytesPerToken) : 0; }
  // Recall (memory.mode 'recall' or 'summary'): everything that leaves the
  // window is archived, and the lines best matching the user's new message
  // are stored in the history just before that user message.  Stored, not
  // injected per prompt, so every later prompt still extends the one before it
  // and the engine's prefix reuse is untouched; they age out like any message.
  get #recallOn() { return this.memory.mode !== 'off'; }
  _archive(session) { const memory = this._memoryState(session); return (memory.archive ??= new RecallArchive(this.recallBounds)); }
  _recallFor(session, message) {
    if (!this.#recallOn) return null;
    const memory = this._memoryState(session); const archive = memory.archive;
    let lines = archive?.size ? archive.search(recallQueries(message, memory.last_user)) : [];
    // A line already in the window (from an earlier recalled block) is not
    // repeated: recalled blocks would otherwise pile up and crowd the window.
    if (lines.length) { const visible = new Set(); for (const stored of session.history) if (isRecallMessage(stored)) for (const line of stored.content.split('\n').slice(1)) visible.add(line.slice(2)); lines = lines.filter(line => !visible.has(line)); }
    return recallMessage(lines);
  }
  // Summary mode only: content that left the window without being summarised
  // waits here (bounded, oldest discarded first) and is folded into the next
  // summary.  In off mode dropped content is simply gone, as it always was.
  _toBacklog(session, messages) {
    if (this.#recallOn && messages.length) this._archive(session).add(messages);
    if (this.memory.mode !== 'summary' || !messages.length) return;
    const memory = this._memoryState(session);
    for (const message of messages) {
      if (typeof message?.content !== 'string' || isElidedToolResult(message)) continue;
      memory.backlog.push(message); memory.backlog_bytes += utf8Bytes(message.content);
    }
    while (memory.backlog.length && memory.backlog_bytes > this.memory.backlogBytes) {
      const lost = memory.backlog.shift(); memory.backlog_bytes -= utf8Bytes(lost.content); memory.lost_messages += 1;
    }
  }
  // Fit the history into the engine window before a model call and persist the
  // result: what was elided or dropped stays that way, so later prompts extend
  // this one byte-for-byte and engine prefix reuse resumes.  Compaction goes
  // down to a low-water mark, so it is rare rather than every turn, and at
  // turn start (where the engine re-prefills anyway) it triggers early to
  // spare the tool loop.  `shrink` is for after an engine overflow: the
  // estimate was just proven wrong, so the cut is relative to what the engine
  // rejected, not to the budget the estimate claimed was met.
  _fitContext(session, tools, emit, { reason = 'budget', turnStart = false, shrink } = {}) {
    const budget = historyBudget({ tools, bytesPerToken: session.bytes_per_token ?? undefined, contextTokens: this.contextTokens, maxOutputTokens: this.maxOutputTokens });
    // The pinned note is part of every prompt, so the history gets what is
    // left after it (with no note this is the whole budget, as before).
    const noteTokens = this._noteTokens(session, budget.bytesPerToken);
    const available = budget.budgetTokens - noteTokens;
    const limits = shrink !== undefined
      ? { triggerTokens: 0, targetTokens: Math.floor(Math.min(available, messagesTokens(session.history, budget.bytesPerToken)) * shrink) }
      : turnStart ? { triggerTokens: Math.floor(available * CONTEXT_DEFAULTS.turnStartTriggerRatio), targetTokens: Math.floor(available * CONTEXT_DEFAULTS.turnStartTargetRatio) } : {};
    const before = session.history;
    const fitted = fitHistory({ messages: session.history, budgetTokens: available, ...limits, bytesPerToken: budget.bytesPerToken });
    const estimatedTokens = fitted.tokens + noteTokens + budget.toolTokens + budget.overheadTokens;
    if (fitted.changed) {
      session.history = fitted.messages; session.history_bytes = fitted.messages.reduce((sum, message) => sum + storedBytes(message), 0); session.context_compactions += 1;
      // A compaction the memory note was not ready for: what it removed
      // waits for the next summary instead of being lost outright.
      this._toBacklog(session, removedBy(before, fitted.messages).removed);
      const state = this._memoryState(session);
      const memory = this.memory.mode === 'summary' ? { memory_note: { state: 'deferred', backlog_messages: state.backlog.length, lost_messages: state.lost_messages } } : {};
      // Counts only: the UI can say "earlier context was condensed" without
      // the host logging any prompt or tool content.
      emit('metrics.snapshot', { context_compaction: { reason, masked_tool_results: fitted.masked, elided_bytes: fitted.maskedBytes, dropped_turns: fitted.droppedTurns, dropped_messages: fitted.droppedMessages, estimated_prompt_tokens: estimatedTokens, context_tokens: this.contextTokens, history_messages: session.history.length, compactions: session.context_compactions }, ...memory });
    }
    return { estimatedTokens, changed: fitted.changed };
  }
  // ---- memory note (memory.mode 'summary') ---------------------------------
  // Timeline, designed so the extra engine call never delays the user:
  //  1. Right after a turn's answer is complete, plan the compaction the next
  //     turn start would make (with some headroom), and summarise what it
  //     would remove -- plus any backlog -- in the background.  The UI gets a
  //     `memory_note: scheduled` event on the turn that just finished.
  //  2. At the next turn start: if the summary is ready, apply the planned
  //     compaction and the new note together, as ONE compaction event.  If it
  //     is still running it is cancelled (after `waitMs`), and if it failed
  //     the turn falls back to plain dropping; either way the turn proceeds.
  // The note therefore changes only when the history head changes anyway,
  // and the summary call only runs when a compaction is imminent, which is
  // when the engine's retained prompt is about to be invalidated regardless.
  memoryIdle() { return this.#memoryJob ? this.#memoryJob.promise : Promise.resolve(); }
  _scheduleMemory(session, tools, emit) {
    try {
      if (this.memory.mode !== 'summary' || this.#memoryJob) return;
      const memory = this._memoryState(session);
      const budget = historyBudget({ tools, bytesPerToken: session.bytes_per_token ?? undefined, contextTokens: this.contextTokens, maxOutputTokens: this.maxOutputTokens });
      const noteLimit = noteByteLimit(this.memory);
      // Planned against the LARGEST note the summary may return, so applying
      // it can never push the next prompt over the budget.
      const available = budget.budgetTokens - messageTokens(noteMessage('x'.repeat(noteLimit)), budget.bytesPerToken);
      // Room for the next user message and answer under the message bound.
      const maxMessages = this.maxHistoryMessages - 3;
      const plan = planCompaction({ history: session.history, budgetTokens: available, triggerTokens: Math.floor(available * CONTEXT_DEFAULTS.turnStartTriggerRatio) - this.memory.planHeadroomTokens, targetTokens: Math.floor(available * this.memory.planTargetPercent / 100), bytesPerToken: budget.bytesPerToken, maxMessages, targetMessages: Math.floor(maxMessages * 0.75) });
      // No compaction coming: nothing is summarised, and a backlog waits for
      // the next one rather than costing a call (and the engine's retained
      // prompt) on a turn that would otherwise reuse it.
      if (!plan) return;
      // The summary prompt itself must fit the engine window with the
      // engine's per-request output reservation.
      const skeleton = summaryRequest({ note: memory.note, excerpt: '', noteLimitBytes: noteLimit });
      const margin = Math.max(CONTEXT_DEFAULTS.minMarginTokens, Math.ceil(this.contextTokens * CONTEXT_DEFAULTS.marginRatio));
      const room = this.contextTokens - this.maxOutputTokens - margin - CONTEXT_DEFAULTS.noToolsOverheadTokens - messagesTokens(skeleton, budget.bytesPerToken);
      const maxBytes = Math.min(this.memory.maxInputBytes, Math.floor(room * budget.bytesPerToken));
      if (maxBytes < 256) return;
      const backlogUsed = memory.backlog.length;
      const excerpt = buildExcerpt([...memory.backlog, ...plan.removed], { maxBytes, perMessageBytes: Math.min(this.memory.perMessageBytes, maxBytes) });
      if (!excerpt.included) return;
      const job = { session, epoch: memory.epoch, base: session.history.slice(), plan, excerpt, backlogUsed, abort: new AbortController(), requestId: opaque('req'), started: Date.now(), promise: null };
      this.#memoryJob = job;
      job.promise = this.#runMemoryJob(job);
      emit('metrics.snapshot', { memory_note: { state: 'scheduled', dropped_turns: plan.droppedTurns, masked_tool_results: plan.masked, summarised_messages: excerpt.included, omitted_messages: excerpt.omitted, backlog_messages: backlogUsed, input_bytes: excerpt.bytes } });
    } catch { /* memory is best-effort: plain dropping still keeps the turn inside the window */ }
  }
  async #runMemoryJob(job) {
    const memory = job.session.memory; const limit = noteByteLimit(this.memory);
    const timeout = AbortSignal.timeout(this.memory.timeoutMs);
    const signal = AbortSignal.any([job.abort.signal, timeout]);
    let outcome;
    try {
      const messages = summaryRequest({ note: memory.note, excerpt: job.excerpt.text, noteLimitBytes: limit });
      let text = '';
      for await (const frame of this.engine.generate({ requestId: job.requestId, sessionId: job.session.id, messages, tools: [], mode: 'normal', signal })) {
        if (signal.aborted) break;
        // Offered no tools, a model that answers with a call has not
        // written a note; nothing of it is kept.
        if (frame.kind === 'tool_call_chunk') throw memoryFailure('memory_tool_call_output');
        // The engine client's answer cap is far above the note's; stop
        // reading (which stops the engine) once there is more than enough.
        if (frame.kind === 'text_delta' && typeof frame.text === 'string') { text += frame.text; if (utf8Bytes(text) > limit * 4) break; }
      }
      if (signal.aborted) throw Object.assign(new Error('memory_aborted'), { code: 'cancelled' });
      const note = sanitizeNote(text, { maxBytes: limit });
      if (!note) throw memoryFailure('memory_empty_note');
      if (memory.epoch !== job.epoch) outcome = { state: 'stale' };
      else { memory.ready = { epoch: job.epoch, base: job.base, plan: job.plan, note, backlogUsed: job.backlogUsed, excerpt: { included: job.excerpt.included, omitted: job.excerpt.omitted }, duration_ms: Date.now() - job.started }; outcome = { state: 'ready' }; }
    } catch (error) {
      const cancelled = job.abort.signal.aborted && !timeout.aborted;
      const code = timeout.aborted || error?.code === 'engine_timeout' ? 'memory_timeout' : cancelled ? 'memory_cancelled' : isContextOverflowError(error) ? 'memory_context_overflow' : error?.code === 'busy' ? 'memory_engine_busy' : error?.memoryOutcome ?? 'memory_engine_error';
      if (cancelled) memory.cancelled += 1; else memory.failures += 1;
      outcome = { state: cancelled ? 'cancelled' : 'failed', code };
    } finally {
      if (this.#memoryJob === job) this.#memoryJob = null;
      if (memory.epoch === job.epoch) memory.outcome = { ...outcome, duration_ms: Date.now() - job.started };
    }
  }
  async _settleMemory(session, tools, emit) {
    const memory = this._memoryState(session); const job = this.#memoryJob; let waited = 0;
    if (job) {
      const start = Date.now();
      if (this.memory.waitMs > 0) await settleWithin(job.promise, this.memory.waitMs);
      if (this.#memoryJob === job) {
        job.abort.abort();
        await settleWithin(job.promise, MEMORY_CANCEL_GRACE_MS);
        try { await this.engine.waitReady?.({ timeoutMs: MEMORY_CANCEL_GRACE_MS }); } catch { /* the turn's own call reports a busy engine */ }
      }
      waited = Date.now() - start;
    }
    const ready = memory.ready; const outcome = memory.outcome; memory.ready = null; memory.outcome = null;
    const startsWithBase = ready && session.history.length >= ready.base.length && ready.base.every((message, index) => session.history[index] === message);
    if (!ready || ready.epoch !== memory.epoch || !startsWithBase) {
      if (outcome) emit('metrics.snapshot', { memory_note: { ...(ready ? { state: 'stale' } : outcome), waited_ms: waited } });
      return;
    }
    // The planned compaction and the new note land together: one change to
    // the head of the prompt, not two.
    session.history = [...ready.plan.messages, ...session.history.slice(ready.base.length)];
    session.history_bytes = session.history.reduce((sum, message) => sum + storedBytes(message), 0);
    // What the plan removed was still in the window until now; the backlog part
    // of the excerpt was archived when it entered the backlog.
    this._archive(session).add(ready.plan.removed);
    memory.note = ready.note; memory.summaries += 1;
    for (const message of memory.backlog.splice(0, ready.backlogUsed)) memory.backlog_bytes -= utf8Bytes(message.content);
    memory.lost_messages += ready.excerpt.omitted;
    session.context_compactions += 1;
    this._enforceHistoryBounds(session);
    const budget = historyBudget({ tools, bytesPerToken: session.bytes_per_token ?? undefined, contextTokens: this.contextTokens, maxOutputTokens: this.maxOutputTokens });
    const noteTokens = this._noteTokens(session, budget.bytesPerToken);
    emit('metrics.snapshot', {
      context_compaction: { reason: 'memory_summary', masked_tool_results: ready.plan.masked, dropped_turns: ready.plan.droppedTurns, dropped_messages: ready.plan.droppedMessages, estimated_prompt_tokens: messagesTokens(session.history, budget.bytesPerToken) + noteTokens + budget.toolTokens + budget.overheadTokens, context_tokens: this.contextTokens, history_messages: session.history.length, compactions: session.context_compactions },
      memory_note: { state: 'applied', note_bytes: utf8Bytes(memory.note), note_tokens_estimate: noteTokens, summarised_messages: ready.excerpt.included, omitted_messages: ready.excerpt.omitted, duration_ms: ready.duration_ms, waited_ms: waited, summaries: memory.summaries },
    });
  }
  // Only a real tokenizer count can move the ratio; see observedBytesPerToken.
  _learnTokenRatio(session, messages, tools, usage) {
    const promptTokens = usage?.prompt_tokens;
    const learned = learnBytesPerToken({ observed: observedBytesPerToken({ messages, tools, promptTokens }), current: session.bytes_per_token ?? undefined, promptTokens, contextTokens: this.contextTokens });
    if (learned !== null) session.bytes_per_token = learned;
  }
  resetSession(sessionId) {
    const session = this.sessions.get(sessionId); if (!session) return false;
    if (session.state !== 'IDLE' && session.state !== 'COMPLETED' && session.state !== 'FAILED' && session.state !== 'CANCELLED') throw Object.assign(new Error('session_busy'), { code: 'session_busy' });
    session.history = []; session.history_bytes = 0; session.state = 'IDLE';
    // A reset forgets the note too, and a summary still running for this
    // session is stopped; its result would describe a conversation that no
    // longer exists (the epoch check refuses it even if it lands).
    if (this.#memoryJob?.session === session) this.#memoryJob.abort.abort();
    session.memory = newMemoryState((session.memory?.epoch ?? 0) + 1);
    return true;
  }
  state(sessionId) { return this.getSession(sessionId).state; }
  _cancelPending(requestId) { const active = this.active; if (!active || active.requestId !== requestId || !active.confirmationId) return false; const item = this.pending.get(active.confirmationId); if (!item) return false; this.pending.delete(active.confirmationId); active.confirmationId = null; item.resolve(CANCELLED_CONFIRMATION); return true; }
  cancel(requestId) { if (this.active?.requestId !== requestId) return false; this.active.controller.abort(); this._cancelPending(requestId); this.engine.cancel?.(requestId); return true; }
  cancelActive() { return this.active ? this.cancel(this.active.requestId) : false; }
  confirm(confirmationId, approved, { requestId, callId } = {}) { const item = this.pending.get(confirmationId); if (!item || typeof approved !== 'boolean') return false; if (typeof requestId !== 'string' || typeof callId !== 'string' || requestId !== item.requestId || callId !== item.callId) return false; this.pending.delete(confirmationId); item.resolve(approved); return true; }
  // True only for a confirmation that this controller let expire, matched to
  // the same request and call: a late click then gets an honest "expired"
  // answer while a guessed or foreign id still looks unknown.
  confirmationExpired(confirmationId, { requestId, callId } = {}) {
    const item = this.expiredConfirmations.get(confirmationId);
    return Boolean(item && typeof requestId === 'string' && typeof callId === 'string' && item.requestId === requestId && item.callId === callId);
  }
  // Resolves to true/false (the user's answer), CANCELLED_CONFIRMATION, or
  // EXPIRED_CONFIRMATION. Expiry is never an implicit approval or denial.
  #awaitConfirmation(confirmationId, requestId, sessionId, callId) {
    return new Promise(resolve => {
      const timer = setTimeout(() => {
        if (!this.pending.delete(confirmationId)) return;
        this.expiredConfirmations.set(confirmationId, { requestId, callId });
        while (this.expiredConfirmations.size > MAX_EXPIRED_CONFIRMATIONS) this.expiredConfirmations.delete(this.expiredConfirmations.keys().next().value);
        resolve(EXPIRED_CONFIRMATION);
      }, this.confirmationTimeoutMs);
      this.pending.set(confirmationId, { resolve: answer => { clearTimeout(timer); resolve(answer); }, requestId, sessionId, callId });
    });
  }
  // Rolls a failed or cancelled turn back to what the user actually saw. If
  // the turn produced nothing (no streamed text, no tool step), its user
  // message is removed: the model never answered it, and leaving it would
  // make the next turn answer two questions. If the user saw a partial
  // answer, the message stays and the partial answer is recorded with a
  // marker, so history matches the transcript. Completed tool steps are
  // always kept, since their side effects happened. Removing only the
  // newest message leaves the earlier history byte-identical, so the
  // engine's prefix reuse for the next prompt is unaffected.
  _settleFailedTurn(session, userMessage, partialText) {
    if (partialText) { this._appendHistory(session, { role: 'assistant', content: partialText + INTERRUPTED_ANSWER_MARKER }); return true; }
    if (session.history.at(-1) !== userMessage) return true;
    session.history.pop(); session.history_bytes -= storedBytes(userMessage);
    // The recalled lines were stored for this message only.
    const before = session.history.at(-1); if (isRecallMessage(before)) { session.history.pop(); session.history_bytes -= storedBytes(before); }
    return false;
  }
  emitFactory(requestId, sessionId, onEvent) { let sequence = 0; return (event, data) => { const output = makeEvent({ event, requestId, sessionId, sequence: sequence++, data }); onEvent?.(output); return output; }; }
  // `tools: 'off'` sends this turn with no tool definitions (plain chat).
  async runTurn({ sessionId, message, mode = 'normal', tools: toolMode = 'auto', requestId = opaque('req'), signal, onEvent, delegateScope } = {}) {
    if (typeof message !== 'string' || !message.trim()) throw Object.assign(new Error('invalid_message'), { code: 'invalid_message' });
    // The engine bounds each message in UTF-8 BYTES, not UTF-16 characters:
    // 20,000 CJK characters are 60,000 bytes and would pass a length check
    // only to be refused by the engine after the turn had started.
    const messageBytes = utf8Bytes(message);
    if (messageBytes > ENGINE_MAX_MESSAGE_BYTES) throw Object.assign(new Error(`Message is too long: ${messageBytes} bytes of UTF-8 text; the limit is ${ENGINE_MAX_MESSAGE_BYTES} bytes. Shorten it or split it into several messages.`), { code: 'invalid_message_too_large', bytes: messageBytes, limit_bytes: ENGINE_MAX_MESSAGE_BYTES });
    if (!/^[A-Za-z0-9_-]{8,96}$/.test(requestId)) throw Object.assign(new Error('invalid_request_id'), { code: 'invalid_request_id' });
    if (!TOOL_MODES.includes(toolMode)) throw Object.assign(new Error('invalid_tools_mode'), { code: 'invalid_tools_mode' });
    // A scope only means something to delegate mode, and delegate mode never
    // runs without one: no scope cannot quietly mean "every folder".
    if ((toolMode === 'delegate') !== (delegateScope !== undefined)) throw Object.assign(new Error('invalid_delegate_scope'), { code: 'invalid_delegate_scope' });
    const scope = toolMode === 'delegate' ? delegateScopeOf(delegateScope) : null;
    const session = this.getSession(sessionId); if (this.active) throw Object.assign(new Error('another_generation_active'), { code: 'busy' });
    if (!['normal', 'deep'].includes(mode)) throw Object.assign(new Error('invalid_mode'), { code: 'invalid_mode' });
    const controller = new AbortController();
    const relayAbort = () => { controller.abort(); this._cancelPending(requestId); }; signal?.addEventListener('abort', relayAbort, { once: true });
    this.active = { requestId, sessionId: session.id, controller, confirmationId: null };
    const emit = this.emitFactory(requestId, session.id, onEvent);
    const userMessage = { role: 'user', content: message };
    session.last_request_id = requestId; this._appendHistory(session, userMessage); session.state = 'BUILDING_PROMPT';
    let text = ''; let calls = 0; let activeJournalOperation = null;
    try {
      emit('message.started', { mode, state: session.state, tools: toolMode });
      while (true) {
        if (controller.signal.aborted) throw Object.assign(new Error('cancelled'), { code: 'cancelled' });
        session.state = calls ? 'CONTINUING_MODEL' : 'INFERENCING'; emit('message.started', { mode, state: session.state, continuation: calls > 0 });
        const journal = journalStatus(this.#journalMethods); const ready = journal.ready;
        // With tools off the request carries an empty list, so the engine's
        // template renders no tool preamble at all and the budget reserves
        // only the small no-tools overhead.
        const tools = toolMode === 'off' ? [] : modelToolDefinitions(new Map([...this.#tools].filter(([name, tool]) => {
          if (scope) return delegateEligible(name, tool, scope);
          const nativeOwned = nativeSupervisorOwnerFor(name, tool, process.platform) !== null;
          return nativeOwned ? ready : !requiresDurableAction(tool) || ready;
        })));
        let callText = ''; let gotCall = false; let usage; let finishReason;
        if (calls === 0 && this.memory.mode === 'summary') await this._settleMemory(session, tools, emit);
        let fit = this._fitContext(session, tools, emit, { turnStart: calls === 0 }); let sent;
        if (calls === 0) {
          // After the fit, so lines it just archived can be found, and then a
          // second fit so the recalled block counts against the budget.
          const memory = this._memoryState(session); const recalled = this._recallFor(session, message); memory.last_user = message;
          if (recalled) {
            const at = session.history.lastIndexOf(userMessage);
            if (at >= 0) { session.history.splice(at, 0, recalled); session.history_bytes += storedBytes(recalled); this._enforceHistoryBounds(session); fit = this._fitContext(session, tools, emit, { turnStart: true }); }
            emit('metrics.snapshot', { memory_recall: { lines: recalled.content.split('\n').length - 1, bytes: utf8Bytes(recalled.content), archive_entries: memory.archive?.size ?? 0 } });
          }
        }
        for (let attempt = 0; ; attempt++) {
          let framed = false; sent = this._promptMessages(session);
          try {
            for await (const frame of this.engine.generate({ requestId, sessionId: session.id, messages: sent, tools, mode, signal: controller.signal })) {
              framed = true;
              if (frame.kind === 'text_delta') { text += frame.text; emit('message.delta', { text: frame.text }); }
              else if (frame.kind === 'tool_call_chunk') { gotCall = true; callText += frame.text; if (Buffer.byteLength(callText) > 32768) throw new EnvelopeError('tool_call_too_large', 'tool call exceeds limit'); }
              else if (frame.kind === 'done') { usage = frame.usage; finishReason = frame.finish_reason; }
            }
            break;
          } catch (error) {
            // The engine refuses an oversized prompt before its first token, so
            // nothing has reached the UI and one retry cannot duplicate output.
            if (framed || controller.signal.aborted || !isContextOverflowError(error, { estimatedTokens: fit.estimatedTokens, contextTokens: this.contextTokens })) throw error;
            if (attempt > 0) throw new ContextBudgetError({ estimated_tokens: fit.estimatedTokens, reason: 'engine_overflow' });
            // The estimate was wrong in the unsafe direction: distrust the
            // ratio for the rest of the session and trim well below budget.
            session.bytes_per_token = penalizeBytesPerToken(session.bytes_per_token ?? undefined);
            fit = this._fitContext(session, tools, emit, { reason: 'engine_overflow', shrink: 0.5 });
            if (!fit.changed) throw new ContextBudgetError({ estimated_tokens: fit.estimatedTokens, reason: 'engine_overflow' });
          }
        }
        this._learnTokenRatio(session, sent, tools, usage);
        if (!gotCall) {
          this._appendHistory(session, { role: 'assistant', content: text }); session.state = 'COMPLETED';
          emit('message.completed', { text, finish_reason: FINISH_REASONS.has(finishReason) ? finishReason : 'stop', usage: reportedUsage(usage), state: session.state });
          emit('metrics.snapshot', { tool_calls: calls, history_messages: session.history.length, history_bytes: session.history_bytes });
          // After the answer, so the summary (if one is due) runs while the
          // user reads, not while they wait.
          if (this.memory.mode === 'summary') this._scheduleMemory(session, tools, emit);
          return { requestId, sessionId: session.id, state: session.state, text };
        }
        if (text.trim()) throw new EnvelopeError('mixed_tool_call_output', 'tool call output cannot contain assistant text');
        // A model can still emit call syntax it was never offered; running a
        // tool the user switched off would make the switch meaningless.
        if (toolMode === 'off') throw Object.assign(new Error('tool_call_not_offered'), { code: 'tool_call_not_offered' });
        calls++; if (calls > this.maxToolCalls) throw Object.assign(new Error('tool_call_limit_exceeded'), { code: 'tool_call_limit_exceeded' });
        const call = parseToolCall(callText); session.state = 'TOOL_PROPOSED';
        const tool = this.#tools.get(call.name); if (!tool) throw Object.assign(new Error('unknown_tool'), { code: 'unknown_tool' });
        // Delegate mode fails closed on anything it did not offer, and on a
        // folder outside the job's scope, before any preview or execution.
        if (scope && !delegateEligible(call.name, tool, scope)) throw Object.assign(new Error('tool_not_offered'), { code: 'tool_not_offered' });
        if (scope && DELEGATE_FILE_TOOLS.has(call.name) && !scope.workspaces.includes(call.arguments?.workspace_id)) throw Object.assign(new Error('workspace_not_offered'), { code: 'workspace_not_offered' });
        coerceBooleanArguments(tool.parameters ?? parameterSchema(call.name), call.arguments);
        validateToolArgumentShape(tool, call);
        const nativeDispatchOwner = nativeSupervisorOwnerFor(tool.name, tool, process.platform);
        if ((nativeDispatchOwner !== null || requiresDurableAction(tool)) && !journal.ready) throw Object.assign(new Error('durable action journal is unavailable'), { code: journal.code });
        let preview;
        if (tool.preview) preview = await invokeWithTimeout(tool, tool.preview, call, controller.signal);
        emit('tool.proposed', { call: publicToolCall(call), ...argumentsSummaryField(call, preview), ...(preview === undefined ? {} : { preview: displayPreview(preview, call) }) });
        let approved = true; let previewAccessDenied = false; let authorization = { kind: 'policy' }; let expiredConfirmationId = null;
        if (preview?.preview_authorization_required === true) {
          session.state = 'WAITING_CONFIRMATION'; const confirmationId = opaque('cnf'); this.active.confirmationId = confirmationId;
          emit('tool.confirmation_required', { confirmation_id: confirmationId, call: publicToolCall(call), ...argumentsSummaryField(call, preview), preview: displayPreview(preview, call), phase: 'preview_access', risk_tier: 'T1', expires_in_ms: this.confirmationTimeoutMs });
          approved = await this.#awaitConfirmation(confirmationId, requestId, session.id, call.id);
          this.active.confirmationId = null;
          if (approved === CANCELLED_CONFIRMATION) { if (activeJournalOperation) { await this.#journalMethods.cancel(activeJournalOperation.id, 'request_cancelled'); activeJournalOperation = null; } throw Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
          if (approved === EXPIRED_CONFIRMATION) { approved = false; expiredConfirmationId = confirmationId; }
          if (!approved) { authorization = { kind: 'policy' }; previewAccessDenied = true; }
          else { preview = await invokeWithTimeout(tool, tool.preview, { ...call, authorization: { kind: 'user_confirmation' }, preview_authorized: true }, controller.signal); emit('tool.proposed', { call: publicToolCall(call), ...argumentsSummaryField(call, preview), preview: displayPreview(preview, call) }); }
        }
        if (nativeDispatchOwner !== null || requiresDurableAction(tool)) {
          if (!journal.ready) throw Object.assign(new Error('durable action journal is unavailable'), { code: journal.code });
          const binding = createActionBinding({ requestId, callId: call.id, toolName: call.name, arguments: call.arguments, preview });
          const receipt = await this.#journalMethods.prepare({ requestId, callId: call.id, toolName: call.name, riskTier: tool.risk_tier, sideEffect: tool.side_effect, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest });
          const dispatchOwner = nativeDispatchOwner;
          activeJournalOperation = {
            id: receipt.operation_id,
            dispatched: false,
            reconcile: RECONCILIATION_REQUIRED_EFFECTS.has(tool.side_effect),
            operationDigest: binding.operationDigest,
            argumentsDigest: binding.argumentsDigest,
            previewDigest: binding.previewDigest,
            dispatchOwner,
            // This handoff record is controller-internal and is never sent to
            // a Node provider.  The native bridge is absent in this slice, so
            // it remains an unaccepted, pre-dispatch description.
            nativeSupervisorHandoff: dispatchOwner === NATIVE_SUPERVISOR_DISPATCH_OWNER ? {
              version: NATIVE_SUPERVISOR_HANDOFF_VERSION,
              journal_dispatch_owner: NATIVE_SUPERVISOR_DISPATCH_OWNER,
              operation_id: receipt.operation_id,
              request_ref: binding.requestRef,
              call_ref: binding.callRef,
              tool: tool.name,
              risk: tool.risk_tier,
              side_effect: tool.side_effect,
              args_digest: binding.argumentsDigest,
              preview_digest: binding.previewDigest,
              operation_digest: binding.operationDigest,
              authorization_kind: null,
              authorized_sequence: null,
              authorized_receipt_digest: null,
              authorized_event_digest: null,
              accepted: false,
            } : null,
          };
        }
        const requiresConfirmation = !previewAccessDenied && (typeof tool.confirmationRequired === 'function' ? await tool.confirmationRequired(call, { preview }) : Boolean(tool.requires_confirmation));
        // Nobody watches a delegated job, so a confirmation would only ever
        // expire; refusing outright keeps that path from existing at all.
        if (scope && requiresConfirmation) throw Object.assign(new Error('confirmation_unavailable'), { code: 'confirmation_unavailable' });
        if (requiresConfirmation) {
          session.state = 'WAITING_CONFIRMATION'; const confirmationId = opaque('cnf');
          this.active.confirmationId = confirmationId; emit('tool.confirmation_required', { confirmation_id: confirmationId, call: publicToolCall(call), ...argumentsSummaryField(call, preview), preview: displayPreview(preview, call), risk_tier: tool.risk_tier, expires_in_ms: this.confirmationTimeoutMs });
          approved = await this.#awaitConfirmation(confirmationId, requestId, session.id, call.id);
          this.active.confirmationId = null;
          if (approved === CANCELLED_CONFIRMATION) { if (activeJournalOperation) { await this.#journalMethods.cancel(activeJournalOperation.id, 'request_cancelled'); activeJournalOperation = null; } throw Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
          if (approved === EXPIRED_CONFIRMATION) { approved = false; expiredConfirmationId = confirmationId; }
          if (approved) authorization = { kind: 'user_confirmation' };
        } else if (approved) { const autoAuthorization = tool.authorize ? await invokeWithTimeout(tool, tool.authorize, { ...call, preview }, controller.signal) : null; if (autoAuthorization && typeof autoAuthorization === 'object') authorization = autoAuthorization; }
        if (activeJournalOperation && !approved) { await this.#journalMethods.cancel(activeJournalOperation.id); activeJournalOperation = null; }
        else if (activeJournalOperation) {
          if (controller.signal.aborted) { await this.#journalMethods.cancel(activeJournalOperation.id, 'request_cancelled'); activeJournalOperation = null; throw Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
          const authorized = await this.#journalMethods.authorize(activeJournalOperation.id, authorization.kind);
          if (activeJournalOperation.dispatchOwner === NATIVE_SUPERVISOR_DISPATCH_OWNER) {
            // There is currently no native owner bridge or proof authority.
            // Close the authorized operation before any handoff, dispatch, or
            // provider call.  The failure is provably pre-dispatch, so the
            // ordinary journal's definitive-failure transition is valid.
            const handoff = activeJournalOperation.nativeSupervisorHandoff;
            if (handoff) {
              const readbackValid = exactAuthorizationReadback(authorized, activeJournalOperation.id);
              handoff.authorization_kind = readbackValid ? authorization.kind : null;
              handoff.authorized_sequence = readbackValid ? 1 : null;
              handoff.authorized_receipt_digest = readbackValid ? authorized.receipt_hash : null;
              // The JS journal has no separately verified native event proof.
              // Do not duplicate its receipt hash into that field: until a
              // future native verifier supplies both proofs, this remains an
              // unaccepted description and never crosses a bridge.
              handoff.authorized_event_digest = null;
            }
            try { await this.#journalMethods.failDefinitive(activeJournalOperation.id, 'pre_dispatch_failure'); }
            finally { activeJournalOperation = null; }
            throw Object.assign(new Error('native_supervisor_unavailable'), { code: 'native_supervisor_unavailable' });
          }
          await this.#journalMethods.dispatch(activeJournalOperation.id); activeJournalOperation.dispatched = true;
        }
        let result;
        if (expiredConfirmationId !== null) {
          // Nobody answered: not an approval, and not the user saying no.
          // The model is told exactly that, so it can ask again rather than
          // report a refusal the user never gave.
          const seconds = Math.round(this.confirmationTimeoutMs / 1000);
          emit('tool.failed', { call: publicToolCall(call), code: 'confirmation_expired', confirmation_id: expiredConfirmationId, message: `The confirmation expired after ${seconds} seconds without an answer, so the action was not run.` });
          session.state = 'TOOL_RUNNING';
          result = makeToolResult({ id: call.id, name: call.name, status: 'cancelled', text: `Not run: the user did not answer the confirmation within ${seconds} seconds, so it expired. The user neither approved nor denied it. Ask whether they still want this done.` });
        } else {
          session.state = 'TOOL_RUNNING'; emit('tool.started', { call: publicToolCall(call), approved, authorization: authorization.kind });
          if (!approved) result = makeToolResult({ id: call.id, name: call.name, status: 'denied', text: 'User denied this action.' });
        }
        if (!result) {
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
        // Measured before this call's two messages are appended, so the cap
        // already accounts for the assistant call message that precedes it.
        const resultCapBytes = toolResultByteCap({ history: session.history, pending: [...(session.memory?.note ? [noteMessage(session.memory.note)] : []), { role: 'assistant', content: callText }], tools, bytesPerToken: session.bytes_per_token ?? undefined, contextTokens: this.contextTokens, maxOutputTokens: this.maxOutputTokens });
        // Provider results are bound to digests of their exact text, so only
        // an unattested result may be cut before validation; the projected,
        // model-visible copy of every result is capped below.
        const attestedResult = BROWSER_TOOL_NAMES.has(call.name) || COPILOT_TOOL_NAMES.has(call.name) || isGraphReadTool(call.name) || activeJournalOperation?.reconcile === true;
        try { result = validateToolResult(attestedResult ? result : boundRawToolResult(result, resultCapBytes)); } catch { throw Object.assign(new Error('invalid_tool_result'), { code: 'invalid_tool_result' }); }
        if (result.id !== call.id || result.name !== call.name) throw Object.assign(new Error('tool_result_mismatch'), { code: 'tool_result_mismatch' });
        transferGraphAttestation(hostResult, result); if (BROWSER_TOOL_NAMES.has(call.name)) transferBrowserAttestation(hostResult, result);
        transferGraphReadAttestation(hostResult, result);
        if (COPILOT_TOOL_NAMES.has(call.name)) transferCopilotAttestation(hostResult, result);
        let strictModelResult = false; let controllerVerified = false; let modelBinding = null;
        if (activeJournalOperation) {
          strictModelResult = activeJournalOperation.reconcile === true;
          modelBinding = activeJournalOperation;
          if (result.status === 'ok') {
            if (activeJournalOperation.reconcile && !providerAttestationMatches(result, activeJournalOperation, call)) {
              // A provider-shaped payload is not an acknowledgement.  Install
              // only the durable dispatch tombstone before entering recovery;
              // otherwise a crash could persist a false provider acknowledgement.
              await this.#journalMethods.beginReconciliation(activeJournalOperation.id);
              const responseDigest = digestEvidence({ status: result.status, content: result.content.map(item => ({ type: item.type, text_digest: digestEvidence(item.text) })) });
              result = makeToolResult({ id: call.id, name: call.name, status: 'failed', text: JSON.stringify({ code: 'action_completion_unverified', operation_id: activeJournalOperation.id, state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified', evidence: { operation_digest: activeJournalOperation.operationDigest, preview_digest: activeJournalOperation.previewDigest, resource_digest: null, response_digest: responseDigest, arguments_digest: activeJournalOperation.argumentsDigest } }) });
            } else {
              // For Graph/browser/Copilot mutations, this transition occurs
              // only after the provider module's private, exact operation-bound
              // attestation has been validated above.  The acknowledged record
              // is therefore the durable provider-proof recovery boundary.
              await this.#journalMethods.acknowledge(activeJournalOperation.id);
              await this.#journalMethods.complete(activeJournalOperation.id);
              controllerVerified = activeJournalOperation.reconcile === true;
            }
          }
          else await this.#journalMethods.markUnknown(activeJournalOperation.id);
          activeJournalOperation = null;
        }
        let modelResult = BROWSER_TOOL_NAMES.has(call.name) ? projectBrowserResult(result, { controllerVerified, reconciliationRequired: strictModelResult }) : strictModelResult && COPILOT_TOOL_NAMES.has(call.name) ? modelVisibleCopilotResult(result, controllerVerified) : strictModelResult ? modelVisibleReconciliationResult(result, controllerVerified, modelBinding) : isGraphReadTool(call.name) ? modelVisibleGraphReadResult(result, call) : modelVisibleToolResult(result);
        modelResult = capModelResult(modelResult, resultCapBytes);
        emit('tool.completed', { result: modelResult });
        this._appendHistory(session, { role: 'assistant', content: callText });
        this._appendHistory(session, { role: 'tool', name: call.name, tool_call_id: call.id, content: toolResultText(modelResult) });
        session.state = 'CONTINUING_MODEL'; text = '';
      }
    } catch (caught) {
      let error = caught;
      if (activeJournalOperation) {
        try { if (activeJournalOperation.dispatched) await this.#journalMethods.markUnknown(activeJournalOperation.id); else await this.#journalMethods.failDefinitive(activeJournalOperation.id); }
        catch (journalError) { error = journalError; }
        activeJournalOperation = null;
      }
      const cancelled = error?.code === 'cancelled' || controller.signal.aborted;
      // `user_message_kept` tells the UI whether the model will see this
      // message next turn, so it can mark the bubble as not sent.
      const userMessageKept = this._settleFailedTurn(session, userMessage, text.trim() ? text : '');
      session.state = cancelled ? 'CANCELLED' : 'FAILED';
      emit(cancelled ? 'request.cancelled' : 'request.failed', { code: cancelled ? 'cancelled' : (error.code ?? 'request_failed'), message: cancelled ? 'Request cancelled.' : error instanceof ContextBudgetError ? error.message : 'Request failed.', user_message_kept: userMessageKept });
      return { requestId, sessionId: session.id, state: session.state, error: cancelled ? 'cancelled' : (error.code ?? 'request_failed') };
    } finally { signal?.removeEventListener('abort', relayAbort); if (this.active?.requestId === requestId) this.active = null; }
  }
}
