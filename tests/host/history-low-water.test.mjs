import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationController } from '../../host/agent/controller.mjs';

// The engine reuses the retained prompt only while each prompt STRICTLY EXTENDS the
// previous one, and trimming the oldest turn changes the start of the prompt. A short
// chat that has reached the hard message bound used to lose one turn on every
// following turn, so every turn re-read the whole history. A trim now drops to a
// low-water mark once, which buys many stable turns before the next trim.
const turns = (controller, session, count) => {
  const trims = []; let stable = 0, extended = 0; let previous = null;
  for (let n = 1; n <= count; n++) {
    const before = session.history.length;
    controller._appendHistory(session, { role: 'user', content: `question ${n}` });
    controller._appendHistory(session, { role: 'assistant', content: `answer ${n}` });
    if (session.history.length < before + 2) trims.push(n);
    const prompt = controller._promptMessages(session).map(m => m.content);
    if (previous) { stable++; if (previous.every((content, i) => prompt[i] === content)) extended++; }
    previous = prompt;
  }
  return { trims, stable, extended };
};

test('a short chat at the message bound trims rarely, not on every turn', () => {
  const controller = new ConversationController({ engine: { async *generate() {} } });
  const session = controller.createSession('ses_low_water_1');
  const { trims, stable, extended } = turns(controller, session, 120);
  // 120 turns = 240 messages against a bound of 64. Trimming exactly to the cap would trim ~88 times.
  assert.ok(trims.length >= 1, 'the bound must still trim');
  assert.ok(trims.length <= 14, `trimmed on ${trims.length} of 120 turns`);
  assert.ok(session.history.length <= controller.maxHistoryMessages, 'the bound is never exceeded');
  // Between trims each prompt is a strict extension of the last, which is what engine reuse needs.
  assert.ok(extended >= stable - trims.length - 1, `${extended} of ${stable} prompts extended the previous one`);
});

test('a trim lands well below the bound, not at it', () => {
  const controller = new ConversationController({ engine: { async *generate() {} }, maxHistoryMessages: 16 });
  const session = controller.createSession('ses_low_water_2');
  let lowest = Infinity, atTrim = null;
  for (let n = 1; n <= 40; n++) {
    const before = session.history.length;
    controller._appendHistory(session, { role: 'user', content: `q${n}` });
    controller._appendHistory(session, { role: 'assistant', content: `a${n}` });
    if (session.history.length < before + 2 && atTrim === null) atTrim = session.history.length;
    lowest = Math.min(lowest, n > 10 ? session.history.length : Infinity);
  }
  assert.ok(atTrim !== null && atTrim <= 12, `the first trim left ${atTrim} messages; expected <= 12 (75% of 16)`);
});
