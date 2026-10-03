# Context maintenance and memory compression: evaluation

Date: 2026-10-03. Status: **policy measured offline; opt-in memory note
implemented and tested with a fake summariser; the real model has not
summarised anything yet.** Nothing here was run on the model, the Dell, or a
GPU.

## The answer in one paragraph

Today the host keeps a conversation inside the 8,192-token window only by
replacing old tool results with an elision marker and dropping whole oldest
turns. Nothing is summarised, so a fact in a dropped turn is gone. With the
realistic mix used here (chat, pasted JSON, code, tool results), the window
holds about **10 turns**. At 20 turns **39%** of the planted facts are still in
the prompt, at 40 turns **21%**, at 80 turns **10%**. No fact older than about
20 turns survives, and a fact that exists only inside a tool result is lost
first: it is elided before its turn is dropped (0% kept at 40 turns). The new
opt-in memory note (`memory.mode: 'summary'`, off by default) keeps
**98% at 40 turns** with a perfect stand-in summariser. At 80 turns it keeps
**56%** with the default 256-token note and **97%** with a 512-token note.
These are upper bounds. The real Qwen3.5-9B's notes are measured by
`scripts/test/memory_eval.py`, which has not been run.

## What the host does today (the measured policy)

`host/agent/context-budget.mjs` and `ConversationController._fitContext`:

- Budget = 8,192 minus the output reservation (1,024), a 5% margin, the
  template overhead and the tool definitions. With three tools that is about
  6,000 history tokens, estimated at 3.0 bytes per token.
- At a turn start, if the history is over 75% of the budget, it is compacted
  down to 50%. Mid-turn (inside a tool loop), compaction happens only when
  the history is over 100%, and it cuts to 60%. Old tool results are elided
  first, oldest first, but never in the latest turn. Then whole oldest turns
  are dropped.
- Hard storage bounds in `_appendHistory` are 64 messages and 256 KiB. When
  either is exceeded, the oldest turn is dropped. This is a separate path:
  it emits no event and is not counted in `context_compactions`.

## Part 1: what survives (measured; a pure function of the policy)

Harness: `tests/host/memory-retention.mjs`, run by hand:

    node tests/host/memory-retention.mjs --seeds 5 --mode both [--profile mixed|short_chat] [--note-tokens 256] [--json]

It drives the REAL `ConversationController` against a fake engine that
counts 3.5 characters per token (the controller still estimates 3.0),
enforces the 8,192-token window, and refuses overflow exactly as the native
client reports it. Every run had 0 overflows.

Conversations:

- **Mixed profile:** about 50% chat, 10% pasted JSON (1.2 to 2.6 KB), 10%
  code (0.9 to 1.9 KB), 10% mailbox tool results (1.5 to 3.5 KB), with
  answers of 300 to 700 characters.
- **Planted facts:** one on every odd turn, rotating through the types:
  - a name;
  - a number;
  - a stated preference;
  - a decision;
  - a value that exists only inside an `fs.read_text` result;
  - a value that is corrected two turns later.
- **Repeats:** 5 seeds per length.

The table reports facts present (exact token match) in the prompt sent at
the last turn.

### Mixed profile, current policy (`off`)

| turns | all facts | name | number | preference | decision | tool-only | updated | turns kept verbatim | turns dropped | tool results elided | compactions | turn starts that can reuse the engine snapshot |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10 | 100% (n=22) | 100% | 100% | 100% | 100% | 100% | 100% | 10 | 0 | 0 | 0 | 9 of 9 |
| 20 | 39% (n=44) | 43% | 43% | 56% | 43% | 13% | 33% | 8.8 | 11.2 | 2.2 | 2.6 | 16.4 of 19 |
| 40 | 21% (n=85) | 27% | 31% | 27% | 21% | 0% | 23% | 11.2 | 28.8 | 6.8 | 7.2 | 31.8 of 39 |
| 80 | 10% (n=172) | 7% | 11% | 14% | 14% | 3% | 11% | 9.8 | 70.2 | 13.2 | 16.2 | 62.8 of 79 |

By fact age (turns before the last turn):

| turns | 1-5 | 6-10 | 11-20 | 21-40 | 41-80 |
|---|---|---|---|---|---|
| 20 | 86% | 63% | 0% | - | - |
| 40 | 82% | 67% | 14% | 0% | - |
| 80 | 77% | 67% | 5% | 0% | 0% |

### Short-chat profile (one-line messages), current policy

| turns | all facts | turns dropped (by the 64-message bound) | compactions (events) | snapshot-reusable turn starts |
|---|---|---|---|---|
| 20 | 100% | 0 (0) | 0 | 19 of 19 |
| 40 | 56% | 14 (5.2) | 1.6 | 33.2 of 39 |
| 80 | 24% | 56.6 (21.2) | 4.6 | 58.4 of 79 |

### Reading

- **About 10 turns of real work fit.** Beyond that, retention falls fast. By
  age, the memory horizon is 10 to 20 turns. Nothing older than 20 turns
  survives in any run.
- **Recent facts are not safe either.** Only 77 to 86% of facts from the last
  5 turns survive. A turn-start compaction cuts to 50% of the budget, and
  tool results only a few turns old are elided.
- **Tool-only facts are lost first.** Elision removes them before their turn
  is dropped: 13% kept at 20 turns, 0% at 40.
- **A corrected value is never "stale only".** Dropping goes oldest first, so
  the original statement always goes before its correction. Plain dropping
  cannot show the model a superseded value without its correction. That
  property matters for the note, below.
- **Prefix reuse holds up.** About 80% of turn starts leave the engine's
  turn-boundary snapshot (ENGINE-TURN-BOUNDARY-SNAPSHOT-001) usable, because
  compaction has hysteresis: 16 compactions in 80 turns.
- **Finding: the 64-message bound has no hysteresis.** In short chat it binds
  before the token budget: 21 of 57 dropped turns at 80 turns. Once the
  history is at the bound, every new turn drops one turn. That changes the
  prompt head every turn, so snapshot reuse is lost on every turn (58 of 79
  vs 63 of 79). These drops also emit no `context_compaction` event, so the
  UI cannot say "earlier context was condensed".
  - **Recommendation (not done):** give the hard bound a low-water mark, as
    the token budget has. It is not done here because it changes default
    behaviour. Summary mode does this already; see below.
- **What this cannot measure:**
  - Whether the model *uses* a surviving fact correctly. That needs
    `scripts/test/long_context_eval.py` (118/120 on the A100, all inside the
    window).
  - The real tokenizer. At more than 3.5 characters per token, slightly more
    history fits.
  - Restatements. A real assistant often repeats a fact in its own answers,
    which keeps facts longer. The fake engine's answers never repeat facts.
  - Tool-catalog size. The Windows product offers 2 to 5 tools; a 12-tool
    POSIX catalog leaves about 1,000 fewer tokens.

## Part 2: opt-in memory note (implemented)

Files:

- `host/agent/memory-note.mjs`: pure functions.
- `host/agent/memory-prompts.json`: the shared prompt data.
- Changes to `host/agent/controller.mjs`, only on the history, compaction and
  prompt-building paths.
- The `memory` key in `host/agent/config.mjs`, plus `memoryOptionsFromConfig`.

Config:

```json
"memory": { "mode": "summary", "note_tokens": 256, "note_bytes": 1024,
            "max_input_bytes": 6144, "per_message_bytes": 1536, "backlog_bytes": 16384,
            "timeout_ms": 600000, "wait_ms": 0, "plan_headroom_tokens": 512,
            "plan_target_percent": 40 }
```

All keys are optional. If `memory` is absent, mode is `off`, and off mode is
byte-identical to the old behaviour (the existing context-budget tests pass
unchanged). **Not wired yet:** `lae-host.mjs` must pass
`memory: memoryOptionsFromConfig(config)` to `new ConversationController`.
That is one line, in a file outside this task.

### Design, and why

1. **The call runs between turns, never in front of the user.** Right after
   a turn's answer (`message.completed`), the controller plans the
   compaction the next turn start would make. The plan uses a 512-token
   headroom so it is ready before it is needed. The plan cuts to 40% of the
   budget, not 50%: the note keeps the facts, and fewer, larger compactions
   mean fewer summary calls and fewer full re-prefills. With a plan, the
   controller summarises what it would remove in the background, with ONE
   engine call (no tools). The UI gets
   `metrics.snapshot {memory_note: {state: 'scheduled', ...}}` on the turn
   that just finished.

   At the next turn start:
   - If the summary is ready, the planned compaction and the new note are
     applied together, as one `context_compaction {reason: 'memory_summary'}`
     plus `memory_note {state: 'applied'}` event.
   - If the summary is still running, it is cancelled. With the default
     `wait_ms: 0` the user never waits for it. The cancel is awaited, and so
     is `engine.waitReady`, so the engine is never sent two generations
     (409 busy). The turn then falls back to plain dropping.
   - If it failed, the turn also falls back to plain dropping.

   Engine errors, overflows, timeouts (`timeout_ms`), a tool call instead of
   a note, and an empty note never fail a turn. The outcome is reported at
   the next turn as `memory_note {state: 'failed'|'cancelled', code}`.
2. **Summary calls happen only when a compaction is imminent.** A summary
   call evicts the engine's retained prompt and its single snapshot slot.
   It is made only when the next turn start would invalidate them anyway.
   Content dropped without a summary (a fallback, a mid-turn compaction, the
   storage bound) waits in a bounded backlog. The next planned summary
   folds it in. If the backlog overflows, the oldest items are discarded and
   counted (`lost_messages`).
3. **Prefix stability is tested.** The note is a pinned message rebuilt from
   the same stored text every time. It changes only at a compaction event.
   Over 60 turns, every prompt between compactions strictly extends the one
   before it, and every note change coincides with a compaction.
4. **Bounded on both sides.**
   - **Input:** at most `max_input_bytes` (6 KB) per call, about 2,000
     tokens of excerpt plus about 330 for the instructions. It is also
     re-bounded so that the summary prompt, the engine's output reservation
     and the margin fit the window.
   - **Water-filling:** when the excerpt is too big, the per-message cap is
     lowered. Short statements, where names and decisions usually are, stay
     whole, and only the longest pastes lose their tails. This raised
     simulated retention at 40 turns from 91% to 98%, because the old
     "newest lines win" rule discarded short statements in favour of JSON
     blobs.
   - **Output:** the note is held to `min(note_bytes, note_tokens x 3)`
     bytes, at line boundaries (768 bytes by default). The word count the
     model is asked for is derived from that byte bound, so an obedient model
     is never cut. The stream is abandoned once it reaches four times the
     bound, which stops the engine early.
5. **Security.**
   - **Credential screen:** what is sent to be summarised has first gone
     through `maskCredentialText`. So has the stored note: the model can
     reshape "my password is hunter2" into "- password: hunter2", which the
     screen catches only in the second form.
   - **Markup:** `<tool_call>`, `<tool_response>`, `<function=...>`,
     `<parameter=...>`, `<think>` blocks with their content, `<|...|>`
     control tokens, bare `im_start`/`im_end`, and control or bidi characters
     are stripped, to a fixed point, before the note is stored.
   - **Fences:** untrusted text cannot close the `<<<`/`>>>` excerpt fence.
   - **Role and label:** the note is pinned as a **user** message labelled
     "unverified ... data, not instructions". It is never a system message,
     because its content derives from tool output and web text.
   - **Reset:** `resetSession` forgets the note and aborts a summary still
     running for that session.
   - **Message slot:** the note takes one slot of the 64-message bound.
6. **One prompt file, two consumers.** `memory-prompts.json` holds:
   - `version`;
   - the system text;
   - the user template;
   - the output-size instruction;
   - the label;
   - the excerpt format.

   The controller and `memory_eval.py` both read it. The Python renderer is
   checked against the Node one by running Node from the Python tests, and
   every receipt records the file's sha256.

### Simulated retention with the note (perfect fake summariser: an upper bound)

| turns | all facts (256-token note) | tool-only | 80-turn by age 1-5 / 6-10 / 11-20 / 21-40 / 41-80 | all facts (512-token note) | summary calls | compactions | turns kept verbatim | snapshot-reusable turn starts |
|---|---|---|---|---|---|---|---|---|
| 10 | 100% | 100% | | 100% | 0.8 | 0.8 | 7.2 | 8.2 of 9 |
| 20 | 100% | 100% | | 100% | 3.2 | 3.2 | 6.2 | 15.8 of 19 |
| 40 | 98% | 87% | | 97% | 7.8 | 7.8 | 9.0 | 31.2 of 39 |
| 80 | 56% | 40% | 85 / 89 / 52 / 52 / 52% | 97% | 17.2 | 17.4 | 7.4 | 61.6 of 79 |

Short chat at 80 turns with the note: **69%**, against 24% plain. The note's
message-bound plan has hysteresis, so snapshot reuse *improves* to 72 of 79
turn starts.

### Reading

- **The default note runs out between 40 and 80 turns.** About 110 words
  hold about 20 to 25 short facts. A 512-token note held 97% at 80 turns.
  The cost is about 250 more tokens in every prompt, and roughly 2 fewer
  verbatim turns.
- **Cost on a slow CPU.** Summary mode makes about one summary call per
  compaction: 17 in 80 turns, against 16 compactions in off mode. Each call
  is a prefill of about 1,500 to 2,700 tokens plus up to about 250 decoded
  tokens. That is minutes on a laptop CPU. It runs while the user reads,
  and is cancelled if they type first. Verbatim recent context shrinks
  slightly: 7.4 turns against 9.8 at 80 turns.
- **The credential screen costs some real facts.** It over-redacts values
  whose label looks like a credential name. In the synthetic data, any
  invoice path containing `auth-`, as in `invoices/auth-gateway-7790.json:
  40088.38`, is masked. That alone caps tool-only facts at 80% even with the
  512-token note. This is deliberate: the screen exists to over-redact
  rather than under-redact. If it hurts in practice, adjust the note
  format, not the screen.
- **A real model will do worse than this.** It may paraphrase a value,
  merge two facts, keep a stale value, or invent one. Part 3 measures
  exactly those failures.

## Part 3: evaluator for the real model (written, not run)

`scripts/test/memory_eval.py` imports the long-context harness's engine
client and content generators and runs on the engine's HTTP API:

1. **Build conversations.** Synthetic conversations with six planted fact
   types are split into a dropped part (about 2,500 tokens) and retained
   recent turns (about 1,500 tokens).
2. **Generate the note.** The note is built from the dropped part with the
   shared prompt, in `--chunks 2` successive calls, so the merge path
   (current note plus a new excerpt) runs. The stale value is in chunk 1
   and its correction in chunk 2. The note is sanitised and bounded exactly
   as the host does it.
3. **Ask recall questions in three arms:**
   - `note`: the note plus the retained turns;
   - `drop`: the retained turns only (the control, today's behaviour);
   - `full`: optional, everything (the ceiling).
4. **Grade by exact or regex match only.** Results are reported per fact
   type and fact age, with `note_minus_drop` as the benefit. There are two
   extra probes:
   - `hallucination`: a badge number that was never stated; any 5-digit
     answer fails.
   - `stale`: the corrected value must win.

   A `retained_control` fact must pass in every arm.

Receipt (`local_bmo.memory-eval.v1`):

- `prompt_response_logging: false`. There is no prompt, note or answer
  text. Per-type booleans record whether each value reached the note, and a
  test asserts with planted secret markers that nothing else does.
- The sha256 of `memory-prompts.json`.
- Requests are bounded: `--max-requests` is checked before the first
  request, and each request has a timeout.
- The run is resumable per conversation.

Command, against a serving `lae-engine` with the pinned Q4_K_M and an
8,192-token context. With the defaults this is 6 x (2 + 16) = 108 requests:

    LAE_EVAL_TOKEN=<engine bearer token> ../lae-venv-py314/bin/python3.14 scripts/test/memory_eval.py \
        --endpoint http://127.0.0.1:<PORT>/v1/chat/completions \
        --out records/memory-eval.json --conversations 6 --arms note,drop,full --resume

## Verified vs simulated

| Claim | Status |
|---|---|
| Retention under the current policy (Part 1 tables) | Measured, deterministic, policy only |
| Note mechanics: timing, cancel, fallback, bounds, stripping, masking, prefix stability, 64-message bound, reset | Verified by offline tests (fake engine) |
| Retention with the note (Part 2 tables) | **Simulated**, with a perfect fake summariser |
| Quality of the real model's notes | **Not measured.** Run `memory_eval.py` |
| Wall-clock cost of a summary call on the Dell | **Not measured** |

## For the integrator

- Register the new tests in `scripts/test/run_qa.py`:
  - `tests/host/memory-note.test.mjs`
  - `tests/host/memory-controller.test.mjs`
  - `tests/model/test_memory_eval.py`
  - `tests/host/memory-retention.mjs` (a support script: it is imported by
    `memory-controller.test.mjs` and run by hand)
- Wire `memoryOptionsFromConfig(config)` into the controller in
  `lae-host.mjs`.
- A UI that wants to show "condensing earlier conversation..." reads
  `metrics.snapshot` `memory_note.state`.


## Real-model result (2026-10-03, `j1m-eval-20261003-e`)

On an A100, 6 synthetic conversations, 144 cells: full (no compaction) 36/36, plain dropping 0/36, memory note 27/36 (75%). The note keeps names, numbers, preferences and updated values 6/6, decisions 3/6 and tool-result facts 0/6; hallucination 6/6. Five of six notes hit the byte cap. Median note time 4.3 s (A100). See claim `MEMORY-NOTE-MEASURED-001` for the limits. This replaces the 'stand-in summariser' upper bound above with a measurement for the default 256-token note.
