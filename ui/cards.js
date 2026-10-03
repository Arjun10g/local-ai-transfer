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

const label = value => (typeof value === 'string' && value ? revealInvisible(value) : 'unknown');

/**
 * The approval card for a job a coding assistant sent (one `pending` item of
 * GET /api/delegation).  The host has already masked credentials and bounded
 * the text; this lays it out as plain text with invisible characters made
 * visible.  The reference material itself is never shown, only its size.
 * The card says plainly that the answer leaves the laptop, because approving
 * is the disclosure-and-confirmation step for that egress.
 * Approve / Deny call onDecision(true | false).
 */
export function buildDelegateCard(doc, job, { onDecision } = {}) {
  const card = doc.createElement('div'); card.className = 'event-card confirmation-card delegate-card';
  const client = label(job?.caller?.client); const name = label(job?.caller?.name);
  const heading = doc.createElement('strong'); heading.textContent = 'A coding assistant asks BMO to do a job'; card.append(heading);
  card.append(detailRow(doc, 'From', `${client} · ${name}`));
  const taskHeading = doc.createElement('div'); taskHeading.className = 'confirmation-detail';
  const taskLabel = doc.createElement('strong'); taskLabel.textContent = Number.isSafeInteger(job?.task_chars) ? `Task (${job.task_chars.toLocaleString('en-US')} characters):` : 'Task:'; taskHeading.append(taskLabel);
  const task = doc.createElement('pre'); task.className = 'excerpt'; task.textContent = typeof job?.task === 'string' ? revealInvisible(job.task) : '';
  card.append(taskHeading, task);
  if (job?.task_masked === true) card.append(detailRow(doc, 'Note', 'Secret values in the task are shown as [redacted]; BMO still receives the task as sent.'));
  const contextChars = Number.isSafeInteger(job?.context_chars) && job.context_chars > 0 ? job.context_chars : 0;
  card.append(detailRow(doc, 'Reference material', contextChars ? `${contextChars.toLocaleString('en-US')} characters attached (not shown; BMO treats it as untrusted)` : 'None'));
  const folders = Array.isArray(job?.workspaces) ? job.workspaces.filter(id => typeof id === 'string').map(revealInvisible) : [];
  card.append(detailRow(doc, 'Files', job?.allow_files === true && folders.length ? `May read (never change) files in: ${folders.join(', ')}` : job?.files_requested === true ? 'Asked to read files, but no folder is shared with coding assistants, so it will read none.' : 'No file access'));
  card.append(detailRow(doc, 'Where the answer goes', `Back to ${client}, which may send it to a cloud service. Approve only if the answer, and anything BMO reads to write it, may leave this laptop.`));
  const countdown = doc.createElement('p'); countdown.className = 'countdown'; card.append(countdown);
  const actions = doc.createElement('div'); actions.className = 'confirmation-actions';
  const approve = doc.createElement('button'); approve.type = 'button'; approve.textContent = 'Approve';
  const deny = doc.createElement('button'); deny.type = 'button'; deny.textContent = 'Deny';
  approve.onclick = () => onDecision?.(true); deny.onclick = () => onDecision?.(false);
  actions.append(approve, deny); card.append(actions);
  return { card, countdown, approve, deny };
}

/** The status-grid badge text for /api/status `delegate`. */
export function delegateBadgeText(delegate) {
  if (!delegate || delegate.enabled !== true) return 'off';
  const queue = Number.isSafeInteger(delegate.queue) && delegate.queue > 0 ? ` · ${delegate.queue} waiting` : '';
  return `${delegate.approval === 'granted' ? 'on · allowed for now' : 'on · asks each time'}${queue}`;
}
