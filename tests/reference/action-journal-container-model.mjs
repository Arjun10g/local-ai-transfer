import { createHash } from 'node:crypto';
import { TextDecoder } from 'node:util';

import {
  ACTION_JOURNAL_PROTOCOL,
  canonicalJson,
  journalEventDigest,
  startupRecovery,
  transitionState,
} from '../../host/agent/action-journal-protocol.mjs';
import { parseStrictJson } from '../../host/agent/tool-envelope.mjs';

/**
 * Test-only, platform-neutral reference model for the v0.1 container layout.
 * It is deliberately located below tests/ and is not imported by the host.
 */
export const ACTION_JOURNAL_CONTAINER_REFERENCE_ONLY = true;
export const ACTION_JOURNAL_CONTAINER_PRODUCTION_AVAILABLE = false;

const KiB = 1024;
const U64_MAX = (1n << 64n) - 1n;
const ZERO_DIGEST = Buffer.alloc(32);
const UTF8_FATAL = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
const OPERATION_ID = /^act_[a-f0-9]{32}$/u;
const TERMINAL_STATES = new Set(['completed', 'cancelled', 'failed_definitive']);
const HEADER_MAGIC = fixedAscii('LAEJRNLCONTAINER', 16);
const BODY_MAGIC = fixedAscii('LAEJRNLBODY', 16);
const MARKER_MAGIC = fixedAscii('LAEJRNLMARKER', 16);
const HEADER_CHECKSUM_OFFSET = 4064;
const MARKER_DIGEST_OFFSET = 112;
const DESCRIPTOR_RESERVED_OFFSET = 144;
const MARKER_RESERVED_OFFSET = 144;
const PROTOCOL_OFFSET = 84;
const PROTOCOL_BYTES = 64;
const CONTAINER_ID_OFFSET = 152;
const EVENT_DATA_OFFSET = 36;
const MARKER_STAGING = 1;
const MARKER_COMMITTED = 2;

export const ACTION_JOURNAL_CONTAINER_LAYOUT = Object.freeze({
  format_version: 1,
  header_bytes: 4 * KiB,
  slot_count: 1024,
  banks_per_slot: 2,
  bank_bytes: 16 * KiB,
  bank_body_bytes: 15 * KiB,
  bank_marker_bytes: 1 * KiB,
  bank_descriptor_bytes: 1 * KiB,
  event_cells_per_bank: 16,
  event_cell_bytes: 896,
  max_event_bytes: 768,
  max_active_records: 256,
  max_terminal_records: 768,
  checksum: 'sha256',
  byte_order: 'little_endian',
});

export const ACTION_JOURNAL_CONTAINER_BYTES = ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes
  + ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count
    * ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot
    * ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes;

const SLOT_BYTES = ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot
  * ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes;

export const ACTION_JOURNAL_CONTAINER_BOUNDARIES = Object.freeze([
  'after_staging_write',
  'after_staging_flush',
  'after_staging_readback',
  'after_body_write',
  'after_body_flush',
  'after_body_readback',
  'after_commit_write',
  'after_commit_flush',
  'after_commit_readback',
]);

export class ActionJournalContainerError extends Error {
  constructor(code) {
    super(code);
    this.name = 'ActionJournalContainerError';
    this.code = code;
  }
}

function fail(code) { throw new ActionJournalContainerError(code); }

function fixedAscii(value, size) {
  const bytes = Buffer.from(value, 'ascii');
  if (bytes.length > size) throw new RangeError('fixed ASCII value is too large');
  const result = Buffer.alloc(size);
  bytes.copy(result);
  return result;
}

function isZero(bytes) {
  for (const byte of bytes) if (byte !== 0) return false;
  return true;
}

function exactBytes(left, right) {
  return Buffer.isBuffer(left)
    && Buffer.isBuffer(right)
    && left.length === right.length
    && left.equals(right);
}

function checkedInteger(value, minimum, maximum, code = 'container_invalid_argument') {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail(code);
  return value;
}

function checkedGeneration(value) {
  if (typeof value !== 'bigint' || value < 0n || value > U64_MAX) fail('container_invalid_generation');
  return value;
}

function checkedRange(offset, length, size) {
  checkedInteger(offset, 0, size);
  checkedInteger(length, 0, size);
  const end = offset + length;
  if (!Number.isSafeInteger(end) || end > size) fail('container_out_of_bounds');
  return end;
}

function checkedContainerId(value) {
  if (!(value instanceof Uint8Array) || value.byteLength !== 32 || isZero(value)) {
    fail('container_invalid_argument');
  }
  return Buffer.from(value);
}

function checkedOperationId(value) {
  if (typeof value !== 'string' || !OPERATION_ID.test(value)) fail('container_invalid_operation');
  return value;
}

function sha256(...parts) {
  const hash = createHash('sha256');
  for (const part of parts) hash.update(part);
  return hash.digest();
}

function domainDigest(label, containerId, bytes) {
  return sha256(
    Buffer.from('lae.action-journal.container.v0.1.0\0', 'utf8'),
    Buffer.from(`${label}\0`, 'utf8'),
    containerId,
    bytes,
  );
}

function writeU64(buffer, offset, value) {
  buffer.writeBigUInt64LE(checkedGeneration(value), offset);
}

function readU64(buffer, offset) {
  return buffer.readBigUInt64LE(offset);
}

export function slotOffset(slotIndex) {
  checkedInteger(slotIndex, 0, ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count - 1);
  return ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes + slotIndex * SLOT_BYTES;
}

export function bankOffset(slotIndex, bankIndex) {
  checkedInteger(bankIndex, 0, ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot - 1);
  const offset = slotOffset(slotIndex) + bankIndex * ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes;
  checkedRange(offset, ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes, ACTION_JOURNAL_CONTAINER_BYTES);
  return offset;
}

export function markerOffset(slotIndex, bankIndex) {
  return bankOffset(slotIndex, bankIndex) + ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes;
}

function encodeHeader(containerId) {
  const header = Buffer.alloc(ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes);
  HEADER_MAGIC.copy(header, 0);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.format_version, 16);
  header.writeUInt32LE(0x01020304, 20);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes, 24);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count, 28);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot, 32);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes, 36);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes, 40);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes, 44);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.bank_descriptor_bytes, 48);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank, 52);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes, 56);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.max_event_bytes, 60);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.max_active_records, 64);
  header.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.max_terminal_records, 68);
  writeU64(header, 72, BigInt(ACTION_JOURNAL_CONTAINER_BYTES));
  const protocol = Buffer.from(ACTION_JOURNAL_PROTOCOL, 'ascii');
  header.writeUInt32LE(protocol.length, 80);
  protocol.copy(header, PROTOCOL_OFFSET);
  header.writeUInt32LE(1, 148); // SHA-256
  containerId.copy(header, CONTAINER_ID_OFFSET);
  domainDigest('header', containerId, header.subarray(0, HEADER_CHECKSUM_OFFSET))
    .copy(header, HEADER_CHECKSUM_OFFSET);
  return header;
}

function decodeHeader(header, deviceSize) {
  if (!Buffer.isBuffer(header) || header.length !== ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes) {
    fail('container_corrupt_header');
  }
  if (!header.subarray(0, 16).equals(HEADER_MAGIC)) fail('container_unformatted');
  const expectedFields = [
    [16, ACTION_JOURNAL_CONTAINER_LAYOUT.format_version],
    [20, 0x01020304],
    [24, ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes],
    [28, ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count],
    [32, ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot],
    [36, ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes],
    [40, ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes],
    [44, ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes],
    [48, ACTION_JOURNAL_CONTAINER_LAYOUT.bank_descriptor_bytes],
    [52, ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank],
    [56, ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes],
    [60, ACTION_JOURNAL_CONTAINER_LAYOUT.max_event_bytes],
    [64, ACTION_JOURNAL_CONTAINER_LAYOUT.max_active_records],
    [68, ACTION_JOURNAL_CONTAINER_LAYOUT.max_terminal_records],
    [148, 1],
  ];
  for (const [offset, expected] of expectedFields) {
    if (header.readUInt32LE(offset) !== expected) fail('container_corrupt_header');
  }
  if (readU64(header, 72) !== BigInt(ACTION_JOURNAL_CONTAINER_BYTES) || deviceSize !== ACTION_JOURNAL_CONTAINER_BYTES) {
    fail('container_size_mismatch');
  }
  const protocolLength = header.readUInt32LE(80);
  const protocol = header.subarray(PROTOCOL_OFFSET, PROTOCOL_OFFSET + protocolLength);
  if (
    protocolLength !== Buffer.byteLength(ACTION_JOURNAL_PROTOCOL, 'ascii')
    || !protocol.equals(Buffer.from(ACTION_JOURNAL_PROTOCOL, 'ascii'))
    || !isZero(header.subarray(PROTOCOL_OFFSET + protocolLength, PROTOCOL_OFFSET + PROTOCOL_BYTES))
  ) fail('container_corrupt_header');
  const containerId = header.subarray(CONTAINER_ID_OFFSET, CONTAINER_ID_OFFSET + 32);
  if (isZero(containerId) || !isZero(header.subarray(CONTAINER_ID_OFFSET + 32, HEADER_CHECKSUM_OFFSET))) {
    fail('container_corrupt_header');
  }
  const expected = domainDigest('header', containerId, header.subarray(0, HEADER_CHECKSUM_OFFSET));
  if (!header.subarray(HEADER_CHECKSUM_OFFSET).equals(expected)) fail('container_corrupt_header');
  return Buffer.from(containerId);
}

function encodeMarker({
  state,
  slotIndex,
  bankIndex,
  generation,
  previousGeneration,
  bodyDigest,
  previousCommitDigest,
  containerId,
}) {
  const marker = Buffer.alloc(ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes);
  MARKER_MAGIC.copy(marker, 0);
  marker.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.format_version, 16);
  marker.writeUInt32LE(state, 20);
  marker.writeUInt32LE(slotIndex, 24);
  marker.writeUInt32LE(bankIndex, 28);
  writeU64(marker, 32, generation);
  writeU64(marker, 40, previousGeneration);
  bodyDigest.copy(marker, 48);
  previousCommitDigest.copy(marker, 80);
  const digest = domainDigest('bank-marker', containerId, marker);
  digest.copy(marker, MARKER_DIGEST_OFFSET);
  return marker;
}

function decodeMarker(marker, { slotIndex, bankIndex, containerId }) {
  if (!Buffer.isBuffer(marker) || marker.length !== ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes) {
    fail('container_corrupt_bank');
  }
  if (isZero(marker)) return Object.freeze({ state: 'unused' });
  if (
    !marker.subarray(0, 16).equals(MARKER_MAGIC)
    || marker.readUInt32LE(16) !== ACTION_JOURNAL_CONTAINER_LAYOUT.format_version
    || marker.readUInt32LE(24) !== slotIndex
    || marker.readUInt32LE(28) !== bankIndex
    || !isZero(marker.subarray(MARKER_RESERVED_OFFSET))
  ) fail('container_corrupt_bank');
  const stateCode = marker.readUInt32LE(20);
  if (![MARKER_STAGING, MARKER_COMMITTED].includes(stateCode)) fail('container_corrupt_bank');
  const actualDigest = marker.subarray(MARKER_DIGEST_OFFSET, MARKER_DIGEST_OFFSET + 32);
  const digestInput = Buffer.from(marker);
  digestInput.fill(0, MARKER_DIGEST_OFFSET, MARKER_DIGEST_OFFSET + 32);
  const expectedDigest = domainDigest('bank-marker', containerId, digestInput);
  if (!actualDigest.equals(expectedDigest)) fail('container_corrupt_bank');
  const bodyDigest = Buffer.from(marker.subarray(48, 80));
  if (stateCode === MARKER_STAGING && !bodyDigest.equals(ZERO_DIGEST)) fail('container_corrupt_bank');
  if (stateCode === MARKER_COMMITTED && bodyDigest.equals(ZERO_DIGEST)) fail('container_corrupt_bank');
  return Object.freeze({
    state: stateCode === MARKER_STAGING ? 'staging' : 'committed',
    generation: readU64(marker, 32),
    previousGeneration: readU64(marker, 40),
    bodyDigest,
    previousCommitDigest: Buffer.from(marker.subarray(80, 112)),
    commitDigest: Buffer.from(actualDigest),
  });
}

function encodeCanonicalEvent(event) {
  let text;
  let digestHex;
  try {
    text = canonicalJson(event);
    digestHex = journalEventDigest(event);
  } catch {
    fail('container_invalid_event');
  }
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length < 2 || bytes.length > ACTION_JOURNAL_CONTAINER_LAYOUT.max_event_bytes) {
    fail('container_event_limit');
  }
  return Object.freeze({ event: structuredClone(event), bytes, digest: Buffer.from(digestHex, 'hex') });
}

function validateHistory(events) {
  if (!Array.isArray(events) || events.length < 1 || events.length > ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank) {
    fail('container_event_limit');
  }
  const encoded = events.map(encodeCanonicalEvent);
  for (let index = 0; index < encoded.length; index++) {
    const current = encoded[index].event;
    if (current.sequence !== index) fail('container_invalid_event');
    if (index === 0) {
      if (
        current.action !== 'prepare'
        || current.state !== 'prepared'
        || current.authorization_kind !== null
        || current.resolution !== null
      ) fail('container_invalid_event');
      continue;
    }
    const previous = encoded[index - 1].event;
    let expected;
    try {
      if (current.action === 'startup_recovery') {
        const recovered = startupRecovery(previous.state);
        expected = recovered.state;
        if (current.resolution !== recovered.resolution) fail('container_invalid_event');
      } else {
        expected = transitionState(previous.state, current.action, current.resolution);
      }
    } catch {
      fail('container_invalid_event');
    }
    if (current.state !== expected) fail('container_invalid_event');
    if (current.action === 'authorize') {
      if (previous.authorization_kind !== null || current.authorization_kind === null) fail('container_invalid_event');
    } else if (current.authorization_kind !== previous.authorization_kind) {
      fail('container_invalid_event');
    }
  }
  return encoded;
}

function eventsDigest(encoded, containerId) {
  const framed = [];
  for (const item of encoded) {
    const length = Buffer.alloc(4);
    length.writeUInt32LE(item.bytes.length);
    framed.push(length, item.digest, item.bytes);
  }
  return domainDigest('events', containerId, Buffer.concat(framed));
}

function encodeBody({ slotIndex, bankIndex, generation, operationId, events, containerId }) {
  const encoded = validateHistory(events);
  const body = Buffer.alloc(ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes);
  BODY_MAGIC.copy(body, 0);
  body.writeUInt32LE(ACTION_JOURNAL_CONTAINER_LAYOUT.format_version, 16);
  body.writeUInt32LE(slotIndex, 20);
  body.writeUInt32LE(bankIndex, 24);
  body.writeUInt32LE(encoded.length, 28);
  writeU64(body, 32, generation);
  Buffer.from(checkedOperationId(operationId), 'ascii').copy(body, 40);
  body.writeUInt32LE(encoded.reduce((total, item) => total + item.bytes.length, 0), 76);
  eventsDigest(encoded, containerId).copy(body, 80);
  encoded.at(-1).digest.copy(body, 112);
  encoded.forEach((item, index) => {
    const offset = ACTION_JOURNAL_CONTAINER_LAYOUT.bank_descriptor_bytes
      + index * ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes;
    body.writeUInt32LE(item.bytes.length, offset);
    item.digest.copy(body, offset + 4);
    item.bytes.copy(body, offset + EVENT_DATA_OFFSET);
  });
  return Object.freeze({ body, encoded, bodyDigest: domainDigest('bank-body', containerId, body) });
}

function decodeBody(body, { slotIndex, bankIndex, marker, containerId }) {
  if (
    !Buffer.isBuffer(body)
    || body.length !== ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes
    || !body.subarray(0, 16).equals(BODY_MAGIC)
    || body.readUInt32LE(16) !== ACTION_JOURNAL_CONTAINER_LAYOUT.format_version
    || body.readUInt32LE(20) !== slotIndex
    || body.readUInt32LE(24) !== bankIndex
    || readU64(body, 32) !== marker.generation
    || !isZero(body.subarray(DESCRIPTOR_RESERVED_OFFSET, ACTION_JOURNAL_CONTAINER_LAYOUT.bank_descriptor_bytes))
    || !domainDigest('bank-body', containerId, body).equals(marker.bodyDigest)
  ) fail('container_corrupt_bank');
  const eventCount = body.readUInt32LE(28);
  if (eventCount < 1 || eventCount > ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank) {
    fail('container_corrupt_bank');
  }
  let operationId;
  try { operationId = checkedOperationId(UTF8_FATAL.decode(body.subarray(40, 76))); } catch { fail('container_corrupt_bank'); }
  const decoded = [];
  let usedBytes = 0;
  for (let index = 0; index < ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank; index++) {
    const offset = ACTION_JOURNAL_CONTAINER_LAYOUT.bank_descriptor_bytes
      + index * ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes;
    const cell = body.subarray(offset, offset + ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes);
    if (index >= eventCount) {
      if (!isZero(cell)) fail('container_corrupt_bank');
      continue;
    }
    const length = cell.readUInt32LE(0);
    if (length < 2 || length > ACTION_JOURNAL_CONTAINER_LAYOUT.max_event_bytes) fail('container_corrupt_bank');
    const bytes = cell.subarray(EVENT_DATA_OFFSET, EVENT_DATA_OFFSET + length);
    if (!isZero(cell.subarray(EVENT_DATA_OFFSET + length))) fail('container_corrupt_bank');
    let text;
    let event;
    try {
      text = UTF8_FATAL.decode(bytes);
      event = parseStrictJson(text, {
        maxBytes: ACTION_JOURNAL_CONTAINER_LAYOUT.max_event_bytes,
        maxDepth: 4,
        maxString: 128,
        maxArray: 0,
        maxObject: 6,
      });
      if (canonicalJson(event) !== text) fail('container_corrupt_bank');
    } catch {
      fail('container_corrupt_bank');
    }
    let digest;
    try { digest = Buffer.from(journalEventDigest(event), 'hex'); } catch { fail('container_corrupt_bank'); }
    if (!digest.equals(cell.subarray(4, 36))) fail('container_corrupt_bank');
    usedBytes += length;
    decoded.push(Object.freeze({ event, bytes: Buffer.from(bytes), digest }));
  }
  let verified;
  try { verified = validateHistory(decoded.map(item => item.event)); } catch { fail('container_corrupt_bank'); }
  if (
    body.readUInt32LE(76) !== usedBytes
    || !eventsDigest(verified, containerId).equals(body.subarray(80, 112))
    || !verified.at(-1).digest.equals(body.subarray(112, 144))
  ) fail('container_corrupt_bank');
  return Object.freeze({
    operationId,
    events: Object.freeze(decoded.map(item => Object.freeze(structuredClone(item.event)))),
    generation: marker.generation,
    state: decoded.at(-1).event.state,
  });
}

function validatePredecessor(marker) {
  if (marker.generation === 0n) {
    if (marker.previousGeneration !== U64_MAX || !marker.previousCommitDigest.equals(ZERO_DIGEST)) {
      fail('container_conflicting_authority');
    }
  } else if (marker.previousGeneration !== marker.generation - 1n || marker.previousCommitDigest.equals(ZERO_DIGEST)) {
    fail('container_conflicting_authority');
  }
}

/** A bounded sparse byte device. Writes are volatile until flush(). */
export class InMemoryJournalBlockDevice {
  constructor(size = ACTION_JOURNAL_CONTAINER_BYTES) {
    this.size = checkedInteger(size, 1, Number.MAX_SAFE_INTEGER, 'container_size_mismatch');
    this.durable = [];
    this.volatile = [];
    this.nextFlushPrefix = null;
  }

  write(offset, bytes) {
    if (!(bytes instanceof Uint8Array)) fail('container_invalid_argument');
    const value = Buffer.from(bytes);
    checkedRange(offset, value.length, this.size);
    this.volatile.push(Object.freeze({ offset, bytes: value }));
  }

  read(offset, length, { durableOnly = false } = {}) {
    checkedRange(offset, length, this.size);
    const result = Buffer.alloc(length);
    const apply = segment => {
      const start = Math.max(offset, segment.offset);
      const end = Math.min(offset + length, segment.offset + segment.bytes.length);
      if (start < end) segment.bytes.copy(result, start - offset, start - segment.offset, end - segment.offset);
    };
    this.durable.forEach(apply);
    if (!durableOnly) this.volatile.forEach(apply);
    return result;
  }

  failNextFlushAfter(byteCount) {
    if (this.nextFlushPrefix !== null) fail('container_invalid_argument');
    this.nextFlushPrefix = checkedInteger(byteCount, 0, this.size);
  }

  flush() {
    let remaining = this.nextFlushPrefix;
    const torn = remaining !== null;
    const pendingBytes = this.volatile.reduce((sum, item) => sum + item.bytes.length, 0);
    if (torn && remaining > pendingBytes) {
      this.nextFlushPrefix = null;
      fail('container_invalid_argument');
    }
    for (const segment of this.volatile) {
      const length = torn ? Math.min(remaining, segment.bytes.length) : segment.bytes.length;
      if (length > 0) this.durable.push(Object.freeze({ offset: segment.offset, bytes: Buffer.from(segment.bytes.subarray(0, length)) }));
      if (torn) remaining -= length;
    }
    this.volatile = [];
    this.nextFlushPrefix = null;
    if (torn) fail('container_simulated_power_loss');
  }

  crash() { this.volatile = []; }

  corruptDurable(offset, bytes) {
    if (!(bytes instanceof Uint8Array)) fail('container_invalid_argument');
    const value = Buffer.from(bytes);
    checkedRange(offset, value.length, this.size);
    this.durable.push(Object.freeze({ offset, bytes: value }));
  }

  clone() {
    const copy = new InMemoryJournalBlockDevice(this.size);
    copy.durable = this.durable.map(segment => Object.freeze({ offset: segment.offset, bytes: Buffer.from(segment.bytes) }));
    return copy;
  }
}

async function boundary(callback, phase, context) {
  if (callback !== undefined) {
    if (typeof callback !== 'function') fail('container_invalid_argument');
    await callback(phase, Object.freeze({ ...context }));
  }
}

function validateBoundaryCallback(callback) {
  if (callback !== undefined && typeof callback !== 'function') fail('container_invalid_argument');
}

function assertBlankDevice(device) {
  const chunkBytes = 64 * KiB;
  for (let offset = 0; offset < device.size; offset += chunkBytes) {
    if (!isZero(device.read(offset, Math.min(chunkBytes, device.size - offset)))) fail('container_already_initialized');
  }
}

export async function formatJournalContainer(device, { containerId, onBoundary } = {}) {
  if (!(device instanceof InMemoryJournalBlockDevice) || device.size !== ACTION_JOURNAL_CONTAINER_BYTES) {
    fail('container_size_mismatch');
  }
  validateBoundaryCallback(onBoundary);
  const id = checkedContainerId(containerId);
  assertBlankDevice(device);
  const header = encodeHeader(id);
  device.write(0, header);
  await boundary(onBoundary, 'after_header_write', {});
  device.flush();
  await boundary(onBoundary, 'after_header_flush', {});
  if (!device.read(0, header.length, { durableOnly: true }).equals(header)) fail('container_readback_failed');
  await boundary(onBoundary, 'after_header_readback', {});
  return Object.freeze({ container_bytes: ACTION_JOURNAL_CONTAINER_BYTES, container_id: id.toString('hex') });
}

function chooseAuthority(banks) {
  const committed = banks.filter(item => item.marker.state === 'committed');
  const staging = banks.filter(item => item.marker.state === 'staging');
  if (committed.length === 2) {
    if (committed[0].marker.generation === committed[1].marker.generation) fail('container_conflicting_authority');
    const high = committed[0].marker.generation > committed[1].marker.generation ? committed[0] : committed[1];
    const low = high === committed[0] ? committed[1] : committed[0];
    if (
      high.marker.generation !== low.marker.generation + 1n
      || high.marker.previousGeneration !== low.marker.generation
      || !high.marker.previousCommitDigest.equals(low.marker.commitDigest)
      || high.record.operationId !== low.record.operationId
      || high.record.events.length !== low.record.events.length + 1
    ) fail('container_conflicting_authority');
    for (let index = 0; index < low.record.events.length; index++) {
      if (canonicalJson(low.record.events[index]) !== canonicalJson(high.record.events[index])) {
        fail('container_conflicting_authority');
      }
    }
    return high;
  }
  if (committed.length === 1) {
    const active = committed[0];
    if (staging.length === 1) {
      const pending = staging[0].marker;
      if (
        pending.generation !== active.marker.generation + 1n
        || pending.previousGeneration !== active.marker.generation
        || !pending.previousCommitDigest.equals(active.marker.commitDigest)
      ) fail('container_conflicting_authority');
    } else if (active.marker.generation !== 0n) {
      fail('container_conflicting_authority');
    }
    return active;
  }
  if (staging.length > 1) fail('container_conflicting_authority');
  if (staging.length === 1) {
    const pending = staging[0].marker;
    if (pending.generation !== 0n || pending.previousGeneration !== U64_MAX || !pending.previousCommitDigest.equals(ZERO_DIGEST)) {
      fail('container_conflicting_authority');
    }
  }
  return null;
}

export class ActionJournalContainerReference {
  static open(device) {
    if (!(device instanceof InMemoryJournalBlockDevice) || device.size !== ACTION_JOURNAL_CONTAINER_BYTES) {
      fail('container_size_mismatch');
    }
    const header = device.read(0, ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes, { durableOnly: true });
    const containerId = decodeHeader(header, device.size);
    const slots = [];
    const records = new Map();
    let activeCount = 0;
    let terminalCount = 0;
    for (let slotIndex = 0; slotIndex < ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count; slotIndex++) {
      const banks = [];
      for (let bankIndex = 0; bankIndex < ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot; bankIndex++) {
        const markerBytes = device.read(
          markerOffset(slotIndex, bankIndex),
          ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes,
          { durableOnly: true },
        );
        const marker = decodeMarker(markerBytes, { slotIndex, bankIndex, containerId });
        if (marker.state === 'unused') {
          const body = device.read(
            bankOffset(slotIndex, bankIndex),
            ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes,
            { durableOnly: true },
          );
          if (!isZero(body)) fail('container_corrupt_bank');
          banks.push(Object.freeze({ bankIndex, marker, record: null }));
        } else if (marker.state === 'staging') {
          validatePredecessor(marker);
          banks.push(Object.freeze({ bankIndex, marker, record: null }));
        } else {
          validatePredecessor(marker);
          const body = device.read(
            bankOffset(slotIndex, bankIndex),
            ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes,
            { durableOnly: true },
          );
          const record = decodeBody(body, { slotIndex, bankIndex, marker, containerId });
          banks.push(Object.freeze({ bankIndex, marker, record }));
        }
      }
      const authority = chooseAuthority(banks);
      slots.push(Object.freeze({ banks, authority }));
      if (authority !== null) {
        if (records.has(authority.record.operationId)) fail('container_duplicate_operation');
        const value = Object.freeze({ slotIndex, bankIndex: authority.bankIndex, marker: authority.marker, ...authority.record });
        records.set(value.operationId, value);
        if (TERMINAL_STATES.has(value.state)) terminalCount++; else activeCount++;
      }
    }
    if (activeCount > ACTION_JOURNAL_CONTAINER_LAYOUT.max_active_records) fail('container_active_limit');
    if (terminalCount > ACTION_JOURNAL_CONTAINER_LAYOUT.max_terminal_records) fail('container_terminal_limit');
    return new ActionJournalContainerReference(device, containerId, slots, records, activeCount, terminalCount);
  }

  constructor(device, containerId, slots, records, activeCount, terminalCount) {
    this.device = device;
    this.containerId = Buffer.from(containerId);
    this.slots = slots;
    this.records = records;
    this.activeCount = activeCount;
    this.terminalCount = terminalCount;
  }

  summary() {
    return Object.freeze({
      active: this.activeCount,
      terminal: this.terminalCount,
      total: this.records.size,
      records: Object.freeze([...this.records.values()].map(record => Object.freeze({
        operation_id: record.operationId,
        slot_index: record.slotIndex,
        bank_index: record.bankIndex,
        generation: record.generation.toString(),
        event_count: record.events.length,
        state: record.state,
      }))),
    });
  }

  detail(operationId) {
    checkedOperationId(operationId);
    const record = this.records.get(operationId);
    if (!record) fail('container_not_found');
    return Object.freeze({
      operation_id: operationId,
      generation: record.generation,
      events: Object.freeze(record.events.map(event => Object.freeze(structuredClone(event)))),
    });
  }

  async append(operationId, event, { onBoundary } = {}) {
    validateBoundaryCallback(onBoundary);
    checkedOperationId(operationId);
    const current = this.records.get(operationId) ?? null;
    const events = current === null ? [event] : [...current.events, event];
    const encoded = validateHistory(events);
    if (current === null && encoded.length !== 1) fail('container_invalid_event');
    if (current !== null && encoded.length !== current.events.length + 1) fail('container_invalid_event');
    if (encoded.length > ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank) fail('container_event_limit');

    const nextState = encoded.at(-1).event.state;
    let projectedActive = this.activeCount;
    let projectedTerminal = this.terminalCount;
    if (current === null) {
      if (TERMINAL_STATES.has(nextState)) projectedTerminal++; else projectedActive++;
    } else if (!TERMINAL_STATES.has(current.state) && TERMINAL_STATES.has(nextState)) {
      projectedActive--;
      projectedTerminal++;
    }
    if (projectedActive > ACTION_JOURNAL_CONTAINER_LAYOUT.max_active_records) fail('container_active_limit');
    if (projectedTerminal > ACTION_JOURNAL_CONTAINER_LAYOUT.max_terminal_records) fail('container_terminal_limit');

    let slotIndex = current?.slotIndex;
    if (slotIndex === undefined) {
      slotIndex = this.slots.findIndex(slot => slot.authority === null);
      if (slotIndex < 0) fail('container_record_limit');
    }
    const currentGeneration = current?.generation ?? null;
    if (currentGeneration === U64_MAX) fail('container_generation_exhausted');
    const generation = currentGeneration === null ? 0n : currentGeneration + 1n;
    const bankIndex = current === null
      ? (this.slots[slotIndex].banks.find(bank => bank.marker.state === 'staging')?.bankIndex ?? 0)
      : 1 - current.bankIndex;
    const previousGeneration = current?.generation ?? U64_MAX;
    const previousCommitDigest = current?.marker.commitDigest ?? ZERO_DIGEST;
    const context = { slot_index: slotIndex, bank_index: bankIndex, generation: generation.toString() };
    const staging = encodeMarker({
      state: MARKER_STAGING,
      slotIndex,
      bankIndex,
      generation,
      previousGeneration,
      bodyDigest: ZERO_DIGEST,
      previousCommitDigest,
      containerId: this.containerId,
    });
    const bodyValue = encodeBody({
      slotIndex,
      bankIndex,
      generation,
      operationId,
      events: encoded.map(item => item.event),
      containerId: this.containerId,
    });
    const committed = encodeMarker({
      state: MARKER_COMMITTED,
      slotIndex,
      bankIndex,
      generation,
      previousGeneration,
      bodyDigest: bodyValue.bodyDigest,
      previousCommitDigest,
      containerId: this.containerId,
    });

    this.device.write(markerOffset(slotIndex, bankIndex), staging);
    await boundary(onBoundary, 'after_staging_write', context);
    this.device.flush();
    await boundary(onBoundary, 'after_staging_flush', context);
    if (!this.device.read(markerOffset(slotIndex, bankIndex), staging.length, { durableOnly: true }).equals(staging)) {
      fail('container_readback_failed');
    }
    await boundary(onBoundary, 'after_staging_readback', context);

    this.device.write(bankOffset(slotIndex, bankIndex), bodyValue.body);
    await boundary(onBoundary, 'after_body_write', context);
    this.device.flush();
    await boundary(onBoundary, 'after_body_flush', context);
    const bodyReadback = this.device.read(
      bankOffset(slotIndex, bankIndex),
      bodyValue.body.length,
      { durableOnly: true },
    );
    if (!bodyReadback.equals(bodyValue.body)) {
      fail('container_readback_failed');
    }
    const expectedMarker = decodeMarker(committed, { slotIndex, bankIndex, containerId: this.containerId });
    decodeBody(bodyReadback, { slotIndex, bankIndex, marker: expectedMarker, containerId: this.containerId });
    await boundary(onBoundary, 'after_body_readback', context);

    this.device.write(markerOffset(slotIndex, bankIndex), committed);
    await boundary(onBoundary, 'after_commit_write', context);
    this.device.flush();
    await boundary(onBoundary, 'after_commit_flush', context);
    const markerReadback = this.device.read(
      markerOffset(slotIndex, bankIndex),
      committed.length,
      { durableOnly: true },
    );
    if (!markerReadback.equals(committed)) {
      fail('container_readback_failed');
    }
    const marker = decodeMarker(markerReadback, { slotIndex, bankIndex, containerId: this.containerId });
    const committedBodyReadback = this.device.read(
      bankOffset(slotIndex, bankIndex),
      bodyValue.body.length,
      { durableOnly: true },
    );
    const verified = decodeBody(committedBodyReadback, {
      slotIndex,
      bankIndex,
      marker,
      containerId: this.containerId,
    });
    await boundary(onBoundary, 'after_commit_readback', context);

    const record = Object.freeze({ slotIndex, bankIndex, marker, ...verified });
    this.records.set(operationId, record);
    this.activeCount = projectedActive;
    this.terminalCount = projectedTerminal;
    return this.detail(operationId);
  }
}

/** Raw layout seeding for adversarial tests only; never a product write path. */
export function seedBankForTest(device, {
  containerId,
  slotIndex,
  bankIndex,
  generation,
  previousGeneration = generation === 0n ? U64_MAX : generation - 1n,
  previousCommitDigest = generation === 0n ? ZERO_DIGEST : Buffer.alloc(32, 0x55),
  operationId,
  events,
  state = 'committed',
} = {}) {
  if (!(device instanceof InMemoryJournalBlockDevice)) fail('container_invalid_argument');
  const id = checkedContainerId(containerId);
  checkedInteger(slotIndex, 0, ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count - 1);
  checkedInteger(bankIndex, 0, 1);
  checkedGeneration(generation);
  checkedGeneration(previousGeneration);
  if (!(previousCommitDigest instanceof Uint8Array) || previousCommitDigest.byteLength !== 32) fail('container_invalid_argument');
  const bodyValue = encodeBody({ slotIndex, bankIndex, generation, operationId, events, containerId: id });
  const marker = encodeMarker({
    state: state === 'staging' ? MARKER_STAGING : MARKER_COMMITTED,
    slotIndex,
    bankIndex,
    generation,
    previousGeneration,
    bodyDigest: state === 'staging' ? ZERO_DIGEST : bodyValue.bodyDigest,
    previousCommitDigest: Buffer.from(previousCommitDigest),
    containerId: id,
  });
  device.write(bankOffset(slotIndex, bankIndex), bodyValue.body);
  device.write(markerOffset(slotIndex, bankIndex), marker);
  device.flush();
  return Object.freeze({ commitDigest: Buffer.from(marker.subarray(MARKER_DIGEST_OFFSET, MARKER_DIGEST_OFFSET + 32)), body: bodyValue.body, marker });
}

export const ACTION_JOURNAL_CONTAINER_TEST_CONSTANTS = Object.freeze({
  u64_max: U64_MAX,
  marker_staging: MARKER_STAGING,
  marker_committed: MARKER_COMMITTED,
});
