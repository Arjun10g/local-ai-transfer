// Short human labels for the live tool names, used in the status line and on
// tool cards.  Pure so node tests can check it covers every tool the host
// defines.  Unknown tools fall back to their raw name: a label must never
// hide which tool is actually running.

const LABELS = new Map(Object.entries({
  'time.now': 'Checking the time',
  'system.get_info': 'Checking system information',
  'clipboard.read': 'Reading the clipboard',
  'clipboard.write': 'Copying text to the clipboard',
  'app.open': 'Opening an app',
  'browser.open_url': 'Opening a web page',
  'fs.list': 'Listing files in a folder',
  'fs.read_text': 'Reading a file',
  'fs.search_text': 'Searching files',
  'fs.write_new': 'Creating a new file',
  'fs.apply_patch': 'Editing a file',
  'process.run_allowlisted': 'Running an approved program',
}));

const STATUS = new Map(Object.entries({ ok: 'done', failed: 'failed', denied: 'you said no', cancelled: 'not run', expired: 'expired' }));
const TOOL_NAME = /^[A-Za-z0-9_.:-]{1,96}$/;

/** "Reading a file" for fs.read_text; the raw name for unknown tools. */
export function toolLabel(name) {
  if (typeof name !== 'string' || !TOOL_NAME.test(name)) return 'Using a tool';
  return LABELS.get(name) ?? name;
}

/** True when `name` has a friendly label (the card then also shows the raw name). */
export function hasToolLabel(name) { return typeof name === 'string' && LABELS.has(name); }

/** Plain words for a tool result status; unknown statuses pass through bounded. */
export function toolStatusText(status) {
  if (typeof status !== 'string' || !status) return 'unknown';
  return STATUS.get(status) ?? status.slice(0, 32);
}
