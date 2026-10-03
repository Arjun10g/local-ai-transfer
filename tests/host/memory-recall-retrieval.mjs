#!/usr/bin/env node
// Can recall FIND a fact that has left the window?  A measurement, run by hand
// (`node tests/host/memory-recall-retrieval.mjs [--turns 60,120] [--seeds 8]`)
// and used by tests/host/memory-recall.test.mjs.
//
// Same synthetic conversations as memory-retention.mjs, driven through the
// real controller in recall mode.  At the end, for every planted fact that is
// no longer in the prompt, the question a user would ask is put to the
// session's archive, in two wordings: the one that shares the fact's own
// words ("exact") and one that does not ("paraphrase").  A hit is a returned
// line containing the planted value (for a corrected fact: the NEWER value).
// This measures retrieval only: whether the model then USES the line is what
// scripts/test/memory_eval.py measures with the real model.
import { pathToFileURL } from 'node:url';
import { FACT_TYPES, containsValue, runConversation } from './memory-retention.mjs';
import { recallQueries } from '../../host/agent/memory-recall.mjs';

export const QUESTIONS = Object.freeze({
  name: { exact: f => `What is the on-call engineer for project ${f.subject}?`, paraphrase: f => `Who is on call for ${f.subject}?` },
  number: { exact: f => `What is the badge number for project ${f.subject}?`, paraphrase: f => `Which badge do I use for ${f.subject}?` },
  preference: { exact: f => `How do I prefer reports formatted for project ${f.subject}?`, paraphrase: f => `What layout did I ask for on ${f.subject} reports?` },
  decision: { exact: f => `Which release branch did we decide on for project ${f.subject}?`, paraphrase: f => `Which branch are we shipping ${f.subject} from?` },
  tool_only: { exact: f => `In the earlier fs.read_text result for ${f.path}, what was the invoice_total?`, paraphrase: f => `How much was invoiced in ${f.path}?` },
  updated: { exact: f => `What is the meeting room for the ${f.subject} sync now?`, paraphrase: f => `Where does the ${f.subject} sync meet these days?` },
});

export async function probe({ turns, seed, profile = 'mixed' }) {
  const run = await runConversation({ turns, seed, profile, mode: 'recall', keepPrompts: false });
  const prompt = run.engine.lastPrompt.map(m => m.content).join('\n');
  const archive = run.session.memory.archive;
  const rows = [];
  for (const fact of run.facts) {
    if (fact.turn === turns) continue;
    const stillInWindow = containsValue(prompt, fact.value);
    for (const wording of ['exact', 'paraphrase']) {
      const question = QUESTIONS[fact.type][wording](fact);
      const lines = archive?.size ? archive.search(recallQueries(question)) : [];
      const hit = lines.some(line => containsValue(line, fact.value));
      rows.push({ type: fact.type, wording, age: turns - fact.turn, in_window: stillInWindow, hit, lines: lines.length, bytes: lines.reduce((s, l) => s + Buffer.byteLength(l), 0), stale_only: fact.type === 'updated' && !hit && lines.some(line => containsValue(line, fact.stale)) });
    }
  }
  return { rows, archive_entries: archive?.size ?? 0, archive_bytes: archive?.bytes ?? 0 };
}

export async function report({ turnsList = [60, 120], seeds = 8 } = {}) {
  const out = { schema: 'local_bmo.memory-recall-retrieval.v1', retrieval_only: true, seeds, results: {} };
  for (const turns of turnsList) {
    const all = [];
    let entries = 0; let bytes = 0;
    for (let seed = 0; seed < seeds; seed++) { const r = await probe({ turns, seed }); all.push(...r.rows); entries += r.archive_entries; bytes += r.archive_bytes; }
    const gone = all.filter(r => !r.in_window);
    const rate = list => (list.length ? Number((list.filter(r => r.hit).length / list.length).toFixed(3)) : null);
    out.results[turns] = {
      facts_out_of_window: gone.length / 2, hit_exact: rate(gone.filter(r => r.wording === 'exact')), hit_paraphrase: rate(gone.filter(r => r.wording === 'paraphrase')),
      by_type_exact: Object.fromEntries(FACT_TYPES.map(t => [t, rate(gone.filter(r => r.type === t && r.wording === 'exact'))])),
      by_type_paraphrase: Object.fromEntries(FACT_TYPES.map(t => [t, rate(gone.filter(r => r.type === t && r.wording === 'paraphrase'))])),
      mean_lines: Number((gone.reduce((s, r) => s + r.lines, 0) / Math.max(1, gone.length)).toFixed(2)), mean_block_bytes: Math.round(gone.reduce((s, r) => s + r.bytes, 0) / Math.max(1, gone.length)),
      mean_archive_entries: Math.round(entries / seeds), mean_archive_bytes: Math.round(bytes / seeds),
      stale_only: gone.filter(r => r.stale_only).length,
    };
  }
  return out;
}

async function main(argv) {
  const arg = (name, fallback) => { const i = argv.indexOf(name); return i === -1 ? fallback : argv[i + 1]; };
  const result = await report({ turnsList: arg('--turns', '60,120').split(',').map(Number), seeds: Number(arg('--seeds', '8')) });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main(process.argv.slice(2));
