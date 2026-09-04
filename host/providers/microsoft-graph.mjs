import { makeToolResult } from '../agent/tool-envelope.mjs';
import { ProviderToolError, exactObject, boundedArray, boundedBoolean, boundedInteger, boundedString, checkAborted, digest, failureResult, jsonResponse, normalizeText, own, providerError, result, safeArray } from './provider-common.mjs';
import { MicrosoftDeviceCodeCredential, MicrosoftGraphHttpsTransport, GRAPH_ORIGIN } from './microsoft-graph-auth.mjs';

const API = '/v1.0';
const FOLDERS = new Set(['inbox', 'sentitems', 'drafts', 'archive']);
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/u;
const CAP_MAIL = 'microsoft.graph.mail';
const CAP_TEAMS = 'microsoft.graph.teams';
const REQUEST_TIMEOUT_MS = 10000;
const MAX_TOKEN_BYTES = 4096;
const MAX_PROPOSALS = 128;
const MAX_WRITE_RECORDS = 256;
const schema = (properties, required = []) => ({ type: 'object', additionalProperties: false, required, properties });
const string = (max, extra = {}) => ({ type: 'string', maxLength: max, ...extra });
const identifier = string(512, { minLength: 1 });
const INPUT_SCHEMAS = Object.freeze({
  'mail.list_messages': schema({ folder: string(32, { enum: [...FOLDERS] }), unread_only: { type: 'boolean' }, limit: { type: 'integer', minimum: 1, maximum: 25 } }),
  'mail.read_message': schema({ message_id: identifier, max_bytes: { type: 'integer', minimum: 1, maximum: 65536 } }, ['message_id']),
  'mail.create_draft': schema({ to: { type: 'array', minItems: 1, maxItems: 20, items: string(320) }, cc: { type: 'array', maxItems: 20, items: string(320) }, subject: string(998), body: string(65536) }, ['to', 'subject', 'body']),
  'mail.send_draft': schema({ draft_id: identifier }, ['draft_id']),
  'mail.mark_read': schema({ message_id: identifier, is_read: { type: 'boolean' } }, ['message_id', 'is_read']),
  'teams.list_chats': schema({ limit: { type: 'integer', minimum: 1, maximum: 25 } }),
  'teams.list_messages': schema({ chat_id: identifier, limit: { type: 'integer', minimum: 1, maximum: 50 } }, ['chat_id']),
  'teams.send_message': schema({ chat_id: identifier, body: string(16384, { minLength: 1 }) }, ['chat_id', 'body'])
});
const descriptions = Object.freeze({
  'mail.list_messages': 'List bounded Outlook messages from the signed-in user mailbox.', 'mail.read_message': 'Read bounded plain text from one Outlook message.', 'mail.create_draft': 'Create one Outlook draft after confirmation.', 'mail.send_draft': 'Send one existing Outlook draft after high-impact confirmation.', 'mail.mark_read': 'Change the read state of one Outlook message.',
  'teams.list_chats': 'List bounded existing Teams chats for the signed-in user.', 'teams.list_messages': 'List bounded messages from one existing Teams chat.', 'teams.send_message': 'Send one message to an existing Teams chat after high-impact confirmation.'
});
const defs = (name, risk, effect, egress, output = 65536) => ({ name, version: '0.1.0', description: descriptions[name], risk_tier: risk, side_effect: effect, network: true, data_egress: egress, requires_confirmation: risk !== 'T1', timeout_ms: 10000, output_limit: output, parameters: INPUT_SCHEMAS[name], input_schema: INPUT_SCHEMAS[name] });

export const graphDefinitions = Object.freeze({
  'mail.list_messages': defs('mail.list_messages', 'T1', 'read_mail', 'none'),
  'mail.read_message': defs('mail.read_message', 'T1', 'read_mail', 'none'),
  'mail.create_draft': defs('mail.create_draft', 'T2', 'create_draft', 'message_content', 8192),
  'mail.send_draft': defs('mail.send_draft', 'T3', 'send_mail', 'message_content', 8192),
  'mail.mark_read': defs('mail.mark_read', 'T2', 'modify_mail', 'none', 8192),
  'teams.list_chats': defs('teams.list_chats', 'T1', 'read_teams', 'none'),
  'teams.list_messages': defs('teams.list_messages', 'T1', 'read_teams', 'none'),
  'teams.send_message': defs('teams.send_message', 'T3', 'send_teams', 'message_content', 8192)
});

const argument = (name, input) => {
  let args;
  if (name === 'mail.list_messages') {
    args = exactObject(input, ['folder', 'unread_only', 'limit']);
    if (own(args, 'folder')) boundedString(args.folder, 'folder', { min: 1, max: 32 });
    if (args.folder && !FOLDERS.has(args.folder)) throw new ProviderToolError('invalid_tool_arguments', 'folder is not allowlisted');
    if (own(args, 'unread_only')) boundedBoolean(args.unread_only, 'unread_only');
    if (own(args, 'limit')) boundedInteger(args.limit, 'limit', 1, 25);
  } else if (name === 'mail.read_message') {
    args = exactObject(input, ['message_id', 'max_bytes'], ['message_id']); boundedString(args.message_id, 'message_id', { min: 1, max: 512, identifier: true });
    if (own(args, 'max_bytes')) boundedInteger(args.max_bytes, 'max_bytes', 1, 65536);
  } else if (name === 'mail.create_draft') {
    args = exactObject(input, ['to', 'cc', 'subject', 'body'], ['to', 'subject', 'body']);
    const recipient = (value, field) => { boundedString(value, field, { min: 3, max: 320, identifier: true }); if (!EMAIL.test(value)) throw new ProviderToolError('invalid_tool_arguments', `${field} must be an email address`); };
    boundedArray(args.to, 'to', { min: 1, max: 20, item: recipient });
    if (own(args, 'cc')) boundedArray(args.cc, 'cc', { max: 20, item: recipient });
    boundedString(args.subject, 'subject', { max: 998 }); boundedString(args.body, 'body', { max: 65536 });
    if (Buffer.byteLength(args.body, 'utf8') > 65536) throw new ProviderToolError('invalid_tool_arguments', 'body exceeds byte limit');
  } else if (name === 'mail.send_draft') {
    args = exactObject(input, ['draft_id'], ['draft_id']); boundedString(args.draft_id, 'draft_id', { min: 1, max: 512, identifier: true });
  } else if (name === 'mail.mark_read') {
    args = exactObject(input, ['message_id', 'is_read'], ['message_id', 'is_read']); boundedString(args.message_id, 'message_id', { min: 1, max: 512, identifier: true }); boundedBoolean(args.is_read, 'is_read');
  } else if (name === 'teams.list_chats') {
    args = exactObject(input, ['limit']); if (own(args, 'limit')) boundedInteger(args.limit, 'limit', 1, 25);
  } else if (name === 'teams.list_messages') {
    args = exactObject(input, ['chat_id', 'limit'], ['chat_id']); boundedString(args.chat_id, 'chat_id', { min: 1, max: 512, identifier: true }); if (own(args, 'limit')) boundedInteger(args.limit, 'limit', 1, 50);
  } else if (name === 'teams.send_message') {
    args = exactObject(input, ['chat_id', 'body'], ['chat_id', 'body']); boundedString(args.chat_id, 'chat_id', { min: 1, max: 512, identifier: true }); boundedString(args.body, 'body', { min: 1, max: 16384 }); if (Buffer.byteLength(args.body, 'utf8') > 16384) throw new ProviderToolError('invalid_tool_arguments', 'body exceeds byte limit');
  } else throw new ProviderToolError('invalid_tool_arguments', `unknown Graph tool: ${name}`);
  return structuredClone(args);
};

const address = value => ({ emailAddress: { address: value } });
const projectionAddress = value => ({ name: typeof value?.name === 'string' ? value.name.slice(0, 256) : '', address: typeof value?.address === 'string' ? value.address.slice(0, 320) : '' });
const safeTeamsUrl = value => {
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 2048) return null;
  try { const url = new URL(value); if (url.protocol !== 'https:' || url.username || url.password || url.hash) return null; if (!(url.hostname === 'teams.microsoft.com' || url.hostname.endsWith('.teams.microsoft.com') || url.hostname === 'teams.live.com' || url.hostname.endsWith('.teams.live.com'))) return null; return url.toString(); } catch { return null; }
};
const projectionMessage = (value, maxPreview = 1024) => {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string') return null;
  const preview = normalizeText(value.bodyPreview, maxPreview);
  return { id: value.id.slice(0, 512), received_at: typeof value.receivedDateTime === 'string' ? value.receivedDateTime : null, from: projectionAddress(value.from?.emailAddress), subject: typeof value.subject === 'string' ? value.subject.slice(0, 998) : '', unread: value.isRead === false, importance: ['low', 'normal', 'high'].includes(value.importance) ? value.importance : 'normal', preview: preview.text, preview_truncated: preview.truncated };
};
const projectionDraft = value => {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string' || value.id.length < 1) return null;
  const body = normalizeText(value.body?.content, 4096); const recipients = [...safeArray(value.toRecipients, 40), ...safeArray(value.ccRecipients, 40)].map(item => item?.emailAddress?.address).filter(address => typeof address === 'string' && EMAIL.test(address)).slice(0, 40);
  return { id: value.id.slice(0, 512), subject: typeof value.subject === 'string' ? value.subject.slice(0, 998) : '', body_preview: body.text.slice(0, 512), recipients, etag: typeof value['@odata.etag'] === 'string' ? value['@odata.etag'].slice(0, 512) : null };
};
const putBounded = (map, key, value, max) => { if (map.size >= max && !map.has(key)) map.delete(map.keys().next().value); map.set(key, value); };
const validGraphPath = path => typeof path === 'string' && path.length <= 2048 && /^\/v1\.0\/(?:me(?:\/mailFolders\/[^/]+\/messages|\/messages(?:\/[^/]+(?:\/send)?)?|\/chats)?|chats\/[^/]+\/messages)$/u.test(path) && !path.includes('..') && !/[\u0000-\u001f\u007f]/u.test(path);
const validGraphMethodPath = (method, path) => {
  if (!['GET', 'POST', 'PATCH'].includes(method) || !validGraphPath(path)) return false;
  if (method === 'GET') return true;
  if (method === 'PATCH') return /^\/v1\.0\/me\/messages\/[^/]+$/u.test(path);
  return /^\/v1\.0\/me\/messages$|^\/v1\.0\/me\/messages\/[^/]+\/send$|^\/v1\.0\/chats\/[^/]+\/messages$/u.test(path);
};
const requiredGraphScope = (method, path) => path === '/v1.0/me' ? 'User.Read' : path === '/v1.0/me/chats' || path.startsWith('/v1.0/chats/') && method === 'GET' ? 'Chat.Read' : path.startsWith('/v1.0/chats/') ? 'ChatMessage.Send' : method === 'POST' && path.endsWith('/send') ? 'Mail.Send' : method === 'POST' ? 'Mail.ReadWrite' : 'Mail.Read';
const scopeAllows = (scopes, required) => scopes.includes(required) || required === 'Mail.Read' && scopes.includes('Mail.ReadWrite') || required === 'Chat.Read' && scopes.includes('Chat.ReadWrite') || required === 'ChatMessage.Send' && scopes.includes('Chat.ReadWrite');

export class MicrosoftGraphProvider {
  constructor({ enabled = false, credentialSource, transport, authTransport, tenant, clientId, scopes, onUserCode, sleep, origin = GRAPH_ORIGIN, permissionProfile = 'always_ask', grantStore, accountFingerprint = 'unknown', scope = 'account', now = () => Date.now(), requestTimeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    if (!['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access'].includes(permissionProfile)) throw new TypeError('invalid permission profile');
    if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 100 || requestTimeoutMs > 120000) throw new TypeError('invalid Graph request timeout');
    let parsed; try { parsed = new URL(origin); } catch { throw new TypeError('invalid Graph origin'); } if (parsed.origin !== 'https://graph.microsoft.com' || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) throw new TypeError('invalid Graph origin');
    if (credentialSource && (tenant !== undefined || clientId !== undefined || scopes !== undefined)) throw new TypeError('credential source and device-code settings are mutually exclusive');
    const graphTransport = transport ?? (tenant !== undefined || clientId !== undefined || scopes !== undefined ? new MicrosoftGraphHttpsTransport({ requestTimeoutMs, sleep }) : undefined);
    this.enabled = enabled === true; this.credentialSource = credentialSource ?? (tenant !== undefined || clientId !== undefined || scopes !== undefined ? new MicrosoftDeviceCodeCredential({ tenant, clientId, scopes, transport: authTransport ?? graphTransport, now, sleep, requestTimeoutMs, onUserCode }) : undefined); this.transport = graphTransport; this.origin = parsed.origin; this.permissionProfile = permissionProfile; this.grantStore = grantStore; this.accountFingerprint = accountFingerprint; this.scope = scope; this.now = now; this.requestTimeoutMs = requestTimeoutMs; this.proposals = new Map(); this.idempotent = new Map(); this.writeLedger = new Map();
  }
  state() { if (!this.enabled) return 'disabled'; if (!this.credentialSource || !this.transport) return 'unconfigured'; return 'ready'; }
  async status(signal) { const state = this.state(); if (state !== 'ready') return state; try { await this.token(signal); if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) { const me = await this.request({ method: 'GET', path: '/v1.0/me', query: { '$select': 'id' }, signal }); if (typeof me.id !== 'string' || me.id.length < 1 || me.id.length > 512) throw new ProviderToolError('provider_invalid_response'); const fingerprint = digest({ tenant: this.credentialSource.tenant, id: me.id }); if (this.accountFingerprint !== 'unknown' && this.accountFingerprint !== fingerprint) { this.clearAuth(); throw new ProviderToolError('provider_unauthorized'); } this.authenticatedAccountFingerprint = fingerprint; this.accountFingerprint = fingerprint; } return 'ready'; } catch (error) { return error.code === 'provider_offline' ? 'offline' : error.code === 'provider_unauthorized' ? 'unauthorized' : 'failed'; } }
  getAccountFingerprint() { return this.authenticatedAccountFingerprint ?? this.accountFingerprint; }
  authStatus() { if (!this.enabled) return { state: 'disabled', prompt: null, accountFingerprint: null }; if (!this.credentialSource || !this.transport) return { state: 'unconfigured', prompt: null, accountFingerprint: null }; if (!(this.credentialSource instanceof MicrosoftDeviceCodeCredential)) return { state: 'external', prompt: null, accountFingerprint: null }; const status = this.credentialSource.authStatus(); return { ...status, state: status.state === 'authenticated' && !this.authenticatedAccountFingerprint ? 'checking_account' : status.state, accountFingerprint: this.authenticatedAccountFingerprint ?? null }; }
  async startAuth(signal) { if (!this.enabled || !(this.credentialSource instanceof MicrosoftDeviceCodeCredential)) throw new ProviderToolError('provider_unconfigured'); await this.credentialSource.start(signal); if (await this.status(signal) !== 'ready') { this.clearAuth(); throw new ProviderToolError('provider_unauthorized'); } return this.getAccountFingerprint(); }
  authConfigured() { return this.enabled && this.credentialSource instanceof MicrosoftDeviceCodeCredential; }
  cancelAuth() { if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) this.credentialSource.cancel(); }
  clearAuth() { if (this.credentialSource instanceof MicrosoftDeviceCodeCredential) { this.credentialSource.clear(); this.authenticatedAccountFingerprint = null; for (const capability of [CAP_MAIL, CAP_TEAMS]) this.grantStore?.revoke?.(capability); } }
  capability(name) { return name.startsWith('mail.') ? CAP_MAIL : CAP_TEAMS; }
  isWrite(name) { return !['mail.list_messages', 'mail.read_message', 'teams.list_chats', 'teams.list_messages'].includes(name); }
  grantValid(capability) { const grant = this.grantStore?.get(capability); if (this.credentialSource instanceof MicrosoftDeviceCodeCredential && (!this.authenticatedAccountFingerprint || this.accountFingerprint === 'unknown' || this.accountFingerprint !== this.authenticatedAccountFingerprint)) return false; return this.permissionProfile === 'full_access' && grant?.profile === 'full_access' && grant.provider === 'microsoft_graph' && grant.account_fingerprint === this.accountFingerprint && grant.scope === this.scope; }
  confirmationRequired(name) {
    const important = name === 'teams.send_message' || name === 'mail.send_draft';
    if (this.permissionProfile === 'always_ask') return true;
    if (this.permissionProfile === 'ask_before_writes') return this.isWrite(name);
    if (this.permissionProfile === 'review_important_actions') return important;
    if (this.permissionProfile === 'full_access') return important || !this.grantValid(this.capability(name));
    return true;
  }
  validate(name, input) { return argument(name, input); }
  async token(signal) {
    checkAborted(signal); if (!this.enabled) throw new ProviderToolError('provider_disabled'); if (!this.credentialSource || !this.transport) throw new ProviderToolError('provider_unconfigured');
    let value; try { value = typeof this.credentialSource === 'function' ? await this.credentialSource(signal) : await this.credentialSource.getAccessToken?.(signal); } catch (error) { if (['provider_cancelled', 'provider_offline', 'provider_timeout'].includes(error?.code)) throw new ProviderToolError(error.code); throw new ProviderToolError('provider_unauthorized'); }
    if (typeof value !== 'string' || !value || Buffer.byteLength(value, 'utf8') > MAX_TOKEN_BYTES || /[\u0000-\u001f\u007f]/u.test(value)) throw new ProviderToolError('provider_unauthorized'); return value;
  }
  async request({ method, path, query, headers = {}, body, signal }) {
    if (!validGraphMethodPath(method, path)) throw new ProviderToolError('provider_destination_rejected');
    if (this.credentialSource instanceof MicrosoftDeviceCodeCredential && !scopeAllows(this.credentialSource.scopes, requiredGraphScope(method, path))) throw new ProviderToolError('provider_unauthorized', 'configured delegated scopes do not cover this operation');
    const bearer = await this.token(signal); checkAborted(signal); const deadline = new AbortController(); let rejectAbort; const relay = () => { deadline.abort(); rejectAbort?.(Object.assign(new Error('provider cancelled'), { code: 'provider_cancelled' })); }; signal?.addEventListener('abort', relay, { once: true }); let timer;
    let response; try {
      const request = { origin: this.origin, method, path, query: query ?? {}, headers: { ...headers, authorization: `Bearer ${bearer}`, accept: 'application/json' }, body, signal: deadline.signal };
      const pending = typeof this.transport === 'function' ? this.transport(request) : this.transport.request(request); let rejectTimeout; const timeout = new Promise((_, reject) => { rejectTimeout = reject; }); const cancelled = new Promise((_, reject) => { rejectAbort = reject; }); timer = setTimeout(() => { deadline.abort(); rejectTimeout(Object.assign(new Error('provider timeout'), { code: 'provider_timeout' })); }, this.requestTimeoutMs); response = await Promise.race([pending, timeout, cancelled]);
    } catch (error) {
      if (signal?.aborted) throw new ProviderToolError('provider_cancelled');
      if (deadline.signal.aborted) throw new ProviderToolError('provider_timeout');
      if (error?.code === 'provider_timeout' || error?.code === 'ETIMEDOUT') throw new ProviderToolError('provider_timeout');
      if (error?.code === 'ENETUNREACH' || error?.code === 'offline') throw new ProviderToolError('provider_offline');
      throw new ProviderToolError('provider_failed');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', relay); }
    if (!response || typeof response.status !== 'number') throw new ProviderToolError('provider_invalid_response');
    if (response.status < 200 || response.status >= 300) throw new ProviderToolError(providerError(response.status));
    return jsonResponse(response);
  }
  remember(call, args) { const capability = this.capability(call.name); const grant = this.grantStore?.get(capability); const revision = digest({ call: call.name, args, nonce: `${this.now()}:${call.id}` }); putBounded(this.proposals, call.id, { name: call.name, digest: digest(args), revision, grantGeneration: grant?.generation ?? null }, MAX_PROPOSALS); return revision; }
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
  replayKey(call, args) { return `${call.name}:${call.id}:${digest(args)}`; }
  beginOperation(capability, signal) {
    const controller = new AbortController(); const relay = () => controller.abort(); signal?.addEventListener('abort', relay, { once: true }); const unsubscribe = this.grantStore?.subscribe(capability, relay);
    return { signal: controller.signal, close: () => { signal?.removeEventListener('abort', relay); unsubscribe?.(); } };
  }
  async preview(call) {
    const args = this.validate(call.name, call.arguments); const proposalRevision = this.remember(call, args);
    if (call.name === 'mail.create_draft') {
      return { provider: 'microsoft_graph', action: 'create_draft', destination: '/me', recipients: [...args.to, ...(args.cc ?? [])], subject: args.subject, body_preview: args.body.slice(0, 512), proposal_revision: proposalRevision, data_categories: ['recipient', 'subject', 'message_body'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.send_draft') {
      const draftResponse = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$select': 'id,subject,body,toRecipients,ccRecipients' }, signal: call.signal }); const draft = projectionDraft(draftResponse); if (!draft) throw new ProviderToolError('provider_invalid_response', 'draft projection unavailable'); const saved = this.proposals.get(call.id); saved.draftBinding = digest(draft); return { provider: 'microsoft_graph', action: 'send_draft', destination: '/me', draft_id: draft.id, subject: draft.subject, body_preview: draft.body_preview, recipients: draft.recipients, etag: draft.etag, proposal_revision: proposalRevision, data_categories: ['recipient', 'subject', 'existing_draft_content'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.mark_read') {
      return { provider: 'microsoft_graph', action: 'mark_read', destination: '/me', message_id: args.message_id, is_read: args.is_read, proposal_revision: proposalRevision, permission_profile: this.permissionProfile };
    }
    if (call.name === 'teams.send_message') {
      return { provider: 'microsoft_graph', action: 'send_message', destination: '/chats', chat_id: args.chat_id, body_preview: args.body.slice(0, 512), data_categories: ['chat_message'], proposal_revision: proposalRevision, permission_profile: this.permissionProfile };
    }
    return { provider: 'microsoft_graph', action: call.name, destination: '/me', permission_profile: this.permissionProfile };
  }
  async execute(call) {
    let args; try { args = this.validate(call.name, call.arguments); const value = await this._execute(call, args); return value; } catch (error) { if (error?.code === 'invalid_tool_arguments') throw error; return failureResult(call, error); }
  }
  async _execute(call, args) {
    checkAborted(call.signal);
    if (call.name === 'mail.list_messages') {
      const folder = args.folder ?? 'inbox'; const query = { '$top': args.limit ?? 25, '$select': 'id,receivedDateTime,from,subject,isRead,importance,bodyPreview' }; if (args.unread_only) query.$filter = 'isRead eq false'; const body = await this.request({ method: 'GET', path: `${API}/me/mailFolders/${encodeURIComponent(folder)}/messages`, query, signal: call.signal });
      const values = safeArray(body.value, args.limit ?? 25); const messages = values.map(value => projectionMessage(value)).filter(Boolean); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', messages, truncated: Array.isArray(body.value) && body.value.length > values.length });
    }
    if (call.name === 'mail.read_message') {
      const body = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.message_id)}`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$select': 'id,receivedDateTime,from,subject,isRead,importance,body' }, signal: call.signal }); const message = projectionMessage({ ...body, bodyPreview: body.body?.content }); const content = normalizeText(body.body?.content, Math.min(args.max_bytes ?? 60000, 60000)); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', message: { ...message, text: content.text, text_truncated: content.truncated } }, { truncated: content.truncated });
    }
    if (call.name === 'teams.list_chats') {
      const body = await this.request({ method: 'GET', path: `${API}/me/chats`, query: { '$top': args.limit ?? 25, '$select': 'id,topic,chatType,lastUpdatedDateTime' }, signal: call.signal }); const values = safeArray(body.value, args.limit ?? 25); const chats = values.filter(value => value && typeof value.id === 'string').map(value => ({ id: value.id.slice(0, 512), topic: typeof value.topic === 'string' ? value.topic.slice(0, 512) : '', type: typeof value.chatType === 'string' ? value.chatType : 'unknown', last_updated: typeof value.lastUpdatedDateTime === 'string' ? value.lastUpdatedDateTime : null, participants: [] })); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', chats, truncated: Array.isArray(body.value) && body.value.length > values.length });
    }
    if (call.name === 'teams.list_messages') {
      const body = await this.request({ method: 'GET', path: `${API}/chats/${encodeURIComponent(args.chat_id)}/messages`, query: { '$top': args.limit ?? 50 }, signal: call.signal }); const values = safeArray(body.value, args.limit ?? 50); const messages = values.filter(value => value && typeof value.id === 'string').map(value => { const content = normalizeText(value.body?.content, 512); return { id: value.id.slice(0, 512), time: typeof value.createdDateTime === 'string' ? value.createdDateTime : null, sender: typeof value.from?.user?.displayName === 'string' ? value.from.user.displayName.slice(0, 256) : '', text: content.text, text_truncated: content.truncated, importance: ['low', 'normal', 'high'].includes(value.importance) ? value.importance : 'normal', web_url: safeTeamsUrl(value.webUrl) }; }); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', messages, truncated: Array.isArray(body.value) && body.value.length > values.length });
    }
    const key = this.replayKey(call, args);
    const saved = this.assertProposal(call, args);
    const capability = this.capability(call.name);
    const authorization = call.authorization;
    if (authorization !== undefined && (!authorization || typeof authorization !== 'object' || Array.isArray(authorization) || !['user_confirmation', 'operator_grant', 'policy'].includes(authorization.kind) || Object.keys(authorization).some(key => !['kind', 'generation'].includes(key)) || (authorization.kind === 'operator_grant' && (typeof authorization.generation !== 'string' || authorization.generation.length !== 32)))) throw new ProviderToolError('provider_permission_insufficient');
    if (authorization?.kind === 'operator_grant') {
      const grant = this.grantStore?.get(capability); if (!grant || grant.profile !== 'full_access' || grant.generation !== saved.grantGeneration || grant.generation !== authorization.generation || !this.grantValid(capability)) throw new ProviderToolError('provider_permission_revoked');
    } else if (authorization?.kind !== 'user_confirmation' && this.confirmationRequired(call.name)) throw new ProviderToolError('provider_permission_insufficient');
    if (this.idempotent.has(key)) { const previous = this.idempotent.get(key); const prior = JSON.parse(previous.content[0].text); return result(call, previous.status, { ...prior, idempotency: 'replayed' }); }
    if (this.writeLedger.has(key)) {
      const previous = this.writeLedger.get(key); if (previous.result) return previous.result;
      return failureResult(call, new ProviderToolError('provider_write_already_attempted'));
    }
    const ledger = { result: null }; putBounded(this.writeLedger, key, ledger, MAX_WRITE_RECORDS);
    const operation = this.beginOperation(capability, call.signal);
    let body; let response;
    try {
      if (call.name === 'mail.create_draft') {
        response = await this.request({ method: 'POST', path: `${API}/me/messages`, body: { subject: args.subject, body: { contentType: 'Text', content: args.body }, toRecipients: args.to.map(address), ccRecipients: (args.cc ?? []).map(address) }, signal: operation.signal });
      } else if (call.name === 'mail.send_draft') {
        const current = projectionDraft(await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$select': 'id,subject,body,toRecipients,ccRecipients' }, signal: operation.signal })); if (!current || current.id !== args.draft_id || digest(current) !== this.proposals.get(call.id)?.draftBinding) throw new ProviderToolError('provider_permission_insufficient', 'draft changed after preview');
        response = await this.request({ method: 'POST', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}/send`, headers: { 'Idempotency-Key': key }, signal: operation.signal });
      } else if (call.name === 'mail.mark_read') {
        response = await this.request({ method: 'PATCH', path: `${API}/me/messages/${encodeURIComponent(args.message_id)}`, body: { isRead: args.is_read }, signal: operation.signal });
      } else if (call.name === 'teams.send_message') {
        response = await this.request({ method: 'POST', path: `${API}/chats/${encodeURIComponent(args.chat_id)}/messages`, headers: { 'Idempotency-Key': key }, body: { body: { content: args.body } }, signal: operation.signal });
      } else throw new ProviderToolError('provider_invalid_response');
      if (authorization?.kind === 'operator_grant' && !this.grantValid(capability)) throw new ProviderToolError('provider_permission_revoked');
      checkAborted(operation.signal); body = response; const fallbackId = call.name === 'mail.mark_read' ? args.message_id : call.name === 'mail.send_draft' ? args.draft_id : call.name === 'teams.send_message' ? args.chat_id : null; const payload = { provider: 'microsoft_graph', state: 'ready', resource_id: typeof body.id === 'string' ? body.id.slice(0, 512) : fallbackId, accepted: true, completed: true, timestamp: new Date(this.now()).toISOString(), idempotency: 'new' };
      const output = result(call, 'ok', payload); ledger.result = output; putBounded(this.idempotent, key, output, MAX_WRITE_RECORDS); return output;
    } catch (error) {
      const output = failureResult(call, error); ledger.result = output; putBounded(this.idempotent, key, output, MAX_WRITE_RECORDS); return output;
    } finally { operation.close(); }
  }
}

export function createMicrosoftGraphTools(options = {}) {
  const provider = options instanceof MicrosoftGraphProvider ? options : new MicrosoftGraphProvider(options);
  return Object.fromEntries(Object.entries(graphDefinitions).map(([name, definition]) => [name, { ...definition, confirmationRequired: call => provider.confirmationRequired(name, call), authorize: call => provider.authorize({ ...call, name }), preview: call => provider.preview({ ...call, name }), execute: call => provider.execute({ ...call, name }) }]));
}

export { MicrosoftDeviceCodeCredential, MicrosoftGraphHttpsTransport } from './microsoft-graph-auth.mjs';
