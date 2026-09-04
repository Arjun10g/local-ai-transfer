import { makeToolResult } from '../agent/tool-envelope.mjs';
import { ProviderToolError, exactObject, boundedArray, boundedBoolean, boundedInteger, boundedString, checkAborted, digest, failureResult, jsonResponse, normalizeText, own, providerError, result, safeArray } from './provider-common.mjs';

const API = '/v1.0';
const FOLDERS = new Set(['inbox', 'sentitems', 'drafts', 'archive']);
const HEX = /^[a-f0-9]{64}$/;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/u;
const CAP_MAIL = 'microsoft.graph.mail';
const CAP_TEAMS = 'microsoft.graph.teams';
const REQUEST_TIMEOUT_MS = 10000;
const schema = (properties, required = []) => ({ type: 'object', additionalProperties: false, required, properties });
const string = (max, extra = {}) => ({ type: 'string', maxLength: max, ...extra });
const identifier = string(512, { minLength: 1 });
const INPUT_SCHEMAS = Object.freeze({
  'mail.list_messages': schema({ folder: string(32, { enum: [...FOLDERS] }), unread_only: { type: 'boolean' }, limit: { type: 'integer', minimum: 1, maximum: 25 } }),
  'mail.read_message': schema({ message_id: identifier, max_bytes: { type: 'integer', minimum: 1, maximum: 65536 } }, ['message_id']),
  'mail.create_draft': schema({ to: { type: 'array', minItems: 1, maxItems: 20, items: string(320) }, cc: { type: 'array', maxItems: 20, items: string(320) }, subject: string(998), body: string(65536) }, ['to', 'subject', 'body']),
  'mail.send_draft': schema({ draft_id: identifier, proposal_revision: string(64, { pattern: '^[a-f0-9]{64}$' }) }, ['draft_id', 'proposal_revision']),
  'mail.mark_read': schema({ message_id: identifier, is_read: { type: 'boolean' } }, ['message_id', 'is_read']),
  'teams.list_chats': schema({ limit: { type: 'integer', minimum: 1, maximum: 25 } }),
  'teams.list_messages': schema({ chat_id: identifier, limit: { type: 'integer', minimum: 1, maximum: 50 } }, ['chat_id']),
  'teams.send_message': schema({ chat_id: identifier, body: string(16384, { minLength: 1 }), proposal_revision: string(64, { pattern: '^[a-f0-9]{64}$' }) }, ['chat_id', 'body', 'proposal_revision'])
});
const defs = (name, risk, effect, egress, output = 65536) => ({ name, version: '0.1.0', risk_tier: risk, side_effect: effect, network: true, data_egress: egress, requires_confirmation: risk !== 'T1', timeout_ms: 10000, output_limit: output, input_schema: INPUT_SCHEMAS[name] });

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
    args = exactObject(input, ['draft_id', 'proposal_revision'], ['draft_id', 'proposal_revision']); boundedString(args.draft_id, 'draft_id', { min: 1, max: 512, identifier: true }); boundedString(args.proposal_revision, 'proposal_revision', { min: 64, max: 64 }); if (!HEX.test(args.proposal_revision)) throw new ProviderToolError('invalid_tool_arguments', 'proposal_revision must be a SHA-256 hex digest');
  } else if (name === 'mail.mark_read') {
    args = exactObject(input, ['message_id', 'is_read'], ['message_id', 'is_read']); boundedString(args.message_id, 'message_id', { min: 1, max: 512, identifier: true }); boundedBoolean(args.is_read, 'is_read');
  } else if (name === 'teams.list_chats') {
    args = exactObject(input, ['limit']); if (own(args, 'limit')) boundedInteger(args.limit, 'limit', 1, 25);
  } else if (name === 'teams.list_messages') {
    args = exactObject(input, ['chat_id', 'limit'], ['chat_id']); boundedString(args.chat_id, 'chat_id', { min: 1, max: 512, identifier: true }); if (own(args, 'limit')) boundedInteger(args.limit, 'limit', 1, 50);
  } else if (name === 'teams.send_message') {
    args = exactObject(input, ['chat_id', 'body', 'proposal_revision'], ['chat_id', 'body', 'proposal_revision']); boundedString(args.chat_id, 'chat_id', { min: 1, max: 512, identifier: true }); boundedString(args.body, 'body', { min: 1, max: 16384 }); if (Buffer.byteLength(args.body, 'utf8') > 16384) throw new ProviderToolError('invalid_tool_arguments', 'body exceeds byte limit'); boundedString(args.proposal_revision, 'proposal_revision', { min: 64, max: 64 }); if (!HEX.test(args.proposal_revision)) throw new ProviderToolError('invalid_tool_arguments', 'proposal_revision must be a SHA-256 hex digest');
  } else throw new ProviderToolError('invalid_tool_arguments', `unknown Graph tool: ${name}`);
  return structuredClone(args);
};

const address = value => ({ emailAddress: { address: value } });
const projectionAddress = value => ({ name: typeof value?.name === 'string' ? value.name.slice(0, 256) : '', address: typeof value?.address === 'string' ? value.address.slice(0, 320) : '' });
const projectionMessage = (value, maxPreview = 1024) => {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string') return null;
  const preview = normalizeText(value.bodyPreview, maxPreview);
  return { id: value.id.slice(0, 512), received_at: typeof value.receivedDateTime === 'string' ? value.receivedDateTime : null, from: projectionAddress(value.from?.emailAddress), subject: typeof value.subject === 'string' ? value.subject.slice(0, 998) : '', unread: value.isRead === false, importance: ['low', 'normal', 'high'].includes(value.importance) ? value.importance : 'normal', preview: preview.text, preview_truncated: preview.truncated };
};

export class MicrosoftGraphProvider {
  constructor({ enabled = false, credentialSource, transport, origin = 'https://graph.microsoft.com', permissionProfile = 'always_ask', grantStore, accountFingerprint = 'unknown', scope = 'account', now = () => Date.now(), requestTimeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    if (!['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access'].includes(permissionProfile)) throw new TypeError('invalid permission profile');
    if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 100 || requestTimeoutMs > 120000) throw new TypeError('invalid Graph request timeout');
    let parsed; try { parsed = new URL(origin); } catch { throw new TypeError('invalid Graph origin'); } if (parsed.origin !== 'https://graph.microsoft.com' || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) throw new TypeError('invalid Graph origin');
    this.enabled = enabled === true; this.credentialSource = credentialSource; this.transport = transport; this.origin = parsed.origin; this.permissionProfile = permissionProfile; this.grantStore = grantStore; this.accountFingerprint = accountFingerprint; this.scope = scope; this.now = now; this.requestTimeoutMs = requestTimeoutMs; this.proposals = new Map(); this.idempotent = new Map();
  }
  state() { if (!this.enabled) return 'disabled'; if (!this.credentialSource || !this.transport) return 'unconfigured'; return 'ready'; }
  async status(signal) { const state = this.state(); if (state !== 'ready') return state; try { await this.token(signal); return 'ready'; } catch (error) { return error.code === 'provider_offline' ? 'offline' : error.code === 'provider_unauthorized' ? 'unauthorized' : 'failed'; } }
  grantValid(capability) { const grant = this.grantStore?.get(capability); return this.permissionProfile === 'full_access' && grant?.profile === 'full_access' && grant.provider === 'microsoft_graph' && grant.account_fingerprint === this.accountFingerprint && grant.scope === this.scope; }
  confirmationRequired(name) { if (name === 'mail.create_draft' || name === 'mail.mark_read') return !this.grantValid(CAP_MAIL); return name === 'teams.send_message' || name === 'mail.send_draft'; }
  validate(name, input) { return argument(name, input); }
  async token(signal) {
    checkAborted(signal); if (!this.enabled) throw new ProviderToolError('provider_disabled'); if (!this.credentialSource || !this.transport) throw new ProviderToolError('provider_unconfigured');
    let value; try { value = typeof this.credentialSource === 'function' ? await this.credentialSource() : await this.credentialSource.getAccessToken?.(); } catch { throw new ProviderToolError('provider_unauthorized'); }
    if (typeof value !== 'string' || !value) throw new ProviderToolError('provider_unauthorized'); return value;
  }
  async request({ method, path, query, headers = {}, body, signal }) {
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
  remember(call, args) { this.proposals.set(call.id, { name: call.name, digest: digest(args) }); }
  assertProposal(call, args) { const saved = this.proposals.get(call.id); if (!saved || saved.name !== call.name || saved.digest !== digest(args)) throw new ProviderToolError('provider_permission_insufficient', 'write proposal no longer matches'); }
  replayKey(call, args) { return `${call.name}:${call.id}:${digest(args)}`; }
  beginOperation(capability, signal) {
    const controller = new AbortController(); const relay = () => controller.abort(); signal?.addEventListener('abort', relay, { once: true }); const unsubscribe = this.grantStore?.subscribe(capability, relay);
    return { signal: controller.signal, close: () => { signal?.removeEventListener('abort', relay); unsubscribe?.(); } };
  }
  async preview(call) {
    const args = this.validate(call.name, call.arguments);
    if (call.name === 'mail.create_draft') {
      this.remember(call, args); return { provider: 'microsoft_graph', action: 'create_draft', destination: '/me', recipients: [...args.to, ...(args.cc ?? [])], subject: args.subject, body_preview: args.body.slice(0, 512), data_categories: ['recipient', 'subject', 'message_body'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.send_draft') {
      this.remember(call, args); return { provider: 'microsoft_graph', action: 'send_draft', destination: '/me', draft_id: args.draft_id, proposal_revision: args.proposal_revision, data_categories: ['existing_draft_content'], permission_profile: this.permissionProfile };
    }
    if (call.name === 'mail.mark_read') {
      this.remember(call, args); return { provider: 'microsoft_graph', action: 'mark_read', destination: '/me', message_id: args.message_id, is_read: args.is_read, permission_profile: this.permissionProfile };
    }
    if (call.name === 'teams.send_message') {
      this.remember(call, args); return { provider: 'microsoft_graph', action: 'send_message', destination: '/me/chats', chat_id: args.chat_id, body_preview: args.body.slice(0, 512), data_categories: ['chat_message'], proposal_revision: args.proposal_revision, permission_profile: this.permissionProfile };
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
      const messages = safeArray(body.value).map(value => projectionMessage(value)).filter(Boolean).slice(0, args.limit ?? 25); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', messages, truncated: safeArray(body.value).length > messages.length });
    }
    if (call.name === 'mail.read_message') {
      const body = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.message_id)}`, query: { '$select': 'id,receivedDateTime,from,subject,isRead,importance,body' }, signal: call.signal }); const message = projectionMessage({ ...body, bodyPreview: body.body?.content }); const content = normalizeText(body.body?.content, Math.min(args.max_bytes ?? 60000, 60000)); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', message: { ...message, text: content.text, text_truncated: content.truncated } }, { truncated: content.truncated });
    }
    if (call.name === 'teams.list_chats') {
      const body = await this.request({ method: 'GET', path: `${API}/me/chats`, query: { '$top': args.limit ?? 25, '$select': 'id,topic,chatType,lastUpdatedDateTime,members' }, signal: call.signal }); const chats = safeArray(body.value).filter(value => value && typeof value.id === 'string').slice(0, args.limit ?? 25).map(value => ({ id: value.id.slice(0, 512), topic: typeof value.topic === 'string' ? value.topic.slice(0, 512) : '', type: typeof value.chatType === 'string' ? value.chatType : 'unknown', last_updated: typeof value.lastUpdatedDateTime === 'string' ? value.lastUpdatedDateTime : null, participants: safeArray(value.members).slice(0, 50).map(member => typeof member?.displayName === 'string' ? member.displayName.slice(0, 256) : '').filter(Boolean) })); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', chats, truncated: safeArray(body.value).length > chats.length });
    }
    if (call.name === 'teams.list_messages') {
      const body = await this.request({ method: 'GET', path: `${API}/me/chats/${encodeURIComponent(args.chat_id)}/messages`, query: { '$top': args.limit ?? 50, '$select': 'id,createdDateTime,from,body,importance,webUrl' }, signal: call.signal }); const messages = safeArray(body.value).filter(value => value && typeof value.id === 'string').slice(0, args.limit ?? 50).map(value => { const content = normalizeText(value.body?.content, 512); return { id: value.id.slice(0, 512), time: typeof value.createdDateTime === 'string' ? value.createdDateTime : null, sender: typeof value.from?.user?.displayName === 'string' ? value.from.user.displayName.slice(0, 256) : '', text: content.text, text_truncated: content.truncated, importance: ['low', 'normal', 'high'].includes(value.importance) ? value.importance : 'normal', web_url: typeof value.webUrl === 'string' && value.webUrl.startsWith('https://') ? value.webUrl.slice(0, 2048) : null }; }); return result(call, 'ok', { provider: 'microsoft_graph', state: 'ready', messages, truncated: safeArray(body.value).length > messages.length });
    }
    const key = this.replayKey(call, args); if (this.idempotent.has(key)) { const previous = this.idempotent.get(key); const prior = JSON.parse(previous.content[0].text); return result(call, 'ok', { ...prior, idempotency: 'replayed' }); }
    this.assertProposal(call, args);
    const capability = call.name.startsWith('mail.') ? CAP_MAIL : CAP_TEAMS;
    const routineWrite = call.name === 'mail.create_draft' || call.name === 'mail.mark_read';
    const grantAtStart = this.grantValid(capability);
    if (this.permissionProfile === 'full_access' && routineWrite && !grantAtStart && call.confirmationApproved !== true) throw new ProviderToolError('provider_permission_insufficient');
    const operation = this.beginOperation(capability, call.signal);
    let body; let response;
    try {
      if (call.name === 'mail.create_draft') {
        response = await this.request({ method: 'POST', path: `${API}/me/messages`, body: { subject: args.subject, body: { contentType: 'Text', content: args.body }, toRecipients: args.to.map(address), ccRecipients: (args.cc ?? []).map(address) }, signal: operation.signal });
      } else if (call.name === 'mail.send_draft') {
        response = await this.request({ method: 'POST', path: `${API}/me/messages/${encodeURIComponent(args.draft_id)}/send`, headers: { 'Idempotency-Key': key }, signal: operation.signal });
      } else if (call.name === 'mail.mark_read') {
        response = await this.request({ method: 'PATCH', path: `${API}/me/messages/${encodeURIComponent(args.message_id)}`, body: { isRead: args.is_read }, signal: operation.signal });
      } else if (call.name === 'teams.send_message') {
        response = await this.request({ method: 'POST', path: `${API}/me/chats/${encodeURIComponent(args.chat_id)}/messages`, headers: { 'Idempotency-Key': key }, body: { body: { content: args.body } }, signal: operation.signal });
      } else throw new ProviderToolError('provider_invalid_response');
      if (grantAtStart && !this.grantValid(capability)) throw new ProviderToolError('provider_permission_revoked');
      checkAborted(operation.signal); body = response; const fallbackId = call.name === 'mail.mark_read' ? args.message_id : call.name === 'mail.send_draft' ? args.draft_id : call.name === 'teams.send_message' ? args.chat_id : null; const payload = { provider: 'microsoft_graph', state: 'ready', resource_id: typeof body.id === 'string' ? body.id.slice(0, 512) : fallbackId, accepted: true, completed: true, timestamp: new Date(this.now()).toISOString(), idempotency: 'new' }; const output = result(call, 'ok', payload); this.idempotent.set(key, output); return output;
    } finally { operation.close(); }
  }
}

export function createMicrosoftGraphTools(options = {}) {
  const provider = options instanceof MicrosoftGraphProvider ? options : new MicrosoftGraphProvider(options);
  return Object.fromEntries(Object.entries(graphDefinitions).map(([name, definition]) => [name, { ...definition, confirmationRequired: call => provider.confirmationRequired(name, call), preview: call => provider.preview(call), execute: call => provider.execute({ ...call, name }) }]));
}
