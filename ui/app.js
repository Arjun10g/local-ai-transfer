import { describeError, isCancellation } from './errors.js';
import { renderMarkdown, revealInvisible, separateThinking } from './markdown.js';
import { clearTranscript, loadTranscript, normalizeItem, purgeLegacyLocalStorage, saveTranscript, transcriptToMarkdown } from './transcript.js';
import { hasToolLabel, toolLabel, toolStatusText } from './tool-labels.js';
import { buildDiff, buildExcerpt } from './cards.js';

// The single-use launch nonce arrives in the URL fragment.  Strip it from the
// address bar (and so from history, bookmarks, and screen shares) before any
// request is made.
const bootstrapParameters = new URLSearchParams(location.hash.startsWith('#') ? location.hash.slice(1) : '');
let bootstrapNonce = bootstrapParameters.size === 1 ? bootstrapParameters.get('bootstrap') : null;
history.replaceState(null, document.title, `${location.pathname}${location.search}`);

const CREDENTIAL = /^[A-Za-z0-9_-]{43}$/;
const OPAQUE_ID = /^[A-Za-z0-9_-]{8,96}$/;
// sessionStorage, not localStorage: the bearer then lives only as long as this
// tab and is never shared with other tabs or written as long-lived site data.
// It lets F5 reconnect after the single-use nonce has been spent.
const TOKEN_KEY = 'bmo.host_token';
const SESSION_KEY = 'bmo.session_id';
const SHOW_DEEP_KEY = 'bmo.ui.show_deep';
const TOOLS_OFF_KEY = 'bmo.ui.tools_off';
// The controller bounds a message in UTF-8 bytes (ENGINE_MAX_MESSAGE_BYTES).
const MAX_MESSAGE_BYTES = 32768;
const STATUS_POLL_MS = 5000;
const SLOW_HINT_MS = 30000;
const STOP_FALLBACK_MS = 15000;

const $ = selector => document.querySelector(selector);
const transcript = $('#transcript');
const status = $('#status');
const input = $('#message');
const sendButton = $('#send');
const resetButton = $('#reset');
const stopButton = $('#stop');

let headers;
let sessionId;
let mode = 'normal';
let toolsOff = false;
let activeRequest = null;
let activeAbort = null;
let stopRequested = false;
let items = [];
let current = null; // the assistant bubble that deltas currently stream into
const previews = new Map();
const pendingConfirmations = new Map();

class HostError extends Error { constructor(code, status = 0) { super(code); this.code = code; this.status = status; } }

function sessionGet(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
function sessionSet(key, value) { try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch { /* reload will need a fresh launch link */ } }
// Everything this page keeps lives in this tab's sessionStorage.  localStorage
// is only touched to delete what earlier builds left there (see transcript.js).
function tabStore() { try { return sessionStorage; } catch { return null; } }
function legacyStore() { try { return localStorage; } catch { return null; } }

// ---- host API ---------------------------------------------------------------

async function exchangeBootstrap() { if(typeof bootstrapNonce!=='string'||!CREDENTIAL.test(bootstrapNonce))throw new HostError('invalid_bootstrap');const nonce=bootstrapNonce;bootstrapNonce=null;let response;try{response=await fetch('/bootstrap',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({nonce}),referrerPolicy:'no-referrer',cache:'no-store',credentials:'omit',redirect:'error'});}catch{throw new HostError('network_error');}const value=await response.json().catch(()=>null);if(!response.ok)throw new HostError(typeof value?.error==='string'?value.error:'invalid_bootstrap',response.status);if(typeof value?.token!=='string'||!CREDENTIAL.test(value.token))throw new HostError('invalid_bootstrap');return value.token; }

async function api(path, { method = 'GET', body, signal } = {}) {
  try { return await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal, cache: 'no-store', referrerPolicy: 'no-referrer' }); }
  catch (error) { if (error?.name === 'AbortError') throw error; throw new HostError('network_error'); }
}
async function apiJson(path, options) {
  const response = await api(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) { const error = new HostError(typeof data?.error === 'string' ? data.error : `http_${response.status}`, response.status); if (response.status === 401) disconnected(); throw error; }
  return data;
}

// ---- transcript -------------------------------------------------------------

function scrollToEnd() { transcript.scrollTop = transcript.scrollHeight; }
// Reload-continuity for this tab only; dies with the tab, cleared by Reset.
function persist() { const store = tabStore(); if (store) saveTranscript(store, items); }

async function copyText(text, button) {
  let copied = false;
  try { await navigator.clipboard.writeText(text); copied = true; }
  catch {
    // Fallback for browsers that refuse the async clipboard on http://127.0.0.1.
    const area = document.createElement('textarea'); area.value = text; area.setAttribute('readonly', ''); area.className = 'offscreen'; document.body.append(area); area.select();
    try { copied = document.execCommand('copy'); } catch { copied = false; } area.remove();
  }
  if (button) { const label = button.textContent; button.textContent = copied ? 'Copied' : 'Copy failed'; setTimeout(() => { button.textContent = label; }, 1500); }
}

function messageActions(getText) {
  const actions = document.createElement('div'); actions.className = 'message-actions';
  const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'copy-button'; copy.textContent = 'Copy';
  copy.addEventListener('click', () => { void copyText(getText(), copy); }); actions.append(copy); return actions;
}

function fillAssistant(body, item, streaming) {
  const { answer, thinking, open } = separateThinking(item.text);
  const nodes = [];
  if (thinking) {
    // Reasoning stays out of the answer; it is available but collapsed.
    const details = document.createElement('details'); details.className = 'thinking';
    const summary = document.createElement('summary'); summary.textContent = open && streaming ? 'Thinking…' : 'Reasoning';
    const text = document.createElement('div'); text.className = 'thinking-text'; text.textContent = thinking;
    details.append(summary, text); nodes.push(details);
  }
  nodes.push(renderMarkdown(answer, { document, onCopy: (button, text) => { void copyText(text, button); } }));
  body.replaceChildren(...nodes);
  return { answer, open };
}

function renderItem(item) {
  const el = document.createElement('div');
  if (item.role === 'user') { el.className = 'message user'; el.textContent = item.text; if (item.status === 'not_sent') markNotSent(el); return el; }
  if (item.role === 'assistant') {
    el.className = 'message assistant'; const body = document.createElement('div'); body.className = 'message-body';
    fillAssistant(body, item, false); el.append(body, messageActions(() => revealInvisible(separateThinking(item.text).answer))); return el;
  }
  if (item.role === 'tool') { el.className = 'event-card tool-completion-card'; const heading = document.createElement('strong'); heading.textContent = `${toolLabel(item.name)}: ${toolStatusText(item.status)}`; el.append(heading); appendRawToolName(heading, item.name); if (item.code) detail(el, 'Problem', describeError(item.code).message); return el; }
  if (item.role === 'error') { el.className = 'event-card error-card'; el.setAttribute('role', 'alert'); const heading = document.createElement('strong'); heading.textContent = item.text; el.append(heading); if (item.action) { const next = document.createElement('p'); next.textContent = item.action; el.append(next); } if (item.code) { const code = document.createElement('small'); code.className = 'error-code'; code.textContent = `code: ${item.code}`; el.append(code); } return el; }
  el.className = 'note'; el.textContent = item.text; return el;
}

// The host reported that the model will not see this message next turn.
function markNotSent(el) { if (el.classList.contains('not-sent')) return; el.classList.add('not-sent'); const label = document.createElement('small'); label.className = 'not-sent-label'; label.textContent = 'Not sent: BMO will not remember this message.'; el.append(label); }

function addItem(raw) {
  const item = normalizeItem({ at: Date.now(), ...raw }); if (!item) return null;
  items.push(item); const el = renderItem(item); transcript.append(el); scrollToEnd(); persist(); return { item, el };
}
function showError(code, extra = {}) { const info = describeError(code, extra); return addItem({ role: 'error', text: info.message, action: info.action, code: info.code }); }
function addNote(text) { return addItem({ role: 'note', text }); }

function renderAll() { transcript.replaceChildren(...items.map(renderItem)); scrollToEnd(); }

// Streaming text is re-rendered at most once per frame; replies are short
// (the engine caps output at 256 tokens per step), so a full re-parse is cheap.
let renderScheduled = false;
function scheduleAssistantRender() {
  if (renderScheduled) return; renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false; if (!current) return;
    const { open } = fillAssistant(current.body, current.item, true);
    if (open) setPhase('Thinking…'); scrollToEnd();
  });
}
function ensureAssistant() {
  if (current) return current;
  const item = normalizeItem({ role: 'assistant', text: '', at: Date.now() }); items.push(item);
  const el = document.createElement('div'); el.className = 'message assistant streaming';
  const body = document.createElement('div'); body.className = 'message-body';
  el.append(body, messageActions(() => revealInvisible(separateThinking(item.text).answer))); transcript.append(el);
  current = { item, el, body }; return current;
}
// After a tool runs, the next text belongs in a new bubble below the tool card.
function closeAssistant(finalText) {
  if (!current) return;
  if (typeof finalText === 'string') current.item.text = finalText;
  fillAssistant(current.body, current.item, false); current.el.classList.remove('streaming');
  if (!current.item.text.trim()) { current.el.remove(); items = items.filter(item => item !== current.item); }
  current = null; persist();
}

// ---- activity line ------------------------------------------------------------

const activity = { startedAt: 0, progressAt: 0, phase: '', waitingOnUser: false, timer: null };
function formatElapsed(ms) { const total = Math.max(0, Math.floor(ms / 1000)); if (total < 60) return `${total} s`; const minutes = Math.floor(total / 60); return `${minutes} min ${String(total % 60).padStart(2, '0')} s`; }
function tickActivity() {
  const now = Date.now();
  $('#activity-elapsed').textContent = formatElapsed(now - activity.startedAt);
  $('#activity-hint').hidden = activity.waitingOnUser || stopRequested || now - activity.progressAt < SLOW_HINT_MS;
}
function setPhase(text, { waitingOnUser = false } = {}) {
  if (activity.phase !== text) { activity.phase = text; $('#activity-text').textContent = text; }
  activity.waitingOnUser = waitingOnUser; activity.progressAt = Date.now(); tickActivity();
}
function beginActivity() {
  activity.startedAt = Date.now(); activity.progressAt = activity.startedAt; activity.phase = '';
  stopButton.disabled = false; stopButton.textContent = 'Stop'; $('#activity').hidden = false;
  setPhase('Sending…'); clearInterval(activity.timer); activity.timer = setInterval(tickActivity, 1000);
}
function endActivity() { clearInterval(activity.timer); activity.timer = null; $('#activity').hidden = true; $('#activity-hint').hidden = true; }
function setBusy(busy) { sendButton.disabled = busy || !headers; resetButton.disabled = busy; sendButton.textContent = busy ? 'Working…' : 'Send'; if (busy) setBadge('working', 'Working'); }

// ---- confirmation and tool cards ----------------------------------------------

function detail(card, label, value) { const row = document.createElement('div'); row.className = 'confirmation-detail'; const name = document.createElement('strong'); name.textContent = `${label}: `; const text = document.createElement('span'); text.textContent = value; row.append(name, text); card.append(row); }


const EGRESS_LABELS = { operator_configured: 'Possibly, as configured by the operator', external_navigation: 'Yes, opens an external web page' };
const HIDDEN_PREVIEW_KEY = /digest|sha256|hash|token|binding|authori[sz]ation|^provider$|_id$|^preview_|truncated$/i;
// Generic preview fields for local tools (files, programs, web addresses).
// Everything goes through textContent; unknown scalar fields are shown
// bounded so a new tool's preview is visible without a UI change.
function renderGenericPreview(card, preview, skip = new Set()) {
  let shown = 0;
  const show = (label, value) => { if (skip.has(label)) return; detail(card, label, value); shown += 1; };
  if (typeof preview.path === 'string') show('File', preview.path);
  if (Number.isSafeInteger(preview.old_bytes) && Number.isSafeInteger(preview.new_bytes)) show('Size', `${preview.old_bytes.toLocaleString()} → ${preview.new_bytes.toLocaleString()} bytes`);
  if (preview.changed === false) show('Change', 'None (the file would stay the same)');
  if (typeof preview.executable === 'string') show('Program', preview.executable);
  if (Array.isArray(preview.argv)) show('Arguments', preview.argv.filter(value => typeof value === 'string').join(' ') || '(none)');
  if (typeof preview.cwd === 'string') show('Folder', preview.cwd);
  if (typeof preview.destination === 'string') show('External destination (network egress)', preview.destination);
  if (typeof preview.network === 'boolean') show('Network', preview.network ? 'May use the network' : 'No network');
  if (typeof preview.data_egress === 'string') show('Data leaves this laptop', EGRESS_LABELS[preview.data_egress] ?? preview.data_egress);
  if (Number.isSafeInteger(preview.timeout_ms)) show('Time limit', formatElapsed(preview.timeout_ms));
  const handled = new Set(['path', 'old_bytes', 'new_bytes', 'changed', 'executable', 'argv', 'cwd', 'destination', 'network', 'data_egress', 'timeout_ms', 'diff', 'diff_lines', 'diff_structured', 'diff_redacted', 'data_categories']);
  let extra = 0;
  for (const [key, value] of Object.entries(preview)) {
    if (handled.has(key) || HIDDEN_PREVIEW_KEY.test(key) || extra >= 8) continue;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') { show(key.replaceAll('_', ' '), String(value).slice(0, 500)); extra += 1; }
  }
  const diff = buildDiff(document, preview); if (diff.length) { card.append(...diff); shown += 1; }
  return shown;
}

// `arguments_summary` is the host's sanitized, bounded display copy of the
// call's arguments (sent beside `call` on tool.confirmation_required).  The
// host has already clipped and redacted it; this only lays it out.
const SUMMARY_KINDS = { file_create: 'Create a new file', file_replace: 'Replace a file\'s contents', process_run: 'Run a program', open_url: 'Open a web page', open_app: 'Open an app', clipboard_write: 'Copy text to the clipboard' };
const SUMMARY_LABELS = { workspace_id: 'Workspace', path: 'File', action_id: 'Action', executable: 'Program', cwd: 'Folder', url: 'Web address', app_id: 'App' };
function renderArgumentsSummary(card, summary) {
  const shown = new Set();
  if (typeof summary.kind === 'string') detail(card, 'Action', SUMMARY_KINDS[summary.kind] ?? summary.kind);
  for (const [key, label] of Object.entries(SUMMARY_LABELS)) if (typeof summary[key] === 'string' && !(key === 'action_id' && summary.kind)) { detail(card, label, summary[key]); shown.add(label); }
  const size = ['content_bytes', 'new_bytes', 'text_bytes'].find(key => Number.isSafeInteger(summary[key]));
  if (size) { detail(card, 'Size', `${Number.isSafeInteger(summary.old_bytes) ? `${summary.old_bytes.toLocaleString()} → ` : ''}${summary[size].toLocaleString()} bytes`); shown.add('Size'); }
  if (summary.changed === false) { detail(card, 'Change', 'None (the file would stay the same)'); shown.add('Change'); }
  if (Array.isArray(summary.argv)) { detail(card, 'Arguments', summary.argv.filter(value => typeof value === 'string').join(' ') || '(none)'); shown.add('Arguments'); }
  if (summary.parameters && typeof summary.parameters === 'object' && !Array.isArray(summary.parameters)) for (const [key, value] of Object.entries(summary.parameters).slice(0, 32)) detail(card, `Parameter ${key}`, value === null ? '(not shown)' : String(value));
  for (const [key, label] of [['content', 'Content'], ['replacement', 'New content'], ['text', 'Text']]) card.append(...buildExcerpt(document, summary, key, label));
  return shown;
}

function renderConfirmation(ev) {
  const call = ev.data?.call ?? {}; const preview = ev.data?.preview ?? previews.get(call.id) ?? {};
  const summary = ev.data?.arguments_summary && typeof ev.data.arguments_summary === 'object' && !Array.isArray(ev.data.arguments_summary) ? ev.data.arguments_summary : null; const card = document.createElement('div'); card.className = 'event-card confirmation-card'; const heading = document.createElement('strong'); heading.textContent = `Confirmation required: ${toolLabel(call.name)}`; appendRawToolName(heading, call.name); card.append(heading);
  if (ev.data?.phase === 'preview_access') { detail(card, 'Purpose', 'Read the existing draft to prepare a send preview'); detail(card, 'Destination', 'Signed-in Outlook mailbox'); }
  else if (call.name === 'mail.create_draft' || call.name === 'mail.send_draft') { if (typeof preview.draft_id === 'string') detail(card, 'Draft', preview.draft_id); if (Array.isArray(preview.recipients)) detail(card, 'Recipients', preview.recipients.join(', ')); if (typeof preview.subject === 'string') detail(card, 'Subject', preview.subject); if (typeof preview.body_preview === 'string') detail(card, 'Body preview', preview.body_preview + (preview.body_truncated ? '…' : '')); }
  else if (call.name === 'teams.send_message') { if (typeof preview.chat_id === 'string') detail(card, 'Chat', preview.chat_id); if (typeof preview.body_preview === 'string') detail(card, 'Message preview', preview.body_preview); }
  else if (call.name === 'browser.fill_field' || call.name === 'browser.activate_control') { if (typeof preview.control_label === 'string') detail(card, 'Control', preview.control_label); if (typeof preview.value_preview === 'string') detail(card, 'Value', preview.value_preview + (preview.value_truncated ? '…' : '')); if (typeof preview.destination === 'string') detail(card, 'Destination', preview.destination); }
  else if (summary) { const shown = renderArgumentsSummary(card, summary); if (preview && typeof preview === 'object' && !Array.isArray(preview)) renderGenericPreview(card, preview, shown); }
  else if (!preview || typeof preview !== 'object' || Array.isArray(preview) || renderGenericPreview(card, preview) === 0) {
    // Without a preview or an arguments summary from the host there is
    // genuinely nothing more specific to show.
    detail(card, 'Effect', 'This action may change something on this computer or send data elsewhere. BMO cannot show its details.');
  }
  if (typeof preview?.data_categories?.join === 'function') detail(card, 'Data', preview.data_categories.join(', '));

  const countdown = document.createElement('p'); countdown.className = 'countdown'; card.append(countdown);
  const actions = document.createElement('div'); actions.className = 'confirmation-actions';
  const approve = document.createElement('button'); approve.type = 'button'; approve.textContent = 'Approve';
  const deny = document.createElement('button'); deny.type = 'button'; deny.textContent = 'Deny';
  actions.append(approve, deny); card.append(actions);
  // The host auto-denies after expires_in_ms; the card counts down from when
  // it arrived, one second early so a last-moment click is not lost silently.
  const expiresIn = Number.isSafeInteger(ev.data?.expires_in_ms) && ev.data.expires_in_ms > 0 ? ev.data.expires_in_ms : 30000;
  const deadline = Date.now() + expiresIn - 1000;
  let timer = null;
  const finish = (text, state) => { clearInterval(timer); for (const button of actions.querySelectorAll('button')) button.disabled = true; countdown.textContent = text; card.dataset.state = state; card.classList.remove('urgent'); pendingConfirmations.delete(call.id); };
  const tick = () => { const left = Math.ceil((deadline - Date.now()) / 1000); if (left <= 0) { finish('Expired. Nobody answered in time, so the action was not run.', 'expired'); return; } countdown.textContent = `Waiting for your decision: ${formatElapsed(left * 1000)} left`; card.classList.toggle('urgent', left <= 10); };
  const submit = async approved => {
    for (const button of actions.querySelectorAll('button')) button.disabled = true;
    countdown.textContent = approved ? 'Approving…' : 'Denying…';
    try {
      const response = await fetch(`/api/tool-confirmations/${ev.data.confirmation_id}`, { method: 'POST', headers, body: JSON.stringify({approved,request_id:ev.request_id,call_id:call.id}) });
      if (response.ok) finish(approved ? 'Approved.' : 'Denied.', approved ? 'approved' : 'denied');
      else if (response.status === 410) finish('Expired. Nobody answered in time, so the action was not run.', 'expired');
      else finish(response.status === 404 ? 'Expired or already answered.' : describeError(null, { status: response.status }).message, 'expired');
    } catch { finish(describeError('network_error').message, 'expired'); }
  };
  let answered = false;
  approve.onclick = () => { answered = true; void submit(true); }; deny.onclick = () => { answered = true; void submit(false); };
  // Outcome reported by the host stream: tool.started, or the request ending.
  pendingConfirmations.set(call.id, outcome => {
    if (outcome === 'approved') finish('Approved.', 'approved');
    else if (outcome === 'denied' && answered) finish('Denied.', 'denied');
    else if (outcome === 'denied' || outcome === 'expired') finish('Expired. Nobody answered in time, so the action was not run.', 'expired');
    else finish('No longer pending.', 'expired');
  });
  tick(); timer = setInterval(tick, 1000);
  transcript.append(card); scrollToEnd();
}

// A side-effecting call the host announced: its sanitized argument summary is
// shown right away; a confirmation card for the same call replaces it.
const proposedCards = new Map();
function renderProposed(ev) {
  const call = ev.data?.call ?? {}; const summary = ev.data?.arguments_summary;
  if (!summary || typeof summary !== 'object' || Array.isArray(summary) || typeof call.id !== 'string') return;
  proposedCards.get(call.id)?.remove();
  const card = document.createElement('div'); card.className = 'event-card proposed-card';
  const heading = document.createElement('strong'); heading.textContent = `About to: ${toolLabel(call.name)}`; appendRawToolName(heading, call.name); card.append(heading);
  renderArgumentsSummary(card, summary);
  proposedCards.set(call.id, card); transcript.append(card); scrollToEnd();
}

// The engine stopped at its output-token cap; one click asks it to go on.
function offerContinue(el) {
  if (!el) return;
  const button = document.createElement('button'); button.type = 'button'; button.className = 'continue-button'; button.textContent = 'Continue';
  button.title = 'The answer hit the length limit. Ask BMO to continue.';
  button.addEventListener('click', () => { if (activeRequest) return; button.remove(); input.value = 'Please continue.'; void send(); });
  (el.querySelector('.message-actions') ?? el).prepend(button);
}

function renderToolCompletion(ev) {
  const result = ev.data?.result ?? {}; const name = typeof result.name === 'string' ? result.name : 'tool action'; const state = typeof result.status === 'string' ? result.status : 'unknown';
  let payload = null; const text = Array.isArray(result.content) ? result.content.find(item => item && item.type === 'text' && typeof item.text === 'string' && item.text.length <= 4096)?.text : undefined; if (text) { try { const parsed = JSON.parse(text); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) payload = parsed; } catch { /* display only bounded typed fields below */ } }
  const code = state === 'failed' && typeof payload?.code === 'string' ? payload.code : undefined;
  const added = addItem({ role: 'tool', name, status: state, code });
  if (added && state === 'failed') { if (typeof payload?.operation_id === 'string') detail(added.el, 'Operation', payload.operation_id); if (typeof payload?.state === 'string') detail(added.el, 'State', payload.state); if (payload?.code === 'action_completion_unverified') detail(added.el, 'Provider result', 'Unverified; reconciliation is required.'); }
}

// ---- chat -----------------------------------------------------------------

function handleEvent(ev) {
  const data = ev.data ?? {};
  if (ev.event === 'message.started') setPhase(data.continuation === true || data.state === 'CONTINUING_MODEL' ? 'Reading the tool result…' : data.state === 'BUILDING_PROMPT' ? 'Preparing…' : 'Reading your message…');
  else if (ev.event === 'message.delta') { if (typeof data.text !== 'string') return; ensureAssistant().item.text += data.text; setPhase('Writing…'); scheduleAssistantRender(); }
  else if (ev.event === 'message.completed') { const bubble = current; closeAssistant(typeof data.text === 'string' ? data.text : undefined); if (data.finish_reason === 'length') offerContinue(bubble?.el); return 'done'; }
  else if (ev.event === 'tool.proposed' && ev.data.call?.id) { const destination = ev.data.preview?.destination; if (typeof destination === 'string') previews.set(ev.data.call.id,destination); previews.set(ev.data.call.id, ev.data.preview ?? {}); closeAssistant(); renderProposed(ev); setPhase(`Getting ready: ${toolLabel(data.call?.name)}…`); }
  else if (ev.event === 'tool.confirmation_required') { closeAssistant(); proposedCards.get(ev.data?.call?.id)?.remove(); proposedCards.delete(ev.data?.call?.id); renderConfirmation(ev); setPhase('Waiting for your approval…', { waitingOnUser: true }); }
  else if (ev.event === 'tool.started') { pendingConfirmations.get(data.call?.id)?.(data.approved === false ? 'denied' : 'approved'); setPhase(data.approved === false ? 'Telling the assistant you said no…' : `${toolLabel(data.call?.name)}…`); }
  else if (ev.event === 'tool.failed') {
    const code = typeof data.code === 'string' ? data.code : 'request_failed';
    // An expired confirmation already has its card (and a tool.completed
    // card follows), so only the card is updated; other failures get a card.
    if (code === 'confirmation_expired') pendingConfirmations.get(data.call?.id)?.('expired');
    else { closeAssistant(); addItem({ role: 'tool', name: toolName(data.call), status: 'failed', code }); }
    setPhase('Reading the tool result…');
  }
  else if (ev.event === 'tool.completed') { closeAssistant(); renderToolCompletion(ev); setPhase('Reading the tool result…'); }
  else if (ev.event === 'request.cancelled') { closeAssistant(); unsentIfDropped(data); addNote('Stopped.'); return 'done'; }
  else if (ev.event === 'request.failed') { closeAssistant(); unsentIfDropped(data); if (isCancellation(data.code)) addNote('Stopped.'); else showError(typeof data.code === 'string' ? data.code : 'request_failed'); return 'done'; }
  else if (ev.event === 'metrics.snapshot' && data.context_compaction) addNote('Older parts of this conversation were shortened to fit the model\'s memory.');
  return undefined;
}
const toolName = call => typeof call?.name === 'string' ? call.name : 'a tool';
let currentUserEntry = null;
function unsentIfDropped(data) { if (data.user_message_kept !== false || !currentUserEntry) return; currentUserEntry.item.status = 'not_sent'; markNotSent(currentUserEntry.el); persist(); }
// The friendly label leads; the exact tool name stays visible beside it so a
// label can never disguise what is being approved or run.
function appendRawToolName(parent, name) { if (!hasToolLabel(name)) return; const raw = document.createElement('small'); raw.className = 'tool-name'; raw.textContent = ` (${name})`; parent.append(raw); }

async function readEvents(stream, onEvent) {
  const reader = stream.getReader(); const decoder = new TextDecoder(); let buffer = ''; let done = false;
  const consume = frame => { const line = frame.split('\n').find(value => value.startsWith('data: ')); if (!line) return; let ev; try { ev = JSON.parse(line.slice(6)); } catch { return; /* malformed UI event is ignored */ } if (ev && typeof ev === 'object' && onEvent(ev) === 'done') done = true; };
  for (;;) {
    const { value, done: ended } = await reader.read();
    if (ended) break;
    buffer += decoder.decode(value, { stream: true }); const frames = buffer.split('\n\n'); buffer = frames.pop();
    for (const frame of frames) consume(frame);
  }
  if (buffer.trim()) consume(buffer);
  return done;
}

async function send(event) {
  event?.preventDefault();
  const message = input.value.trim();
  if (!message || activeRequest || !headers || !sessionId) return;
  if (new TextEncoder().encode(message).length > MAX_MESSAGE_BYTES) { showError('invalid_message_too_large'); return; }
  input.value = '';
  const userEntry = addItem({ role: 'user', text: message }); currentUserEntry = userEntry;
  const requestId = `req_${crypto.randomUUID().replaceAll('-', '')}`;
  activeRequest = requestId; stopRequested = false; activeAbort = new AbortController();
  setBusy(true); beginActivity();
  let responded = false;
  // The host refused or was unreachable before starting: give the text back
  // so nothing the user typed is lost.
  const unsend = () => { if (userEntry) { userEntry.el.remove(); items = items.filter(item => item !== userEntry.item); persist(); } if (!input.value) input.value = message; };
  try {
    const response = await api('/api/chat', { method: 'POST', body: { session_id: sessionId, message, mode, request_id: requestId, ...(toolsOff ? { tools: 'off' } : {}) }, signal: activeAbort.signal });
    responded = true;
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      unsend();
      showError(typeof data?.error === 'string' ? data.error : null, { status: response.status });
      if (response.status === 401) disconnected();
      return;
    }
    const finished = await readEvents(response.body, handleEvent);
    if (!finished) { closeAssistant(); if (stopRequested) addNote('Stopped.'); else showError('stream_interrupted'); }
  } catch (error) {
    closeAssistant();
    if (!responded) unsend();
    if (stopRequested) addNote('Stopped.');
    else showError(error instanceof HostError ? error.code : 'stream_interrupted');
  } finally {
    for (const settle of [...pendingConfirmations.values()]) settle('gone');
    activeRequest = null; activeAbort = null; currentUserEntry = null; previews.clear(); proposedCards.clear(); closeAssistant(); persist();
    endActivity(); setBusy(false); input.focus(); void refreshStatus();
  }
}

async function stop() {
  if (!activeRequest || stopRequested) return;
  stopRequested = true; stopButton.disabled = true; stopButton.textContent = 'Stopping…';
  setPhase('Stopping… the engine finishes its current step first.');
  const requestId = activeRequest; const abort = activeAbort;
  try { await api('/api/cancel', { method: 'POST', body: { request_id: requestId } }); } catch { /* the fallback below still ends the stream */ }
  // If the host never confirms, drop the connection; the host treats a
  // closed stream as a cancellation too.
  setTimeout(() => { if (activeRequest === requestId) abort?.abort(); }, STOP_FALLBACK_MS);
}

async function resetConversation() {
  if (activeRequest) { addNote('Press Stop first, then Reset.'); return; }
  if (!confirm('Start a new conversation?\n\nThis clears the messages on screen and BMO\'s memory of them.')) return;
  try { await apiJson('/api/sessions', { method: 'POST', body: { session_id: sessionId, reset: true } }); }
  catch (error) { showError(error instanceof HostError ? error.code : 'network_error', { status: error?.status }); return; }
  items = []; current = null; const store = tabStore(); if (store) clearTranscript(store); renderAll();
}

function exportConversation() {
  const markdown = transcriptToMarkdown(items);
  const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
  const link = document.createElement('a'); link.href = url; link.download = `bmo-conversation-${stamp}.md`; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- status -----------------------------------------------------------------

function setBadge(state, text) { const badge = $('#health'); badge.dataset.state = state; badge.textContent = text; }
const isActiveProvider = state => typeof state === 'string' && !['disabled', 'unconfigured', 'unavailable'].includes(state);

function applyStatus(info) {
  const engine = info?.engine ?? {};
  $('#engine').textContent = [engine.engine, engine.model].filter(value => typeof value === 'string').join(' · ') || 'unknown';
  $('#backend').textContent = typeof engine.backend === 'string' ? engine.backend : 'unknown';
  $('#network').textContent = info?.network?.enabled === true ? info.network.provider : 'off (local only)';
  const journal = info?.action_journal ?? {};
  $('#actions').textContent = journal.durable_action_dispatch === true ? 'available' : 'read-only (safety log unavailable)';
  if (engine.ready === true) { setBadge('ready', 'Ready'); status.textContent = 'Ready. Answers are generated on this laptop and can take a minute or two.'; }
  else { const hint = describeError('not_ready'); setBadge('not-ready', 'Not ready'); status.textContent = `${hint.message} ${hint.action}`; }
  // Parked integrations stay hidden unless something is actually configured.
  const providers = info?.providers && typeof info.providers === 'object' ? info.providers : {};
  const graph = isActiveProvider(providers.microsoft_graph);
  const grants = Number.isSafeInteger(info?.operator_grants?.available) && info.operator_grants.available > 0;
  $('#graph-controls').hidden = !graph;
  $('#access-panel').hidden = !(graph || grants);
  $('#external-notice').hidden = !(Object.values(providers).some(isActiveProvider) || info?.network?.enabled === true);
  return { graph, grants };
}

let statusInFlight = false;
async function refreshStatus() {
  if (!headers || statusInFlight) return;
  // While generating, the engine may be too busy to answer health checks
  // quickly; the activity line already shows progress.
  if (activeRequest) { setBadge('working', 'Working'); return; }
  statusInFlight = true;
  try { applyStatus(await apiJson('/api/status')); }
  catch (error) { if (error?.code !== 'unauthorized' && error?.status !== 401) { setBadge('offline', 'Offline'); const info = describeError('network_error'); status.textContent = `${info.message} ${info.action}`; } }
  finally { statusInFlight = false; }
}

function disconnected() {
  sessionSet(TOKEN_KEY, null); headers = undefined;
  setBadge('offline', 'Disconnected'); const info = describeError('session_expired'); status.textContent = `${info.message} ${info.action}`;
  sendButton.disabled = true;
}

// ---- operator grants and Microsoft Graph (parked unless configured) -----------

async function renderGrants() { const response = await api('/api/operator-grants'); if (!response.ok) return; const { capabilities = [] } = await response.json(); const list = $('#grant-list'); list.replaceChildren(); if (!capabilities.length) { const empty = document.createElement('p'); empty.textContent = 'No grantable capabilities are configured.'; list.append(empty); return; } for (const item of capabilities) { const row = document.createElement('div'); row.className = 'grant-row'; const description = document.createElement('span'); description.textContent = item.label; const detailText = document.createElement('small'); detailText.textContent = `${item.provider} · ${item.scope}${item.granted ? ` · expires ${new Date(item.expires_at).toLocaleTimeString()}` : ''}`; description.append(detailText); const button = document.createElement('button'); button.type = 'button'; button.textContent = item.granted ? 'Revoke' : 'Grant 1 hour'; button.onclick = async () => { if (!item.granted && !confirm(`Grant one hour of full access to: ${item.label}?\n\nThis does not auto-approve external sends, installs, elevation, purchases, deletion, or security changes.`)) return; const body = item.granted ? { granted: false } : { granted: true, duration_ms: 3600000 }; await api(`/api/operator-grants/${item.capability}`, { method: 'POST', body }); await renderGrants(); }; row.append(description, button); list.append(row); } }
async function refreshGraphAuth() { const response = await api('/api/provider-auth/microsoft_graph'); if (!response.ok) return; const auth = (await response.json()).microsoft_graph ?? {}; const label = $('#graph-auth-status'); label.textContent = auth.prompt ? `Enter ${auth.prompt.userCode} at ${auth.prompt.verificationUri}` : auth.state === 'authenticated' && auth.account_verified === true ? 'Connected (account verified)' : auth.state; }
async function startGraphAuth() { const response = await api('/api/provider-auth/microsoft_graph/start', { method: 'POST', body: {} }); if (!response.ok) return; for (let count = 0; count < 900; count += 1) { await refreshGraphAuth(); const statusResponse = await api('/api/provider-auth/microsoft_graph'); const state = (await statusResponse.json()).microsoft_graph?.state; if (state !== 'requesting_device_code' && state !== 'awaiting_user') break; await new Promise(resolve => setTimeout(resolve, 1000)); } }

// ---- start-up ---------------------------------------------------------------

async function connect() {
  let token = null; let fresh = false; let failure = 'no_credential';
  if (bootstrapNonce !== null) { try { token = await exchangeBootstrap(); fresh = true; } catch (error) { failure = error instanceof HostError ? error.code : 'invalid_bootstrap'; } }
  if (!token) { const stored = sessionGet(TOKEN_KEY); if (stored && CREDENTIAL.test(stored)) token = stored; }
  if (!token) throw new HostError(failure);
  headers = { 'authorization': `Bearer ${token}`, 'content-type': 'application/json' };
  let info;
  try { info = await apiJson('/api/status'); }
  catch (error) { if (error?.status === 401) throw new HostError('session_expired', 401); throw error; }
  sessionSet(TOKEN_KEY, token);
  return { fresh, info };
}

async function init() {
  purgeLegacyLocalStorage(legacyStore());
  const store = tabStore(); items = store ? loadTranscript(store) : [];
  if (sessionGet(SHOW_DEEP_KEY) === '1') { $('#normal').hidden = false; $('#deep').hidden = false; }
  toolsOff = sessionGet(TOOLS_OFF_KEY) === '1'; applyToolsToggle();
  renderAll();
  let connection;
  try { connection = await connect(); }
  catch (error) { const code = error instanceof HostError ? error.code : 'network_error'; const info = describeError(code); setBadge('offline', code === 'network_error' ? 'Offline' : 'Disconnected'); status.textContent = `${info.message} ${info.action}`; sendButton.disabled = true; return; }
  const { fresh, info } = connection;
  // A fresh launch link means a new host process (and a new port, so a new
  // origin with empty tab storage); a reload of the same tab resumes both.
  const previousSession = fresh ? null : sessionGet(SESSION_KEY);
  const data = await apiJson('/api/sessions', { method: 'POST', body: previousSession && OPAQUE_ID.test(previousSession) ? { session_id: previousSession } : {} });
  sessionId = data.session_id; sessionSet(SESSION_KEY, sessionId);
  const { graph, grants } = applyStatus(info);
  if (grants) await renderGrants().catch(() => {});
  if (graph) await refreshGraphAuth().catch(() => {});
  sendButton.disabled = false; input.focus();
  setInterval(() => { void refreshStatus(); }, STATUS_POLL_MS);
}

$('#chat').addEventListener('submit', send);
input.addEventListener('keydown', event => {
  // Enter sends; Shift+Enter (or an IME composition) inserts a newline.
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
  event.preventDefault(); if (!sendButton.disabled) void send();
});
stopButton.onclick = () => { void stop(); };
resetButton.onclick = () => { void resetConversation(); };
$('#export').onclick = exportConversation;
function applyToolsToggle() { const button = $('#tools'); button.setAttribute('aria-pressed', String(!toolsOff)); button.textContent = toolsOff ? 'Tools off' : 'Tools on'; }
$('#tools').onclick = () => { toolsOff = !toolsOff; sessionSet(TOOLS_OFF_KEY, toolsOff ? '1' : null); applyToolsToggle(); };
$('#normal').onclick = () => { mode = 'normal'; $('#normal').setAttribute('aria-pressed', 'true'); $('#deep').setAttribute('aria-pressed', 'false'); };
$('#deep').onclick = () => { mode = 'deep'; $('#deep').setAttribute('aria-pressed', 'true'); $('#normal').setAttribute('aria-pressed', 'false'); };
$('#revoke-all').onclick = async () => { if (activeRequest) await api('/api/cancel', { method: 'POST', body: { request_id: activeRequest } }).catch(() => {}); await api('/api/operator-grants/revoke-all', { method: 'POST', body: {} }).catch(() => {}); await renderGrants().catch(() => {}); };
$('#graph-auth').onclick = () => { void startGraphAuth().catch(() => { $('#graph-auth-status').textContent = 'Authentication failed'; }); };
$('#graph-auth-cancel').onclick = async () => { await api('/api/provider-auth/microsoft_graph/cancel', { method: 'POST', body: {} }).catch(() => {}); await refreshGraphAuth().catch(() => {}); };
$('#graph-auth-clear').onclick = async () => { await api('/api/provider-auth/microsoft_graph/clear', { method: 'POST', body: {} }).catch(() => {}); await refreshGraphAuth().catch(() => {}); };
addEventListener('pagehide', persist);
sendButton.disabled = true;
init().catch(error => { setBadge('offline', 'Offline'); const info = describeError(error instanceof HostError ? error.code : 'network_error'); status.textContent = `${info.message} ${info.action}`; });
