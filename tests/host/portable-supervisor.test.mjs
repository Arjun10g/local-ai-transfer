import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { PORTABLE_LAUNCH_STATE, refusePortableLaunch } from '../../portable-supervisor.mjs';

test('portable supervisor exposes only a fixed NOT_READY refusal', async () => {
  assert.equal(PORTABLE_LAUNCH_STATE, 'NOT_READY-identity-pinned-native-launcher-unavailable');
  assert.throws(() => refusePortableLaunch(), new RegExp(PORTABLE_LAUNCH_STATE));

  const source = await readFile(new URL('../../portable-supervisor.mjs', import.meta.url), 'utf8');
  for (const primitive of ["node:child_process", 'spawn(', 'exec(', 'createServer(', 'readFile(', 'writeFile(']) {
    assert.equal(source.includes(primitive), false, primitive);
  }
});
