import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ACTION_JOURNAL_LIMITS,
  PRODUCTION_ACTION_JOURNAL_PROTOCOL_AVAILABLE,
  journalEventDigest,
} from '../../host/agent/action-journal-protocol.mjs';
import {
  ACTION_JOURNAL_CONTAINER_BOUNDARIES,
  ACTION_JOURNAL_CONTAINER_BYTES,
  ACTION_JOURNAL_CONTAINER_LAYOUT,
  ACTION_JOURNAL_CONTAINER_PRODUCTION_AVAILABLE,
  ACTION_JOURNAL_CONTAINER_REFERENCE_ONLY,
  ACTION_JOURNAL_CONTAINER_TEST_CONSTANTS,
  ActionJournalContainerReference,
  InMemoryJournalBlockDevice,
  bankOffset,
  formatJournalContainer,
  markerOffset,
  seedBankForTest,
  slotOffset,
} from '../reference/action-journal-container-model.mjs';

const CONTAINER_ID = Buffer.from('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'hex');
const RECEIPT = 'f'.repeat(64);
const OTHER_RECEIPT = 'e'.repeat(64);
const { u64_max: U64_MAX } = ACTION_JOURNAL_CONTAINER_TEST_CONSTANTS;

function operation(index) {
  return `act_${index.toString(16).padStart(32, '0')}`;
}

function event(state, sequence, overrides = {}) {
  const defaults = {
    prepared: { action: 'prepare', authorization_kind: null, resolution: null },
    authorized: { action: 'authorize', authorization_kind: 'user_confirmation', resolution: null },
    dispatching: { action: 'dispatch', authorization_kind: 'user_confirmation', resolution: null },
    acknowledged: { action: 'acknowledge', authorization_kind: 'user_confirmation', resolution: 'provider_acknowledged' },
    reconciling: { action: 'begin_reconciliation', authorization_kind: 'user_confirmation', resolution: null },
    completed: { action: 'complete', authorization_kind: 'user_confirmation', resolution: 'completed' },
    cancelled: { action: 'cancel', authorization_kind: null, resolution: 'request_cancelled' },
    failed_definitive: { action: 'fail_definitive', authorization_kind: null, resolution: 'pre_dispatch_failure' },
    unknown_manual: { action: 'mark_unknown', authorization_kind: 'user_confirmation', resolution: 'dispatch_ambiguous' },
  }[state];
  return { ...defaults, receipt_digest: RECEIPT, sequence, state, ...overrides };
}

function prepared(receipt = RECEIPT) {
  return event('prepared', 0, { receipt_digest: receipt });
}

function authorized(receipt = RECEIPT) {
  return event('authorized', 1, { receipt_digest: receipt });
}

function cancelled(receipt = RECEIPT) {
  return event('cancelled', 1, { receipt_digest: receipt });
}

function errorCode(fn, code) {
  assert.throws(fn, error => error?.name === 'ActionJournalContainerError' && error.code === code);
}

async function rejectionCode(promise, code) {
  await assert.rejects(promise, error => error?.name === 'ActionJournalContainerError' && error.code === code);
}

async function formatted() {
  const device = new InMemoryJournalBlockDevice();
  await formatJournalContainer(device, { containerId: CONTAINER_ID });
  return device;
}

async function baseline() {
  const device = await formatted();
  const journal = ActionJournalContainerReference.open(device);
  await journal.append(operation(1), prepared());
  return device;
}

test('machine contract exactly matches the fixed bounded layout and remains inert', async () => {
  const contract = JSON.parse(await readFile(new URL('../../contracts/action-journal-container/v0.1.0.json', import.meta.url), 'utf8'));
  assert.equal(ACTION_JOURNAL_CONTAINER_REFERENCE_ONLY, true);
  assert.equal(ACTION_JOURNAL_CONTAINER_PRODUCTION_AVAILABLE, false);
  assert.equal(PRODUCTION_ACTION_JOURNAL_PROTOCOL_AVAILABLE, false);
  assert.equal(contract.production_available, false);
  assert.equal(contract.native_helper_added, false);
  assert.equal(contract.filesystem_backend_added, false);
  assert.equal(contract.transport_added, false);
  assert.equal(contract.production_import_added, false);
  assert.deepEqual(contract.container, {
    bytes: ACTION_JOURNAL_CONTAINER_BYTES,
    header_bytes: ACTION_JOURNAL_CONTAINER_LAYOUT.header_bytes,
    slot_count: ACTION_JOURNAL_CONTAINER_LAYOUT.slot_count,
    banks_per_slot: ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot,
    slot_bytes: ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes * ACTION_JOURNAL_CONTAINER_LAYOUT.banks_per_slot,
    bank_bytes: ACTION_JOURNAL_CONTAINER_LAYOUT.bank_bytes,
    bank_body_bytes: ACTION_JOURNAL_CONTAINER_LAYOUT.bank_body_bytes,
    bank_marker_bytes: ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes,
    max_active_records: ACTION_JOURNAL_CONTAINER_LAYOUT.max_active_records,
    max_terminal_records: ACTION_JOURNAL_CONTAINER_LAYOUT.max_terminal_records,
  });
  assert.equal(contract.bank.event_cells, ACTION_JOURNAL_CONTAINER_LAYOUT.event_cells_per_bank);
  assert.equal(contract.bank.event_cells, ACTION_JOURNAL_LIMITS.max_events_per_operation);
  assert.equal(contract.bank.event_cells, ACTION_JOURNAL_LIMITS.max_detail_events);
  assert.equal(contract.bank.event_cell_bytes, ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes);
  assert.equal(contract.bank.max_canonical_event_bytes, ACTION_JOURNAL_CONTAINER_LAYOUT.max_event_bytes);
  assert.deepEqual(contract.write_protocol.length, 8);
});

test('format requires an exact-size blank device and a durable exact header', async () => {
  errorCode(() => new InMemoryJournalBlockDevice(0), 'container_size_mismatch');
  const wrongSize = new InMemoryJournalBlockDevice(ACTION_JOURNAL_CONTAINER_BYTES - 1);
  await rejectionCode(formatJournalContainer(wrongSize, { containerId: CONTAINER_ID }), 'container_size_mismatch');
  const nonblank = new InMemoryJournalBlockDevice();
  nonblank.corruptDurable(5000, Buffer.from([1]));
  await rejectionCode(formatJournalContainer(nonblank, { containerId: CONTAINER_ID }), 'container_already_initialized');

  const device = await formatted();
  assert.equal(ActionJournalContainerReference.open(device).summary().total, 0);
  await rejectionCode(formatJournalContainer(device, { containerId: CONTAINER_ID }), 'container_already_initialized');
  const corrupt = device.clone();
  corrupt.corruptDurable(200, Buffer.from([1]));
  errorCode(() => ActionJournalContainerReference.open(corrupt), 'container_corrupt_header');
});

test('torn header initialization is either unformatted/corrupt or exactly usable', async () => {
  for (const cut of [0, 1, 16, 512, 4095, 4096]) {
    const device = new InMemoryJournalBlockDevice();
    await rejectionCode(formatJournalContainer(device, {
      containerId: CONTAINER_ID,
      onBoundary(phase) {
        if (phase === 'after_header_write') device.failNextFlushAfter(cut);
      },
    }), 'container_simulated_power_loss');
    device.crash();
    if (cut === 4096) assert.equal(ActionJournalContainerReference.open(device).summary().total, 0);
    else assert.throws(() => ActionJournalContainerReference.open(device), { name: 'ActionJournalContainerError' });
  }
});

test('commits alternate exact COW banks and bind canonical protocol event digests', async () => {
  const device = await formatted();
  let journal = ActionJournalContainerReference.open(device);
  const op = operation(1);
  await journal.append(op, prepared());
  let record = journal.summary().records[0];
  assert.deepEqual(record, {
    operation_id: op,
    slot_index: 0,
    bank_index: 0,
    generation: '0',
    event_count: 1,
    state: 'prepared',
  });
  const firstCell = device.read(bankOffset(0, 0) + ACTION_JOURNAL_CONTAINER_LAYOUT.bank_descriptor_bytes, ACTION_JOURNAL_CONTAINER_LAYOUT.event_cell_bytes, { durableOnly: true });
  assert.equal(firstCell.subarray(4, 36).toString('hex'), journalEventDigest(prepared()));
  await journal.append(op, authorized());
  device.crash();
  journal = ActionJournalContainerReference.open(device);
  record = journal.summary().records[0];
  assert.equal(record.bank_index, 1);
  assert.equal(record.generation, '1');
  assert.equal(record.event_count, 2);
  assert.deepEqual(journal.detail(op).events, [prepared(), authorized()]);
  assert.equal(slotOffset(1) - slotOffset(0), 32768);
  assert.equal(markerOffset(0, 0) - bankOffset(0, 0), 15360);
});

test('sequential operations occupy distinct stable slots and reopen identically', async () => {
  const device = await formatted();
  const journal = ActionJournalContainerReference.open(device);
  await journal.append(operation(1), prepared());
  await journal.append(operation(2), prepared(OTHER_RECEIPT));
  const before = journal.summary();
  assert.deepEqual(before.records.map(record => [record.operation_id, record.slot_index]), [
    [operation(1), 0],
    [operation(2), 1],
  ]);
  const reopened = ActionJournalContainerReference.open(device);
  assert.deepEqual(reopened.summary(), before);
  assert.deepEqual(reopened.detail(operation(1)).events, [prepared()]);
  assert.deepEqual(reopened.detail(operation(2)).events, [prepared(OTHER_RECEIPT)]);
});

test('existing operation reuses its slot without overwriting another operation', async () => {
  const device = await formatted();
  const journal = ActionJournalContainerReference.open(device);
  await journal.append(operation(1), prepared());
  await journal.append(operation(2), prepared(OTHER_RECEIPT));
  const secondBefore = journal.detail(operation(2));
  await journal.append(operation(1), authorized());
  assert.deepEqual(journal.detail(operation(1)).events, [prepared(), authorized()]);
  assert.deepEqual(journal.detail(operation(2)), secondBefore);
  await rejectionCode(journal.append(operation(1), prepared()), 'container_invalid_event');
  assert.deepEqual(ActionJournalContainerReference.open(device).detail(operation(2)), secondBefore);
});

test('a crash between new operations preserves allocation and committed summary authority', async () => {
  const device = await formatted();
  const journal = ActionJournalContainerReference.open(device);
  await journal.append(operation(1), prepared());
  device.crash();
  await journal.append(operation(2), prepared(OTHER_RECEIPT));
  assert.deepEqual(journal.summary().records.map(record => [record.operation_id, record.slot_index]), [
    [operation(1), 0],
    [operation(2), 1],
  ]);

  const lostAck = ActionJournalContainerReference.open(await formatted());
  await assert.rejects(lostAck.append(operation(3), prepared(), {
    onBoundary(phase) { if (phase === 'after_commit_flush') throw new Error('lost acknowledgement'); },
  }), /lost acknowledgement/);
  assert.deepEqual(lostAck.summary().records.map(record => [record.operation_id, record.slot_index]), [
    [operation(3), 0],
  ]);
});

test('sequential allocation reaches the exact active cap without reusing a committed slot', async () => {
  const device = await formatted();
  for (let slotIndex = 0; slotIndex < 255; slotIndex++) {
    seedBankForTest(device, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 0,
      generation: 0n,
      operationId: operation(slotIndex + 1),
      events: [prepared()],
    });
  }
  const journal = ActionJournalContainerReference.open(device);
  await journal.append(operation(256), prepared());
  assert.deepEqual(journal.summary().records.at(-1), {
    operation_id: operation(256),
    slot_index: 255,
    bank_index: 0,
    generation: '0',
    event_count: 1,
    state: 'prepared',
  });
  await rejectionCode(journal.append(operation(257), prepared()), 'container_active_limit');
  assert.deepEqual(ActionJournalContainerReference.open(device).summary(), journal.summary());
});

test('every write, flush, and readback boundary recovers only old or committed authority', async () => {
  const source = await baseline();
  const committedBoundaries = new Set(['after_commit_flush', 'after_commit_readback']);
  for (const target of ACTION_JOURNAL_CONTAINER_BOUNDARIES) {
    const device = source.clone();
    const journal = ActionJournalContainerReference.open(device);
    await assert.rejects(journal.append(operation(1), authorized(), {
      onBoundary(phase) {
        if (phase === target) throw new Error(`crash:${phase}`);
      },
    }), new RegExp(`crash:${target}`));
    device.crash();
    const recovered = ActionJournalContainerReference.open(device);
    const expected = committedBoundaries.has(target) ? 'authorized' : 'prepared';
    assert.equal(recovered.summary().records[0].state, expected, target);
  }
});

test('partial staging, body, and commit flushes have bounded fail-closed outcomes', async () => {
  const source = await baseline();
  // Marker suffixes are reserved zero bytes. A short write that persisted every
  // meaningful byte is indistinguishable from a complete marker and is valid.
  for (const cut of [0, 144, 512, 1023, 1024]) {
    const device = source.clone();
    const journal = ActionJournalContainerReference.open(device);
    await rejectionCode(journal.append(operation(1), authorized(), {
      onBoundary(phase) { if (phase === 'after_staging_write') device.failNextFlushAfter(cut); },
    }), 'container_simulated_power_loss');
    device.crash();
    assert.equal(ActionJournalContainerReference.open(device).summary().records[0].state, 'prepared');
  }
  for (const cut of [1, 16, 112, 143]) {
    const device = source.clone();
    const journal = ActionJournalContainerReference.open(device);
    await rejectionCode(journal.append(operation(1), authorized(), {
      onBoundary(phase) { if (phase === 'after_staging_write') device.failNextFlushAfter(cut); },
    }), 'container_simulated_power_loss');
    device.crash();
    errorCode(() => ActionJournalContainerReference.open(device), 'container_corrupt_bank');
  }

  for (const cut of [0, 1, 512, 8192, 15359, 15360]) {
    const device = source.clone();
    const journal = ActionJournalContainerReference.open(device);
    await rejectionCode(journal.append(operation(1), authorized(), {
      onBoundary(phase) { if (phase === 'after_body_write') device.failNextFlushAfter(cut); },
    }), 'container_simulated_power_loss');
    device.crash();
    assert.equal(ActionJournalContainerReference.open(device).summary().records[0].state, 'prepared');
  }

  for (const cut of [0, 1, 20]) {
    const device = source.clone();
    const journal = ActionJournalContainerReference.open(device);
    await rejectionCode(journal.append(operation(1), authorized(), {
      onBoundary(phase) { if (phase === 'after_commit_write') device.failNextFlushAfter(cut); },
    }), 'container_simulated_power_loss');
    device.crash();
    assert.equal(ActionJournalContainerReference.open(device).summary().records[0].state, 'prepared');
  }
  for (const cut of [21, 48, 80, 112, 143]) {
    const device = source.clone();
    const journal = ActionJournalContainerReference.open(device);
    await rejectionCode(journal.append(operation(1), authorized(), {
      onBoundary(phase) { if (phase === 'after_commit_write') device.failNextFlushAfter(cut); },
    }), 'container_simulated_power_loss');
    device.crash();
    errorCode(() => ActionJournalContainerReference.open(device), 'container_corrupt_bank');
  }
  for (const cut of [144, 512, 1023, 1024]) {
    const fullyPersisted = source.clone();
    const journal = ActionJournalContainerReference.open(fullyPersisted);
    await rejectionCode(journal.append(operation(1), authorized(), {
      onBoundary(phase) { if (phase === 'after_commit_write') fullyPersisted.failNextFlushAfter(cut); },
    }), 'container_simulated_power_loss');
    fullyPersisted.crash();
    assert.equal(ActionJournalContainerReference.open(fullyPersisted).summary().records[0].state, 'authorized');
  }
});

test('readback detects durable corruption without falsely acknowledging', async () => {
  const bodyCorrupt = await baseline();
  let journal = ActionJournalContainerReference.open(bodyCorrupt);
  await rejectionCode(journal.append(operation(1), authorized(), {
    onBoundary(phase, context) {
      if (phase === 'after_body_flush') bodyCorrupt.corruptDurable(bankOffset(context.slot_index, context.bank_index) + 200, Buffer.from([1]));
    },
  }), 'container_readback_failed');
  bodyCorrupt.crash();
  assert.equal(ActionJournalContainerReference.open(bodyCorrupt).summary().records[0].state, 'prepared');

  const commitCorrupt = await baseline();
  journal = ActionJournalContainerReference.open(commitCorrupt);
  await rejectionCode(journal.append(operation(1), authorized(), {
    onBoundary(phase, context) {
      if (phase === 'after_commit_flush') commitCorrupt.corruptDurable(markerOffset(context.slot_index, context.bank_index) + 112, Buffer.from([1]));
    },
  }), 'container_readback_failed');
  commitCorrupt.crash();
  errorCode(() => ActionJournalContainerReference.open(commitCorrupt), 'container_corrupt_bank');

  const committedBodyCorrupt = await baseline();
  journal = ActionJournalContainerReference.open(committedBodyCorrupt);
  await rejectionCode(journal.append(operation(1), authorized(), {
    onBoundary(phase, context) {
      if (phase === 'after_commit_flush') {
        committedBodyCorrupt.corruptDurable(bankOffset(context.slot_index, context.bank_index) + 200, Buffer.from([1]));
      }
    },
  }), 'container_corrupt_bank');
  committedBodyCorrupt.crash();
  errorCode(() => ActionJournalContainerReference.open(committedBodyCorrupt), 'container_corrupt_bank');
});

test('startup ignores an exact staged successor but rejects checksum, orphan, and marker corruption', async () => {
  const device = await formatted();
  const low = seedBankForTest(device, {
    containerId: CONTAINER_ID,
    slotIndex: 0,
    bankIndex: 0,
    generation: 0n,
    operationId: operation(1),
    events: [prepared()],
  });
  seedBankForTest(device, {
    containerId: CONTAINER_ID,
    slotIndex: 0,
    bankIndex: 1,
    generation: 1n,
    previousCommitDigest: low.commitDigest,
    operationId: operation(1),
    events: [prepared(), authorized()],
    state: 'staging',
  });
  device.corruptDurable(bankOffset(0, 1) + 500, Buffer.from([0xaa]));
  assert.equal(ActionJournalContainerReference.open(device).summary().records[0].state, 'prepared');

  const bodyCorrupt = (await baseline()).clone();
  bodyCorrupt.corruptDurable(bankOffset(0, 0) + 500, Buffer.from([0xaa]));
  errorCode(() => ActionJournalContainerReference.open(bodyCorrupt), 'container_corrupt_bank');

  const markerCorrupt = (await baseline()).clone();
  markerCorrupt.corruptDurable(markerOffset(0, 0), Buffer.from([0]));
  errorCode(() => ActionJournalContainerReference.open(markerCorrupt), 'container_corrupt_bank');

  const orphan = await formatted();
  orphan.corruptDurable(bankOffset(0, 0), Buffer.from([1]));
  errorCode(() => ActionJournalContainerReference.open(orphan), 'container_corrupt_bank');
});

test('equal, gapped, divergent, and predecessor-mismatched committed banks fail closed', async () => {
  const cases = [
    { lowGeneration: 0n, highGeneration: 0n, highPrevious: U64_MAX, highReceipt: RECEIPT },
    { lowGeneration: 0n, highGeneration: 2n, highPrevious: 1n, highReceipt: RECEIPT },
    { lowGeneration: 0n, highGeneration: 1n, highPrevious: 0n, highReceipt: OTHER_RECEIPT },
  ];
  for (const [index, value] of cases.entries()) {
    const device = await formatted();
    const low = seedBankForTest(device, {
      containerId: CONTAINER_ID,
      slotIndex: 0,
      bankIndex: 0,
      generation: value.lowGeneration,
      operationId: operation(index + 1),
      events: [prepared()],
    });
    seedBankForTest(device, {
      containerId: CONTAINER_ID,
      slotIndex: 0,
      bankIndex: 1,
      generation: value.highGeneration,
      previousGeneration: value.highPrevious,
      previousCommitDigest: low.commitDigest,
      operationId: operation(index + 1),
      events: [prepared(value.highReceipt), authorized(value.highReceipt)],
    });
    errorCode(() => ActionJournalContainerReference.open(device), 'container_conflicting_authority');
  }

  const wrongDigest = await formatted();
  seedBankForTest(wrongDigest, {
    containerId: CONTAINER_ID,
    slotIndex: 0,
    bankIndex: 0,
    generation: 0n,
    operationId: operation(9),
    events: [prepared()],
  });
  seedBankForTest(wrongDigest, {
    containerId: CONTAINER_ID,
    slotIndex: 0,
    bankIndex: 1,
    generation: 1n,
    previousGeneration: 0n,
    previousCommitDigest: Buffer.alloc(32, 0x77),
    operationId: operation(9),
    events: [prepared(), authorized()],
  });
  errorCode(() => ActionJournalContainerReference.open(wrongDigest), 'container_conflicting_authority');
});

test('duplicate operation IDs across slots and invalid event histories fail closed', async () => {
  const duplicate = await formatted();
  for (const slotIndex of [0, 1]) {
    seedBankForTest(duplicate, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 0,
      generation: 0n,
      operationId: operation(1),
      events: [prepared()],
    });
  }
  errorCode(() => ActionJournalContainerReference.open(duplicate), 'container_duplicate_operation');

  const journal = ActionJournalContainerReference.open(await formatted());
  await rejectionCode(journal.append(operation(2), event('authorized', 0)), 'container_invalid_event');
  await rejectionCode(journal.append(operation(2), { ...prepared(), provider_payload: 'forbidden' }), 'container_invalid_event');
  await rejectionCode(journal.append(operation(2), prepared(), {
    onBoundary: 'not-a-function',
  }), 'container_invalid_argument');
  journal.device.crash();
  assert.equal(ActionJournalContainerReference.open(journal.device).summary().total, 0);
  errorCode(() => bankOffset(1024, 0), 'container_invalid_argument');
  errorCode(() => bankOffset(0, 2), 'container_invalid_argument');
});

test('unsigned generation wrap and committed slot transplant are refused', async () => {
  const device = await formatted();
  const low = seedBankForTest(device, {
    containerId: CONTAINER_ID,
    slotIndex: 0,
    bankIndex: 0,
    generation: U64_MAX - 1n,
    previousGeneration: U64_MAX - 2n,
    previousCommitDigest: Buffer.alloc(32, 0x44),
    operationId: operation(1),
    events: [prepared()],
  });
  seedBankForTest(device, {
    containerId: CONTAINER_ID,
    slotIndex: 0,
    bankIndex: 1,
    generation: U64_MAX,
    previousGeneration: U64_MAX - 1n,
    previousCommitDigest: low.commitDigest,
    operationId: operation(1),
    events: [prepared(), authorized()],
  });
  const journal = ActionJournalContainerReference.open(device);
  await rejectionCode(journal.append(operation(1), event('dispatching', 2)), 'container_generation_exhausted');

  const transplanted = await baseline();
  const marker = transplanted.read(markerOffset(0, 0), ACTION_JOURNAL_CONTAINER_LAYOUT.bank_marker_bytes, { durableOnly: true });
  transplanted.corruptDurable(markerOffset(1, 0), marker);
  errorCode(() => ActionJournalContainerReference.open(transplanted), 'container_corrupt_bank');
});

test('fixed 256 active, 768 terminal, and 1024 total caps are exact', { timeout: 30_000 }, async () => {
  const exact = await formatted();
  for (let slotIndex = 0; slotIndex < 256; slotIndex++) {
    seedBankForTest(exact, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 0,
      generation: 0n,
      operationId: operation(slotIndex + 1),
      events: [prepared()],
    });
  }
  for (let slotIndex = 256; slotIndex < 1024; slotIndex++) {
    const low = seedBankForTest(exact, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 0,
      generation: 0n,
      operationId: operation(slotIndex + 1),
      events: [prepared()],
    });
    seedBankForTest(exact, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 1,
      generation: 1n,
      previousCommitDigest: low.commitDigest,
      operationId: operation(slotIndex + 1),
      events: [prepared(), cancelled()],
    });
  }
  const full = ActionJournalContainerReference.open(exact);
  assert.deepEqual({ active: full.summary().active, terminal: full.summary().terminal, total: full.summary().total }, { active: 256, terminal: 768, total: 1024 });
  await rejectionCode(full.append(operation(2000), prepared()), 'container_active_limit');

  const activeOverflow = await formatted();
  for (let slotIndex = 0; slotIndex < 257; slotIndex++) {
    seedBankForTest(activeOverflow, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 0,
      generation: 0n,
      operationId: operation(slotIndex + 1),
      events: [prepared()],
    });
  }
  errorCode(() => ActionJournalContainerReference.open(activeOverflow), 'container_active_limit');

  const terminalOverflow = await formatted();
  for (let slotIndex = 0; slotIndex < 769; slotIndex++) {
    const low = seedBankForTest(terminalOverflow, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 0,
      generation: 0n,
      operationId: operation(slotIndex + 1),
      events: [prepared()],
    });
    seedBankForTest(terminalOverflow, {
      containerId: CONTAINER_ID,
      slotIndex,
      bankIndex: 1,
      generation: 1n,
      previousCommitDigest: low.commitDigest,
      operationId: operation(slotIndex + 1),
      events: [prepared(), cancelled()],
    });
  }
  errorCode(() => ActionJournalContainerReference.open(terminalOverflow), 'container_terminal_limit');
});
