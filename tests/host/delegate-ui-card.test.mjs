import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { buildDelegateCard, delegateBadgeText } from '../../ui/cards.js';

class FakeElement {
  constructor(tagName) { this.tagName = tagName; this.childNodes = []; this.className = ''; this.disabled = false; }
  append(...nodes) { this.childNodes.push(...nodes); }
  set textContent(value) { this.childNodes = [{ nodeType: 3, data: String(value) }]; }
  get textContent() { return this.childNodes.map(node => node.nodeType === 3 ? node.data : node.textContent).join('\n'); }
  set innerHTML(_) { throw new Error('innerHTML must never be used'); }
}
const doc = { createElement: tag => new FakeElement(tag) };
const job = { job_id: 'job_abcdefgh', caller: { client: 'vscode', name: 'copilot<b>' }, task: 'Summarise <script>alert(1)</script> password=[redacted] x‮y', task_masked: true, task_chars: 61, context_chars: 1234, allow_files: true, files_requested: true, workspaces: ['notes'], expires_in_ms: 120000 };

test('the approval card shows caller, task, context size, files, and where the answer goes, as text', () => {
  const decisions = [];
  const { card, countdown, approve, deny } = buildDelegateCard(doc, job, { onDecision: value => decisions.push(value) });
  const text = card.textContent;
  assert.match(text, /vscode · copilot<b>/, 'caller labels are text, not markup');
  assert.match(text, /<script>alert\(1\)<\/script>/);
  assert.match(text, /x‹U\+202E›y/, 'a bidi override is made visible');
  assert.match(text, /\[redacted\]/); assert.match(text, /shown as \[redacted\]/);
  assert.match(text, /1,234 characters attached \(not shown/);
  assert.match(text, /May read \(never change\) files in: notes/);
  assert.match(text, /Back to vscode, which may send it to a cloud service/);
  assert.equal(countdown.className, 'countdown');
  approve.onclick(); deny.onclick();
  assert.deepEqual(decisions, [true, false]);
});

test('no files, refused files, and a hostile shape degrade to plain, honest text', () => {
  const none = buildDelegateCard(doc, { ...job, allow_files: false, files_requested: false, workspaces: [], context_chars: 0, task_masked: false }).card.textContent;
  assert.match(none, /No file access/); assert.match(none, /Reference material: \nNone/); assert.doesNotMatch(none, /redacted\]; BMO/);
  const refused = buildDelegateCard(doc, { ...job, allow_files: false, files_requested: true, workspaces: [] }).card.textContent;
  assert.match(refused, /no folder is shared with coding assistants, so it will read none/);
  const junk = buildDelegateCard(doc, { caller: 7, task: { toString: () => 'evil' }, workspaces: 'notes' }).card.textContent;
  assert.match(junk, /unknown · unknown/); assert.doesNotMatch(junk, /evil/);
});

test('the badge reads off, per job, granted, and the waiting count', () => {
  assert.equal(delegateBadgeText(undefined), 'off');
  assert.equal(delegateBadgeText({ enabled: false, queue: 2 }), 'off');
  assert.equal(delegateBadgeText({ enabled: true, queue: 0, approval: 'per_job' }), 'on · asks each time');
  assert.equal(delegateBadgeText({ enabled: true, queue: 2, approval: 'granted' }), 'on · allowed for now · 2 waiting');
});

test('the page polls the operator routes with the UI bearer and wires stop-all and the short grants', async () => {
  const app = await readFile(new URL('../../ui/app.js', import.meta.url), 'utf8');
  const html = await readFile(new URL('../../ui/index.html', import.meta.url), 'utf8');
  assert.match(app, /api\('\/api\/delegation'\)/); assert.match(app, /\/api\/delegation\/jobs\/\$\{job\.job_id\}\/decision/);
  assert.match(app, /\/api\/delegation\/stop/); assert.match(app, /grant\(900000\)/); assert.match(app, /grant\(3600000\)/);
  assert.doesNotMatch(app, /\/api\/delegate\//, 'the page never calls the bridge routes');
  assert.match(html, /id="delegate-panel"[^>]*hidden/); assert.match(html, /id="delegate-list"/); assert.match(html, /<strong id="delegate"/);
});
