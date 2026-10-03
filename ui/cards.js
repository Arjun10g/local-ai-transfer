// DOM builders for the parts of a confirmation card that show what a tool
// will actually do: the change diff and argument excerpts.  Rule: a card must
// never show LESS of what will run than the host sent; when anything is
// uncertain, the data is shown plainly rather than hidden or prettified.
// Everything is text via textContent; `doc` is injected so node tests can use
// a fake document.
import { revealInvisible } from './markdown.js';

const KIND_CLASS = { header: 'diff-header', removed: 'diff-del', added: 'diff-add', context: 'diff-context' };
const KIND_MARK = { header: '', removed: '- ', added: '+ ', context: '  ' };
const MAX_DIFF_LINES = 2000;

const codePoints = value => Array.from(value).length;

export function detailRow(doc, label, value) {
  const row = doc.createElement('div'); row.className = 'confirmation-detail';
  const name = doc.createElement('strong'); name.textContent = `${label}: `;
  const text = doc.createElement('span'); text.textContent = value;
  row.append(name, text); return row;
}

/**
 * The change as a list of nodes.  Prefers the host's structured
 * `diff_lines` (each line classified from the replacement's structure, not
 * from its first character).  The legacy `diff` string is shown uncoloured:
 * its leading characters cannot be trusted to say old from new.
 */
export function buildDiff(doc, preview) {
  const structured = Array.isArray(preview?.diff_lines);
  if (!structured && (typeof preview?.diff !== 'string' || !preview.diff)) return [];
  const pre = doc.createElement('pre'); pre.className = 'diff';
  const lines = structured
    ? preview.diff_lines.slice(0, MAX_DIFF_LINES).map(line => { const kind = Object.hasOwn(KIND_CLASS, line?.kind) ? line.kind : 'context'; return { kind, text: KIND_MARK[kind] + revealInvisible(typeof line?.text === 'string' ? line.text : String(line?.text ?? '')) }; })
    : preview.diff.slice(0, 16384).split('\n').map(text => ({ kind: 'context', text: revealInvisible(text) }));
  for (const line of lines) { const span = doc.createElement('span'); span.className = KIND_CLASS[line.kind]; span.textContent = line.text || ' '; pre.append(span); }
  const nodes = [pre];
  if (!structured || preview.diff_structured === false) nodes.push(detailRow(doc, 'Note', 'Lines are not colour-coded: BMO could not tell old text from new, so every line is shown as is.'));
  if (preview.diff_redacted === true) nodes.push(detailRow(doc, 'Note', 'Secret values in the change are shown as [redacted]; everything else is shown.'));
  if (preview.diff_truncated === true || (structured && preview.diff_lines.length > MAX_DIFF_LINES)) nodes.push(detailRow(doc, 'Note', 'The change is longer than shown here.'));
  return nodes;
}

/**
 * An argument excerpt (`{key}_excerpt` and friends from the host's
 * arguments_summary): the head, then the tail of long values with an explicit
 * count of what lies between, so text appended after the head is never hidden.
 */
export function buildExcerpt(doc, summary, key, label) {
  const chars = Number.isSafeInteger(summary?.[`${key}_chars`]) && summary[`${key}_chars`] >= 0 ? summary[`${key}_chars`] : null;
  const size = chars === null ? '' : ` (${chars.toLocaleString('en-US')} characters)`;
  if (summary?.[`${key}_redacted`] === true) return [detailRow(doc, label, `Not shown: too large for BMO to check${size}.`)];
  const head = summary?.[`${key}_excerpt`];
  if (typeof head !== 'string') return [];
  const tail = summary[`${key}_excerpt_tail`];
  let text = revealInvisible(head);
  if (typeof tail === 'string' && tail) {
    const omitted = chars === null ? null : chars - codePoints(head) - codePoints(tail);
    text += `\n… ${omitted !== null && omitted >= 0 ? omitted.toLocaleString('en-US') : 'some'} characters omitted …\n${revealInvisible(tail)}`;
  } else if (summary[`${key}_excerpt_truncated`] === true) text += '\n… the rest is not shown …';
  const heading = doc.createElement('div'); heading.className = 'confirmation-detail';
  const name = doc.createElement('strong'); name.textContent = `${label}${size}:`; heading.append(name);
  const pre = doc.createElement('pre'); pre.className = 'excerpt'; pre.textContent = text;
  const nodes = [heading, pre];
  if (summary[`${key}_masked`] === true) nodes.push(detailRow(doc, 'Note', 'Secret values hidden: shown as [redacted]; everything else is shown.'));
  return nodes;
}
