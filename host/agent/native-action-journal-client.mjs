import { createHash, randomBytes } from 'node:crypto';
import { performance } from 'node:perf_hooks';

import {
  ACTION_JOURNAL_LIMITS,
  ActionJournalFrameDecoder,
  ActionJournalProtocolError,
  ActionJournalProtocolReceiver,
  buildRequest,
  canonicalJson,
  encodeEnvelope,
  journalEventDigest,
} from './action-journal-protocol.mjs';

// This adapter has no production transport, import, trust anchor, or launcher.
export const PRODUCTION_NATIVE_ACTION_JOURNAL_CLIENT_AVAILABLE = false;

export const NATIVE_ACTION_JOURNAL_CLIENT_LIMITS = Object.freeze({
  deadline_ms: 15_000,
  settlement_grace_ms: 1_000,
  max_connections: 4,
  max_pending_calls: ACTION_JOURNAL_LIMITS.max_buffered_frames,
  max_read_chunks_per_response: ACTION_JOURNAL_LIMITS.max_buffered_frames,
  max_response_bytes: ACTION_JOURNAL_LIMITS.max_frame_bytes,
  max_recovery_records: ACTION_JOURNAL_LIMITS.max_records,
});

const OPERATION_ID = /^act_[a-f0-9]{32}$/u;
const OPAQUE_ID = /^[A-Za-z0-9_-]{8,96}$/u;
const NONCE = /^[a-f0-9]{32}$/u;
const DIGEST = /^[a-f0-9]{64}$/u;
const TOOL_NAME = /^[a-z][a-z0-9_.-]{1,95}$/u;
const TERMINAL = new Set(['completed', 'cancelled', 'failed_definitive']);
const STATES = new Set([
  'prepared', 'authorized', 'dispatching', 'acknowledged', 'reconciling',
  'completed', 'cancelled', 'failed_definitive', 'unknown_manual',
]);
const TRANSITIONS = Object.freeze({
  authorize: Object.freeze({ from: ['prepared'], to: 'authorized' }),
  dispatch: Object.freeze({ from: ['authorized'], to: 'dispatching' }),
  acknowledge: Object.freeze({ from: ['dispatching'], to: 'acknowledged' }),
  begin_reconciliation: Object.freeze({ from: ['dispatching', 'acknowledged'], to: 'reconciling' }),
  complete: Object.freeze({ from: ['acknowledged', 'reconciling', 'unknown_manual'], to: 'completed' }),
  cancel: Object.freeze({ from: ['prepared', 'authorized'], to: 'cancelled' }),
  fail_definitive: Object.freeze({ from: ['prepared', 'authorized', 'reconciling', 'unknown_manual'], to: 'failed_definitive' }),
  mark_unknown: Object.freeze({ from: ['dispatching', 'acknowledged', 'reconciling'], to: 'unknown_manual' }),
});

export class NativeActionJournalClientError extends Error {
  constructor(code) {
    super(code);
    this.name = 'NativeActionJournalClientError';
    this.code = code;
    this.stack = `${this.name}: ${code}`;
  }
}

class AmbiguousExchangeError extends Error {}

function fail(code) { throw new NativeActionJournalClientError(code); }

function finiteNow(now) {
  const value = now();
  if (!Number.isSafeInteger(value) || value < 0 || value > ACTION_JOURNAL_LIMITS.max_unix_ms) {
    fail('action_journal_clock_invalid');
  }
  return value;
}

function exactOperation(value) {
  if (typeof value !== 'string' || !OPERATION_ID.test(value)) fail('action_journal_invalid_request');
  return value;
}

function exactDigest(value) {
  if (typeof value !== 'string' || !DIGEST.test(value)) fail('action_journal_invalid_request');
  return value;
}

function cloneReceipt(value) {
  if (!value || value.redacted !== true) fail('action_journal_invalid_response');
  return Object.freeze({
    authorization_kind: value.authorization_kind,
    operation_id: value.operation_id,
    receipt_digest: value.receipt_digest,
    recovery_required: value.recovery_required,
    redacted: true,
    resolution: value.resolution,
    sequence: value.sequence,
    state: value.state,
  });
}

function cloneEvent(value) {
  return Object.freeze({
    action: value.action,
    authorization_kind: value.authorization_kind,
    receipt_digest: value.receipt_digest,
    resolution: value.resolution,
    sequence: value.sequence,
    state: value.state,
  });
}

function sha256(...parts) {
  const digest = createHash('sha256');
  for (const part of parts) digest.update(part, 'utf8');
  return digest.digest('hex');
}

function helperDigest(label, ...parts) {
  return sha256(`lae.action-journal.helper.v0.1.0\0${label}\0`, ...parts);
}

function bridgeDigest(method, operationId, priorReceipt) {
  return sha256(
    'lae.action-journal-client.v0.1.0\0controller-transition\0', method, '\0',
    operationId, '\0', exactDigest(priorReceipt.receipt_digest),
  );
}

function activeBinding(toolName, argumentsDigest) {
  return `${toolName}\0${exactDigest(argumentsDigest)}`;
}

function resolutionFor(method, body) {
  if (method === 'acknowledge') return 'provider_acknowledged';
  if (['complete', 'cancel', 'fail_definitive', 'mark_unknown'].includes(method)) return body.resolution;
  return null;
}

function expectedTransition(method, operationId, previous, body) {
  const transition = TRANSITIONS[method];
  if (!transition || !transition.from.includes(previous.state)) fail('action_journal_invalid_transition');
  const authorization = method === 'authorize' ? body.authorization_kind : previous.authorization_kind;
  const resolution = resolutionFor(method, body);
  const state = transition.to;
  const receiptDigest = helperDigest(
    'receipt', operationId, '\0', previous.receipt_digest, '\0', method, '\0',
    canonicalJson(body), '\0', state, '\0', authorization ?? '', '\0', resolution ?? '',
  );
  const event = Object.freeze({
    action: method,
    authorization_kind: authorization,
    receipt_digest: receiptDigest,
    resolution,
    sequence: previous.sequence + 1,
    state,
  });
  return Object.freeze({ event, event_digest: journalEventDigest(event) });
}

function prepareReceiptDigest(body) {
  return helperDigest('prepare-binding', canonicalJson(body));
}

function receiptMatchesEvent(receipt, event) {
  return receipt.operation_id !== null
    && receipt.sequence === event.sequence
    && receipt.state === event.state
    && receipt.authorization_kind === event.authorization_kind
    && receipt.resolution === event.resolution
    && receipt.receipt_digest === event.receipt_digest;
}

const MISSING = Symbol('missing');

// Read data descriptors only.  In particular, never invoke a factory-supplied
// accessor while deciding whether its result is safe to adopt.  Every
// reflective operation is guarded because a test seam can contain a Proxy.
function safeDataProperty(value, name) {
  if (value === null || (typeof value !== 'object' && typeof value !== 'function')) {
    return MISSING;
  }
  let current = value;
  for (let depth = 0; depth < 8 && current !== null; depth++) {
    try {
      const descriptor = Object.getOwnPropertyDescriptor(current, name);
      if (descriptor !== undefined) {
        return Object.prototype.hasOwnProperty.call(descriptor, 'value')
          ? descriptor.value : MISSING;
      }
      current = Object.getPrototypeOf(current);
    } catch {
      return MISSING;
    }
  }
  return MISSING;
}

function safeTransport(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const write = safeDataProperty(value, 'write');
  const read = safeDataProperty(value, 'read');
  const close = safeDataProperty(value, 'close');
  if (typeof write !== 'function' || typeof read !== 'function' || typeof close !== 'function') return null;
  // Bind the descriptor values once.  Later calls cannot invoke a hostile
  // transport getter or silently replace a method.
  return Object.freeze({
    write: (...args) => Reflect.apply(write, value, args),
    read: (...args) => Reflect.apply(read, value, args),
    close: (...args) => Reflect.apply(close, value, args),
  });
}

function inspectConnection(value) {
  const inspected = { key: null, nonce: null, transport: null, shape: false };
  // Capture the independently accessible key first.  Thus a malformed
  // transport/nonce cannot strand a key that the factory did return.
  try {
    const key = safeDataProperty(value, 'key');
    if (exactOwnedKey(key)) inspected.key = key;
  } catch { /* malformed/proxy values remain unavailable */ }
  try {
    const nonce = safeDataProperty(value, 'nonce');
    if (typeof nonce === 'string') inspected.nonce = nonce;
  } catch { /* handled as malformed below */ }
  try {
    const transport = safeDataProperty(value, 'transport');
    inspected.transport = safeTransport(transport);
  } catch { /* handled as malformed below */ }
  try {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const names = Object.keys(value).sort();
      inspected.shape = names.join('\0') === ['key', 'nonce', 'transport'].join('\0');
    }
  } catch { inspected.shape = false; }
  return Object.freeze(inspected);
}

function exactConnectionShape(value) {
  return inspectConnection(value).shape;
}

function exactOwnedKey(value) {
  return value instanceof Uint8Array
    && !(typeof SharedArrayBuffer === 'function' && value.buffer instanceof SharedArrayBuffer)
    && value.buffer instanceof ArrayBuffer
    && value.byteOffset === 0
    && value.byteLength === 32
    && value.buffer.byteLength === 32;
}

function takeKey(value) {
  if (!exactOwnedKey(value)) fail('action_journal_transport_unavailable');
  // The test seam transfers this exact ArrayBuffer. The factory's supplied view
  // becomes detached; this does not claim erasure of copies made before return.
  return structuredClone(value, { transfer: [value.buffer] });
}

function exactTransport(value) { return safeTransport(value) !== null; }

function timerResult(delay, value) {
  let timer;
  const promise = new Promise(resolve => {
    timer = setTimeout(() => resolve(value), delay);
    timer.unref?.();
  });
  return { promise, clear: () => clearTimeout(timer) };
}

export class NativeActionJournalClient {
  #connectionFactory;
  #now;
  #requestIdFactory;
  #deadlineMs;
  #settlementGraceMs;
  #connection = null;
  #closingState = null;
  #closingPromise = null;
  #closePromise = null;
  #poisoned = false;
  #closeRequested = false;
  #closeEpoch = 0;
  #closeAbortController = new AbortController();
  #factorySlots = new Set();
  #requestSequence = 0;
  #responses = null;
  #decoder = null;
  #connections = 0;
  #tail = Promise.resolve();
  #queued = 0;
  #closed = false;
  #health = Object.freeze({ state: 'uninitialized', error: 'action_journal_unavailable' });
  #operations = new Map();
  #activeBindings = new Map();
  #manualRecovery = new Set();

  constructor({
    testOnly = false,
    connectionFactory,
    now = () => Date.now(),
    requestIdFactory = () => `req_${randomBytes(16).toString('hex')}`,
    deadlineMs = NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.deadline_ms,
    settlementGraceMs = NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.settlement_grace_ms,
  } = {}) {
    if (testOnly !== true) throw new TypeError('native action-journal client is test-only');
    if (typeof connectionFactory !== 'function' || typeof now !== 'function' || typeof requestIdFactory !== 'function') {
      throw new TypeError('invalid test transport configuration');
    }
    if (!Number.isSafeInteger(deadlineMs) || deadlineMs < 1 || deadlineMs > ACTION_JOURNAL_LIMITS.max_deadline_span_ms) {
      throw new TypeError('invalid journal deadline');
    }
    if (!Number.isSafeInteger(settlementGraceMs) || settlementGraceMs < 1 || settlementGraceMs > deadlineMs) {
      throw new TypeError('invalid settlement grace');
    }
    this.#connectionFactory = connectionFactory;
    this.#now = now;
    this.#requestIdFactory = requestIdFactory;
    this.#deadlineMs = deadlineMs;
    this.#settlementGraceMs = settlementGraceMs;
  }

  static async open(options) {
    const client = new NativeActionJournalClient(options);
    await client.#initialize();
    return client;
  }

  health() { return this.#health; }

  close() {
    if (this.#closePromise) return this.#closePromise;
    this.#closeRequested = true;
    this.#closeEpoch++;
    this.#closeAbortController.abort();
    this.#closed = true;
    this.#setBlocked('action_journal_unavailable');
    this.#closePromise = this.#tail.catch(() => {}).then(async () => {
      await this.#awaitFactorySlots();
      await this.#disconnect();
      this.#operations.clear();
      this.#activeBindings.clear();
      this.#manualRecovery.clear();
    });
    return this.#closePromise;
  }

  prepare({ requestId, callId, toolName, riskTier, sideEffect, argumentsDigest, previewDigest, operationDigest } = {}) {
    return this.#enqueue(async () => {
      this.#assertReady();
      if (typeof requestId !== 'string' || !OPAQUE_ID.test(requestId)
        || typeof callId !== 'string' || !OPAQUE_ID.test(callId)
        || typeof toolName !== 'string' || !TOOL_NAME.test(toolName)) {
        fail('action_journal_invalid_request');
      }
      for (const value of [argumentsDigest, previewDigest, operationDigest]) exactDigest(value);
      const binding = activeBinding(toolName, argumentsDigest);
      if (this.#activeBindings.has(binding)) fail('action_journal_duplicate_active');
      const body = {
        arguments_digest: argumentsDigest,
        call_ref: sha256('call\0', callId),
        operation_digest: operationDigest,
        preview_digest: previewDigest,
        request_ref: sha256('request\0', requestId),
        risk_tier: riskTier,
        side_effect: sideEffect,
        tool_name: toolName,
      };
      const receipt = await this.#mutate('prepare', null, body, null);
      this.#assertOpenForIo();
      this.#operations.set(receipt.operation_id, { binding, receipt });
      this.#activeBindings.set(binding, receipt.operation_id);
      this.#assertOpenForIo();
      return receipt;
    });
  }

  authorize(operationId, authorizationKind) {
    return this.#queuedMutation('authorize', operationId, { authorization_kind: authorizationKind });
  }
  dispatch(operationId) { return this.#queuedMutation('dispatch', operationId, {}); }
  acknowledge(operationId) {
    return this.#enqueue(async () => {
      this.#assertReady();
      const operation = exactOperation(operationId);
      const prior = this.#knownReceipt(operation);
      return this.#mutate('acknowledge', operation, {
        provider_receipt_digest: bridgeDigest('acknowledge', operation, prior),
      }, prior);
    });
  }
  beginReconciliation(operationId) {
    return this.#queuedMutation('begin_reconciliation', operationId, { reason: 'postcondition_pending' });
  }
  complete(operationId) {
    return this.#enqueue(async () => {
      this.#assertReady();
      const operation = exactOperation(operationId);
      const prior = this.#knownReceipt(operation);
      const resolution = prior.state === 'unknown_manual' ? 'manual_completed' : 'completed';
      return this.#mutate('complete', operation, {
        receipt_digest: bridgeDigest('complete', operation, prior), resolution,
      }, prior);
    });
  }
  cancel(operationId, resolution = 'user_denied') {
    return this.#queuedMutation('cancel', operationId, { resolution });
  }
  failDefinitive(operationId, resolution = 'pre_dispatch_failure') {
    return this.#enqueue(async () => {
      const operation = exactOperation(operationId);
      if (this.#manualRecovery.has(operation)) resolution = 'manual_failed_definitive';
      else this.#assertReady();
      const prior = this.#knownReceipt(operation);
      const receipt = await this.#mutate('fail_definitive', operation, { resolution }, prior);
      this.#manualRecovery.delete(operation);
      this.#restoreReadyAfterRecovery();
      this.#assertOpenForIo();
      return receipt;
    });
  }
  markUnknown(operationId) {
    return this.#queuedMutation('mark_unknown', operationId, { resolution: 'dispatch_ambiguous' });
  }

  summary({ limit = 100, state } = {}) {
    return this.#enqueue(async () => {
      const epoch = this.#closeEpoch;
      this.#assertQueryable();
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) fail('action_journal_invalid_request');
      if (state !== undefined && !STATES.has(state)) fail('action_journal_invalid_request');
      const records = await this.#allReceipts(true);
      this.#assertOpenForIo(epoch);
      const filtered = state === undefined ? records : records.filter(item => item.state === state);
      return Object.freeze({
        health: this.health(),
        total: records.length,
        active: records.filter(item => !TERMINAL.has(item.state)).length,
        records: Object.freeze(filtered.slice(0, limit)),
      });
    });
  }

  detail(operationId) {
    return this.#enqueue(async () => {
      const epoch = this.#closeEpoch;
      this.#assertQueryable();
      const detail = await this.#detailRaw(exactOperation(operationId));
      this.#assertOpenForIo(epoch);
      return detail;
    });
  }

  resolve(operationId, resolution) {
    return this.#enqueue(async () => {
      const operation = exactOperation(operationId);
      if (!this.#manualRecovery.has(operation)) fail('action_journal_invalid_transition');
      const prior = this.#knownReceipt(operation);
      let receipt;
      if (resolution === 'completed') {
        receipt = await this.#mutate('complete', operation, {
          receipt_digest: bridgeDigest('complete', operation, prior),
          resolution: 'manual_completed',
        }, prior);
      } else if (resolution === 'failed_definitive') {
        receipt = await this.#mutate('fail_definitive', operation, {
          resolution: 'manual_failed_definitive',
        }, prior);
      } else fail('action_journal_invalid_request');
      this.#manualRecovery.delete(operation);
      this.#restoreReadyAfterRecovery();
      this.#assertOpenForIo();
      return receipt;
    });
  }

  async reconcile() { fail('action_reconciliation_unavailable'); }

  #queuedMutation(method, operationId, body) {
    return this.#enqueue(async () => {
      this.#assertReady();
      const operation = exactOperation(operationId);
      const prior = this.#knownReceipt(operation);
      return this.#mutate(method, operation, body, prior);
    });
  }

  #knownReceipt(operationId) {
    const known = this.#operations.get(operationId);
    if (!known) fail('action_journal_invalid_transition');
    return known.receipt;
  }

  #enqueue(operation) {
    if (this.#closed) return Promise.reject(new NativeActionJournalClientError('action_journal_unavailable'));
    if (this.#queued >= NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_pending_calls) {
      return Promise.reject(new NativeActionJournalClientError('action_journal_queue_full'));
    }
    this.#queued++;
    const epoch = this.#closeEpoch;
    const pending = this.#tail.then(() => {
      this.#assertOpenForIo(epoch);
      return operation();
    });
    this.#tail = pending.catch(() => {});
    return pending.finally(() => { this.#queued--; });
  }

  async #initialize() {
    try {
      const health = await this.#establish();
      if (health.status !== 'ready' || health.platform_available !== true || health.production_enabled !== false) {
        this.#setBlocked('action_journal_platform_unavailable');
        await this.#disconnect({ suppress: true });
        return;
      }
      const active = await this.#allReceipts(false);
      for (const receipt of active) {
        const detail = await this.#detailRaw(receipt.operation_id);
        if (detail.receipt.state !== 'unknown_manual') fail('action_journal_recovery_incomplete');
        this.#operations.set(receipt.operation_id, { binding: null, receipt: detail.receipt });
        this.#manualRecovery.add(receipt.operation_id);
      }
      if (this.#manualRecovery.size > 0) this.#setBlocked('action_journal_recovery_required');
      else this.#setReady();
    } catch (error) {
      const code = error instanceof NativeActionJournalClientError
        && ['action_journal_recovery_incomplete', 'action_journal_recovery_required'].includes(error.code)
        ? error.code : 'action_journal_transport_unavailable';
      this.#setBlocked(code);
      await this.#disconnect({ suppress: true });
    }
  }

  #clock() {
    const issued = finiteNow(this.#now);
    const deadline = issued + this.#deadlineMs;
    if (!Number.isSafeInteger(deadline) || deadline > ACTION_JOURNAL_LIMITS.max_unix_ms) fail('action_journal_clock_invalid');
    return Object.freeze({ issued, deadline });
  }

  #assertOpenForIo(epoch = this.#closeEpoch) {
    if (this.#closeRequested || epoch !== this.#closeEpoch) {
      fail('action_journal_close_requested');
    }
  }

  #registerFactorySlot() {
    let resolve;
    let reject;
    let done;
    done = new Promise((finish, failDone) => { resolve = finish; reject = failDone; });
    // The slot's rejection is also surfaced through the retained poisoned
    // close transaction; mark it handled here so a malformed factory cannot
    // create an unhandled-rejection side channel before public close runs.
    done.catch(() => {});
    const slot = {
      raw: MISSING,
      parts: null,
      routed: false,
      done,
      resolve: () => {
        if (slot.routed) {
          this.#factorySlots.delete(slot);
          resolve();
        }
      },
      reject: error => {
        if (slot.routed) {
          this.#factorySlots.delete(slot);
          reject(error);
        }
      },
    };
    this.#factorySlots.add(slot);
    return slot;
  }

  #captureFactoryResult(slot, raw) {
    if (slot.routed) {
      this.#poisoned = true;
      this.#setBlocked('action_journal_close_unproven');
      return;
    }
    slot.raw = raw;
    // Capture descriptor-safe ownership once while the factory result is
    // available. Later close routing never rereads a hostile Proxy.
    slot.parts = inspectConnection(raw);
  }

  #finishFactoryWithoutResult(slot) {
    if (slot.routed) return;
    slot.routed = true;
    slot.raw = MISSING;
    slot.parts = null;
    slot.resolve();
  }

  #adoptFactoryResult(slot) {
    if (slot.routed || slot.raw === MISSING) fail('action_journal_transport_unavailable');
    const raw = slot.raw;
    slot.routed = true;
    slot.raw = MISSING;
    slot.parts = null;
    slot.resolve();
    return raw;
  }

  async #routeFactoryResult(slot) {
    if (slot.routed) return slot.done;
    slot.routed = true;
    const raw = slot.raw;
    const parts = slot.parts;
    slot.raw = MISSING;
    slot.parts = null;
    try {
      if (raw !== MISSING) await this.#closeRaw(parts ?? raw);
      slot.resolve();
    } catch (error) {
      // #closeRaw/#beginClose retain captured ownership on rejection.  The
      // slot is nevertheless settled because ownership has moved to the
      // retained poisoned close transaction.
      slot.reject(error);
      throw error;
    }
    return slot.done;
  }

  async #awaitFactorySlots() {
    while (this.#factorySlots.size > 0) {
      const pending = [...this.#factorySlots].map(slot => slot.done.catch(() => {}));
      await Promise.all(pending);
    }
  }

  async #settle(start, clock, { onFulfilled = null, onRejected = null, onEpochClose = null } = {}) {
    const epoch = this.#closeEpoch;
    this.#assertOpenForIo(epoch);
    const remaining = clock.deadline - finiteNow(this.#now);
    if (remaining <= 0) fail('action_journal_deadline_expired');
    const monotonicStarted = performance.now();
    const controller = new AbortController();
    const closeAbort = () => controller.abort();
    this.#closeAbortController.signal.addEventListener('abort', closeAbort, { once: true });
    if (this.#closeRequested || epoch !== this.#closeEpoch) controller.abort();
    const settled = Promise.resolve().then(() => {
      // The epoch check is immediately before the injected operation.
      // A close request therefore cannot start a new factory/read/write.
      this.#assertOpenForIo(epoch);
      return start({
        signal: controller.signal,
        deadlineAtMs: clock.deadline,
      });
    }).then(
      value => {
        onFulfilled?.(value);
        return { kind: 'fulfilled', value };
      },
      error => {
        onRejected?.(error);
        return { kind: 'rejected', error };
      },
    );
    const routeOnClose = async outcomeToRoute => {
      let final = outcomeToRoute;
      if (final.kind === 'deadline') final = await settled.catch(() => null);
      if (final?.kind === 'fulfilled') await onEpochClose?.(final.value);
    };
    const deadlineTimer = timerResult(remaining, { kind: 'deadline' });
    let outcome = await Promise.race([settled, deadlineTimer.promise]);
    deadlineTimer.clear();
    if (this.#closeRequested || epoch !== this.#closeEpoch) {
      controller.abort();
      await routeOnClose(outcome);
      await settled.catch(() => {});
      this.#closeAbortController.signal.removeEventListener('abort', closeAbort);
      fail('action_journal_close_requested');
    }
    if (outcome.kind !== 'deadline') {
      controller.abort();
      let clockCheckFailed = false;
      let timedOut = false;
      try {
        timedOut = finiteNow(this.#now) >= clock.deadline
          || performance.now() - monotonicStarted >= remaining;
      } catch {
        // Preserve a fulfilled value so callers can deterministically close
        // returned resources even when a hostile clock fails after settlement.
        clockCheckFailed = true;
      }
      if (this.#closeRequested || epoch !== this.#closeEpoch) {
        await routeOnClose(outcome);
        await settled.catch(() => {});
        this.#closeAbortController.signal.removeEventListener('abort', closeAbort);
        fail('action_journal_close_requested');
      }
      this.#closeAbortController.signal.removeEventListener('abort', closeAbort);
      return Object.freeze({
        ...outcome,
        timedOut,
        clockCheckFailed,
      });
    }
    controller.abort();
    const graceTimer = timerResult(this.#settlementGraceMs, { kind: 'grace' });
    outcome = await Promise.race([settled, graceTimer.promise]);
    graceTimer.clear();
    if (this.#closeRequested || epoch !== this.#closeEpoch) {
      await routeOnClose(outcome);
      await settled.catch(() => {});
      this.#closeAbortController.signal.removeEventListener('abort', closeAbort);
      fail('action_journal_close_requested');
    }
    if (outcome.kind === 'grace') {
      this.#setBlocked('action_journal_transport_settlement_unproven');
      outcome = await settled; // Deliberate fail-stop: never return ahead of the injected operation.
      this.#closeAbortController.signal.removeEventListener('abort', closeAbort);
      return Object.freeze({ ...outcome, timedOut: true, missedGrace: true });
    }
    this.#closeAbortController.signal.removeEventListener('abort', closeAbort);
    return Object.freeze({ ...outcome, timedOut: true, missedGrace: false });
  }

  async #settledCall(start, clock) {
    const outcome = await this.#settle(start, clock);
    if (outcome.missedGrace) fail('action_journal_transport_settlement_unproven');
    if (outcome.clockCheckFailed) fail('action_journal_clock_invalid');
    if (outcome.timedOut) fail('action_journal_deadline_expired');
    if (outcome.kind === 'rejected') fail('action_journal_transport_unavailable');
    return outcome.value;
  }

  async #closeRaw(raw, { suppress = false } = {}) {
    const parts = inspectConnection(raw);
    if (parts.transport !== null) {
      try {
        await this.#beginClose({
          connection: { transport: parts.transport },
          key: parts.key,
          responseKey: null,
          decoderBuffer: null,
        });
        return;
      } catch (error) {
        // #beginClose retains the captured connection/key on rejection.
        if (!suppress) throw error;
        return;
      }
    }
    try { if (exactOwnedKey(parts.key)) parts.key.fill(0); }
    catch { if (!suppress) fail('action_journal_close_unproven'); }
  }

  async #runClose(state) {
    const controller = new AbortController();
    const closeDeadline = performance.now() + this.#deadlineMs;
    const settled = Promise.resolve().then(() => state.connection.transport.close({
      signal: controller.signal,
      deadlineAtMs: null,
    })).then(
      value => ({ kind: 'fulfilled', value }),
      error => ({ kind: 'rejected', error }),
    );
    const timeout = timerResult(Math.max(1, closeDeadline - performance.now()), { kind: 'deadline' });
    let outcome = await Promise.race([settled, timeout.promise]);
    timeout.clear();
    if (outcome.kind === 'deadline') {
      controller.abort();
      const grace = timerResult(this.#settlementGraceMs, { kind: 'grace' });
      outcome = await Promise.race([settled, grace.promise]);
      grace.clear();
      if (outcome.kind === 'grace') {
        this.#setBlocked('action_journal_close_unproven');
        // Fail-stop: retain the state and do not return until the one close
        // operation has actually settled.
        outcome = await settled;
      }
    }
    if (outcome.kind === 'rejected') {
      this.#poisoned = true;
      this.#setBlocked('action_journal_close_unproven');
      throw new NativeActionJournalClientError('action_journal_close_unproven');
    }
    let zeroed = true;
    try { state.key?.fill?.(0); } catch { zeroed = false; }
    try { state.responseKey?.fill?.(0); } catch { zeroed = false; }
    try { state.decoderBuffer?.fill?.(0); } catch { zeroed = false; }
    if (!zeroed) {
      this.#poisoned = true;
      this.#setBlocked('action_journal_close_unproven');
      throw new NativeActionJournalClientError('action_journal_close_unproven');
    }
    this.#closingState = null;
    this.#closingPromise = null;
  }

  #beginClose(state) {
    if (this.#closingPromise) return this.#closingPromise;
    this.#closingState = state;
    this.#closingPromise = this.#runClose(state);
    return this.#closingPromise;
  }

  async #establish() {
    if (this.#connections >= NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_connections) fail('action_journal_transport_unavailable');
    if (this.#poisoned || this.#closingState || this.#closingPromise) {
      fail('action_journal_close_unproven');
    }
    await this.#disconnect();
    this.#assertOpenForIo();
    const clock = this.#clock();
    const slot = this.#registerFactorySlot();
    let outcome;
    try {
      outcome = await this.#settle(
        options => this.#connectionFactory(options),
        clock,
        {
          onFulfilled: raw => this.#captureFactoryResult(slot, raw),
          onRejected: () => this.#finishFactoryWithoutResult(slot),
          onEpochClose: () => this.#routeFactoryResult(slot),
        },
      );
    } catch (error) {
      if (!slot.routed) {
        if (slot.raw !== MISSING) await this.#routeFactoryResult(slot).catch(() => {});
        else this.#finishFactoryWithoutResult(slot);
      }
      throw error;
    }
    if (outcome.kind !== 'fulfilled') fail('action_journal_transport_unavailable');
    const raw = slot.raw;
    if (raw === MISSING || slot.parts === null) fail('action_journal_transport_unavailable');
    const parts = slot.parts;
    if (outcome.clockCheckFailed) {
      await this.#routeFactoryResult(slot).catch(() => {});
      fail('action_journal_clock_invalid');
    }
    if (outcome.timedOut) {
      await this.#routeFactoryResult(slot).catch(() => {});
      fail('action_journal_deadline_expired');
    }
    if (!parts.shape || !exactOwnedKey(parts.key)
      || typeof parts.nonce !== 'string'
      || !NONCE.test(parts.nonce)
      || parts.transport === null) {
      await this.#routeFactoryResult(slot).catch(() => {});
      fail('action_journal_transport_unavailable');
    }
    let key;
    try {
      this.#assertOpenForIo();
      key = takeKey(parts.key);
      this.#connection = { key, nonce: parts.nonce, transport: parts.transport };
      this.#requestSequence = 0;
      this.#responses = new ActionJournalProtocolReceiver({ key, kind: 'response', nonce: parts.nonce, now: this.#now });
      this.#decoder = new ActionJournalFrameDecoder();
      this.#connections++;
      this.#adoptFactoryResult(slot);
    } catch (error) {
      if (!slot.routed) await this.#routeFactoryResult(slot).catch(() => {});
      throw error;
    }
    const response = await this.#exchangeOnce('health', null, {});
    if (response.error !== null) fail(`action_journal_${response.error.code}`);
    this.#assertOpenForIo();
    return response.body;
  }

  async #disconnect({ suppress = false } = {}) {
    const current = this.#connection;
    this.#connection = null;
    const currentKey = current?.key ?? null;
    const responseKey = this.#responses?.key ?? null;
    const decoderBuffer = this.#decoder?.buffer ?? null;
    let failed = false;
    if (current) {
      this.#responses = null;
      this.#decoder = null;
      try {
        await this.#beginClose({
          connection: current,
          key: currentKey,
          responseKey,
          decoderBuffer,
        });
      } catch (error) {
        failed = true;
        if (!suppress) throw error;
      }
    } else if (this.#closingPromise) {
      try { await this.#closingPromise; }
      catch (error) {
        failed = true;
        if (!suppress) throw error;
      }
    }
    if (failed) {
      this.#setBlocked('action_journal_close_unproven');
    }
    if (this.#poisoned && !suppress) {
      fail('action_journal_close_unproven');
    }
  }

  async #exchangeOnce(method, operationId, body) {
    if (!this.#connection || !this.#responses || !this.#decoder) fail('action_journal_transport_unavailable');
    const epoch = this.#closeEpoch;
    this.#assertOpenForIo(epoch);
    let clock;
    let request;
    let frame;
    try {
      // Clock/request-id failures happen before a transport call.  Keep them
      // a finite pre-write failure so mutation recovery can never mistake an
      // injected clock/ID exception for a dispatched action.
      clock = this.#clock();
      const requestId = this.#requestIdFactory();
      request = buildRequest({
        requestId,
        sequence: this.#requestSequence,
        nonce: this.#connection.nonce,
        issuedAtMs: clock.issued,
        deadlineAtMs: clock.deadline,
        method,
        operationId,
        body,
      });
      frame = encodeEnvelope(request, this.#connection.key);
      this.#responses.expectResponse(request);
    } catch (error) {
      try { frame?.fill(0); } catch { /* bounded cleanup only */ }
      if (error instanceof ActionJournalProtocolError) fail('action_journal_invalid_request');
      fail('action_journal_prewrite_invalid');
    }
    this.#requestSequence++;
    let responseBytes = 0;
    let writeStarted = false;
    try {
      const write = await this.#settle(options => {
        writeStarted = true;
        return this.#connection.transport.write(frame, options);
      }, clock);
      this.#assertOpenForIo(epoch);
      frame.fill(0);
      frame = null;
      if (write.missedGrace) fail('action_journal_transport_settlement_unproven');
      if (write.clockCheckFailed) throw new AmbiguousExchangeError();
      if (write.kind !== 'fulfilled' || write.timedOut) throw new AmbiguousExchangeError();
      for (let reads = 0; reads < NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_read_chunks_per_response; reads++) {
        this.#assertOpenForIo(epoch);
        const read = await this.#settle(options => this.#connection.transport.read(options), clock);
        this.#assertOpenForIo(epoch);
        if (read.missedGrace) fail('action_journal_transport_settlement_unproven');
        if (read.clockCheckFailed) throw new AmbiguousExchangeError();
        if (read.kind !== 'fulfilled' || read.timedOut) throw new AmbiguousExchangeError();
        const chunk = read.value;
        if (!(chunk instanceof Uint8Array)
          || (typeof SharedArrayBuffer === 'function' && chunk.buffer instanceof SharedArrayBuffer)
          || !(chunk.buffer instanceof ArrayBuffer)
          || chunk.byteLength === 0
          || chunk.byteLength > NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_response_bytes - responseBytes) {
          throw new AmbiguousExchangeError();
        }
        responseBytes += chunk.byteLength;
        const frames = this.#decoder.push(chunk);
        if (frames.length === 0) continue;
        if (frames.length !== 1 || this.#decoder.buffer.length !== 0) throw new AmbiguousExchangeError();
        const response = this.#responses.receive(frames[0]);
        // Authentication and receipt parsing are complete, but a reentrant
        // receiver callback may have requested close during that work.
        this.#assertOpenForIo(epoch);
        return response;
      }
    } catch (error) {
      if (frame) frame.fill(0);
      if (error instanceof NativeActionJournalClientError
        && error.code === 'action_journal_close_requested') throw error;
      if (error instanceof NativeActionJournalClientError
        && error.code === 'action_journal_transport_settlement_unproven') throw error;
      if (!writeStarted && error instanceof NativeActionJournalClientError
        && ['action_journal_clock_invalid', 'action_journal_deadline_expired'].includes(error.code)) {
        fail('action_journal_prewrite_invalid');
      }
      throw new AmbiguousExchangeError();
    }
    throw new AmbiguousExchangeError();
  }

  async #mutate(method, operationId, body, prior) {
    this.#assertOpenForIo();
    const expected = method === 'prepare' ? null : expectedTransition(method, operationId, prior, body);
    let response;
    try { response = await this.#exchangeOnce(method, operationId, body); }
    catch (error) {
      if (!(error instanceof AmbiguousExchangeError)) throw error;
      return this.#recoverLostMutation(method, operationId, body, expected);
    }
    if (response.error !== null) {
      if (response.error.code === 'commit_non_cancellable') {
        return this.#recoverLostMutation(method, operationId, body, expected);
      }
      fail(`action_journal_${response.error.code}`);
    }
    this.#assertOpenForIo();
    const receipt = cloneReceipt(response.body.receipt);
    if (method === 'prepare') {
      if (receipt.sequence !== 0 || receipt.state !== 'prepared'
        || receipt.authorization_kind !== null || receipt.resolution !== null
        || receipt.receipt_digest !== prepareReceiptDigest(body)) fail('action_journal_invalid_response');
    } else if (!receiptMatchesEvent(receipt, expected.event)) fail('action_journal_invalid_response');
    this.#acceptReceipt(receipt);
    this.#assertOpenForIo();
    return receipt;
  }

  async #recoverLostMutation(method, operationId, body, expected) {
    const epoch = this.#closeEpoch;
    try {
      this.#assertOpenForIo(epoch);
      await this.#disconnect();
      this.#assertOpenForIo(epoch);
      const health = await this.#establish();
      this.#assertOpenForIo(epoch);
      if (health.status !== 'ready' || health.platform_available !== true || health.production_enabled !== false) {
        fail('action_journal_transport_unavailable');
      }
      if (method === 'prepare') {
        const retry = await this.#exchangeOnce(method, null, body);
        this.#assertOpenForIo(epoch);
        if (retry.error !== null) fail(`action_journal_${retry.error.code}`);
        const receipt = cloneReceipt(retry.body.receipt);
        if (receipt.sequence !== 0 || receipt.state !== 'prepared'
          || receipt.authorization_kind !== null || receipt.resolution !== null
          || receipt.receipt_digest !== prepareReceiptDigest(body)) fail('action_journal_invalid_response');
        this.#acceptReceipt(receipt);
        this.#assertOpenForIo(epoch);
        return receipt;
      }
      this.#assertOpenForIo(epoch);
      const detail = await this.#detailRaw(exactOperation(operationId), { update: false });
      this.#assertOpenForIo(epoch);
      const finalEvent = detail.events.at(-1);
      if (finalEvent
        && receiptMatchesEvent(detail.receipt, expected.event)
        && journalEventDigest(finalEvent) === expected.event_digest) {
        this.#acceptReceipt(detail.receipt);
        return detail.receipt;
      }
      if (detail.receipt.state === 'unknown_manual') {
        this.#operations.set(operationId, { ...this.#operations.get(operationId), receipt: detail.receipt });
        this.#manualRecovery.add(operationId);
        this.#setBlocked('action_journal_recovery_required');
        fail('action_journal_recovery_required');
      }
      fail('action_journal_commit_unknown');
    } catch (error) {
      if (error instanceof NativeActionJournalClientError && error.code === 'action_journal_close_requested') throw error;
      if (error instanceof NativeActionJournalClientError && error.code === 'action_journal_recovery_required') throw error;
      this.#setBlocked('action_journal_commit_unknown');
      await this.#disconnect({ suppress: true });
      fail('action_journal_commit_unknown');
    }
  }

  #acceptReceipt(receipt) {
    const record = this.#operations.get(receipt.operation_id);
    if (record) this.#operations.set(receipt.operation_id, { ...record, receipt });
    if (TERMINAL.has(receipt.state) && record?.binding !== null && record?.binding !== undefined) {
      if (this.#activeBindings.get(record.binding) === receipt.operation_id) this.#activeBindings.delete(record.binding);
    }
    if (receipt.state === 'unknown_manual') {
      this.#manualRecovery.add(receipt.operation_id);
      this.#setBlocked('action_journal_recovery_required');
    }
  }

  async #allReceipts(includeTerminal) {
    const epoch = this.#closeEpoch;
    const output = [];
    let cursor = null;
    const seen = new Set();
    do {
      const response = await this.#queryExchange('summary', null, {
        cursor,
        include_terminal: includeTerminal,
        limit: ACTION_JOURNAL_LIMITS.max_summary_records,
      });
      if (response.error !== null) fail(`action_journal_${response.error.code}`);
      for (const raw of response.body.records) {
        const receipt = cloneReceipt(raw);
        if (seen.has(receipt.operation_id)) fail('action_journal_invalid_response');
        seen.add(receipt.operation_id);
        output.push(receipt);
        if (output.length > NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_recovery_records) {
          fail('action_journal_record_limit_exceeded');
        }
      }
      cursor = response.body.next_cursor;
      this.#assertOpenForIo(epoch);
    } while (cursor !== null);
    this.#assertOpenForIo(epoch);
    return Object.freeze(output);
  }

  async #detailRaw(operationId, { update = true } = {}) {
    const epoch = this.#closeEpoch;
    const response = await this.#queryExchange('detail', operationId, {
      after_event_digest: null,
      after_sequence: null,
      limit: ACTION_JOURNAL_LIMITS.max_detail_events,
    });
    if (response.error !== null) fail(`action_journal_${response.error.code}`);
    const receipt = cloneReceipt(response.body.receipt);
    const events = Object.freeze(response.body.events.map(cloneEvent));
    if (response.body.truncated || response.body.next_sequence !== null || events.length === 0) {
      fail('action_journal_invalid_response');
    }
    if (update) {
      const record = this.#operations.get(operationId);
      if (record) this.#operations.set(operationId, { ...record, receipt });
    }
    this.#assertOpenForIo(epoch);
    return Object.freeze({ receipt, events });
  }

  async #queryExchange(method, operationId, body) {
    const epoch = this.#closeEpoch;
    try {
      const response = await this.#exchangeOnce(method, operationId, body);
      this.#assertOpenForIo(epoch);
      return response;
    }
    catch (error) {
      if (error instanceof NativeActionJournalClientError && error.code === 'action_journal_close_requested') throw error;
      const code = error instanceof NativeActionJournalClientError
        && error.code === 'action_journal_transport_settlement_unproven'
        ? error.code : 'action_journal_transport_unavailable';
      this.#setBlocked(code);
      await this.#disconnect({ suppress: true });
      fail(code);
    }
  }

  #assertReady() {
    if (this.#closed || this.#health.state !== 'ready') fail(this.#health.error ?? 'action_journal_unavailable');
  }
  #assertQueryable() {
    if (this.#closed || !this.#connection || !['ready', 'blocked'].includes(this.#health.state)) {
      fail(this.#health.error ?? 'action_journal_unavailable');
    }
  }
  #setReady() { this.#health = Object.freeze({ state: 'ready', error: null }); }
  #setBlocked(code) { this.#health = Object.freeze({ state: 'blocked', error: code }); }
  #restoreReadyAfterRecovery() {
    if (this.#manualRecovery.size === 0 && this.#connection && !this.#closed) this.#setReady();
  }
}
