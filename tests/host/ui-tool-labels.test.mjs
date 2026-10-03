import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { hasToolLabel, toolLabel, toolStatusText } from '../../ui/tool-labels.js';

async function sources(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await sources(path)); else if (entry.name.endsWith('.mjs')) out.push(await readFile(path, 'utf8'));
  }
  return out;
}

test('every live local tool the host defines has a friendly label', async () => {
  const names = new Set();
  for (const text of await sources(fileURLToPath(new URL('../../host/tools/', import.meta.url)))) for (const match of text.matchAll(/name: '([a-z_]+\.[a-z_]+)', version/g)) names.add(match[1]);
  assert.deepEqual([...names].sort(), ['app.open', 'browser.open_url', 'clipboard.read', 'clipboard.write', 'fs.apply_patch', 'fs.list', 'fs.read_text', 'fs.search_text', 'fs.write_new', 'process.run_allowlisted', 'system.get_info', 'time.now']);
  for (const name of names) { assert.equal(hasToolLabel(name), true, name); assert.notEqual(toolLabel(name), name); assert.match(toolLabel(name), /^[A-Z][a-z]/); }
  assert.equal(toolLabel('time.now'), 'Checking the time'); assert.equal(toolLabel('fs.read_text'), 'Reading a file');
});

test('unknown tools fall back to their raw name; junk never reaches the page', () => {
  assert.equal(toolLabel('mail.send_draft'), 'mail.send_draft'); assert.equal(hasToolLabel('mail.send_draft'), false);
  for (const junk of [undefined, null, 42, '', '<img src=x>', 'a b', 'x'.repeat(200), 'constructor ']) assert.equal(toolLabel(junk), 'Using a tool', String(junk));
  assert.equal(hasToolLabel('constructor'), false); assert.equal(toolLabel('constructor'), 'constructor');
});

test('tool result statuses read as plain words', () => {
  assert.deepEqual(['ok', 'failed', 'denied', 'cancelled', 'expired'].map(toolStatusText), ['done', 'failed', 'you said no', 'not run', 'expired']);
  assert.equal(toolStatusText(undefined), 'unknown'); assert.equal(toolStatusText('weird_new_status').length <= 32, true);
});
