import { randomBytes } from 'node:crypto';
import { makeToolResult } from '../agent/tool-envelope.mjs';
import { ProviderToolError, boundedBoolean, boundedInteger, boundedString, digest, exactObject, normalizeText, own } from './provider-common.mjs';

const API = '/v1.0';
const FOLDERS = new Set(['inbox', 'sentitems', 'drafts', 'archive']);
const READ_TOOL_NAMES = new Set([
  'mail.list_messages', 'mail.search_messages', 'mail.read_message',
  'teams.list_chats', 'teams.list_messages', 'teams.read_message',
  'teams.list_channels', 'teams.list_channel_messages', 'teams.read_channel_message'
]);
const GRAPH_ID = /^[A-Za-z0-9][A-Za-z0-9._~:@!$'()*+,;=-]{0,511}$/u;
const PAGE_CURSOR = /^gpg_[a-f0-9]{32}$/u;
const GRAPH_DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,12})?Z$/u;
const MAX_CURSOR_RECORDS = 128;
const MAX_NEXT_LINK_BYTES = 8192;
const MAX_PAGE_ITEMS = 50;
const MAX_ITEM_TEXT_BYTES = 4096;
const MAX_BODY_BYTES = 60000;

const schema = (properties, required = [], extra = {}) => ({ type: 'object', additionalProperties: false, required, properties, ...extra });
const string = (max, extra = {}) => ({ type: 'string', maxLength: max, ...extra });
const identifier = string(512, { minLength: 1, pattern: GRAPH_ID.source });
const cursor = string(36, { minLength: 36, pattern: PAGE_CURSOR.source });
const pageSchema = (properties, required = []) => schema({ ...properties, page_cursor: cursor }, [], {
  oneOf: [
    { required, not: { required: ['page_cursor'] } },
    { required: ['page_cursor'], not: { anyOf: Object.keys(properties).map(key => ({ required: [key] })) } }
  ]
});

export const graphReadScopes = Object.freeze({
  'mail.list_messages': Object.freeze(['Mail.Read']),
  'mail.search_messages': Object.freeze(['Mail.Read']),
  'mail.read_message': Object.freeze(['Mail.Read']),
  'teams.list_chats': Object.freeze(['Chat.Read']),
  'teams.list_messages': Object.freeze(['Chat.Read']),
  'teams.read_message': Object.freeze(['Chat.Read']),
  'teams.list_channels': Object.freeze(['Channel.ReadBasic.All']),
  'teams.list_channel_messages': Object.freeze(['ChannelMessage.Read.All']),
  'teams.read_channel_message': Object.freeze(['ChannelMessage.Read.All'])
});

const INPUT_SCHEMAS = Object.freeze({
  'mail.list_messages': pageSchema({ folder: string(32, { enum: [...FOLDERS] }), unread_only: { type: 'boolean' }, limit: { type: 'integer', minimum: 1, maximum: 25 } }),
  'mail.search_messages': pageSchema({ query: string(128, { minLength: 1 }), folder: string(32, { enum: [...FOLDERS] }), limit: { type: 'integer', minimum: 1, maximum: 25 } }, ['query']),
  'mail.read_message': schema({ message_id: identifier, max_bytes: { type: 'integer', minimum: 1, maximum: 60000 } }, ['message_id']),
  'teams.list_chats': pageSchema({ limit: { type: 'integer', minimum: 1, maximum: 25 } }),
  'teams.list_messages': pageSchema({ chat_id: identifier, search_text: string(128, { minLength: 1 }), limit: { type: 'integer', minimum: 1, maximum: 50 } }, ['chat_id']),
  'teams.read_message': schema({ chat_id: identifier, message_id: identifier, max_bytes: { type: 'integer', minimum: 1, maximum: 60000 } }, ['chat_id', 'message_id']),
  'teams.list_channels': pageSchema({ team_id: identifier, limit: { type: 'integer', minimum: 1, maximum: 50 } }, ['team_id']),
  'teams.list_channel_messages': pageSchema({ team_id: identifier, channel_id: identifier, search_text: string(128, { minLength: 1 }), limit: { type: 'integer', minimum: 1, maximum: 50 } }, ['team_id', 'channel_id']),
  'teams.read_channel_message': schema({ team_id: identifier, channel_id: identifier, message_id: identifier, max_bytes: { type: 'integer', minimum: 1, maximum: 60000 } }, ['team_id', 'channel_id', 'message_id'])
});

const DESCRIPTIONS = Object.freeze({
  'mail.list_messages': 'List one bounded page of Outlook messages from the signed-in user mailbox.',
  'mail.search_messages': 'Search one bounded Outlook message page using a fixed Microsoft Graph search query.',
  'mail.read_message': 'Read bounded safe text from one Outlook message in the signed-in user mailbox.',
  'teams.list_chats': 'List one bounded page of existing Teams chats for the signed-in user.',
  'teams.list_messages': 'List or locally filter one bounded page of messages from one exact Teams chat.',
  'teams.read_message': 'Read bounded safe text from one exact message in one exact Teams chat.',
  'teams.list_channels': 'List one bounded page of channels from one exact Teams team.',
  'teams.list_channel_messages': 'List or locally filter one bounded page of messages from one exact Teams channel.',
  'teams.read_channel_message': 'Read bounded safe text from one exact message in one exact Teams channel.'
});

const definition = name => Object.freeze({
  name, version: '0.1.0', description: DESCRIPTIONS[name], risk_tier: 'T1',
  side_effect: name.startsWith('mail.') ? 'read_mail' : 'read_teams', network: true,
  data_egress: 'none', requires_confirmation: false, timeout_ms: 10000,
  output_limit: 65536, parameters: INPUT_SCHEMAS[name], input_schema: INPUT_SCHEMAS[name],
  required_scopes: graphReadScopes[name]
});

export const graphReadDefinitions = Object.freeze(Object.fromEntries([...READ_TOOL_NAMES].map(name => [name, definition(name)])));
export const isGraphReadTool = name => READ_TOOL_NAMES.has(name);

function identifierArgument(value, field) {
  boundedString(value, field, { min: 1, max: 512, identifier: true });
  if (!GRAPH_ID.test(value)) throw new ProviderToolError('invalid_tool_arguments', `${field} is not a Graph identifier`);
}

function searchArgument(value, field) {
  boundedString(value, field, { min: 1, max: 128 });
  if (Buffer.byteLength(value, 'utf8') > 256 || /["\\]/u.test(value) || !value.trim()) throw new ProviderToolError('invalid_tool_arguments', `${field} is not a safe search phrase`);
}

export function validateGraphReadArguments(name, input) {
  if (!READ_TOOL_NAMES.has(name)) throw new ProviderToolError('invalid_tool_arguments', 'unknown Graph read tool');
  const allowed = Object.keys(INPUT_SCHEMAS[name].properties);
  const required = own(input, 'page_cursor') ? [] : INPUT_SCHEMAS[name].oneOf?.[0]?.required ?? INPUT_SCHEMAS[name].required ?? [];
  const args = exactObject(input, allowed, required);
  if (own(args, 'page_cursor')) {
    boundedString(args.page_cursor, 'page_cursor', { min: 36, max: 36 });
    if (!PAGE_CURSOR.test(args.page_cursor) || Object.keys(args).length !== 1) throw new ProviderToolError('invalid_tool_arguments', 'page_cursor must be used alone');
    return structuredClone(args);
  }
  for (const field of ['message_id', 'chat_id', 'team_id', 'channel_id']) if (own(args, field)) identifierArgument(args[field], field);
  for (const field of ['query', 'search_text']) if (own(args, field)) searchArgument(args[field], field);
  if (own(args, 'folder')) { boundedString(args.folder, 'folder', { min: 1, max: 32 }); if (!FOLDERS.has(args.folder)) throw new ProviderToolError('invalid_tool_arguments', 'folder is not allowlisted'); }
  if (own(args, 'unread_only')) boundedBoolean(args.unread_only, 'unread_only');
  if (own(args, 'limit')) boundedInteger(args.limit, 'limit', 1, name.startsWith('mail.') || name === 'teams.list_chats' ? 25 : 50);
  if (own(args, 'max_bytes')) boundedInteger(args.max_bytes, 'max_bytes', 1, MAX_BODY_BYTES);
  return structuredClone(args);
}

const graphReadAttestations = new WeakMap();
const issueGraphReadAttestation = (output, call, payloadText) => {
  graphReadAttestations.set(output, Object.freeze({ provider: 'microsoft_graph', kind: 'read', call_id: call.id, tool_name: call.name, payload_digest: digest(payloadText) }));
  return output;
};
export const readGraphReadAttestation = output => output && typeof output === 'object' ? graphReadAttestations.get(output) ?? null : null;
export const transferGraphReadAttestation = (source, target) => { const attestation = readGraphReadAttestation(source); if (attestation && target && typeof target === 'object') graphReadAttestations.set(target, attestation); return target; };

function readResult(call, status, payload, truncated = false) {
  const text = JSON.stringify(payload);
  if (Buffer.byteLength(text, 'utf8') > 65536) throw new ProviderToolError('provider_response_too_large');
  return issueGraphReadAttestation(makeToolResult({ id: call.id, name: call.name, status, text, truncated }), call, text);
}

function exactProviderObject(value, allowed, required = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ProviderToolError('provider_invalid_response');
  const keys = Object.keys(value);
  if (keys.length > allowed.length || keys.some(key => !allowed.includes(key)) || required.some(key => !own(value, key))) throw new ProviderToolError('provider_invalid_response');
  return value;
}

function providerString(value, maxBytes, { nullable = false, optional = false } = {}) {
  if ((value === null && nullable) || (value === undefined && optional)) return value ?? null;
  if (typeof value !== 'string' || value.length > maxBytes || Buffer.byteLength(value, 'utf8') > maxBytes || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) throw new ProviderToolError('provider_invalid_response');
  return value;
}

function providerId(value, expected = null) {
  providerString(value, 512);
  if (!GRAPH_ID.test(value) || expected !== null && value !== expected) throw new ProviderToolError('provider_invalid_response');
  return value;
}

function graphDate(value, optional = false) {
  if (value === undefined && optional) return null;
  providerString(value, 64);
  const match = GRAPH_DATE_TIME.exec(value);
  if (!match) throw new ProviderToolError('provider_invalid_response');
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > days[month - 1] || hour > 23 || minute > 59 || second > 59) throw new ProviderToolError('provider_invalid_response');
  return value;
}

function safeText(value, maxBytes) {
  providerString(value, Math.min(maxBytes * 4, 65536));
  return normalizeText(value, maxBytes);
}

function address(value) {
  if (value === null || value === undefined) return { name: '', address: '' };
  exactProviderObject(value, ['emailAddress'], ['emailAddress']);
  const email = exactProviderObject(value.emailAddress, ['name', 'address'], ['address']);
  const name = providerString(email.name, 256, { optional: true }) ?? '';
  const emailAddress = providerString(email.address, 320);
  return { name, address: emailAddress };
}

function mailSummary(value) {
  exactProviderObject(value, ['id', 'receivedDateTime', 'from', 'subject', 'isRead', 'importance', 'bodyPreview', '@odata.etag'], ['id']);
  if (own(value, '@odata.etag')) providerString(value['@odata.etag'], 512);
  const preview = safeText(value.bodyPreview ?? '', 1024);
  const importance = value.importance === undefined ? 'normal' : value.importance;
  if (!['low', 'normal', 'high'].includes(importance) || value.isRead !== undefined && typeof value.isRead !== 'boolean') throw new ProviderToolError('provider_invalid_response');
  return { id: providerId(value.id), received_at: graphDate(value.receivedDateTime, true), from: address(value.from), subject: providerString(value.subject, 998, { optional: true }) ?? '', unread: value.isRead === false, importance, preview: preview.text, preview_truncated: preview.truncated };
}

function mailDetail(value, expectedId, maxBytes) {
  exactProviderObject(value, ['id', 'receivedDateTime', 'from', 'subject', 'isRead', 'importance', 'body', '@odata.etag'], ['id', 'body']);
  const body = exactProviderObject(value.body, ['contentType', 'content'], ['contentType', 'content']);
  const contentType = providerString(body.contentType, 16).toLowerCase();
  if (!['text', 'html'].includes(contentType)) throw new ProviderToolError('provider_invalid_response');
  const text = safeText(body.content, maxBytes);
  const summary = mailSummary({ id: value.id, receivedDateTime: value.receivedDateTime, from: value.from, subject: value.subject, isRead: value.isRead, importance: value.importance, bodyPreview: '', ...(own(value, '@odata.etag') ? { '@odata.etag': value['@odata.etag'] } : {}) });
  if (providerId(summary.id, expectedId) !== expectedId) throw new ProviderToolError('provider_invalid_response');
  return { ...summary, text: text.text, text_truncated: text.truncated };
}

function chatSummary(value) {
  exactProviderObject(value, ['id', 'topic', 'chatType', 'lastUpdatedDateTime', '@odata.etag'], ['id', 'chatType']);
  if (own(value, '@odata.etag')) providerString(value['@odata.etag'], 512);
  const type = providerString(value.chatType, 32);
  if (!['oneOnOne', 'group', 'meeting', 'unknownFutureValue'].includes(type)) throw new ProviderToolError('provider_invalid_response');
  return { id: providerId(value.id), topic: providerString(value.topic, 512, { nullable: true, optional: true }) ?? '', type, last_updated: graphDate(value.lastUpdatedDateTime, true) };
}

function sender(value) {
  if (value === null || value === undefined) return { kind: 'unknown', name: '' };
  exactProviderObject(value, ['user', 'application', 'device']);
  const entries = ['user', 'application', 'device'].filter(key => value[key] !== undefined && value[key] !== null);
  if (entries.length !== 1) throw new ProviderToolError('provider_invalid_response');
  const kind = entries[0]; const identity = exactProviderObject(value[kind], ['id', 'displayName'], ['id']);
  providerId(identity.id); return { kind, name: providerString(identity.displayName, 256, { optional: true }) ?? '' };
}

function teamsMessage(value, expectedId = null, maxBytes = 1024) {
  exactProviderObject(value, ['id', 'replyToId', 'createdDateTime', 'lastModifiedDateTime', 'importance', 'from', 'body', '@odata.etag'], ['id', 'createdDateTime', 'body']);
  if (own(value, '@odata.etag')) providerString(value['@odata.etag'], 512);
  const body = exactProviderObject(value.body, ['contentType', 'content'], ['contentType', 'content']);
  const contentType = providerString(body.contentType, 16).toLowerCase();
  if (!['text', 'html'].includes(contentType)) throw new ProviderToolError('provider_invalid_response');
  const content = safeText(body.content, maxBytes);
  const importance = value.importance === undefined ? 'normal' : value.importance;
  if (!['low', 'normal', 'high'].includes(importance)) throw new ProviderToolError('provider_invalid_response');
  return { id: providerId(value.id, expectedId), reply_to_id: value.replyToId === null || value.replyToId === undefined ? null : providerId(value.replyToId), created_at: graphDate(value.createdDateTime), modified_at: graphDate(value.lastModifiedDateTime, true), sender: sender(value.from), text: content.text, text_truncated: content.truncated, importance };
}

function channelSummary(value) {
  exactProviderObject(value, ['id', 'displayName', 'description', 'membershipType', '@odata.etag'], ['id', 'displayName']);
  if (own(value, '@odata.etag')) providerString(value['@odata.etag'], 512);
  const membership = value.membershipType === undefined ? 'standard' : providerString(value.membershipType, 32);
  if (!['standard', 'private', 'shared', 'unknownFutureValue'].includes(membership)) throw new ProviderToolError('provider_invalid_response');
  const description = safeText(value.description ?? '', 1024);
  return { id: providerId(value.id), name: providerString(value.displayName, 256), description: description.text, description_truncated: description.truncated, membership };
}

function collection(value, mapItem, max) {
  exactProviderObject(value, ['@odata.context', '@odata.nextLink', 'value'], ['value']);
  if (own(value, '@odata.context')) providerString(value['@odata.context'], 2048);
  if (!Array.isArray(value.value) || value.value.length > max) throw new ProviderToolError('provider_invalid_response');
  const ids = new Set(); const items = value.value.map(mapItem);
  for (const item of items) { if (ids.has(item.id)) throw new ProviderToolError('provider_invalid_response'); ids.add(item.id); }
  return { items, nextLink: value['@odata.nextLink'] };
}

function parseNextLink(raw, expectedPath, expectedQuery) {
  providerString(raw, MAX_NEXT_LINK_BYTES);
  let url; try { url = new URL(raw); } catch { throw new ProviderToolError('provider_invalid_response'); }
  if (url.origin !== 'https://graph.microsoft.com' || url.username || url.password || url.hash || url.pathname !== expectedPath) throw new ProviderToolError('provider_invalid_response');
  const query = {}; const seen = new Set();
  for (const [key, value] of url.searchParams.entries()) {
    if (seen.has(key) || ![...Object.keys(expectedQuery), '$skiptoken', '$skip'].includes(key) || Buffer.byteLength(value, 'utf8') > 4096) throw new ProviderToolError('provider_invalid_response');
    seen.add(key); query[key] = value;
  }
  for (const [key, value] of Object.entries(expectedQuery)) if (query[key] !== String(value)) throw new ProviderToolError('provider_invalid_response');
  if (Number(seen.has('$skiptoken')) + Number(seen.has('$skip')) !== 1) throw new ProviderToolError('provider_invalid_response');
  return { path: expectedPath, query };
}

function failureCode(error) {
  return ['provider_disabled', 'provider_unconfigured', 'provider_unauthorized', 'provider_permission_insufficient', 'provider_cancelled', 'provider_timeout', 'provider_offline', 'provider_rate_limited', 'provider_response_too_large', 'provider_invalid_request', 'provider_invalid_response', 'provider_destination_rejected', 'provider_failed'].includes(error?.code) ? error.code : 'provider_failed';
}

export class MicrosoftGraphReadBoundary {
  constructor({ request, accountFingerprint } = {}) {
    if (typeof request !== 'function' || typeof accountFingerprint !== 'function') throw new TypeError('Graph read boundary dependencies are required');
    this.request = request; this.accountFingerprint = accountFingerprint; this.pages = new Map();
  }
  clear() { this.pages.clear(); }
  rememberPage(name, identity, path, query, rawNextLink) {
    if (rawNextLink === undefined || rawNextLink === null) return null;
    const next = parseNextLink(rawNextLink, path, query);
    const pageCursor = `gpg_${randomBytes(16).toString('hex')}`;
    if (this.pages.size >= MAX_CURSOR_RECORDS) this.pages.delete(this.pages.keys().next().value);
    this.pages.set(pageCursor, Object.freeze({ name, identity, account: this.accountFingerprint(), path: next.path, query: Object.freeze({ ...next.query }) }));
    return pageCursor;
  }
  resolvePage(name, args) {
    if (!own(args, 'page_cursor')) return null;
    const page = this.pages.get(args.page_cursor);
    if (!page || page.name !== name || page.account !== this.accountFingerprint()) throw new ProviderToolError('provider_invalid_request', 'page cursor is unavailable');
    this.pages.delete(args.page_cursor);
    return page;
  }
  async execute(call, args) {
    try { return await this._execute(call, args); }
    catch (error) { return readResult(call, 'failed', { provider: 'microsoft_graph', state: 'unavailable', code: failureCode(error) }); }
  }
  async page(call, args, spec) {
    const saved = this.resolvePage(call.name, args);
    const identity = saved?.identity ?? spec.identity;
    const path = saved?.path ?? spec.path;
    const query = saved?.query ?? spec.query;
    const response = await this.request({ method: 'GET', path, query, ...(spec.headers ? { headers: spec.headers } : {}), signal: call.signal });
    if (response.status !== 200) throw new ProviderToolError('provider_invalid_response');
    const projected = collection(response.body, spec.mapItem, spec.max);
    let items = projected.items;
    if (identity.search_text) { const needle = identity.search_text.toLocaleLowerCase('en-US'); items = items.filter(item => item.text.toLocaleLowerCase('en-US').includes(needle)); }
    const nextPage = this.rememberPage(call.name, identity, path, query, projected.nextLink);
    return readResult(call, 'ok', { provider: 'microsoft_graph', state: 'ready', source_untrusted: true, kind: spec.kind, account_scope: 'signed_in_user', [spec.field]: items, item_count: items.length, next_page: nextPage, search_scope: identity.search_text ? 'current_graph_page' : null });
  }
  async _execute(call, args) {
    const next = own(args, 'page_cursor');
    if (call.name === 'mail.list_messages' || call.name === 'mail.search_messages') {
      const folder = call.name === 'mail.list_messages' ? args.folder ?? 'inbox' : args.folder ?? null; const limit = args.limit ?? 25;
      const identity = next ? null : { folder, unread_only: args.unread_only === true, query: args.query ?? null, limit };
      const path = call.name === 'mail.search_messages' && folder === null ? `${API}/me/messages` : `${API}/me/mailFolders/${encodeURIComponent(folder)}/messages`;
      const query = { '$top': limit, '$select': 'id,receivedDateTime,from,subject,isRead,importance,bodyPreview' };
      if (args.unread_only) query.$filter = 'isRead eq false';
      if (args.query) query.$search = `\"${args.query}\"`;
      return this.page(call, args, { identity, path, query, max: 25, mapItem: mailSummary, kind: call.name === 'mail.search_messages' ? 'mail_search_page' : 'mail_page', field: 'messages' });
    }
    if (call.name === 'mail.read_message') {
      const response = await this.request({ method: 'GET', path: `${API}/me/messages/${encodeURIComponent(args.message_id)}`, headers: { Prefer: 'outlook.body-content-type="text"' }, query: { '$select': 'id,receivedDateTime,from,subject,isRead,importance,body' }, signal: call.signal });
      if (response.status !== 200) throw new ProviderToolError('provider_invalid_response');
      const message = mailDetail(response.body, args.message_id, args.max_bytes ?? MAX_BODY_BYTES);
      return readResult(call, 'ok', { provider: 'microsoft_graph', state: 'ready', source_untrusted: true, kind: 'mail_message', account_scope: 'signed_in_user', message }, message.text_truncated);
    }
    if (call.name === 'teams.list_chats') {
      return this.page(call, args, { identity: next ? null : { limit: args.limit ?? 25 }, path: `${API}/me/chats`, query: { '$top': args.limit ?? 25, '$select': 'id,topic,chatType,lastUpdatedDateTime' }, max: 25, mapItem: chatSummary, kind: 'teams_chat_page', field: 'chats' });
    }
    if (call.name === 'teams.list_messages') {
      const identity = next ? null : { chat_id: args.chat_id, search_text: args.search_text ?? null, limit: args.limit ?? 50 };
      return this.page(call, args, { identity, path: `${API}/chats/${encodeURIComponent(args.chat_id)}/messages`, query: { '$top': args.limit ?? 50, '$select': 'id,replyToId,createdDateTime,lastModifiedDateTime,importance,from,body' }, max: MAX_PAGE_ITEMS, mapItem: value => teamsMessage(value, null, MAX_ITEM_TEXT_BYTES), kind: 'teams_chat_message_page', field: 'messages' });
    }
    if (call.name === 'teams.read_message') {
      const response = await this.request({ method: 'GET', path: `${API}/chats/${encodeURIComponent(args.chat_id)}/messages/${encodeURIComponent(args.message_id)}`, query: { '$select': 'id,replyToId,createdDateTime,lastModifiedDateTime,importance,from,body' }, signal: call.signal });
      if (response.status !== 200) throw new ProviderToolError('provider_invalid_response');
      const message = teamsMessage(response.body, args.message_id, args.max_bytes ?? MAX_BODY_BYTES);
      return readResult(call, 'ok', { provider: 'microsoft_graph', state: 'ready', source_untrusted: true, kind: 'teams_chat_message', account_scope: 'signed_in_user', chat_id: args.chat_id, message }, message.text_truncated);
    }
    if (call.name === 'teams.list_channels') {
      const identity = next ? null : { team_id: args.team_id, limit: args.limit ?? 50 };
      return this.page(call, args, { identity, path: `${API}/teams/${encodeURIComponent(args.team_id)}/channels`, query: { '$top': args.limit ?? 50, '$select': 'id,displayName,description,membershipType' }, max: MAX_PAGE_ITEMS, mapItem: channelSummary, kind: 'teams_channel_page', field: 'channels' });
    }
    if (call.name === 'teams.list_channel_messages') {
      const identity = next ? null : { team_id: args.team_id, channel_id: args.channel_id, search_text: args.search_text ?? null, limit: args.limit ?? 50 };
      return this.page(call, args, { identity, path: `${API}/teams/${encodeURIComponent(args.team_id)}/channels/${encodeURIComponent(args.channel_id)}/messages`, query: { '$top': args.limit ?? 50, '$select': 'id,replyToId,createdDateTime,lastModifiedDateTime,importance,from,body' }, max: MAX_PAGE_ITEMS, mapItem: value => teamsMessage(value, null, MAX_ITEM_TEXT_BYTES), kind: 'teams_channel_message_page', field: 'messages' });
    }
    if (call.name === 'teams.read_channel_message') {
      const response = await this.request({ method: 'GET', path: `${API}/teams/${encodeURIComponent(args.team_id)}/channels/${encodeURIComponent(args.channel_id)}/messages/${encodeURIComponent(args.message_id)}`, query: { '$select': 'id,replyToId,createdDateTime,lastModifiedDateTime,importance,from,body' }, signal: call.signal });
      if (response.status !== 200) throw new ProviderToolError('provider_invalid_response');
      const message = teamsMessage(response.body, args.message_id, args.max_bytes ?? MAX_BODY_BYTES);
      return readResult(call, 'ok', { provider: 'microsoft_graph', state: 'ready', source_untrusted: true, kind: 'teams_channel_message', account_scope: 'signed_in_user', team_id: args.team_id, channel_id: args.channel_id, message }, message.text_truncated);
    }
    throw new ProviderToolError('provider_invalid_request');
  }
}
