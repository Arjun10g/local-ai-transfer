import test from 'node:test';
import assert from 'node:assert/strict';
import { ToolCallStreamDecoder, parseToolCall, MAX_ENVELOPE_BYTES } from '../../host/agent/tool-envelope.mjs';

// The streaming decoder must reach the same verdict however the engine's SSE
// frames happen to split the model's output: that is what lets it release text
// early without ever releasing the start of a tool call.

const CALL = '<tool_call>\n<function=time.now>\n<parameter=format>\nlocal\n</parameter>\n</function>\n</tool_call>';
const CALL_TWO = '<tool_call>\n<function=system.get_info>\n</function>\n</tool_call>';

// [label, model output, decoder options, expected outcome]
const CASES = [
  ['plain text', 'Hello, world. Two lines\nof text.', {}, { text: 'Hello, world. Two lines\nof text.', calls: 0 }],
  ['prose with angle brackets', 'If a < b and c <= d, use <div>x</div>, <t, <to, <tool and <b>.', {}, { text: 'If a < b and c <= d, use <div>x</div>, <t, <to, <tool and <b>.', calls: 0 }],
  ['multi-byte text', 'héllo 😀 — <b>naïve</b> ✓', {}, { text: 'héllo 😀 — <b>naïve</b> ✓', calls: 0 }],
  ['text ending in a short tag fragment', 'ends with <to', {}, { text: 'ends with <to', calls: 0 }],
  ['empty output', '', {}, { text: '', calls: 0 }],
  ['whitespace only', ' \n\n ', {}, { text: ' \n\n ', calls: 0 }],
  ['tool call', CALL, {}, { text: '', calls: 1 }],
  ['whitespace then tool call', '\n\n  \n<tool_call>\n<function=time.now>\n</function>\n</tool_call>', {}, { text: '', calls: 1 }],
  ['tool call then trailing whitespace', `${CALL}\n\n`, {}, { text: '', calls: 1 }],
  ['text then tool call (mixed)', `Sure, checking.\n${CALL}`, {}, { text: 'Sure, checking.\n', calls: 1 }],
  ['tool call then trailing text', `${CALL} done`, {}, { error: 'malformed_tool_call' }],
  ['two calls', `${CALL}\n${CALL_TWO}`, {}, { error: 'malformed_tool_call' }],
  ['truncated call', '<tool_call>\n<function=time.now>\n<param', {}, { error: 'malformed_tool_call' }],
  // A fragment that never became `<tool_call>` is prose, however close it got.
  ['text ending in a long tag fragment', 'answer <tool_ca', {}, { text: 'answer <tool_ca', calls: 0 }],
  ['bare tag fragment (truncated call, no prose)', '\n<tool', {}, { error: 'malformed_tool_call' }],
  ['quoted <tool_calls>', 'see <tool_calls> here', {}, { text: 'see <tool_calls> here', calls: 0 }],
  ['quoted <tool_response>', 'The file says <tool_response> ok </tool_response>.', {}, { text: 'The file says <tool_response> ok </tool_response>.', calls: 0 }],
  ['reply opening with <tool_response>', '\n<tool_response>{"a":1}</tool_response>', {}, { text: '\n<tool_response>{"a":1}</tool_response>', calls: 0 }],
  ['quoted bare <tool_ and <tool_x>', 'Tags like <tool_ and <tool_x> and <tool_call are not calls', {}, { text: 'Tags like <tool_ and <tool_x> and <tool_call are not calls', calls: 0 }],
  ['quoted closing tag', 'It ends with </tool_call> and </function>.', {}, { text: 'It ends with </tool_call> and </function>.', calls: 0 }],
  ['quoted tags then a real call (mixed)', `Found <tool_response> in the file.\n${CALL}`, {}, { text: 'Found <tool_response> in the file.\n', calls: 1 }],
  ['quoted tags then a garbled call', 'Found <tool_calls>. <tool_call>\n<function=time.now>\n', {}, { error: 'malformed_tool_call' }],
  ['deep: reasoning then answer', 'Let me think: 2 < 3.\n</think>\n\nThe answer is 3.', { reasoning: true }, { reasoning: 'Let me think: 2 < 3.\n', text: '\n\nThe answer is 3.', calls: 0 }],
  ['deep: reasoning then call', `Need the time.\n</think>\n\n${CALL}`, { reasoning: true }, { reasoning: 'Need the time.\n', text: '', calls: 1 }],
  ['deep: call before think close', `Need the time. ${CALL}`, { reasoning: true }, { reasoning: 'Need the time. ', text: '', calls: 1 }],
  ['deep: reasoning never closed', 'Thinking about </th and <t but cut off', { reasoning: true }, { reasoning: 'Thinking about </th and <t but cut off', text: 'Thinking about </th and <t but cut off', fallback: true, calls: 0 }],
  ['deep: reasoning cut inside a tag fragment', 'Maybe call <tool_ca', { reasoning: true }, { reasoning: 'Maybe call <tool_ca', text: 'Maybe call <tool_ca', fallback: true, calls: 0 }],
  ['deep: reasoning quoting tags then answer', 'the doc has <tool_response></think>Done.', { reasoning: true }, { reasoning: 'the doc has <tool_response>', text: 'Done.', calls: 0 }],
  ['deep: reasoning then mixed text and call', `plan</think>Okay. ${CALL}`, { reasoning: true }, { reasoning: 'plan', text: 'Okay. ', calls: 1 }],
];

function decode(chunks, options) {
  const decoder = new ToolCallStreamDecoder({ stream: true, ...options });
  const events = []; let error = null; let visible = '';
  const take = produced => {
    for (const event of produced) {
      events.push(event);
      if (event.kind === 'text_delta') visible += event.text;
      // Anything already shown is literal model output; an opened call is
      // never part of it (quoted look-alikes such as <tool_response> are).
      assert.equal(visible.includes('<tool_call>'), false, `visible text leaked a call: ${JSON.stringify(visible)}`);
    }
  };
  try { for (const chunk of chunks) take(decoder.push(chunk)); take(decoder.finish()); } catch (caught) { error = caught; }
  return { events, error, visible };
}

function summary({ events, error }) {
  if (error) return { error: error.code };
  const join = kind => events.filter(e => e.kind === kind).map(e => e.text).join('');
  return { text: join('text_delta'), reasoning: join('reasoning_delta'), calls: events.filter(e => e.kind === 'tool_call_chunk').map(e => e.text), fallback: events.some(e => e.reasoning_fallback === true) };
}

function* splits(text, { exhaustive = true } = {}) {
  yield [text];
  for (let i = 0; i <= text.length; i++) yield [text.slice(0, i), text.slice(i)];
  if (exhaustive) for (let i = 0; i <= text.length; i++) for (let j = i; j <= text.length; j++) yield [text.slice(0, i), text.slice(i, j), text.slice(j)];
  yield [...text];
}

for (const [label, output, options, expected] of CASES) {
  test(`streaming decoder is chunking-invariant: ${label}`, () => {
    const whole = decode([output], options); const reference = summary(whole);
    if (expected.error) assert.deepEqual(reference, { error: expected.error });
    else {
      assert.equal(reference.text, expected.text); assert.equal(reference.calls.length, expected.calls);
      assert.equal(reference.reasoning, expected.reasoning ?? ''); assert.equal(reference.fallback, expected.fallback === true);
      for (const call of reference.calls) assert.match(parseToolCall(call).name, /^(time\.now|system\.get_info)$/);
      // Text shown before a call is never part of the call the host parses.
      for (const call of reference.calls) assert.ok(call.startsWith('<tool_call>') && call.endsWith('</tool_call>'));
    }
    let count = 0;
    for (const chunks of splits(output)) {
      count++;
      const run = decode(chunks, options); const where = `${label} split ${JSON.stringify(chunks.map(c => c.length))}`;
      assert.deepEqual(summary(run), reference, where);
      // Whatever was shown before the stream ended is a prefix of the final
      // answer: streamed text is never retracted or reordered.
      if (!run.error) assert.ok(reference.text.startsWith(run.visible), where);
      else if (!options.reasoning) assert.ok(output.startsWith(run.visible) || output.trimStart().startsWith(run.visible), where);
      // Text released before the first call/reasoning/error is final.
      const firstOther = run.events.findIndex(e => e.kind !== 'text_delta');
      const early = run.events.slice(0, firstOther < 0 ? run.events.length : firstOther).map(e => e.text).join('');
      if (!reference.error) assert.ok(reference.text.startsWith(early), where);
    }
    assert.ok(count > output.length);
  });
}

test('streaming decoder releases text before the stream ends and holds only an undecided tag', () => {
  const decoder = new ToolCallStreamDecoder({ stream: true });
  assert.deepEqual(decoder.push('Hello'), [{ kind: 'text_delta', text: 'Hello' }]);
  assert.deepEqual(decoder.push(' a < b <'), [{ kind: 'text_delta', text: ' a < b ' }]);
  assert.deepEqual(decoder.push('tool'), []);
  assert.deepEqual(decoder.push(' box'), [{ kind: 'text_delta', text: '<tool box' }]);
  // A diverged look-alike is released as soon as it diverges, not refused.
  assert.deepEqual(decoder.push(' <tool_'), [{ kind: 'text_delta', text: ' ' }]);
  assert.deepEqual(decoder.push('response>'), [{ kind: 'text_delta', text: '<tool_response>' }]);
  assert.deepEqual(decoder.finish(), []);
  const lead = new ToolCallStreamDecoder({ stream: true });
  assert.deepEqual(lead.push('\n\n'), [], 'leading whitespace waits for the first visible character');
  assert.deepEqual(lead.push('Hi'), [{ kind: 'text_delta', text: '\n\nHi' }]);
});

test('streaming decoder bounds a tool call the same way however it is chunked', () => {
  const oversized = `<tool_call>\n<function=time.now>\n<parameter=format>\n${'y'.repeat(MAX_ENVELOPE_BYTES)}\n</parameter>\n</function>\n</tool_call>`;
  for (const chunks of splits(oversized, { exhaustive: false })) {
    if (chunks.length > 2 && chunks.length !== oversized.length) continue;
    assert.deepEqual(summary(decode(chunks, {})), { error: 'tool_call_too_large' });
  }
  // Long plain text is not a call and is not held, so it is not refused.
  const prose = 'z'.repeat(MAX_ENVELOPE_BYTES * 2);
  assert.deepEqual(summary(decode([prose.slice(0, 5), prose.slice(5)], {})).text, prose);
  // A partial call that is already over the limit fails before it completes.
  const growing = new ToolCallStreamDecoder({ stream: true }); growing.push('<tool_call>');
  assert.throws(() => growing.push('x'.repeat(MAX_ENVELOPE_BYTES + 64)), error => error.code === 'tool_call_too_large');
});

test('streaming decoder refuses chunks after finish and keeps the buffered mode unchanged', () => {
  const decoder = new ToolCallStreamDecoder({ stream: true }); decoder.push('ok'); decoder.finish();
  assert.throws(() => decoder.push('more'), error => error.code === 'invalid_tool_stream');
  assert.deepEqual(decoder.finish(), []);
  const buffered = new ToolCallStreamDecoder();
  assert.deepEqual(buffered.push('Hello'), [], 'the default decoder still holds all text until finish');
  assert.deepEqual(buffered.finish(), [{ kind: 'text_delta', text: 'Hello' }]);
});
