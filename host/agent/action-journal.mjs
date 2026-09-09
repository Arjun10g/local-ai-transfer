import { constants } from 'node:fs';
import { lstat, open, readdir, realpath, unlink } from 'node:fs/promises';
import { createHash, randomBytes } from 'node:crypto';
import { isAbsolute, join, resolve } from 'node:path';
import { parseStrictJson } from './tool-envelope.mjs';

export const ACTION_JOURNAL_LIMITS = Object.freeze({ max_event_bytes: 64 * 1024, max_active: 256, max_records: 1024, max_terminal_records: 768, max_events_per_operation: 16 });
export const ACTION_STATES = Object.freeze(['prepared', 'authorized', 'dispatching', 'acknowledged', 'reconciling', 'completed', 'cancelled', 'failed_definitive', 'unknown_manual']);

const VERSION = 1;
const ZERO_HASH = '0'.repeat(64);
const MAX_BINDING_BYTES = 256 * 1024;
const OPERATION_ID = /^act_[a-f0-9]{32}$/u;
const TOOL_NAME = /^[a-z][a-z0-9_.-]{1,95}$/u;
const SIDE_EFFECT = /^[a-z][a-z0-9_.-]{0,63}$/u;
const HASH = /^[a-f0-9]{64}$/u;
const RISK = new Set(['T0', 'T1', 'T2', 'T3', 'T4']);
const AUTHORIZATION = new Set(['policy', 'user_confirmation', 'operator_grant']);
const RESOLUTIONS = new Set(['user_denied', 'request_cancelled', 'pre_dispatch_failure', 'dispatch_ambiguous', 'provider_acknowledged', 'completed', 'startup_recovery', 'manual_completed', 'manual_failed_definitive']);
const TERMINAL = new Set(['completed', 'cancelled', 'failed_definitive']);
const ACTIVE = new Set(ACTION_STATES.filter(state => !TERMINAL.has(state)));
const GRAPH_MUTATION_TOOLS = new Set(['mail.create_draft', 'mail.send_draft', 'mail.mark_read', 'teams.send_message']);
const TRANSITIONS = Object.freeze({
  prepared: new Set(['authorized', 'cancelled', 'failed_definitive']),
  authorized: new Set(['dispatching', 'cancelled', 'failed_definitive']),
  dispatching: new Set(['acknowledged', 'reconciling', 'unknown_manual']),
  acknowledged: new Set(['completed', 'reconciling', 'unknown_manual']),
  reconciling: new Set(['acknowledged', 'completed', 'failed_definitive', 'unknown_manual']),
  unknown_manual: new Set(['completed', 'failed_definitive']),
  completed: new Set(), cancelled: new Set(), failed_definitive: new Set()
});
const EVENT_KEYS = Object.freeze(['version', 'operation_id', 'sequence', 'state', 'timestamp_utc', 'tool_name', 'risk_tier', 'side_effect', 'request_ref', 'call_ref', 'arguments_digest', 'preview_digest', 'operation_digest', 'authorization_kind', 'resolution', 'prev_hash', 'hash']);
const MAX_FILE_BYTES = ACTION_JOURNAL_LIMITS.max_event_bytes * ACTION_JOURNAL_LIMITS.max_events_per_operation;

export class ActionJournalError extends Error {
  constructor(code, message = code) { super(message); this.name = 'ActionJournalError'; this.code = code; }
}

function sha256(value) { return createHash('sha256').update(value).digest('hex'); }
function canonicalJson(value, depth = 0, budget = { bytes: 0, nodes: 0 }) {
  if (depth > 16) throw new ActionJournalError('action_journal_invalid_record');
  if (++budget.nodes > 4096) throw new ActionJournalError('action_journal_invalid_record');
  const account = text => { budget.bytes += Buffer.byteLength(text, 'utf8'); if (budget.bytes > MAX_BINDING_BYTES) throw new ActionJournalError('action_journal_invalid_record'); return text; };
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return account(JSON.stringify(value));
  if (typeof value === 'number' && Number.isFinite(value)) return account(JSON.stringify(value));
  if (Array.isArray(value)) {
    account('[]'); const values = value.map((item, index) => { if (index) account(','); return canonicalJson(item, depth + 1, budget); }); return `[${values.join(',')}]`;
  }
  if (value && typeof value === 'object' && [Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    account('{}'); const values = Object.keys(value).sort().map((key, index) => { if (index) account(','); const encoded = account(JSON.stringify(key)); account(':'); return `${encoded}:${canonicalJson(value[key], depth + 1, budget)}`; }); return `{${values.join(',')}}`;
  }
  throw new ActionJournalError('action_journal_invalid_record');
}
export function createActionBinding({ requestId, callId, toolName, arguments: args, preview } = {}) {
  validateOpaque(requestId, 'requestId'); validateOpaque(callId, 'callId'); if (!TOOL_NAME.test(toolName ?? '') || !args || typeof args !== 'object' || Array.isArray(args)) throw new ActionJournalError('action_journal_invalid_record');
  const requestRef = sha256(`request\0${requestId}`); const callRef = sha256(`call\0${callId}`); const argumentsDigest = sha256(`arguments\0${canonicalJson(args)}`); const previewDigest = preview === undefined ? ZERO_HASH : sha256(`preview\0${canonicalJson(preview)}`);
  const operationDigest = sha256(canonicalJson({ version: VERSION, request_ref: requestRef, call_ref: callRef, tool_name: toolName, arguments_digest: argumentsDigest, preview_digest: previewDigest }));
  return Object.freeze({ requestRef, callRef, argumentsDigest, previewDigest, operationDigest });
}
function exactKeys(value) { return value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === EVENT_KEYS.length && EVENT_KEYS.every(key => Object.hasOwn(value, key)); }
function isoTimestamp(value) { return typeof value === 'string' && value.length >= 20 && value.length <= 32 && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value; }
function regularFile(value) { return Boolean(value && (typeof value.isFile === 'function' ? value.isFile() : value.isFile === true)); }
function singleLink(value) { return Number.isInteger(value?.nlink) && value.nlink === 1; }
function directory(value) { return Boolean(value && (typeof value.isDirectory === 'function' ? value.isDirectory() : value.isDirectory === true)); }
function symlink(value) { return Boolean(value && (typeof value.isSymbolicLink === 'function' ? value.isSymbolicLink() : value.isSymbolicLink === true)); }
function sameIdentity(left, right) { return left && right && left.dev === right.dev && left.ino === right.ino; }
function ownedByCurrentUser(value, platform) { return platform === 'win32' || typeof process.getuid !== 'function' || value?.uid === process.getuid(); }
function activeCount(records) { return [...records.values()].filter(record => ACTIVE.has(record.state)).length; }
function validateOpaque(value, name) { if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{8,96}$/u.test(value)) throw new ActionJournalError('action_journal_invalid_record', `${name} is invalid`); }
export function isGraphMutationToolName(value) { return typeof value === 'string' && GRAPH_MUTATION_TOOLS.has(value); }
function validateDirectoryPath(value, platform) {
  const platformAbsolute = platform === 'win32' ? /^[A-Za-z]:[\\/]/u.test(value ?? '') && !/^\\/u.test(value) : isAbsolute(value ?? '') && !/^\/\//u.test(value);
  if (!platformAbsolute || typeof value !== 'string' || value.length > 1024 || /[\u0000-\u001f\u007f]/u.test(value)) throw new TypeError('action journal directory must be an absolute local path');
  return resolve(value);
}
function eventPayload(event) {
  return {
    version: event.version, operation_id: event.operation_id, sequence: event.sequence, state: event.state, timestamp_utc: event.timestamp_utc,
    tool_name: event.tool_name, risk_tier: event.risk_tier, side_effect: event.side_effect, request_ref: event.request_ref, call_ref: event.call_ref, arguments_digest: event.arguments_digest, preview_digest: event.preview_digest, operation_digest: event.operation_digest,
    authorization_kind: event.authorization_kind, resolution: event.resolution, prev_hash: event.prev_hash
  };
}
function validateEvent(event, filename, previous) {
  if (!exactKeys(event) || event.version !== VERSION || !OPERATION_ID.test(event.operation_id) || filename !== `${event.operation_id}.jsonl` || !Number.isInteger(event.sequence) || event.sequence < 0 || event.sequence >= ACTION_JOURNAL_LIMITS.max_events_per_operation || !ACTION_STATES.includes(event.state) || !isoTimestamp(event.timestamp_utc) || !TOOL_NAME.test(event.tool_name) || !RISK.has(event.risk_tier) || !SIDE_EFFECT.test(event.side_effect) || !HASH.test(event.request_ref) || !HASH.test(event.call_ref) || !HASH.test(event.arguments_digest) || !HASH.test(event.preview_digest) || !HASH.test(event.operation_digest) || event.authorization_kind !== null && !AUTHORIZATION.has(event.authorization_kind) || event.resolution !== null && !RESOLUTIONS.has(event.resolution) || !HASH.test(event.prev_hash) || !HASH.test(event.hash)) throw new ActionJournalError('action_journal_corrupt');
  if (event.hash !== sha256(JSON.stringify(eventPayload(event)))) throw new ActionJournalError('action_journal_corrupt');
  if (!previous) {
    if (event.sequence !== 0 || event.state !== 'prepared' || event.prev_hash !== ZERO_HASH || event.authorization_kind !== null || event.resolution !== null) throw new ActionJournalError('action_journal_corrupt');
  } else {
    if (event.sequence !== previous.sequence + 1 || event.prev_hash !== previous.hash || !TRANSITIONS[previous.state]?.has(event.state)) throw new ActionJournalError('action_journal_corrupt');
    for (const key of ['operation_id', 'tool_name', 'risk_tier', 'side_effect', 'request_ref', 'call_ref', 'arguments_digest', 'preview_digest', 'operation_digest']) if (event[key] !== previous[key]) throw new ActionJournalError('action_journal_corrupt');
    if (previous.authorization_kind !== null && event.authorization_kind !== previous.authorization_kind) throw new ActionJournalError('action_journal_corrupt');
  }
  return event;
}
function publicEvent(event) { return Object.freeze({ sequence: event.sequence, state: event.state, timestamp_utc: event.timestamp_utc, authorization_kind: event.authorization_kind, resolution: event.resolution, receipt_hash: event.hash, previous_receipt_hash: event.prev_hash }); }
function publicRecord(record, includeEvents = false) {
  const last = record.events.at(-1);
  return Object.freeze({ operation_id: record.operation_id, tool_name: record.tool_name, risk_tier: record.risk_tier, side_effect: record.side_effect, state: record.state, arguments_digest: record.arguments_digest, preview_digest: record.preview_digest, operation_digest: record.operation_digest, created_at_utc: record.events[0].timestamp_utc, updated_at_utc: last.timestamp_utc, receipt_hash: last.hash, ...(includeEvents ? { events: Object.freeze(record.events.map(publicEvent)) } : {}) });
}

async function readBoundedFile(path) {
  const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const before = await handle.stat();
    if (!regularFile(before) || !singleLink(before) || before.size < 1 || before.size > MAX_FILE_BYTES) throw new ActionJournalError('action_journal_corrupt');
    const buffer = Buffer.alloc(before.size + 1); let offset = 0;
    while (offset < buffer.length) { const { bytesRead } = await handle.read(buffer, offset, buffer.length - offset, offset); if (!bytesRead) break; offset += bytesRead; }
    const after = await handle.stat();
    if (!sameIdentity(before, after) || !singleLink(after) || after.size !== before.size || offset !== before.size) throw new ActionJournalError('action_journal_corrupt');
    return { text: buffer.subarray(0, offset).toString('utf8'), identity: after, size: after.size };
  } finally { await handle.close(); }
}

export class ActionJournal {
  constructor({ directory: journalDirectory, platform = process.platform, testOnly = false, now = () => new Date().toISOString(), idFactory = () => `act_${randomBytes(16).toString('hex')}`, fault, maxActive = ACTION_JOURNAL_LIMITS.max_active, maxRecords = ACTION_JOURNAL_LIMITS.max_records, maxTerminalRecords = ACTION_JOURNAL_LIMITS.max_terminal_records } = {}) {
    if (!Number.isInteger(maxActive) || maxActive < 1 || maxActive > ACTION_JOURNAL_LIMITS.max_active || !Number.isInteger(maxRecords) || maxRecords < maxActive || maxRecords > ACTION_JOURNAL_LIMITS.max_records || !Number.isInteger(maxTerminalRecords) || maxTerminalRecords < 0 || maxTerminalRecords > maxRecords - maxActive) throw new TypeError('invalid action journal limits');
    this.platform = platform; this.testOnly = testOnly === true; this.directory = validateDirectoryPath(journalDirectory, platform); this.now = now; this.idFactory = idFactory; this.fault = fault; this.maxActive = maxActive; this.maxRecords = maxRecords; this.maxTerminalRecords = maxTerminalRecords;
    this.records = new Map(); this.directoryIdentity = null; this.healthState = 'uninitialized'; this.failureCode = 'action_journal_unavailable'; this.tail = Promise.resolve();
  }
  static async open(options) { const journal = new ActionJournal(options); await journal.initialize(); return journal; }
  _block(code) { this.healthState = 'blocked'; this.failureCode = code; }
  _assertHealthy() { if (this.healthState !== 'ready') throw new ActionJournalError(this.failureCode); }
  _serialize(operation) { const pending = this.tail.then(operation); this.tail = pending.catch(() => {}); return pending; }
  _timestamp() { const value = this.now(); if (!isoTimestamp(value)) throw new ActionJournalError('action_journal_clock_invalid'); return value; }
  async initialize() {
    if (this.healthState !== 'uninitialized') return this;
    if (this.platform === 'win32') { this._block('action_journal_platform_unavailable'); return this; }
    // Node's promises API has no openat/renameat/unlinkat equivalent.  The
    // pathname seam below cannot survive an ancestor swap, so it is test-only
    // and must never authorize a production action.
    if (!this.testOnly) { this._block('action_journal_handle_relative_unavailable'); return this; }
    try {
      const resolvedDirectory = await realpath(this.directory); if (resolvedDirectory !== this.directory) throw new ActionJournalError('action_journal_permissions_invalid');
      const info = await lstat(this.directory); if (symlink(info) || !directory(info) || !ownedByCurrentUser(info, this.platform) || (info.mode & 0o777) !== 0o700) throw new ActionJournalError('action_journal_permissions_invalid');
      this.directoryIdentity = info;
      const entries = await readdir(this.directory, { withFileTypes: true });
      if (entries.length > this.maxRecords) throw new ActionJournalError('action_journal_limit_exceeded');
      for (const entry of entries) {
        if (!entry.isFile() || !/^act_[a-f0-9]{32}\.jsonl$/u.test(entry.name)) throw new ActionJournalError('action_journal_corrupt');
        const loaded = await readBoundedFile(join(this.directory, entry.name)); if (!ownedByCurrentUser(loaded.identity, this.platform) || !singleLink(loaded.identity) || this.platform !== 'win32' && (loaded.identity.mode & 0o777) !== 0o600) throw new ActionJournalError('action_journal_permissions_invalid'); const lines = loaded.text.split('\n');
        if (lines.at(-1) !== '') throw new ActionJournalError('action_journal_corrupt'); lines.pop();
        if (!lines.length || lines.length > ACTION_JOURNAL_LIMITS.max_events_per_operation) throw new ActionJournalError('action_journal_corrupt');
        let previous; const events = lines.map(line => {
          if (!line || Buffer.byteLength(line, 'utf8') > ACTION_JOURNAL_LIMITS.max_event_bytes) throw new ActionJournalError('action_journal_corrupt');
          let parsed; try { parsed = parseStrictJson(line, { maxBytes: ACTION_JOURNAL_LIMITS.max_event_bytes, maxDepth: 2, maxString: 1024, maxArray: 0, maxObject: EVENT_KEYS.length }); } catch { throw new ActionJournalError('action_journal_corrupt'); }
          const event = validateEvent(parsed, entry.name, previous); previous = event; return event;
        });
        const first = events[0]; this.records.set(first.operation_id, { operation_id: first.operation_id, tool_name: first.tool_name, risk_tier: first.risk_tier, side_effect: first.side_effect, request_ref: first.request_ref, call_ref: first.call_ref, arguments_digest: first.arguments_digest, preview_digest: first.preview_digest, operation_digest: first.operation_digest, state: previous.state, events, identity: loaded.identity, size: loaded.size });
      }
      await this._verifyDirectoryLocked(); if (activeCount(this.records) > this.maxActive) throw new ActionJournalError('action_journal_limit_exceeded');
      this.healthState = 'ready'; this.failureCode = null;
      for (const record of [...this.records.values()]) {
        if (record.state === 'prepared' || record.state === 'authorized') await this._appendLocked(record.operation_id, 'cancelled', { resolution: 'startup_recovery' });
        else if (record.state === 'dispatching') await this._appendLocked(record.operation_id, 'unknown_manual', { resolution: 'dispatch_ambiguous' });
      }
      await this._pruneTerminalLocked();
      return this;
    } catch (error) { const code = error instanceof ActionJournalError ? error.code : 'action_journal_corrupt'; this.records.clear(); this._block(code); return this; }
  }
  health() { return Object.freeze({ state: this.healthState, error: this.healthState === 'ready' ? null : this.failureCode }); }
  async prepare({ requestId, callId, toolName, riskTier, sideEffect, argumentsDigest, previewDigest, operationDigest } = {}) {
    return this._serialize(async () => {
      this._assertHealthy(); validateOpaque(requestId, 'requestId'); validateOpaque(callId, 'callId');
      const requestRef = sha256(`request\0${requestId}`); const callRef = sha256(`call\0${callId}`); const expectedOperation = sha256(canonicalJson({ version: VERSION, request_ref: requestRef, call_ref: callRef, tool_name: toolName, arguments_digest: argumentsDigest, preview_digest: previewDigest }));
      if (!TOOL_NAME.test(toolName ?? '') || !RISK.has(riskTier) || !SIDE_EFFECT.test(sideEffect ?? '') || !HASH.test(argumentsDigest ?? '') || !HASH.test(previewDigest ?? '') || !HASH.test(operationDigest ?? '') || operationDigest !== expectedOperation) throw new ActionJournalError('action_journal_invalid_record');
      if ([...this.records.values()].some(record => ACTIVE.has(record.state) && record.tool_name === toolName && record.arguments_digest === argumentsDigest)) throw new ActionJournalError('action_journal_duplicate_active');
      await this._pruneTerminalLocked(1); if (activeCount(this.records) >= this.maxActive || this.records.size >= this.maxRecords) throw new ActionJournalError('action_journal_limit_exceeded');
      let operationId; for (let attempt = 0; attempt < 4; attempt++) { const candidate = this.idFactory(); if (OPERATION_ID.test(candidate) && !this.records.has(candidate)) { operationId = candidate; break; } }
      if (!operationId) throw new ActionJournalError('action_journal_id_unavailable');
      const seed = { operation_id: operationId, tool_name: toolName, risk_tier: riskTier, side_effect: sideEffect, request_ref: requestRef, call_ref: callRef, arguments_digest: argumentsDigest, preview_digest: previewDigest, operation_digest: operationDigest, state: null, events: [], identity: null, size: 0 };
      this.records.set(operationId, seed);
      try { await this._appendLocked(operationId, 'prepared'); return publicRecord(this.records.get(operationId)); } catch (error) { this.records.delete(operationId); throw error; }
    });
  }
  authorize(operationId, authorizationKind) { return this._transition(operationId, 'authorized', { authorizationKind }); }
  dispatch(operationId) { return this._transition(operationId, 'dispatching'); }
  acknowledge(operationId) { return this._transition(operationId, 'acknowledged', { resolution: 'provider_acknowledged' }); }
  complete(operationId) { return this._transition(operationId, 'completed', { resolution: 'completed' }); }
  cancel(operationId, resolution = 'user_denied') { return this._transition(operationId, 'cancelled', { resolution }); }
  failDefinitive(operationId, resolution = 'pre_dispatch_failure') { return this._transition(operationId, 'failed_definitive', { resolution }); }
  markUnknown(operationId) { return this._transition(operationId, 'unknown_manual', { resolution: 'dispatch_ambiguous' }); }
  beginReconciliation(operationId) { return this._transition(operationId, 'reconciling'); }
  _transition(operationId, state, options = {}) { return this._serialize(async () => { this._assertHealthy(); const receipt = await this._appendLocked(operationId, state, options); if (TERMINAL.has(state)) await this._pruneTerminalLocked(); return receipt; }); }
  async _appendLocked(operationId, state, { authorizationKind, resolution = null } = {}) {
    this._assertHealthy(); const record = this.records.get(operationId); if (!record || !OPERATION_ID.test(operationId)) throw new ActionJournalError('action_journal_not_found');
    const previous = record.events.at(-1); if (!previous && state !== 'prepared' || previous && !TRANSITIONS[previous.state]?.has(state)) throw new ActionJournalError('action_journal_invalid_transition');
    const auth = authorizationKind ?? previous?.authorization_kind ?? null;
    if (state === 'authorized' && !AUTHORIZATION.has(auth) || state !== 'prepared' && auth === null && !['cancelled', 'failed_definitive'].includes(state) || resolution !== null && !RESOLUTIONS.has(resolution)) throw new ActionJournalError('action_journal_invalid_transition');
    const payload = { version: VERSION, operation_id: operationId, sequence: record.events.length, state, timestamp_utc: this._timestamp(), tool_name: record.tool_name, risk_tier: record.risk_tier, side_effect: record.side_effect, request_ref: record.request_ref, call_ref: record.call_ref, arguments_digest: record.arguments_digest, preview_digest: record.preview_digest, operation_digest: record.operation_digest, authorization_kind: auth, resolution, prev_hash: previous?.hash ?? ZERO_HASH };
    const event = { ...payload, hash: sha256(JSON.stringify(payload)) }; const bytes = Buffer.from(`${JSON.stringify(event)}\n`, 'utf8');
    if (bytes.length - 1 > ACTION_JOURNAL_LIMITS.max_event_bytes || record.events.length >= ACTION_JOURNAL_LIMITS.max_events_per_operation) throw new ActionJournalError('action_journal_limit_exceeded');
    const path = join(this.directory, `${operationId}.jsonl`); const creating = record.events.length === 0; let handle;
    try {
      await this._verifyDirectoryLocked();
      await this.fault?.({ phase: 'before_append', operation_id: operationId, state });
      const flags = constants.O_WRONLY | constants.O_APPEND | (constants.O_NOFOLLOW ?? 0) | (creating ? constants.O_CREAT | constants.O_EXCL : 0);
      handle = await open(path, flags, 0o600); const before = await handle.stat();
      if (!regularFile(before) || !singleLink(before) || !ownedByCurrentUser(before, this.platform) || !creating && (!sameIdentity(before, record.identity) || before.size !== record.size) || creating && before.size !== 0 || this.platform !== 'win32' && (before.mode & 0o777) !== 0o600) throw new ActionJournalError('action_journal_corrupt');
      let offset = 0; while (offset < bytes.length) { const written = await handle.write(bytes, offset, bytes.length - offset, null); if (!written.bytesWritten) throw new ActionJournalError('action_journal_write_failed'); offset += written.bytesWritten; }
      await this.fault?.({ phase: 'after_append_before_sync', operation_id: operationId, state }); await handle.sync();
      const after = await handle.stat(); if (!sameIdentity(before, after) || !singleLink(after) || after.size !== before.size + bytes.length) throw new ActionJournalError('action_journal_corrupt');
      await handle.close(); handle = null;
      if (creating) { const directoryHandle = await open(this.directory, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0)); try { const directoryInfo = await directoryHandle.stat(); if (!directory(directoryInfo) || !sameIdentity(directoryInfo, this.directoryIdentity)) throw new ActionJournalError('action_journal_corrupt'); await directoryHandle.sync(); } finally { await directoryHandle.close(); } }
      await this._verifyDirectoryLocked();
      await this.fault?.({ phase: 'after_fsync', operation_id: operationId, state });
      record.events.push(event); record.state = state; record.identity = after; record.size = after.size; return publicRecord(record);
    } catch (error) {
      await handle?.close().catch(() => {}); const code = error instanceof ActionJournalError ? error.code : 'action_journal_write_failed'; this._block(code); throw new ActionJournalError(code);
    }
  }
  async _pruneTerminalLocked(reserve = 0) {
    try { await this._verifyDirectoryLocked(); } catch (error) { const code = error instanceof ActionJournalError ? error.code : 'action_journal_write_failed'; this._block(code); throw new ActionJournalError(code); }
    const terminal = [...this.records.values()].filter(record => TERMINAL.has(record.state)).sort((a, b) => a.events.at(-1).timestamp_utc.localeCompare(b.events.at(-1).timestamp_utc) || a.operation_id.localeCompare(b.operation_id));
    while (terminal.length > this.maxTerminalRecords || this.records.size + reserve > this.maxRecords) {
      const record = terminal.shift(); if (!record) throw new ActionJournalError('action_journal_limit_exceeded'); const path = join(this.directory, `${record.operation_id}.jsonl`);
      try { const info = await lstat(path); if (symlink(info) || !regularFile(info) || !singleLink(info) || !sameIdentity(info, record.identity) || info.size !== record.size) throw new ActionJournalError('action_journal_corrupt'); await unlink(path); const directoryHandle = await open(this.directory, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0)); try { const directoryInfo = await directoryHandle.stat(); if (!directory(directoryInfo) || !sameIdentity(directoryInfo, this.directoryIdentity)) throw new ActionJournalError('action_journal_corrupt'); await directoryHandle.sync(); } finally { await directoryHandle.close(); } await this._verifyDirectoryLocked(); this.records.delete(record.operation_id); }
      catch (error) { const code = error instanceof ActionJournalError ? error.code : 'action_journal_write_failed'; this._block(code); throw new ActionJournalError(code); }
    }
  }
  async _verifyDirectoryLocked() {
    const info = await lstat(this.directory);
    if (symlink(info) || !directory(info) || !ownedByCurrentUser(info, this.platform) || (info.mode & 0o777) !== 0o700 || !sameIdentity(info, this.directoryIdentity)) throw new ActionJournalError('action_journal_permissions_invalid');
    return info;
  }
  async summary({ limit = 100, state } = {}) {
    await this.tail; if (this.healthState !== 'ready') return Object.freeze({ health: this.health(), total: 0, active: 0, records: Object.freeze([]) });
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || state !== undefined && !ACTION_STATES.includes(state)) throw new ActionJournalError('action_journal_invalid_request');
    const selected = [...this.records.values()].filter(record => state === undefined || record.state === state).sort((a, b) => b.events.at(-1).timestamp_utc.localeCompare(a.events.at(-1).timestamp_utc) || a.operation_id.localeCompare(b.operation_id)).slice(0, limit).map(record => publicRecord(record));
    return Object.freeze({ health: this.health(), total: this.records.size, active: activeCount(this.records), records: Object.freeze(selected) });
  }
  async detail(operationId) { await this.tail; this._assertHealthy(); if (!OPERATION_ID.test(operationId ?? '')) throw new ActionJournalError('action_journal_invalid_request'); const record = this.records.get(operationId); if (!record) throw new ActionJournalError('action_journal_not_found'); return publicRecord(record, true); }
  async resolve(operationId, resolution) {
    if (!['completed', 'failed_definitive'].includes(resolution)) throw new ActionJournalError('action_journal_invalid_request');
    return this._serialize(async () => {
      this._assertHealthy();
      const record = this.records.get(operationId);
      if (!record || !OPERATION_ID.test(operationId)) throw new ActionJournalError('action_journal_not_found');
      // Graph mutation outcomes require provider-owned, operation-bound
      // evidence. No such capability exists in this slice, so an operator
      // assertion must not clear ambiguity or manufacture definitive failure.
      // Controller-owned pre-dispatch transitions continue to use cancel() or
      // failDefinitive(), never this manual endpoint.
      if (isGraphMutationToolName(record.tool_name)) throw new ActionJournalError('action_journal_provider_proof_required');
      const receipt = await this._appendLocked(operationId, resolution, { resolution: resolution === 'completed' ? 'manual_completed' : 'manual_failed_definitive' });
      if (TERMINAL.has(resolution)) await this._pruneTerminalLocked();
      return receipt;
    });
  }
  async reconcile() { throw new ActionJournalError('action_reconciliation_unavailable'); }
}
