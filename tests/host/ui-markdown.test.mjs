import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { MAX_MARKDOWN_CHARS, parseMarkdown, renderMarkdown, revealInvisible, separateThinking } from '../../ui/markdown.js';

// Minimal DOM stand-in: enough to prove the renderer only ever builds nodes
// through createElement/createTextNode and never sets risky attributes.
class FakeElement {
  constructor(tagName) { this.tagName = tagName; this.childNodes = []; this.attributes = new Map(); this.className = ''; this.listeners = new Map(); }
  append(...nodes) { for (const node of nodes) { if (node instanceof FakeElement && node.tagName === '#fragment') this.childNodes.push(...node.childNodes); else this.childNodes.push(node); } }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  set textContent(value) { this.childNodes = [{ nodeType: 3, data: String(value) }]; }
  get textContent() { return this.childNodes.map(node => node.nodeType === 3 ? node.data : node.textContent).join(''); }
  set innerHTML(_) { throw new Error('innerHTML must never be used'); }
}
const fakeDocument = { createElement: tag => new FakeElement(tag), createTextNode: data => ({ nodeType: 3, data: String(data) }), createDocumentFragment: () => new FakeElement('#fragment') };
function* walk(node) { yield node; for (const child of node.childNodes ?? []) yield* walk(child); }
const ALLOWED_TAGS = new Set(['#fragment', 'p', 'h3', 'h4', 'h5', 'h6', 'hr', 'blockquote', 'ul', 'ol', 'li', 'div', 'span', 'button', 'pre', 'code', 'strong', 'em', 'br']);
const types = blocks => blocks.map(block => block.type);

test('parses the supported subset into a plain AST', () => {
  const ast = parseMarkdown('# Title\n\nSome **bold**, *italic*, _under_ and `code`.\n\n- one\n- two\n  - nested\n\n3. three\n4. four\n\n> quoted\n\n---\n```js\nlet a = 1;\n```');
  assert.deepEqual(types(ast), ['heading', 'paragraph', 'list', 'list', 'blockquote', 'hr', 'code_block']);
  assert.deepEqual(ast[1].children.map(node => node.type), ['text', 'strong', 'text', 'em', 'text', 'em', 'text', 'code', 'text']);
  assert.equal(ast[2].ordered, false); assert.equal(ast[2].items.length, 2); assert.equal(ast[2].items[1][1].type, 'list');
  assert.equal(ast[3].ordered, true); assert.equal(ast[3].start, 3);
  assert.deepEqual(ast[6], { type: 'code_block', lang: 'js', text: 'let a = 1;', closed: true });
});

test('emphasis can wrap code spans, and code span contents are never interpreted', () => {
  const [paragraph] = parseMarkdown('Run **`npm test`** then `**not bold** <b>`');
  assert.deepEqual(paragraph.children, [{ type: 'text', text: 'Run ' }, { type: 'strong', children: [{ type: 'code', text: 'npm test' }] }, { type: 'text', text: ' then ' }, { type: 'code', text: '**not bold** <b>' }]);
  assert.deepEqual(parseMarkdown('snake_case_name and 2*3*4')[0].children, [{ type: 'text', text: 'snake_case_name and 2*3*4' }]);
  // Private-use placeholder characters in the input cannot forge code spans.
  assert.equal(JSON.stringify(parseMarkdown('0 `x`')).includes(''), false);
});

test('hostile input stays inert text: raw HTML, javascript: links, unterminated fences', () => {
  const html = parseMarkdown('<script>alert(1)</script><img src=x onerror=alert(1)>');
  assert.deepEqual(html, [{ type: 'paragraph', children: [{ type: 'text', text: '<script>alert(1)</script><img src=x onerror=alert(1)>' }] }]);
  const link = parseMarkdown('[click me](javascript:alert(1))')[0].children[0];
  assert.equal(link.type, 'link'); assert.match(link.url, /^javascript:/);
  const open = parseMarkdown('before\n```python\nprint("never closed")\n<script>');
  assert.deepEqual(open[1], { type: 'code_block', lang: 'python', text: 'print("never closed")\n<script>', closed: false });
  assert.deepEqual(types(parseMarkdown('```js `oops` ```\ntext')), ['paragraph']);

  const fragment = renderMarkdown('<script>alert(1)</script>\n\n[x](javascript:alert(1)) **<b>hi</b>**\n\n```html\n<iframe src="javascript:alert(1)"></iframe>\n```', { document: fakeDocument });
  for (const node of walk(fragment)) {
    if (node.nodeType === 3) continue;
    assert.ok(ALLOWED_TAGS.has(node.tagName), `unexpected element <${node.tagName}>`);
    assert.deepEqual([...node.attributes.keys()].filter(name => name !== 'start'), [], `unexpected attribute on <${node.tagName}>`);
    assert.equal(node.href, undefined); assert.equal(node.src, undefined);
  }
  const text = fragment.textContent;
  assert.match(text, /<script>alert\(1\)<\/script>/); assert.match(text, /x \(javascript:alert\(1\)\)/); assert.match(text, /<iframe src="javascript:alert\(1\)"><\/iframe>/);
});

test('code blocks get a Copy button wired to the exact code text', () => {
  const copies = [];
  const fragment = renderMarkdown('```sh\necho "<hi>" && rm -i x\n```', { document: fakeDocument, onCopy: (button, text) => copies.push([button.textContent, text]) });
  const button = [...walk(fragment)].find(node => node.tagName === 'button');
  assert.equal(button.type, 'button'); assert.equal(button.textContent, 'Copy');
  button.listeners.get('click')();
  assert.deepEqual(copies, [['Copy', 'echo "<hi>" && rm -i x']]);
  const ordered = [...walk(renderMarkdown('7. seven\n8. eight', { document: fakeDocument }))].find(node => node.tagName === 'ol');
  assert.equal(ordered.attributes.get('start'), '7');
});

test('pathological inputs parse in bounded time', () => {
  const hostile = {
    backticks: '`'.repeat(200 * 1024),
    alternatingBackticks: '` `` '.repeat(40 * 1024),
    stars: '*a '.repeat(70 * 1024),
    doubleStars: '** '.repeat(70 * 1024),
    underscores: '_a '.repeat(70 * 1024),
    brackets: '['.repeat(200 * 1024) + ']',
    links: '[a]('.repeat(50 * 1024),
    headingSpaces: '# ' + ' '.repeat(200 * 1024) + 'x',
    nestedQuotes: '>'.repeat(200 * 1024),
    nestedLists: '  - x\n'.repeat(30 * 1024),
    fences: '```\n'.repeat(50 * 1024),
    escapes: '\\'.repeat(200 * 1024),
  };
  for (const [name, input] of Object.entries(hostile)) {
    const started = performance.now(); const ast = parseMarkdown(input); const elapsed = performance.now() - started;
    assert.ok(Array.isArray(ast), name); assert.ok(elapsed < 1500, `${name} took ${elapsed.toFixed(0)} ms`);
  }
  const huge = 'x'.repeat(MAX_MARKDOWN_CHARS + 1);
  assert.deepEqual(parseMarkdown(huge), [{ type: 'paragraph', children: [{ type: 'text', text: huge }] }]);
  assert.deepEqual(parseMarkdown(undefined), []); assert.deepEqual(parseMarkdown({ toString: () => '# x' }), []);
});

test('model reasoning is separated from the visible answer', () => {
  assert.deepEqual(separateThinking('<think>\nplan\n</think>\n\nThe answer.'), { answer: 'The answer.', thinking: 'plan', open: false });
  assert.deepEqual(separateThinking('plan from template</think>Answer'), { answer: 'Answer', thinking: 'plan from template', open: false });
  assert.deepEqual(separateThinking('Hi <think>still going'), { answer: 'Hi ', thinking: 'still going', open: true });
  assert.deepEqual(separateThinking('No tags here.'), { answer: 'No tags here.', thinking: '', open: false });
  const started = performance.now(); separateThinking('<think>'.repeat(30000) + '</think>'.repeat(30000)); assert.ok(performance.now() - started < 1500);
});

test('UI sources never assign HTML strings', async () => {
  for (const file of ['app.js', 'markdown.js', 'errors.js', 'transcript.js', 'tool-labels.js', 'cards.js']) {
    const source = await readFile(new URL(`../../ui/${file}`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/u, file);
  }
});

test('invisible bidi and zero-width characters are made visible in code and in the Copy payload', () => {
  // Displayed (RLO-reversed) this looks like "...gpj.exe"; the bytes say otherwise.
  const rlo = 'Remove-Item C:\\Users\\me\\photo\u202Egpj.exe';
  const copies = [];
  const fragment = renderMarkdown(`\`\`\`powershell\n${rlo}\nWrite-Host ok\u200B\u2066x\u2069\n\`\`\`\nInline \`rm\u202E -rf\``, { document: fakeDocument, onCopy: (_, text) => copies.push(text) });
  [...walk(fragment)].find(node => node.tagName === 'button').listeners.get('click')();
  assert.equal(copies[0], 'Remove-Item C:\\Users\\me\\photo‹U+202E›gpj.exe\nWrite-Host ok‹U+200B›‹U+2066›x‹U+2069›');
  const text = fragment.textContent;
  assert.doesNotMatch(text, /[\u202A-\u202E\u2066-\u2069\u200B-\u200F\uFEFF]/u); assert.match(text, /rm‹U\+202E› -rf/u);
  assert.equal(revealInvisible('a\u00ADb\uFEFFc\x07d\te\nf'), 'a‹U+00AD›b‹U+FEFF›c‹U+0007›d\te\nf');
  assert.equal(revealInvisible('plain 👍🏽 text'), 'plain 👍🏽 text');
  assert.equal(revealInvisible(undefined), '');
});
