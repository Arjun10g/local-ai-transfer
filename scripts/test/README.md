# QA entry point

From a clean checkout, run:

```text
python3 scripts/test/run_qa.py --output out/evidence/qa-local/summary.json
```

The command runs Python fixture tests, contract conformance, adversarial
fixtures, Node's built-in host tests when Node is available, the REL-001
skeleton scan, and local CMake/CTest if available. It never installs packages
or contacts a network. Any unavailable/native-Windows/real-model/Shadeform
capability is recorded as `SKIP`, not `PASS`.

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
