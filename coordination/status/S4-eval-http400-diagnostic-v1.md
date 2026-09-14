# Status Packet — S4 — `EVAL-HTTP400-DIAGNOSTIC-001`

Base exact `main@c03a2695`. No provider, network, orchestrator, lifecycle, ssh
or paid command was run. **USD 0.00 spent.** This is a source claim only and
confers no gate approval.

## What was wrong

Every one of the 37 cases in the shipping fixture errored `http_400`, canary
included, so a paid run in this state would have scored 0/37. The cause is a
single bound.

`native/server/chat_request.cpp` carried `kMaxTools = 32` and refuses a request
whose `tools` array is larger:

    if (!is_type(tools, ...) || tools->array.size() > kMaxTools)
        throw ParseFailure("tools are invalid or too large", false);

The message contains "too large", so `parse_chat_request` returns the code
`request_too_large`, and `http_server.cpp:426` answers `fail(400, parse_error)`.

**The product's own tool surface is exactly 33** — 22 external tools in
`contracts/external-tools/v0.1.0.json` plus the 11 local tools in
`host/tools/local/`, which is the same 33 the shipping fixture declares and the
same 33 `host/agent/controller.mjs:321` assembles at runtime. So the engine
refused the full product tool list by one, before the model was asked anything.
This was never an evaluation-harness defect: the same bound refuses the real
assistant.

The published contract agreed with the engine and not with the product —
`contracts/engine-api/contract.json` declared `"tools":{"max":32,...}` — so the
bound was wrong in the contract, in the engine, and in the README together.

## Proof, offline and free

The whole diagnosis was reproduced without a host and without spending, by
compiling the production parser alone and feeding it the exact bytes the
evaluator puts on the wire (`tests/model/production_tool_call_eval.json` case
`prod-time-001`, 33 tools, 17,056 bytes):

    before: FAIL code=request_too_large tools=0 messages=1
    after:  OK   code=          tools=33 messages=1

Trimming the same payload to 32 tools made it parse on the old bound, which
isolates the count as the whole cause rather than any property of a tool.

## The fix

1. `native/server/chat_request.cpp` — `kMaxTools` 32 → 48, with the reason
   recorded at the constant.
2. `contracts/engine-api/contract.json` — `chat_request.tools.max` 32 → 48.
3. `contracts/engine-api/README.md` — the prose bound, 32 → 48.

**Why 48 and not 33.** 33 is the surface today and a bound with no headroom
turns the next tool into this same outage. 48 keeps roadmap room while staying
strictly below the parser's generic 64-element array rule
(`chat_request.cpp:152`, `value.array.size() >= kMaxMessages`), which applies to
*every* array: at or above 64 the generic rule would be the one that fires and
an oversized tool list would be refused under the wrong name. A test pins that
ordering rather than leaving it to a reader.

## Deliberate contract change — the diagnostic vocabulary

The slice's stated scope. `_error_diagnostic` collapsed every refusal to
`http_400` and discarded the engine's own code, which is why 37 identical
tokens could not say which of eight different 400-producing rules had fired.

`scripts/test/evaluate_tool_calls.py` now reads the error body under a hard
4,096-byte bound and appends the engine's code: `http_400_request_too_large`.
**The vocabulary stays finite.** It is the cross product of two closed sets —
the ten permitted statuses and `ENGINE_ERROR_CODES`, which is every code
published in `contracts/error-codes/error-codes.json` plus the transport-level
codes the HTTP front door emits before a request reaches the contract surface.
A code outside that set is discarded and the diagnostic falls back to
`http_400`, so nothing the wire chooses can reach a receipt. Unreadable,
oversized, non-JSON and wrong-shaped bodies all fall back the same way.

`scripts/test/remote_model_eval.py` carries the mirrored `EVAL_*` sets, because
it re-validates the receipt; a test pins that the two never drift apart.

## Evidence

- `python3 -m unittest discover -s tests -t . -p 'test_*.py'` — **881 OK**
  (+9 in `tests/model/test_tool_call_eval.py`).
- `python3 -m unittest discover -s tests/native -p 'test_*static.py'` — 309 OK.
- `python3 -m unittest tests.qa.test_safe_runner` — 20 OK.
- `python3 scripts/j1m_dry_run.py` — PASS, 24 checks, USD 0.00.
- Engine rebuilt from the changed source (`cmake --build ... --target lae-engine`)
  and driven against the real 5.6 GB Q4 artifact on CPU.

New tests, each pinning a thing that was actually wrong rather than restating
the patch: engine source, published contract and shipping fixture must agree on
the bound; the bound must keep headroom above the shipping surface; the bound
must stay below the generic array rule; every code the engine source emits must
be in the evaluator's vocabulary (a source scan, so a new engine code cannot be
added silently); the two vocabularies must be identical; and a hostile error
body cannot introduce a token of its own.

## What this does NOT establish

**There is still no score on the shipping 33/37 profile.** This slice removes
the defect that guaranteed 0/37; it does not measure the model.

Local evidence that the blocker is gone, and its limit: with the rebuilt engine
the same fixture no longer returns a single `http_400`. All three attempted
cases and the canary now reach the model and fail `transport_timeout` instead —
the request is accepted and generation starts, but this 8 GiB machine pages the
5.6 GB artifact from disk and takes ~200 s per case (engine ready alone took
91.3 s; peak engine RSS 914 MB). That is the machine, not the program:
`remote_model_eval.py` passes one number as both the evaluator's per-request
timeout and its own subprocess kill timeout, and `evaluate_tool_calls.py` caps
`--timeout` at 600, so a full 37-case local run cannot fit inside the harness's
own bounds on CPU. **Scoring the shipping profile needs the CUDA host the lane
was designed around — which is now unblocked, and is the user's launch to make.**

## End-to-end scoring evidence, on the smaller fixture

The shipping fixture cannot be scored on this machine, but the 11-tool dev
fixture (`tests/model/tool_call_eval.json`, ~1.4 k-token prompts) can carry the
path far enough to prove it works. Four cases against the rebuilt engine:

    canary: passed=true, prompt_tokens=1414, error_code=null
    case_count=4, passed=1, failed=0, errors=3
    error_diagnostics: {"http_503_not_ready": 2, "transport_timeout": 1}

**The canary passed.** It had never passed before — every prior run recorded
`prompt_tokens: null` with `http_400`. The engine now accepts the request,
tokenizes 1,414 prompt tokens and returns a well-formed response with usage,
and one case scored a genuine `pass` through the full path: request → model →
XML tool call → parser → scoring. That is the whole pipeline working, which no
run had ever demonstrated.

**The widened vocabulary earned itself on its first real use.** Two errors came
back as `http_503_not_ready` — not the bare `http_503` the old code would have
recorded. The engine was in neither `READY` nor `BUSY`
(`http_server.cpp:423`), so it was in one of `LOADING_MODEL`, `WARMING`,
`DEGRADED`, `STOPPING` or `FAILED`. **The trigger is specific to this machine
and should not be read as a defect in the lane:** the preceding case exhausted
the 600 s client timeout, so the evaluator abandoned a request the engine was
still generating, and the engine did not answer the next two as `READY`. On a
host that finishes a case in seconds this sequence does not arise. Which state
it actually reached was not determined here, and is filed rather than guessed.

## Follow-up recorded, not folded in

`request_too_large` is published as HTTP 413 in
`contracts/error-codes/error-codes.json`, and the header-level body check at
`http_server.cpp:350` returns 413 correctly — but the parse-level failure
returns the same code as **400** (`http_server.cpp:426`, `fail(400, parse_error)`).
Had it been contract-conformant, the old receipts would have read `http_413`
and the size class of the failure would have been obvious without any of this
work. It is a pre-existing contract-visible status change, orthogonal to this
blocker, so it is filed as its own unclaimed row rather than smuggled in here.

`ENGINE-ABANDONED-REQUEST-STATE-001` records the `http_503_not_ready`
observation above: after a client abandons an in-flight generation, the engine
answered the next requests from a non-`READY`, non-`BUSY` state. Observed once,
on an underpowered host, with the cause not established — so it is filed as an
investigation with its evidence, not asserted as a defect.
