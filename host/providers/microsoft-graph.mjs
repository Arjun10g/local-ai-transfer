import { makeToolResult } from '../agent/tool-envelope.mjs';
import { ProviderToolError, exactObject, boundedArray, boundedBoolean, boundedString, checkAborted, digest, failureResult, jsonResponse, normalizeText, own, providerError, result } from './provider-common.mjs';
import { MicrosoftDeviceCodeCredential, MicrosoftGraphHttpsTransport, GRAPH_ORIGIN } from './microsoft-graph-auth.mjs';
import { graphReadDefinitions, graphReadScopes, isGraphReadEgressTool, isGraphReadTool, MicrosoftGraphReadBoundary, validateGraphReadArguments } from './microsoft-graph-reads.mjs';

const API = '/v1.0';
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/u;
const CAP_MAIL = 'microsoft.graph.mail';
const CAP_TEAMS = 'microsoft.graph.teams';
const REQUEST_TIMEOUT_MS = 10000;
const MAX_TOKEN_BYTES = 4096;
const MAX_PROPOSALS = 128;
const MAX_WRITE_RECORDS = 256;
const MAX_RECONCILIATION_ITEMS = 50;
const ACCOUNT_OBJECT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
// Graph message/chat creation and sent timestamps are documented as UTC.
// Keep the proof parser narrower than generic RFC3339: accepting a local
// offset would make independently produced timestamps comparable after a
// conversion the provider did not attest to. Fractional seconds remain
// bounded to the OData-compatible 1–12 digits.
const GRAPH_DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,12}))?Z$/u;
const OPERATION_ID = /^act_[a-f0-9]{32}$/u;
const DIGEST = /^[a-f0-9]{64}$/u;
const LAE_OPERATION_HEADER = 'x-lae-operation';
const GRAPH_TOOL_SCOPES = Object.freeze({
  ...graphReadScopes,
  'mail.create_draft': Object.freeze(['Mail.ReadWrite']),
  'mail.send_draft': Object.freeze(['Mail.Read', 'Mail.Send']),
  'mail.mark_read': Object.freeze(['Mail.ReadWrite']),
  'teams.send_message': Object.freeze(['ChatMessage.Send'])
});
const schema = (properties, required = []) => ({ type: 'object', additionalProperties: false, required, properties });
const string = (max, extra = {}) => ({ type: 'string', maxLength: max, ...extra });
const identifier = string(512, { minLength: 1 });
const INPUT_SCHEMAS = Object.freeze({
  'mail.create_draft': schema({ to: { type: 'array', minItems: 1, maxItems: 20, items: string(320) }, cc: { type: 'array', maxItems: 20, items: string(320) }, subject: string(998), body: string(65536) }, ['to', 'subject', 'body']),
  'mail.send_draft': schema({ draft_id: identifier }, ['draft_id']),
  'mail.mark_read': schema({ message_id: identifier, is_read: { type: 'boolean' } }, ['message_id', 'is_read']),
  'teams.send_message': schema({ chat_id: identifier, body: string(16384, { minLength: 1 }) }, ['chat_id', 'body'])
});
const descriptions = Object.freeze({
  'mail.create_draft': 'Create one Outlook draft after confirmation.', 'mail.send_draft': 'Send one existing Outlook draft after high-impact confirmation.', 'mail.mark_read': 'Change the read state of one Outlook message.',
  'teams.send_message': 'Send one message to an existing Teams chat after high-impact confirmation.'
});
const defs = (name, risk, effect, egress, output = 65536) => ({ name, version: '0.1.0', description: descriptions[name], risk_tier: risk, side_effect: effect, network: true, data_egress: egress, requires_confirmation: risk !== 'T1', timeout_ms: 10000, output_limit: output, parameters: INPUT_SCHEMAS[name], input_schema: INPUT_SCHEMAS[name] });

export const graphDefinitions = Object.freeze({
  'mail.list_messages': graphReadDefinitions['mail.list_messages'],
  'mail.search_messages': graphReadDefinitions['mail.search_messages'],
  'mail.read_message': graphReadDefinitions['mail.read_message'],
  'mail.create_draft': defs('mail.create_draft', 'T2', 'create_draft', 'message_content', 8192),
  'mail.send_draft': defs('mail.send_draft', 'T3', 'send_mail', 'message_content', 8192),
  'mail.mark_read': defs('mail.mark_read', 'T2', 'modify_mail', 'none', 8192),
  'teams.list_chats': graphReadDefinitions['teams.list_chats'],
  'teams.list_messages': graphReadDefinitions['teams.list_messages'],
  'teams.read_message': graphReadDefinitions['teams.read_message'],
  'teams.list_channels': graphReadDefinitions['teams.list_channels'],
  'teams.list_channel_messages': graphReadDefinitions['teams.list_channel_messages'],
  'teams.read_channel_message': graphReadDefinitions['teams.read_channel_message'],
  'teams.send_message': defs('teams.send_message', 'T3', 'send_teams', 'message_content', 8192)
});

const argument = (name, input) => {
  if (isGraphReadTool(name)) return validateGraphReadArguments(name, input);
  let args;
  const bodyArgument = (value, field, max, min = 0) => { if (typeof value !== 'string' || value.length < min || value.length > max || Buffer.byteLength(value, 'utf8') > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) throw new ProviderToolError('invalid_tool_arguments', `${field} must be a bounded body`); };
  if (name === 'mail.create_draft') {
    args = exactObject(input, ['to', 'cc', 'subject', 'body'], ['to', 'subject', 'body']);
    const recipient = (value, field) => { boundedString(value, field, { min: 3, max: 320, identifier: true }); if (!EMAIL.test(value)) throw new ProviderToolError('invalid_tool_arguments', `${field} must be an email address`); };
    boundedArray(args.to, 'to', { min: 1, max: 20, item: recipient });
    if (own(args, 'cc')) boundedArray(args.cc, 'cc', { max: 20, item: recipient });
    boundedString(args.subject, 'subject', { max: 998 }); bodyArgument(args.body, 'body', 65536);
  } else if (name === 'mail.send_draft') {
    args = exactObject(input, ['draft_id'], ['draft_id']); boundedString(args.draft_id, 'draft_id', { min: 1, max: 512, identifier: true });
  } else if (name === 'mail.mark_read') {
    args = exactObject(input, ['message_id', 'is_read'], ['message_id', 'is_read']); boundedString(args.message_id, 'message_id', { min: 1, max: 512, identifier: true }); boundedBoolean(args.is_read, 'is_read');
  } else if (name === 'teams.send_message') {
    args = exactObject(input, ['chat_id', 'body'], ['chat_id', 'body']); boundedString(args.chat_id, 'chat_id', { min: 1, max: 512, identifier: true }); bodyArgument(args.body, 'body', 16384, 1);
  } else throw new ProviderToolError('invalid_tool_arguments', `unknown Graph tool: ${name}`);
  return structuredClone(args);
};

const address = value => ({ emailAddress: { address: value } });
const previewText = (value, maxBytes) => {
  const bytes = Buffer.from(value, 'utf8'); const truncated = bytes.byteLength > maxBytes; let text = new TextDecoder().decode(bytes.subarray(0, maxBytes)); while (text.endsWith('\uFFFD')) text = text.slice(0, -1); return { text, truncated };
};
const graphDateTimeMs = value => {
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 64) return NaN;
  const match = GRAPH_DATE_TIME.exec(value); if (!match) return NaN;
  const year = Number(match[1]); const month = Number(match[2]); const day = Number(match[3]); const hour = Number(match[4]); const minute = Number(match[5]); const second = Number(match[6]);
  if (year < 1 || year > 9999 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return NaN;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0); const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]; if (day < 1 || day > monthDays[month - 1]) return NaN;
  const fraction = match[7] ?? ''; const millis = fraction ? Number(fraction.slice(0, 3).padEnd(3, '0')) : 0; const subMilliseconds = fraction.length > 3 ? Number(`0.${fraction.slice(3, 12)}`) : 0; const wall = new Date(0); wall.setUTCFullYear(year, month - 1, day); wall.setUTCHours(hour, minute, second, millis);
  if (wall.getUTCFullYear() !== year || wall.getUTCMonth() !== month - 1 || wall.getUTCDate() !== day || wall.getUTCHours() !== hour || wall.getUTCMinutes() !== minute || wall.getUTCSeconds() !== second || wall.getUTCMilliseconds() !== millis) return NaN;
  // JavaScript dates have millisecond resolution. Preserve the first nine
  // fractional digits as a bounded sub-millisecond value and deterministically
  // floor any remaining OData precision rather than invoking permissive parsing.
  const timestamp = wall.getTime() + subMilliseconds; return Number.isFinite(timestamp) ? timestamp : NaN;
};
const projectionDraft = value => {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string' || value.id.length < 1) return null;
  const boundedField = (field, max, required = false) => { if (field === undefined && !required) return ''; if (typeof field !== 'string' || field.length < (required ? 1 : 0) || field.length > max || /[\u0000-\u001f\u007f]/u.test(field)) return null; return field; };
  const boundedBody = field => typeof field === 'string' && field.length <= 65536 && Buffer.byteLength(field, 'utf8') <= 65536 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(field) ? field : null;
  const id = boundedField(value.id, 512, true); const subject = boundedField(value.subject, 998) ?? (value.subject === undefined ? '' : null); const rawBody = boundedBody(value.body?.content); const contentType = boundedField(value.body?.contentType, 32) ?? (value.body?.contentType === undefined ? '' : null);
  if (!id || subject === null || rawBody === null || contentType === null) return null;
  const body = normalizeText(rawBody, 65536); if (body.truncated) return null;
  const recipients = [];
  for (const [field, list] of [['to', value.toRecipients], ['cc', value.ccRecipients]]) {
    if (list === undefined) continue;
    if (!Array.isArray(list) || list.length > 20) return null;
    for (const item of list) {
      if (!item || typeof item !== 'object' || Array.isArray(item) || !item.emailAddress || typeof item.emailAddress !== 'object' || Array.isArray(item.emailAddress)) return null;
      const email = item.emailAddress; if (Object.keys(email).some(key => !['name', 'address'].includes(key))) return null;
      const address = boundedField(email.address, 320, true); const name = boundedField(email.name, 256) ?? (email.name === undefined ? '' : null); if (!address || name === null || !EMAIL.test(address)) return null;
      recipients.push({ field, address, name });
    }
  }
  const etag = value['@odata.etag'] === undefined ? null : boundedField(value['@odata.etag'], 512); const changeKey = value.changeKey === undefined ? null : boundedField(value.changeKey, 512); if (etag === null && value['@odata.etag'] !== undefined || changeKey === null && value.changeKey !== undefined) return null;
  const operationHeaders = Array.isArray(value.internetMessageHeaders) ? value.internetMessageHeaders : [];
  const operationHeader = operationHeaders.find(header => header && typeof header === 'object' && !Array.isArray(header) && typeof header.name === 'string' && header.name.toLowerCase() === LAE_OPERATION_HEADER);
  const operationMarkerValue = operationHeader?.value;
  const operation_marker = operationMarkerValue === undefined ? null : typeof operationMarkerValue === 'string' && /^act_[a-f0-9]{32}:[a-f0-9]{64}$/u.test(operationMarkerValue) ? operationMarkerValue : null;
  if (operationMarkerValue !== undefined && operation_marker === null) return null;
  const bodyPreview = previewText(body.text, 512);
  return { id, subject, raw_body: rawBody, content_type: contentType, body: body.text, body_preview: bodyPreview.text, body_truncated: bodyPreview.truncated, recipients, etag, change_key: changeKey, operation_marker };
};
const draftContent = draft => ({ subject: draft.subject, raw_body: draft.raw_body, content_type: draft.content_type, recipients: draft.recipients });
const draftContentDigest = draft => digest(draftContent(draft));
const requestedDraftContentDigest = args => digest({ subject: args.subject, raw_body: normalizeText(args.body, 65536).text, content_type: 'Text', recipients: [...args.to.map(address => ({ field: 'to', address, name: '' })), ...(args.cc ?? []).map(address => ({ field: 'cc', address, name: '' }))] });
const journalBinding = call => {
  const binding = call?.internal?.journal_binding;
  if (binding === undefined) return null;
  if (!binding || typeof binding !== 'object' || Array.isArray(binding) || Object.keys(binding).sort().join(',') !== 'arguments_digest,operation_digest,operation_id,preview_digest' || !OPERATION_ID.test(binding.operation_id) || !DIGEST.test(binding.arguments_digest) || !DIGEST.test(binding.operation_digest) || !DIGEST.test(binding.preview_digest)) throw new ProviderToolError('provider_invalid_request', 'invalid internal journal binding');
  return Object.freeze({ operation_id: binding.operation_id, operation_digest: binding.operation_digest, arguments_digest: binding.arguments_digest, preview_digest: binding.preview_digest });
};
// The issuer is intentionally module-private. The controller can only read or
// transfer an attestation already issued by this adapter; generic tools can
// copy the JSON envelope but cannot manufacture this identity-bound marker.
const graphAttestations = new WeakMap();
const issueGraphAttestation = (result, attestation) => { graphAttestations.set(result, Object.freeze({ ...attestation })); return result; };
export const readGraphAttestation = result => result && typeof result === 'object' ? graphAttestations.get(result) ?? null : null;
export const transferGraphAttestation = (source, target) => { const attestation = readGraphAttestation(source); if (attestation && target && typeof target === 'object') graphAttestations.set(target, attestation); return target; };
const operationMarker = binding => binding ? `${binding.operation_id}:${binding.operation_digest}` : null;
const verifiedPayload = ({ response, resource, reconciliation = null, completed = true }) => ({ provider: 'microsoft_graph', state: completed ? 'completed' : 'reconciling', provider_completion: completed ? 'verified' : 'unverified', completion: completed ? 'provider_verified' : 'manual_required', http_status: response?.status ?? null, resource_id: typeof resource?.id === 'string' ? resource.id.slice(0, 512) : null, accepted: true, completed, reconciliation, timestamp: new Date().toISOString() });
const reconcilingPayload = ({ response = null, reconciliation, resource = null }) => ({ provider: 'microsoft_graph', state: 'reconciling', provider_completion: 'unverified', completion: 'manual_required', accepted: true, completed: false, code: 'provider_action_reconciling', http_status: response?.status ?? null, resource_id: typeof resource?.id === 'string' ? resource.id.slice(0, 512) : null, reconciliation });
const verifiedResult = ({ call, binding, response, resource, reconciliation }) => {
  const output = result(call, 'ok', verifiedPayload({ response, resource, reconciliation }));
  if (binding) issueGraphAttestation(output, { provider: 'microsoft_graph', call_id: call.id, tool_name: call.name, operation_id: binding.operation_id, operation_digest: binding.operation_digest, arguments_digest: binding.arguments_digest, preview_digest: binding.preview_digest, proof: reconciliation });
  return output;
};
const validResource = (value, expectedId = null) => value && typeof value === 'object' && !Array.isArray(value) && typeof value.id === 'string' && value.id.length >= 1 && value.id.length <= 512 && (!expectedId || value.id === expectedId);
const messageState = (body, expectedId) => body && typeof body === 'object' && !Array.isArray(body) && body.id === expectedId && typeof body.isRead === 'boolean' ? { id: body.id, is_read: body.isRead } : null;
const teamsResource = (body, chatId, content) => validResource(body) && (!body.chatId || body.chatId === chatId) && body.body && typeof body.body === 'object' && body.body.content === content ? { id: body.id, chat_id: chatId } : null;
const isNotFound = error => error?.httpStatus === 404;
const putBounded = (map, key, value, max) => { if (map.size >= max && !map.has(key)) map.delete(map.keys().next().value); map.set(key, value); };
const proofCollection = (body, max = MAX_RECONCILIATION_ITEMS) => {
  if (!body || typeof body !== 'object' || Array.isArray(body) || !Array.isArray(body.value)) return { values: null, truncated: false };
  const truncated = body.value.length > max || Object.hasOwn(body, '@odata.nextLink');
  return { values: body.value.slice(0, max), truncated };
};
const uniqueProofMap = (values, mapItem) => {
  if (!Array.isArray(values)) return null;
  const ids = new Set(); const mapped = [];
  for (const value of values) {
    const item = mapItem(value);
    if (!item || typeof item.id !== 'string' || ids.has(item.id)) return null;
    ids.add(item.id); mapped.push(item);
  }
  return mapped;
};
const validGraphPath = path => typeof path === 'string' && path.length <= 2048 && /^\/v1\.0\/(?:me(?:\/mailFolders\/[^/]+\/messages|\/messages(?:\/[^/]+(?:\/send)?)?|\/chats)?|chats\/[^/]+\/messages(?:\/[^/]+)?|teams\/[^/]+\/channels(?:\/[^/]+\/messages(?:\/[^/]+)?)?)$/u.test(path) && !path.includes('..') && !/[\u0000-\u001f\u007f]/u.test(path);
const validGraphMethodPath = (method, path) => {
  if (!['GET', 'POST', 'PATCH'].includes(method) || !validGraphPath(path)) return false;
  if (method === 'GET') return true;
  if (method === 'PATCH') return /^\/v1\.0\/me\/messages\/[^/]+$/u.test(path);
  return /^\/v1\.0\/me\/messages$|^\/v1\.0\/me\/messages\/[^/]+\/send$|^\/v1\.0\/chats\/[^/]+\/messages$/u.test(path);
};
const requiredGraphScope = (method, path) => path === '/v1.0/me' ? 'User.Read' : path.includes('/channels/') && path.includes('/messages') ? 'ChannelMessage.Read.All' : /^\/v1\.0\/teams\/[^/]+\/channels$/u.test(path) ? 'Channel.ReadBasic.All' : path === '/v1.0/me/chats' || path.startsWith('/v1.0/chats/') && method === 'GET' ? 'Chat.Read' : path.startsWith('/v1.0/chats/') ? 'ChatMessage.Send' : method === 'PATCH' ? 'Mail.ReadWrite' : method === 'POST' && path.endsWith('/send') ? 'Mail.Send' : method === 'POST' ? 'Mail.ReadWrite' : 'Mail.Read';
const scopeAllows = (scopes, required) => scopes.includes(required) || required === 'Mail.Read' && scopes.includes('Mail.ReadWrite') || required === 'Chat.Read' && scopes.includes('Chat.ReadWrite') || required === 'ChatMessage.Send' && scopes.includes('Chat.ReadWrite');

export class MicrosoftGraphProvider {
  constructor({ enabled = false, credentialSource, transport, authTransport, tenant, clientId, scopes, onUserCode, sleep, origin = GRAPH_ORIGIN, permissionProfile = 'always_ask', grantStore, accountFingerprint = 'unknown', scope = 'account', now = () => Date.now(), requestTimeoutMs = REQUEST_TIMEOUT_MS, testOnly = false } = {}) {
    if (!['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access'].includes(permissionProfile)) throw new TypeError('invalid permission profile');
    if (credentialSource && permissionProfile === 'full_access' && testOnly !== true) throw new TypeError('external credentials cannot use full_access');
    if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 100 || requestTimeoutMs > 120000) throw new TypeError('invalid Graph request timeout');
    let parsed; try { parsed = new URL(origin); } catch { throw new TypeError('invalid Graph origin'); } if (parsed.origin !== 'https://graph.microsoft.com' || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) throw new TypeError('invalid Graph origin');
    if (credentialSource && (tenant !== undefined || clientId !== undefined || scopes !== undefined)) throw new TypeError('credential source and device-code settings are mutually exclusive');
    const graphTransport = transport ?? (tenant !== undefined || clientId !== undefined || scopes !== undefined ? new MicrosoftGraphHttpsTransport({ requestTimeoutMs, sleep }) : undefined);
    this.enabled = enabled === true; Object.defineProperty(this, 'testOnly', { value: testOnly === true, enumerable: false }); this.credentialSource = credentialSource ?? (tenant !== undefined || clientId !== undefined || scopes !== undefined ? new MicrosoftDeviceCodeCredential({ tenant, clientId, scopes, transport: authTransport ?? graphTransport, now, sleep, requestTimeoutMs, onUserCode }) : undefined); this.transport = graphTransport; this.origin = parsed.origin; this.permissionProfile = permissionProfile; this.grantStore = grantStore; this.accountFingerprint = accountFingerprint; this.scope = scope; this.now = now; this.requestTimeoutMs = requestTimeoutMs; this.proposals = new Map(); this.writeLedger = new Map(); this.readBoundary = new MicrosoftGraphReadBoundary({ request: request => this.request(request), accountFingerprint: () => this.getAccountFingerprint() });
  }
  state() { if (!this.enabled) return 'disabled'; if (!this.credentialSource || !this.transport) return 'unconfigured'; return 'ready'; }
  configuredToolNames() {
    if (this.state() !== 'ready') return Object.freeze([]);
    if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) {
      const scopes = new Set(this.credentialSource.scopes);
      const satisfies = required => scopes.has(required) || required === 'Mail.Read' && scopes.has('Mail.ReadWrite') || required === 'Chat.Read' && scopes.has('Chat.ReadWrite') || required === 'ChatMessage.Send' && scopes.has('Chat.ReadWrite');
      return Object.freeze(Object.entries(GRAPH_TOOL_SCOPES).filter(([, required]) => required.every(satisfies)).map(([name]) => name));
    }
    // An injected credential has no inspectable scope contract. Only explicit
    // synthetic fixtures may advertise that otherwise-unknown capability.
    return Object.freeze(this.testOnly ? Object.keys(graphDefinitions) : []);
  }
  async status(signal) { const state = this.state(); if (state !== 'ready') return state; try { await this.token(signal); if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) { const meResponse = await this.request({ method: 'GET', path: '/v1.0/me', query: { '$select': 'id' }, signal }); const me = meResponse.body; if (typeof me.id !== 'string' || !ACCOUNT_OBJECT_ID.test(me.id)) throw new ProviderToolError('provider_invalid_response'); const fingerprint = digest({ tenant: this.credentialSource.tenant, id: me.id }); if (this.accountFingerprint !== 'unknown' && this.accountFingerprint !== fingerprint) { this.clearAuth(); throw new ProviderToolError('provider_unauthorized'); } this.authenticatedAccountFingerprint = fingerprint; this.accountFingerprint = fingerprint; } return 'ready'; } catch (error) { if (error.code === 'provider_unauthorized') this.clearAuth(); return error.code === 'provider_offline' ? 'offline' : error.code === 'provider_unauthorized' ? 'unauthorized' : 'failed'; } }
  getAccountFingerprint() { return this.authenticatedAccountFingerprint ?? this.accountFingerprint; }
  authStatus() { if (!this.enabled) return { state: 'disabled', prompt: null, accountFingerprint: null, accountVerified: false }; if (!this.credentialSource || !this.transport) return { state: 'unconfigured', prompt: null, accountFingerprint: null, accountVerified: false }; if (!(this.credentialSource instanceof MicrosoftDeviceCodeCredential)) return { state: 'external', prompt: null, accountFingerprint: null, accountVerified: false }; const status = this.credentialSource.authStatus(); const accountFingerprint = this.authenticatedAccountFingerprint ?? null; const accountVerified = typeof accountFingerprint === 'string' && DIGEST.test(accountFingerprint) && this.accountFingerprint === accountFingerprint; return { ...status, state: status.state === 'authenticated' && !this.authenticatedAccountFingerprint ? 'checking_account' : status.state, accountFingerprint, accountVerified }; }
  async startAuth(signal) { if (!this.enabled || !(this.credentialSource instanceof MicrosoftDeviceCodeCredential)) throw new ProviderToolError('provider_unconfigured'); await this.credentialSource.start(signal); if (await this.status(signal) !== 'ready') { this.clearAuth(); throw new ProviderToolError('provider_unauthorized'); } return this.getAccountFingerprint(); }
  authConfigured() { return this.enabled && this.credentialSource instanceof MicrosoftDeviceCodeCredential; }
  cancelAuth() { if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) this.credentialSource.cancel(); this.authenticatedAccountFingerprint = null; this.readBoundary.clear(); for (const capability of [CAP_MAIL, CAP_TEAMS]) this.grantStore?.revoke?.(capability); }
  clearAuth() { if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) this.credentialSource.clear(); this.authenticatedAccountFingerprint = null; this.readBoundary.clear(); for (const capability of [CAP_MAIL, CAP_TEAMS]) this.grantStore?.revoke?.(capability); }
  capability(name) { return name.startsWith('mail.') ? CAP_MAIL : CAP_TEAMS; }
  isWrite(name) { return !isGraphReadTool(name); }
  grantValid(capability) { const grant = this.grantStore?.get(capability); if (this.credentialSource instanceof MicrosoftDeviceCodeCredential && (!this.authenticatedAccountFingerprint || this.accountFingerprint === 'unknown' || this.accountFingerprint !== this.authenticatedAccountFingerprint)) return false; return this.permissionProfile === 'full_access' && grant?.profile === 'full_access' && grant.provider === 'microsoft_graph' && grant.account_fingerprint === this.accountFingerprint && grant.scope === this.scope; }
  confirmationRequired(name) {
    const important = name === 'teams.send_message' || name === 'mail.send_draft';
    if (this.permissionProfile === 'always_ask') return true;
    if (this.permissionProfile === 'ask_before_writes') return this.isWrite(name) || isGraphReadEgressTool(name);
    if (this.permissionProfile === 'review_important_actions') return important || isGraphReadEgressTool(name);
    if (this.permissionProfile === 'full_access') return important || !this.grantValid(this.capability(name));
    return true;
  }
  validate(name, input) { return argument(name, input); }
  async token(signal) {
    checkAborted(signal); if (!this.enabled) throw new ProviderToolError('provider_disabled'); if (!this.credentialSource || !this.transport) throw new ProviderToolError('provider_unconfigured');
    let value; try { value = typeof this.credentialSource === 'function' ? await this.credentialSource(signal) : await this.credentialSource.getAccessToken?.(signal); } catch (error) { if (['provider_cancelled', 'provider_offline', 'provider_timeout'].includes(error?.code)) throw new ProviderToolError(error.code); this.clearAuth(); throw new ProviderToolError('provider_unauthorized'); }
    if (typeof value !== 'string' || !value || Buffer.byteLength(value, 'utf8') > MAX_TOKEN_BYTES || /[\u0000-\u001f\u007f]/u.test(value)) throw new ProviderToolError('provider_unauthorized'); return value;
  }
  async request({ method, path, query, headers = {}, body, signal, onDispatch }) {
    if (!validGraphMethodPath(method, path)) throw new ProviderToolError('provider_destination_rejected');
    if (this.credentialSource instanceof MicrosoftDeviceCodeCredential && !scopeAllows(this.credentialSource.scopes, requiredGraphScope(method, path))) throw new ProviderToolError('provider_unauthorized', 'configured delegated scopes do not cover this operation');
    const bearer = await this.token(signal); checkAborted(signal); const deadline = new AbortController(); let rejectAbort; const relay = () => { deadline.abort(); rejectAbort?.(Object.assign(new Error('provider cancelled'), { code: 'provider_cancelled' })); }; signal?.addEventListener('abort', relay, { once: true }); let timer;
    let response; try {
      const request = { origin: this.origin, method, path, query: query ?? {}, headers: { ...headers, authorization: `Bearer ${bearer}`, accept: 'application/json' }, body, signal: deadline.signal };
      if (onDispatch !== undefined && typeof onDispatch !== 'function') throw new ProviderToolError('provider_invalid_request');
      checkAborted(signal);
      const pending = typeof this.transport === 'function' ? this.transport({ ...request, onDispatch }) : this.transport.request({ ...request, onDispatch }); let rejectTimeout; const timeout = new Promise((_, reject) => { rejectTimeout = reject; }); const cancelled = new Promise((_, reject) => { rejectAbort = reject; }); timer = setTimeout(() => { deadline.abort(); rejectTimeout(Object.assign(new Error('provider timeout'), { code: 'provider_timeout' })); }, this.requestTimeoutMs); response = await Promise.race([pending, timeout, cancelled]);
    } catch (error) {
      if (signal?.aborted) throw new ProviderToolError('provider_cancelled');
      if (deadline.signal.aborted) throw new ProviderToolError('provider_timeout');
      if (error?.code === 'provider_timeout' || error?.code === 'ETIMEDOUT') throw new ProviderToolError('provider_timeout');
      if (error?.code === 'ENETUNREACH' || error?.code === 'offline') throw new ProviderToolError('provider_offline');
      throw new ProviderToolError('provider_failed');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', relay); }
    if (!response || typeof response.status !== 'number') throw new ProviderToolError('provider_invalid_response');
    if (response.status < 200 || response.status >= 300) { const error = new ProviderToolError(providerError(response.status)); error.httpStatus = response.status; throw error; }
    return { status: response.status, headers: response.headers, body: jsonResponse(response) };
  }
  remember(call, args) { const capability = this.capability(call.name); const grant = this.grantStore?.get(capability); const revision = digest({ call: call.name, args, nonce: `${this.now()}:${call.id}` }); putBounded(this.proposals, call.id, { name: call.name, digest: digest(args), revision, grantGeneration: grant?.generation ?? null, prewrite_verified: false, post_attempted: false, reconciliation_allowed: false }, MAX_PROPOSALS); return revision; }
  assertProposal(call, args) { const saved = this.proposals.get(call.id); if (!saved || saved.name !== call.name || saved.digest !== digest(args)) throw new ProviderToolError('provider_permission_insufficient', 'write proposal no longer matches'); return saved; }
  async authorize(call) {
    const args = this.validate(call.name, call.arguments); const saved = this.assertProposal(call, args); const capability = this.capability(call.name);
    if (this.confirmationRequired(call.name)) return { kind: 'policy' };
    const grant = this.grantStore?.get(capability);
    if (this.permissionProfile === 'full_access') {
      if (!grant || grant.profile !== 'full_access' || grant.generation !== saved.grantGeneration || !this.grantValid(capability)) throw new ProviderToolError('provider_permission_insufficient', 'operator grant changed after preview');
      return { kind: 'operator_grant', generation: grant.generation };
    }
    return { kind: 'policy' };
  }
  beginOperation(capability, signal) {
    const controller = new AbortController(); const relay = () => controller.abort(); signal?.addEventListener('abort', relay, { once: true }); const unsubscribe = this.grantStore?.subscribe(capability, relay);
    return { signal: controller.signal, close: () => { signal?.removeEventListener('abort', relay); unsubscribe?.(); } };
  }
  async readMessageState(messageId, signal) {
    const response = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(messageId)}`, query: { '$select': 'id,isRead' }, signal });
    const state = messageState(response.body, messageId); if (!state) throw new ProviderToolError('provider_invalid_response'); return state;
  }
  async listDraftsForMarker(marker, expectedDigest, signal) {
    if (!marker) return { values: null, truncated: false };
    const response = await this.request({ method: 'GET', path: `${API}/me/mailFolders/drafts/messages`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$top': MAX_RECONCILIATION_ITEMS, '$select': 'id,subject,body,toRecipients,ccRecipients,internetMessageHeaders,changeKey' }, signal });
    const collection = proofCollection(response.body); if (!collection.values || collection.truncated) return { values: collection.values, truncated: collection.truncated };
    const mapped = uniqueProofMap(collection.values, value => {
      if (!Array.isArray(value?.internetMessageHeaders)) return null;
      const draft = projectionDraft(value); if (!draft) return null;
      return draft;
    }); if (!mapped) return { values: null, truncated: false };
    return { values: mapped.filter(value => value.operation_marker === marker && draftContentDigest(value) === expectedDigest), truncated: false };
  }
  async listSentForDigest(expectedDigest, existingIds, snapshotAt, sentMarker, signal) {
    const response = await this.request({ method: 'GET', path: `${API}/me/mailFolders/sentitems/messages`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$top': MAX_RECONCILIATION_ITEMS, '$orderby': 'sentDateTime desc', '$select': 'id,subject,body,toRecipients,ccRecipients,changeKey,sentDateTime,internetMessageHeaders' }, signal });
    const collection = proofCollection(response.body); if (!collection.values || collection.truncated) return { values: collection.values, truncated: collection.truncated };
    const upper = this.now() + 60000;
    const mapped = uniqueProofMap(collection.values, value => {
      if (!Array.isArray(value?.internetMessageHeaders) || typeof value?.sentDateTime !== 'string') return null;
      const item = projectionDraft(value); const sentAt = graphDateTimeMs(value.sentDateTime); if (!item || !Number.isFinite(sentAt)) return null;
      return { ...item, sent_at_ms: sentAt };
    }); if (!mapped) return { values: null, truncated: false };
    return { values: mapped.filter(item => typeof sentMarker === 'string' && item.operation_marker === sentMarker && !existingIds?.has(item.id) && draftContentDigest(item) === expectedDigest && Number.isFinite(snapshotAt) && item.sent_at_ms >= snapshotAt - 1000 && item.sent_at_ms <= upper), truncated: false };
  }
  async listChatMessagesForProof(chatId, signal) {
    const response = await this.request({ method: 'GET', path: `${API}/chats/${encodeURIComponent(chatId)}/messages`, query: { '$top': MAX_RECONCILIATION_ITEMS }, signal });
    const collection = proofCollection(response.body); if (!collection.values || collection.truncated) return { values: collection.values, truncated: collection.truncated };
    const mapped = uniqueProofMap(collection.values, value => {
      const createdMs = graphDateTimeMs(value?.createdDateTime);
      if (!validResource(value) || !value.body || typeof value.body !== 'object' || typeof value.body.content !== 'string' || value.body.contentType !== 'text' || !Number.isFinite(createdMs) || typeof value.from?.user?.id !== 'string' || !value.from.user.id.trim()) return null;
      return { id: value.id, content: value.body.content, content_type: 'text', created: value.createdDateTime, created_ms: createdMs, sender_id: value.from.user.id.slice(0, 512) };
    }); if (!mapped) return { values: null, truncated: false };
    return { values: mapped, truncated: false };
  }
  teamsProofMatches(items, args, saved) {
    if (!saved.prewrite_verified || !saved.post_attempted || !saved.reconciliation_allowed || typeof saved.team_sender_id !== 'string' || !Number.isFinite(saved.team_prewrite_at)) return [];
    const upper = this.now() + 60000;
    return items.filter(item => item.content_type === 'text' && item.content === args.body && item.sender_id === saved.team_sender_id && Number.isFinite(item.created_ms) && item.created_ms >= saved.team_prewrite_at - 1000 && item.created_ms <= upper);
  }
  async reconcileCreateDraft(call, args, binding, response = null, signal) {
    const collection = await this.listDraftsForMarker(operationMarker(binding), requestedDraftContentDigest(args), signal); const matches = collection.values ?? [];
    if (!collection.values || collection.truncated) return result(call, 'ok', reconcilingPayload({ response, reconciliation: collection.truncated ? 'draft_collection_truncated' : 'draft_collection_unavailable' }));
    if (matches.length === 1) return verifiedResult({ call, binding, response, resource: { id: matches[0].id }, reconciliation: 'unique_exact_draft' });
    return result(call, 'ok', reconcilingPayload({ response, reconciliation: matches.length > 1 ? 'multiple_exact_drafts' : 'draft_not_found' }));
  }
  async reconcileSendDraft(call, args, binding, response, saved, signal) {
    if (!saved.prewrite_verified || !saved.post_attempted || !saved.reconciliation_allowed) return result(call, 'ok', reconcilingPayload({ response, reconciliation: 'send_reconciliation_not_authorized' }));
    let draftPresent = true;
    try { await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}`, query: { '$select': 'id' }, signal }); }
    catch (error) { if (isNotFound(error)) draftPresent = false; else return result(call, 'ok', reconcilingPayload({ response, reconciliation: 'draft_state_unavailable' })); }
    if (draftPresent) return result(call, 'ok', reconcilingPayload({ response, reconciliation: 'draft_still_present' }));
    const collection = await this.listSentForDigest(saved.draftBinding, saved.preexistingSentIds, saved.sent_snapshot_at, saved.sent_marker, signal); if (!collection.values || collection.truncated) return result(call, 'ok', reconcilingPayload({ response, reconciliation: collection.truncated ? 'sent_collection_truncated' : 'sent_collection_unavailable' })); const sent = collection.values;
    if (sent.length === 1) return verifiedResult({ call, binding, response, resource: { id: sent[0].id }, reconciliation: 'unique_sent_item' });
    return result(call, 'ok', reconcilingPayload({ response, reconciliation: sent.length > 1 ? 'multiple_matching_sent_items' : 'sent_item_not_found' }));
  }
  async preview(call) {
    const args = this.validate(call.name, call.arguments); const proposalRevision = this.remember(call, args);
    if (isGraphReadEgressTool(call.name)) {
      const identity = this.readBoundary.previewIdentity(call.name, args);
      return { provider: 'microsoft_graph', action: 'search_messages', destination: identity.folder ? `/me/mailFolders/${identity.folder}/messages` : '/me/messages', query: identity.query, proposal_revision: proposalRevision, data_categories: ['search_query'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.create_draft') {
      const bodyPreview = previewText(args.body, 512); return { provider: 'microsoft_graph', action: 'create_draft', destination: '/me', recipients: [...args.to, ...(args.cc ?? [])], subject: args.subject, body_preview: bodyPreview.text, body_truncated: bodyPreview.truncated, proposal_revision: proposalRevision, data_categories: ['recipient', 'subject', 'message_body'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.send_draft') {
      if (this.permissionProfile === 'always_ask' && call.preview_authorized !== true) return { provider: 'microsoft_graph', action: 'send_draft_preview_access', destination: '/me', draft_id: args.draft_id, preview_authorization_required: true, data_categories: ['existing_draft_content'], permission_profile: this.permissionProfile };
      const draftResponse = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$select': 'id,subject,body,toRecipients,ccRecipients,internetMessageHeaders,changeKey' }, signal: call.signal }); const draft = projectionDraft(draftResponse.body); if (!draft) throw new ProviderToolError('provider_invalid_response', 'draft projection unavailable'); if (!draft.etag || !draft.change_key) throw new ProviderToolError('provider_invalid_response', 'draft version unavailable'); const saved = this.proposals.get(call.id); saved.draftBinding = draftContentDigest(draft); saved.sent_marker = draft.operation_marker; saved.draftIdentity = Object.freeze({ etag: draft.etag, change_key: draft.change_key }); return { provider: 'microsoft_graph', action: 'send_draft', destination: '/me', draft_id: draft.id, subject: draft.subject, body_preview: draft.body_preview, body_truncated: draft.body_truncated, recipients: draft.recipients.map(recipient => recipient.address), etag: draft.etag, change_key: draft.change_key, proposal_revision: proposalRevision, data_categories: ['recipient', 'subject', 'existing_draft_content'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.mark_read') {
      return { provider: 'microsoft_graph', action: 'mark_read', destination: '/me', message_id: args.message_id, is_read: args.is_read, proposal_revision: proposalRevision, permission_profile: this.permissionProfile };
    }
    if (call.name === 'teams.send_message') {
      const bodyPreview = previewText(args.body, 512); return { provider: 'microsoft_graph', action: 'send_message', destination: '/chats', chat_id: args.chat_id, body_preview: bodyPreview.text, body_truncated: bodyPreview.truncated, data_categories: ['chat_message'], proposal_revision: proposalRevision, permission_profile: this.permissionProfile };
    }
    return { provider: 'microsoft_graph', action: call.name, destination: '/me', permission_profile: this.permissionProfile };
  }
  async execute(call) {
    let args; try { args = this.validate(call.name, call.arguments); const value = await this._execute(call, args); return value; } catch (error) { if (error?.code === 'invalid_tool_arguments') throw error; return failureResult(call, error); }
  }
  async _execute(call, args) {
    if (isGraphReadTool(call.name)) {
      if (isGraphReadEgressTool(call.name)) {
        const saved = this.assertProposal(call, args); const authorization = call.authorization;
        if (authorization !== undefined && (!authorization || typeof authorization !== 'object' || Array.isArray(authorization) || !['user_confirmation', 'operator_grant'].includes(authorization.kind) || authorization.kind === 'user_confirmation' && Object.keys(authorization).length !== 1 || authorization.kind === 'operator_grant' && (Object.keys(authorization).length !== 2 || typeof authorization.generation !== 'string' || !/^[a-f0-9]{32}$/u.test(authorization.generation)))) throw new ProviderToolError('provider_permission_insufficient');
        if (authorization?.kind === 'operator_grant') {
          const capability = this.capability(call.name); const grant = this.grantStore?.get(capability);
          if (!grant || grant.profile !== 'full_access' || grant.generation !== saved.grantGeneration || grant.generation !== authorization.generation || !this.grantValid(capability)) throw new ProviderToolError('provider_permission_revoked');
        } else if (authorization?.kind !== 'user_confirmation' && this.confirmationRequired(call.name)) throw new ProviderToolError('provider_permission_insufficient');
      }
      return this.readBoundary.execute(call, args);
    }
    checkAborted(call.signal);
    const saved = this.assertProposal(call, args);
    const binding = journalBinding(call);
    const capability = this.capability(call.name);
    const authorization = call.authorization;
    if (authorization !== undefined && (!authorization || typeof authorization !== 'object' || Array.isArray(authorization) || !['user_confirmation', 'operator_grant'].includes(authorization.kind) || authorization.kind === 'user_confirmation' && Object.keys(authorization).length !== 1 || authorization.kind === 'operator_grant' && (Object.keys(authorization).length !== 2 || typeof authorization.generation !== 'string' || !/^[a-f0-9]{32}$/u.test(authorization.generation)))) throw new ProviderToolError('provider_permission_insufficient');
    if (authorization?.kind === 'operator_grant') {
      const grant = this.grantStore?.get(capability); if (!grant || grant.profile !== 'full_access' || grant.generation !== saved.grantGeneration || grant.generation !== authorization.generation || !this.grantValid(capability)) throw new ProviderToolError('provider_permission_revoked');
    } else if (authorization?.kind !== 'user_confirmation' && this.confirmationRequired(call.name)) throw new ProviderToolError('provider_permission_insufficient');
    const repeatable = call.name === 'mail.mark_read';
    const key = `${call.name}:${binding?.operation_id ?? call.id}:${digest(args)}`;
    if (!repeatable && this.writeLedger.has(key)) return failureResult(call, new ProviderToolError('provider_write_already_attempted'));
    if (!repeatable) putBounded(this.writeLedger, key, { attempted_at: this.now() }, MAX_WRITE_RECORDS);
    const operation = this.beginOperation(capability, call.signal);
    let response;
    try {
      if (call.name === 'mail.create_draft') {
        saved.post_attempted = false; const marker = operationMarker(binding); const body = { subject: args.subject, body: { contentType: 'Text', content: args.body }, toRecipients: args.to.map(address), ccRecipients: (args.cc ?? []).map(address) };
        if (marker) body.internetMessageHeaders = [{ name: LAE_OPERATION_HEADER, value: marker }];
        response = await this.request({ method: 'POST', path: `${API}/me/messages`, body, signal: operation.signal, onDispatch: () => { saved.post_attempted = true; } });
        const created = response.status === 201 && validResource(response.body);
        if (created && saved.post_attempted) return verifiedResult({ call, binding, response, resource: { id: response.body.id }, reconciliation: 'created_resource' });
        if (created && !saved.post_attempted) return result(call, 'ok', reconcilingPayload({ response, resource: { id: response.body.id }, reconciliation: 'dispatch_unconfirmed' }));
        return await this.reconcileCreateDraft(call, args, binding, response, operation.signal);
      }
      if (call.name === 'mail.send_draft') {
        saved.prewrite_verified = false; saved.post_attempted = false; saved.reconciliation_allowed = false;
        const currentResponse = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$select': 'id,subject,body,toRecipients,ccRecipients,internetMessageHeaders,changeKey' }, signal: operation.signal }); const current = projectionDraft(currentResponse.body); if (!current || !saved.draftIdentity?.etag || !saved.draftIdentity?.change_key || !current.etag || !current.change_key || current.id !== args.draft_id || draftContentDigest(current) !== saved.draftBinding || current.operation_marker !== saved.sent_marker || current.etag !== saved.draftIdentity.etag || current.change_key !== saved.draftIdentity.change_key) throw new ProviderToolError('provider_permission_insufficient', 'draft identity changed after preview');
        const sentBefore = await this.request({ method: 'GET', path: `${API}/me/mailFolders/sentitems/messages`, query: { '$top': MAX_RECONCILIATION_ITEMS, '$orderby': 'sentDateTime desc', '$select': 'id,sentDateTime' }, signal: operation.signal }); const sentCollection = proofCollection(sentBefore.body); if (!sentCollection.values || sentCollection.truncated) throw new ProviderToolError('provider_invalid_response', sentCollection.truncated ? 'sent proof collection is incomplete' : 'sent proof collection unavailable'); const sentSnapshot = uniqueProofMap(sentCollection.values, item => typeof item?.sentDateTime === 'string' && Number.isFinite(graphDateTimeMs(item.sentDateTime)) && typeof item.id === 'string' && item.id.length > 0 ? { id: item.id } : null); if (!sentSnapshot) throw new ProviderToolError('provider_invalid_response', 'sent proof collection contains an incomplete or duplicate item'); saved.preexistingSentIds = new Set(sentSnapshot.map(item => item.id)); saved.sent_snapshot_at = this.now(); saved.prewrite_verified = true; saved.reconciliation_allowed = true;
        const sendHeaders = { 'If-Match': saved.draftIdentity.etag }; response = await this.request({ method: 'POST', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}/send`, headers: sendHeaders, signal: operation.signal, onDispatch: () => { saved.post_attempted = true; } });
        return await this.reconcileSendDraft(call, args, binding, response, saved, operation.signal);
      }
      if (call.name === 'mail.mark_read') {
        const before = await this.readMessageState(args.message_id, operation.signal);
        if (before.is_read === args.is_read) return verifiedResult({ call, binding, response: null, resource: { id: args.message_id, is_read: before.is_read }, reconciliation: 'pre_read_already_desired' });
        saved.post_attempted = false; response = await this.request({ method: 'PATCH', path: `${API}/me/messages/${encodeURIComponent(args.message_id)}`, body: { isRead: args.is_read }, signal: operation.signal, onDispatch: () => { saved.post_attempted = true; } });
        const after = await this.readMessageState(args.message_id, operation.signal); if (after.is_read !== args.is_read) return result(call, 'ok', reconcilingPayload({ response, reconciliation: 'desired_state_not_observed' }));
        if (!saved.post_attempted) return result(call, 'ok', reconcilingPayload({ response, resource: { id: after.id }, reconciliation: 'dispatch_unconfirmed' })); return verifiedResult({ call, binding, response, resource: { id: after.id, is_read: after.is_read }, reconciliation: 'post_write_get_verified' });
      }
      if (call.name === 'teams.send_message') {
        saved.prewrite_verified = false; saved.post_attempted = false; saved.reconciliation_allowed = false; saved.team_sender_id = null; saved.team_prewrite_at = this.now();
        const before = await this.listChatMessagesForProof(args.chat_id, operation.signal); if (!before.values || before.truncated) throw new ProviderToolError('provider_invalid_response', before.truncated ? 'chat proof collection is incomplete' : 'chat proof collection unavailable'); saved.preexistingTeamIds = new Set(before.values.map(item => item.id)); saved.prewrite_verified = true; saved.reconciliation_allowed = true;
        response = await this.request({ method: 'POST', path: `${API}/chats/${encodeURIComponent(args.chat_id)}/messages`, body: { body: { contentType: 'text', content: args.body } }, signal: operation.signal, onDispatch: () => { saved.post_attempted = true; } });
        const resource = response.status === 201 ? teamsResource(response.body, args.chat_id, args.body) : null;
        if (resource && saved.post_attempted) return verifiedResult({ call, binding, response, resource, reconciliation: 'created_resource' });
        if (resource && !saved.post_attempted) return result(call, 'ok', reconcilingPayload({ response, resource, reconciliation: 'dispatch_unconfirmed' }));
        const afterCollection = await this.listChatMessagesForProof(args.chat_id, operation.signal); if (!afterCollection.values || afterCollection.truncated) return result(call, 'ok', reconcilingPayload({ response, reconciliation: afterCollection.truncated ? 'chat_collection_truncated' : 'chat_collection_unavailable' })); const matches = this.teamsProofMatches(afterCollection.values, args, saved).filter(item => !saved.preexistingTeamIds.has(item.id));
        if (matches.length === 1) return verifiedResult({ call, binding, response, resource: { id: matches[0].id }, reconciliation: 'unique_chat_message' });
        return result(call, 'ok', reconcilingPayload({ response, reconciliation: matches.length > 1 ? 'multiple_matching_chat_messages' : 'chat_message_not_found' }));
      }
      throw new ProviderToolError('provider_invalid_response');
    } catch (error) {
      if (call.name === 'mail.create_draft' && ['provider_timeout', 'provider_failed'].includes(error?.code) && !operation.signal.aborted && saved.post_attempted) {
        try { return await this.reconcileCreateDraft(call, args, binding, null, operation.signal); } catch {}
      }
      if (call.name === 'mail.send_draft' && ['provider_timeout', 'provider_failed'].includes(error?.code) && !operation.signal.aborted && saved.draftBinding && saved.prewrite_verified && saved.post_attempted && saved.reconciliation_allowed) {
        try { return await this.reconcileSendDraft(call, args, binding, null, saved, operation.signal); } catch {}
      }
      if (call.name === 'mail.mark_read' && ['provider_timeout', 'provider_failed'].includes(error?.code) && !operation.signal.aborted && saved.post_attempted) {
        try { const after = await this.readMessageState(args.message_id, operation.signal); if (after.is_read === args.is_read) return verifiedResult({ call, binding, response: null, resource: { id: after.id, is_read: after.is_read }, reconciliation: 'timeout_get_verified' }); return result(call, 'ok', reconcilingPayload({ reconciliation: 'timeout_state_unknown' })); } catch {}
      }
      if (call.name === 'teams.send_message' && ['provider_timeout', 'provider_failed'].includes(error?.code) && !operation.signal.aborted && saved.prewrite_verified && saved.post_attempted && saved.reconciliation_allowed) {
        try { const afterCollection = await this.listChatMessagesForProof(args.chat_id, operation.signal); if (!afterCollection.values || afterCollection.truncated) return result(call, 'ok', reconcilingPayload({ reconciliation: afterCollection.truncated ? 'timeout_chat_collection_truncated' : 'timeout_chat_collection_unavailable' })); const matches = this.teamsProofMatches(afterCollection.values, args, saved).filter(item => !saved.preexistingTeamIds?.has(item.id)); if (matches.length === 1) return verifiedResult({ call, binding, response: null, resource: { id: matches[0].id }, reconciliation: 'timeout_unique_chat_message' }); return result(call, 'ok', reconcilingPayload({ reconciliation: matches.length > 1 ? 'timeout_multiple_chat_messages' : 'timeout_chat_message_not_found' })); } catch {}
      }
      return failureResult(call, error);
    } finally { operation.close(); }
  }
}

export function createMicrosoftGraphTools(options = {}) {
  const provider = options instanceof MicrosoftGraphProvider ? options : new MicrosoftGraphProvider(options);
  return Object.fromEntries(Object.entries(graphDefinitions).map(([name, definition]) => [name, { ...definition, confirmationRequired: call => provider.confirmationRequired(name, call), authorize: call => provider.authorize({ ...call, name }), preview: call => provider.preview({ ...call, name }), execute: call => provider.execute({ ...call, name }) }]));
}

export { MicrosoftDeviceCodeCredential, MicrosoftGraphHttpsTransport } from './microsoft-graph-auth.mjs';
