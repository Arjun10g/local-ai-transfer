# QA entry point

From a clean checkout, run:

```text
python3 scripts/test/run_qa.py --output out/evidence/qa-local/summary.json
```

This script is the canonical complete gate. It enumerates every
`tests/**/test_*.py` module explicitly, avoiding the `tests/qa` versus top-level
`qa` package shadowing that occurs with `unittest discover -s tests` when no
top-level directory is supplied. For Python-only diagnosis, use
`python3 -m unittest discover -s tests -t . -p 'test_*.py'`.

The command runs Python fixture tests, contract conformance, adversarial
fixtures, Node's built-in host and `tests/security` tests when Node is available, the REL-001
skeleton scan, and local CMake/CTest if available. It never installs packages
or contacts a network. Any unavailable/native-Windows/real-model/Shadeform
capability is recorded as mandatory `SKIP`/`UNPROVEN`, not `PASS`. A successful
fixture run is therefore a typed `CONDITIONAL_PASS` with `release_passed=false`
until every mandatory target-evidence record is proven. Node 24 specifically is
mandatory release evidence even if a different local Node version runs the tests.

## Remote external-tool QA

`remote_external_tools_qa.mjs` is a bounded hostile-emulator harness for the
Graph, browser CDP/proxy, Copilot ACP, and operator-grant boundaries. It uses
synthetic credentials, loopback emulators, injected fake ACP/CDP processes, and
never contacts a real account or provider. The emulator/fuzz/soak path refuses
to start unless a remote orchestrator supplies both the exact marker and a
bounded run ID:

```text
LAE_REMOTE_QA_MARKER=REMOTE-EXTERNAL-TOOLS-V1 \
LAE_REMOTE_RUN_ID=remote-2026-09-04-a \
node scripts/test/remote_external_tools_qa.mjs \
  --seed=17,31,73 --fuzz-cases=64 --soak-iterations=25 \
  --output=out/evidence/remote-external-tools.json
```

The receipt contains aggregate case IDs/counters, deterministic seeds, bounds,
and typed outcomes only; prompt text, response content, headers, tokens, and
other credential material are excluded. Receipt writes are bounded and atomic.
`--self-test` is the only lightweight local execution mode; a local run without
the marker exits with status 2 and emits `remote_marker_required`.

## Local Qwen tool-call evaluation

The deterministic eight-case fixture covers tool selection, exact arguments,
no-tool responses, malformed/unknown tools, and prompt injection. It sends
the native `tools` field, which the pinned runtime renders through Qwen's chat
template. Validate the fixture without starting an engine with:

```text
python3 scripts/test/evaluate_tool_calls.py --dry-run
python3 -m unittest discover -s tests/model -p 'test_*.py' -q
```

Against an already-running local engine, provide a protected token file or an
inherited `LAE_EVAL_TOKEN` environment variable (the evaluator never prints
or accepts token material as an argument), and optionally pass the engine PID
for RSS samples. The endpoint is fail-closed to explicit-port HTTP on
numeric `127.0.0.1`, with exactly `/v1/chat/completions` and no
credentials/query/fragment. Hostnames (including `localhost`) are rejected.
Proxy environment variables are ignored and HTTP redirects are rejected:

```text
python3 scripts/test/evaluate_tool_calls.py \
  --endpoint http://127.0.0.1:PORT/v1/chat/completions \
  --token-file /protected/path/native-token \
  --timeout 90 --engine-pid PID
```

Result JSON contains case IDs, pass/fail/error reasons, latency, and optional
RSS only; prompts and generated text are intentionally excluded.

The parser contract and value-framing vectors are shared with the host parser
in `tests/model/qwen_xml_vectors.json`; quoted values remain text, while only
exact JSON primitives/containers are normalized. Alternate fixtures are
strictly schema-validated and bounded before any engine request.
