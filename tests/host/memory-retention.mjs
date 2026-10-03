#!/usr/bin/env node
// What survives in the prompt as a conversation outgrows the context window?
//
// Run by hand (it is a measurement, not a test):
//
//     node tests/host/memory-retention.mjs            # Markdown tables
//     node tests/host/memory-retention.mjs --json     # the raw numbers
//     node tests/host/memory-retention.mjs --turns 10,20,40,80 --seeds 5 --mode both \
//         [--profile mixed|short_chat] [--note-tokens 256] [--chars-per-token 3.5]
//
// It builds synthetic conversations of N turns at realistic message sizes
// (short chat, pasted JSON, tool results, code), plants facts of several
// types, and drives them through the REAL ConversationController -- the real
// budget, elision, turn dropping, hard storage bounds and (with
// `--mode summary`) the memory-note path -- against a fake engine that
// enforces the 8,192-token window.  At the last turn it reports which planted
// facts are still in the prompt the model would be sent.
//
// This is a pure function of the host's policy.  It cannot say whether the
// real model would USE a surviving fact correctly (that needs
// scripts/test/long_context_eval.py), and in summary mode the summariser is
// a deterministic fake that copies facts perfectly, so those rows are an
// upper bound on what a real note could keep, not a measurement of one
// (scripts/test/memory_eval.py measures the real model's notes).
import { pathToFileURL } from 'node:url';
import { ConversationController } from '../../host/agent/controller.mjs';
import { estimatePromptTokens, isElidedToolResult } from '../../host/agent/context-budget.mjs';
import { MEMORY_PROMPTS } from '../../host/agent/memory-note.mjs';
import { NativeEngineError } from '../../host/engine/native-engine-client.mjs';

export const FACT_TYPES = Object.freeze(['name', 'number', 'preference', 'decision', 'tool_only', 'updated']);
export const AGE_BUCKETS = Object.freeze([[1, 5], [6, 10], [11, 20], [21, 40], [41, 80]]);

// Deterministic PRNG (mulberry32): the same seed builds the same conversation.
export function rngFor(seed) {
  let a = (seed * 0x9e3779b1) >>> 0;
  const next = () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  return { next, int: (lo, hi) => lo + Math.floor(next() * (hi - lo + 1)), pick: list => list[Math.floor(next() * list.length)] };
}

// Same register as scripts/test/long_context_eval.py's fillers.  None of
// these may match a fact pattern below.
const FILLER = [
  'I moved the standup to half past nine so the people commuting by train are not rushing.',
  'The build on the release branch went green again after we pinned the compiler version.',
  'Can you remind me why we chose a weekly cadence for the dependency updates?',
  'We still need someone to own the onboarding checklist for new contractors.',
  'My laptop fan has been loud all morning; I think the indexer is running again.',
  'The design review notes are in the shared folder under the October heading.',
  'If the vendor cannot confirm delivery by Thursday we should switch to the backup supplier.',
  'The dashboard numbers looked odd because the timezone was set to the server default.',
  'Please keep the summary under one page; nobody reads the second page.',
  'Most of the flaky tests were waiting on a fixed sleep instead of polling for readiness.',
];
const REPLIES = [
  'Understood. Smaller, more frequent changes are easier to review and to roll back, so that cadence makes sense.',
  'That explains the odd numbers: a dashboard that renders in the server timezone will shift every daily bucket by the offset.',
  'Polling for readiness is more reliable than a fixed sleep, because the sleep is either too short on a slow machine or wasted time on a fast one.',
  'I can draft that summary whenever you are ready; one page with the decisions first and the open questions last usually works.',
  'If delivery slips past Thursday, switching to the backup supplier is the safer option, even at a slightly higher unit price.',
  'A short checklist owned by one person tends to stay current; a long one owned by everyone usually goes stale.',
];
const FIRST = ['Thessaly', 'Ignatius', 'Marisol', 'Oluwaseun', 'Bronwyn', 'Leopold', 'Anouk', 'Teodoro', 'Saoirse', 'Kasimir'];
const LAST = ['Okonkwo', 'Lindqvist', 'Arbuthnot', 'Nakashima', 'Delacroix', 'Rautio', 'Fairweather', 'Szymanski', 'Abernathy', 'Quaresma'];
const WORDS = ['AMBER', 'FALCON', 'COBALT', 'MERIDIAN', 'LANTERN', 'SABLE', 'TIDEWATER', 'QUARTZ', 'HEMLOCK', 'SPARROW'];
const FORMATS = ['two-column-landscape', 'single-page-memo', 'bulleted-brief', 'table-first-digest', 'plain-text-letter'];
const SERVICES = ['billing-api', 'search-indexer', 'notifier', 'auth-gateway', 'report-builder'];
const SUBJECTS = ['Quarterly planning follow-up', 'Re: travel booking', 'Lunch on Thursday?', 'Invoice reminder', 'Updated agenda'];

const sentences = (rng, chars) => { let text = ''; while (text.length < chars) text += rng.pick(FILLER) + ' '; return text.trim(); };
const reply = (rng, chars) => { let text = ''; while (text.length < chars) text += rng.pick(REPLIES) + ' '; return text.trim(); };

function jsonBlob(rng, chars) {
  const config = { service: rng.pick(SERVICES), replicas: rng.int(2, 9), routes: [] };
  while (JSON.stringify(config, null, 2).length < chars) config.routes.push({ path: `/v${rng.int(1, 3)}/${rng.pick(SERVICES)}/${rng.int(1, 999)}`, timeout_ms: rng.pick([250, 500, 1000, 2500]), retries: rng.int(0, 3), cache: rng.next() < 0.5 });
  return JSON.stringify(config, null, 2);
}
function codeBlob(rng, chars) {
  const lines = ['```python']; let n = 0;
  while (lines.join('\n').length < chars) { n += 1; lines.push(`def bucket_${n}(rows, width=${rng.int(2, 64)}):`, '    out = {}', '    for row in rows:', `        key = row['${rng.pick(['ts', 'size', 'owner', 'region'])}'] // width`, '        out.setdefault(key, []).append(row)', '    return out', ''); }
  lines.push('```'); return lines.join('\n');
}
function mailResult(rng, chars) {
  const messages = [];
  while (JSON.stringify({ messages }).length < chars) messages.push({ id: `msg_${rng.int(1e7, 1e8 - 1)}`, from: `${rng.pick(FIRST).toLowerCase()}@example.org`, subject: rng.pick(SUBJECTS), unread: rng.next() < 0.5, received: `2026-09-${rng.int(10, 28)}T${String(rng.int(7, 18)).padStart(2, '0')}:00:00Z` });
  return JSON.stringify({ messages });
}

// Each fact type: how it is stated, and how a summariser (the fake below)
// recognises it.  Every fact has its own subject (a project), so no fact
// supersedes another except the deliberate `updated` correction; values are
// unique strings so presence is an exact match.
const PROJECTS = ['Orion', 'Juniper', 'Halcyon', 'Marlowe', 'Kestrel', 'Tamarind', 'Basalt', 'Quillon', 'Sorrel', 'Vantage', 'Larkspur', 'Cinder'];
const FACT_SPECS = {
  name: { make: rng => `${rng.pick(FIRST)} ${rng.pick(LAST)}`, say: (p, v) => `By the way, the on-call engineer for project ${p} is ${v}.`, key: 'on-call engineer', pattern: /the on-call engineer for project (\w+) is ([A-Z][a-z]+ [A-Z][A-Za-z-]+)\./g },
  number: { make: rng => String(rng.int(10000, 99999)), say: (p, v) => `For the access form, the badge number for project ${p} is ${v}.`, key: 'badge number', pattern: /the badge number for project (\w+) is (\d{5})\./g },
  preference: { make: rng => `${rng.pick(FORMATS)}-${rng.int(10, 99)}`, say: (p, v) => `For project ${p}, I prefer reports formatted as ${v}.`, key: 'report format', pattern: /For project (\w+), I prefer reports formatted as ([a-z0-9-]+)\./g },
  decision: { make: rng => `${rng.pick(WORDS)}-${rng.pick(WORDS)}-${rng.int(1000, 9999)}`, say: (p, v) => `We decided to release project ${p} from branch ${v}.`, key: 'release branch', pattern: /We decided to release project (\w+) from branch ([A-Z0-9-]+)\./g },
  updated: { make: rng => `R-${rng.int(100, 999)}`, say: (p, v) => `The meeting room for the ${p} sync is ${v}.`, sayNew: (p, v) => `Correction: the meeting room for the ${p} sync is now ${v}.`, key: 'sync room', pattern: /the meeting room for the (\w+) sync is (?:now )?(R-\d{3})\./g },
};
const TOOL_FACT = /"path": ?"(invoices\/[a-z0-9-]+\.json)".{0,200}?"invoice_total": ?"([0-9.]+)"/g;

// The whole script of one conversation: per turn, the user message, whether
// the "model" calls a tool and what the tool returns, and the answer.
// `profile`: 'mixed' (pasted JSON, code, tool results, chat) or 'short_chat'
// (one-line messages and short answers, the shape that reaches the hard
// 64-message bound before the token budget).
export function buildConversation({ turns, seed = 0, profile = 'mixed' }) {
  const rng = rngFor(seed + 1); const script = []; const facts = []; const types = FACT_TYPES;
  let typeIndex = (seed * 2) % types.length; let pendingUpdate = null;
  // A fresh subject per fact of a type: Orion, Juniper, ..., then Orion2.
  const used = new Map();
  const subject = type => { const n = used.get(type) ?? 0; used.set(type, n + 1); const base = PROJECTS[(n + seed) % PROJECTS.length]; return n < PROJECTS.length ? base : `${base}${Math.floor(n / PROJECTS.length) + 1}`; };
  for (let t = 1; t <= turns; t++) {
    const short = profile === 'short_chat';
    let user; let tool = null; let answer = short ? reply(rng, rng.int(60, 160)) : reply(rng, rng.int(300, 700));
    const factTurn = t < turns && t % 2 === 1;
    if (pendingUpdate && pendingUpdate.turn === t && t < turns) {
      user = `${FACT_SPECS.updated.sayNew(pendingUpdate.subject, pendingUpdate.value)} ${sentences(rng, rng.int(60, 200))}`;
      facts.push({ type: 'updated', turn: t, value: pendingUpdate.value, stale: pendingUpdate.old }); pendingUpdate = null;
    } else if (factTurn) {
      const type = types[typeIndex % types.length]; typeIndex += 1;
      if (type === 'tool_only') {
        const path = `invoices/${rng.pick(SERVICES)}-${rng.int(1000, 9999)}.json`; const total = `${rng.int(10000, 99999)}.${rng.int(10, 99)}`;
        user = `Open ${path} from the finance workspace and tell me if it looks right.`;
        const body = { path, content: { customer: 'Northwind Traders', invoice_total: total, currency: 'EUR', line_items: rng.int(3, 12), status: 'issued', notes: sentences(rng, rng.int(900, 2200)) } };
        tool = { name: 'fs.read_text', arguments: { workspace_id: 'ws-finance', path }, text: JSON.stringify(body) };
        answer = reply(rng, rng.int(200, 400));
        facts.push({ type, turn: t, value: total });
      } else if (type === 'updated') {
        const old = FACT_SPECS.updated.make(rng); let value = old; while (value === old) value = FACT_SPECS.updated.make(rng);
        const p = subject(type);
        user = `${FACT_SPECS.updated.say(p, old)} ${sentences(rng, rng.int(60, 200))}`;
        pendingUpdate = { turn: t + 2, value, old, subject: p };
      } else {
        const value = FACT_SPECS[type].make(rng);
        user = `${sentences(rng, rng.int(40, 160))} ${FACT_SPECS[type].say(subject(type), value)} ${sentences(rng, rng.int(40, 160))}`;
        facts.push({ type, turn: t, value });
      }
    } else if (t === turns) {
      user = 'Thanks, that is everything for today. Anything I should follow up on tomorrow?';
    } else if (short) {
      user = sentences(rng, rng.int(40, 120));
    } else {
      const kind = rng.next();
      if (kind < 0.2) user = `Here is the config block from staging, can you sanity-check it?\n${jsonBlob(rng, rng.int(1200, 2600))}`;
      else if (kind < 0.4) user = `Can you check this helper? It feels slow.\n${codeBlob(rng, rng.int(900, 1900))}`;
      else if (kind < 0.6) { user = 'Check my inbox for anything from the vendor.'; tool = { name: 'mail.search', arguments: { query: 'vendor' }, text: mailResult(rng, rng.int(1500, 3500)) }; answer = reply(rng, rng.int(200, 400)); }
      else user = sentences(rng, rng.int(80, 300));
    }
    script.push({ turn: t, user: `${user} (#${t})`, tool, answer });
  }
  return { script, facts };
}

// Tools the fake "model" calls.  Read-only (T1), so no action journal.
export function retentionTools(lookup) {
  const execute = async ({ id, name, arguments: args }) => ({ id, name, status: 'ok', content: [{ type: 'text', text: lookup(name, args) }], metadata: { truncated: false, duration_ms: 0 } });
  return {
    'fs.read_text': { name: 'fs.read_text', risk_tier: 'T1', side_effect: 'read_sensitive', description: 'Read bounded UTF-8 text from an approved workspace file.', parameters: { type: 'object', properties: { workspace_id: { type: 'string', maxLength: 64 }, path: { type: 'string', maxLength: 4096 } }, required: ['workspace_id', 'path'], additionalProperties: false }, execute },
    'mail.search': { name: 'mail.search', risk_tier: 'T1', side_effect: 'read_mail', description: 'Search the signed-in mailbox.', parameters: { type: 'object', properties: { query: { type: 'string', maxLength: 512 } }, required: ['query'], additionalProperties: false }, execute },
  };
}

// The fake summariser: copies every recognised fact from the current note
// and the excerpt into "- key: value" lines (a later value replaces an
// earlier one) and keeps the NEWEST lines that fit the requested word count.
// Perfect recall by construction; a real model is measured elsewhere.
export function fakeDigest(userContent) {
  const facts = new Map();
  const noteStart = userContent.indexOf('Current memory note:\n'); const excerptStart = userContent.indexOf('<<<\n');
  const note = noteStart === -1 || excerptStart === -1 ? '' : userContent.slice(noteStart, excerptStart);
  for (const line of note.split('\n')) { const m = /^- (.+?): (.+)$/.exec(line); if (m) facts.set(m[1], m[2]); }
  const excerpt = excerptStart === -1 ? userContent : userContent.slice(excerptStart);
  const found = [];
  for (const spec of Object.values(FACT_SPECS)) for (const m of excerpt.matchAll(spec.pattern)) found.push([m.index, `${spec.key} of ${m[1]}`, m[2]]);
  for (const m of excerpt.matchAll(TOOL_FACT)) found.push([m.index, `invoice_total of ${m[1]}`, m[2]]);
  for (const [, key, value] of found.sort((a, b) => a[0] - b[0])) { facts.delete(key); facts.set(key, value); }
  const words = Number(/at most (\d+) words/.exec(userContent)?.[1] ?? 150);
  const lines = [...facts].map(([key, value]) => `- ${key}: ${value}`); const kept = []; let count = 0;
  for (let i = lines.length - 1; i >= 0; i--) { const n = lines[i].split(/\s+/).length; if (count + n > words) break; kept.unshift(lines[i]); count += n; }
  return kept.join('\n');
}

// A stand-in for the engine.  It counts a token per `charsPerToken`
// characters of the rendered prompt plus a fixed preamble, refuses a prompt
// that leaves no room for `maxTokens` exactly as the native client reports
// the engine's overflow, and never answers from anything but its script.
export function retentionEngine({ script, contextTokens = 8192, maxTokens = 1024, charsPerToken = 3.5, keepPrompts = false, summarise = fakeDigest }) {
  const byUser = new Map(script.map(spec => [spec.user, spec]));
  const engine = {
    maxTokens, calls: 0, summaryCalls: 0, overflows: 0, prompts: [], lastPrompt: null, maxPromptTokens: 0,
    countTokens(messages, tools) {
      const preamble = tools.length ? 620 + 1040 + JSON.stringify(tools).length : 32;
      return Math.ceil(preamble / charsPerToken) + 5 + messages.reduce((sum, m) => sum + 5 + Math.ceil(((m.content ?? '').length + (m.name ?? '').length) / charsPerToken), 0);
    },
    async *generate({ messages, tools = [] }) {
      engine.calls += 1;
      if (messages.length > 64) throw new NativeEngineError('invalid_messages', 'native message history is invalid');
      const tokens = engine.countTokens(messages, tools);
      if (tokens + maxTokens > contextTokens) { engine.overflows += 1; throw new NativeEngineError('invalid_request', 'engine returned an error event'); }
      const summary = messages[0]?.role === 'system' && messages[0].content === MEMORY_PROMPTS.system;
      if (summary) { engine.summaryCalls += 1; yield { kind: 'text_delta', text: summarise(messages[1].content) }; yield { kind: 'done', finish_reason: 'stop', usage: { prompt_tokens: tokens, completion_tokens: 64 } }; return; }
      const snapshot = keepPrompts ? structuredClone(messages) : messages.slice();
      engine.lastPrompt = snapshot; engine.lastTools = tools; if (keepPrompts) engine.prompts.push(snapshot); engine.maxPromptTokens = Math.max(engine.maxPromptTokens, tokens);
      const latest = messages.findLastIndex(m => m.role === 'user');
      const spec = byUser.get(messages[latest].content);
      if (spec?.tool && !messages.slice(latest).some(m => m.role === 'tool')) {
        yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: `call_t${spec.turn}`, name: spec.tool.name, arguments: spec.tool.arguments }) };
        yield { kind: 'done', finish_reason: 'tool_calls', usage: { prompt_tokens: tokens, completion_tokens: 30 } }; return;
      }
      yield { kind: 'text_delta', text: spec?.answer ?? 'Noted.' };
      yield { kind: 'done', finish_reason: 'stop', usage: { prompt_tokens: tokens, completion_tokens: 120 } };
    },
  };
  return engine;
}

const isNote = message => message.role === 'user' && message.content.startsWith(MEMORY_PROMPTS.note_label);

// Message-level form of the engine's turn-boundary snapshot rule: the new
// prompt can resume from the previous turn's snapshot only if every message
// up to and including that turn's last user message is unchanged.
export function boundaryReusable(previous, next) {
  const boundary = previous.findLastIndex(m => m.role === 'user');
  if (boundary < 0 || next.length <= boundary) return false;
  for (let i = 0; i <= boundary; i++) if (JSON.stringify(previous[i]) !== JSON.stringify(next[i])) return false;
  return true;
}

export async function runConversation({ turns, seed = 0, profile = 'mixed', mode = 'off', charsPerToken = 3.5, contextTokens = 8192, maxTokens = 1024, keepPrompts = false, memory = {}, summarise } = {}) {
  const { script, facts } = buildConversation({ turns, seed, profile });
  const toolText = new Map(script.filter(s => s.tool).map(s => [JSON.stringify([s.tool.name, s.tool.arguments]), s.tool.text]));
  const engine = retentionEngine({ script, contextTokens, maxTokens, charsPerToken, keepPrompts, ...(summarise ? { summarise } : {}) });
  const controller = new ConversationController({ engine, contextTokens, toolRegistry: retentionTools((name, args) => toolText.get(JSON.stringify([name, args])) ?? '{}'), memory: { mode, ...memory } });
  const sessionId = `ses_retention_${seed}_${turns}`; const events = [];
  for (const spec of script) {
    const result = await controller.runTurn({ sessionId, requestId: `req_ret_${String(spec.turn).padStart(4, '0')}`, message: spec.user, onEvent: event => events.push(event) });
    if (result.state !== 'COMPLETED') throw new Error(`turn ${spec.turn} ended ${result.state} (${result.error})`);
    // A user who reads the answer before typing: the background summary
    // (if one was scheduled) finishes before the next turn starts.
    if (mode === 'summary') await controller.memoryIdle();
  }
  const session = controller.sessions.get(sessionId);
  return { script, facts, engine, controller, session, events };
}

// An exact value, not a substring of a longer token (a 5-digit badge number
// must not be "found" inside an 8-digit message id).
export const containsValue = (text, value) => new RegExp(`(?<![A-Za-z0-9])${value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![A-Za-z0-9])`).test(text);

export function measure(run, { turns } = {}) {
  const prompt = run.engine.lastPrompt; const tools = run.engine.lastTools ?? []; const text = prompt.map(m => m.content).join('\n');
  const compactions = run.events.filter(e => e.data.context_compaction).map(e => e.data.context_compaction);
  const userTurns = prompt.filter(m => m.role === 'user' && !isNote(m)).length;
  const evented = compactions.reduce((sum, c) => sum + c.dropped_turns, 0);
  const facts = run.facts.map(fact => {
    const present = containsValue(text, fact.value); const age = turns - fact.turn;
    const state = fact.type !== 'updated' ? (present ? 'present' : 'missing') : present ? 'latest' : containsValue(text, fact.stale) ? 'stale_only' : 'missing';
    return { type: fact.type, age, present, state };
  });
  const memory = run.session.memory;
  return {
    turns, retained_turns: userTurns, dropped_turns: turns - userTurns,
    storage_cap_dropped_turns: Math.max(0, turns - userTurns - evented),
    tool_results_elided_total: compactions.reduce((sum, c) => sum + (c.masked_tool_results ?? 0), 0),
    tool_results_elided_in_prompt: prompt.filter(isElidedToolResult).length,
    compactions: run.session.context_compactions, compaction_events: compactions.length,
    estimated_prompt_tokens: estimatePromptTokens({ messages: prompt, tools }),
    fake_engine_prompt_tokens: run.engine.countTokens(prompt, tools), max_fake_prompt_tokens: run.engine.maxPromptTokens,
    engine_overflows: run.engine.overflows, summary_calls: run.engine.summaryCalls,
    note_bytes: memory?.note ? Buffer.byteLength(memory.note) : 0, note_redactions: (memory?.note?.match(/\[redacted\]/g) ?? []).length, summaries_applied: memory?.summaries ?? 0, memory_failures: memory?.failures ?? 0,
    facts,
  };
}

function aggregate(rows) {
  const out = { by_type: {}, by_age: {}, overall: null };
  const share = list => (list.length ? Number((list.filter(f => f.present).length / list.length).toFixed(3)) : null);
  const all = rows.flatMap(r => r.facts);
  for (const type of FACT_TYPES) { const list = all.filter(f => f.type === type); out.by_type[type] = { n: list.length, present: share(list), ...(type === 'updated' ? { stale_only: list.filter(f => f.state === 'stale_only').length } : {}) }; }
  for (const [lo, hi] of AGE_BUCKETS) { const list = all.filter(f => f.age >= lo && f.age <= hi); if (list.length) out.by_age[`${lo}-${hi}`] = { n: list.length, present: share(list) }; }
  out.overall = { n: all.length, present: share(all) };
  const mean = key => Number((rows.reduce((sum, r) => sum + r[key], 0) / rows.length).toFixed(1));
  for (const key of ['retained_turns', 'dropped_turns', 'storage_cap_dropped_turns', 'tool_results_elided_total', 'compactions', 'estimated_prompt_tokens', 'fake_engine_prompt_tokens', 'max_fake_prompt_tokens', 'engine_overflows', 'summary_calls', 'note_bytes', 'note_redactions', 'boundary_reuse_turns']) out[`mean_${key}`] = mean(key);
  return out;
}

export async function retentionReport({ turnsList = [10, 20, 40, 80], seeds = 3, modes = ['off'], charsPerToken = 3.5, memory = {}, profile = 'mixed' } = {}) {
  const report = { schema: 'local_bmo.memory-retention.v1', policy_only: true, summary_rows_simulated: modes.includes('summary'), profile, chars_per_token: charsPerToken, context_tokens: 8192, max_output_tokens: 1024, seeds, memory, results: {} };
  for (const mode of modes) {
    report.results[mode] = {};
    for (const turns of turnsList) {
      const rows = [];
      for (let seed = 0; seed < seeds; seed++) {
        const run = await runConversation({ turns, seed, profile, mode, charsPerToken, keepPrompts: true, memory });
        const row = measure(run, { turns });
        // How many turn starts could resume the engine's turn-boundary
        // snapshot (the first prompt of each turn ends with its user message).
        const firsts = run.engine.prompts.filter(prompt => prompt.at(-1).role === 'user');
        row.boundary_reuse_turns = firsts.slice(1).filter((prompt, i) => boundaryReusable(firsts[i], prompt)).length;
        rows.push(row);
      }
      report.results[mode][turns] = aggregate(rows);
    }
  }
  return report;
}

function markdown(report) {
  const lines = [];
  for (const [mode, byTurns] of Object.entries(report.results)) {
    lines.push(`\n### mode: ${mode}${mode === 'summary' ? ' (SIMULATED: perfect fake summariser, an upper bound)' : ''}\n`);
    lines.push(`| turns | all facts | ${FACT_TYPES.join(' | ')} | retained turns | dropped turns (of which storage cap) | tool results elided | compactions | summary calls | est. prompt tokens | turn starts reusing snapshot |`);
    lines.push(`|${'---|'.repeat(FACT_TYPES.length + 9)}`);
    for (const [turns, r] of Object.entries(byTurns)) {
      const pct = v => (v === null ? 'n/a' : `${Math.round(v * 100)}%`);
      lines.push(`| ${turns} | ${pct(r.overall.present)} (n=${r.overall.n}) | ${FACT_TYPES.map(t => `${pct(r.by_type[t].present)} (n=${r.by_type[t].n})`).join(' | ')} | ${r.mean_retained_turns} | ${r.mean_dropped_turns} (${r.mean_storage_cap_dropped_turns}) | ${r.mean_tool_results_elided_total} | ${r.mean_compactions} | ${r.mean_summary_calls} | ${r.mean_estimated_prompt_tokens} | ${r.mean_boundary_reuse_turns} of ${Number(turns) - 1} |`);
    }
    lines.push('\nBy fact age (turns before the last turn):\n');
    lines.push('| turns | ' + AGE_BUCKETS.map(([lo, hi]) => `${lo}-${hi}`).join(' | ') + ' |');
    lines.push(`|${'---|'.repeat(AGE_BUCKETS.length + 1)}`);
    for (const [turns, r] of Object.entries(byTurns)) lines.push(`| ${turns} | ${AGE_BUCKETS.map(([lo, hi]) => { const b = r.by_age[`${lo}-${hi}`]; return b ? `${Math.round(b.present * 100)}% (n=${b.n})` : '-'; }).join(' | ')} |`);
  }
  return lines.join('\n');
}

async function main(argv) {
  const arg = (name, fallback) => { const i = argv.indexOf(name); return i === -1 ? fallback : argv[i + 1]; };
  const turnsList = arg('--turns', '10,20,40,80').split(',').map(Number);
  const seeds = Number(arg('--seeds', '3'));
  const mode = arg('--mode', 'both');
  const charsPerToken = Number(arg('--chars-per-token', '3.5'));
  const noteTokens = Number(arg('--note-tokens', '256'));
  const profile = arg('--profile', 'mixed');
  if (turnsList.some(n => !Number.isInteger(n) || n < 2 || n > 200) || !Number.isInteger(seeds) || seeds < 1 || seeds > 20 || !['off', 'summary', 'both'].includes(mode) || !(charsPerToken >= 1 && charsPerToken <= 8) || !Number.isInteger(noteTokens) || noteTokens < 32 || noteTokens > 1024 || !['mixed', 'short_chat'].includes(profile)) {
    process.stderr.write('usage: node tests/host/memory-retention.mjs [--json] [--turns 10,20,40,80] [--seeds 3] [--mode off|summary|both] [--chars-per-token 3.5] [--note-tokens 256] [--profile mixed|short_chat]\n'); process.exitCode = 2; return;
  }
  // The note's byte bound follows its token budget (noteByteLimit), so
  // raising the budget raises the byte cap with it.
  const memory = noteTokens === 256 ? {} : { noteTokens, noteBytes: Math.min(4096, Math.max(1024, noteTokens * 4)) };
  const report = await retentionReport({ turnsList, seeds, modes: mode === 'both' ? ['off', 'summary'] : [mode], charsPerToken, memory, profile });
  process.stdout.write(argv.includes('--json') ? `${JSON.stringify(report, null, 2)}\n` : `${markdown(report)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main(process.argv.slice(2));
