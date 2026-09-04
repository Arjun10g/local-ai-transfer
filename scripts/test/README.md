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
no-tool responses, malformed/unknown tools, and prompt injection. It uses the
native engine's Qwen XML tool protocol because this API does not accept an
OpenAI `tools` field. Validate the fixture without starting an engine with:

```text
python3 scripts/test/evaluate_tool_calls.py --dry-run
python3 -m unittest discover -s tests/model -p 'test_*.py' -q
```

Against an already-running local engine, pass its bearer token on the command
line or from a secret manager (the evaluator never prints it), and optionally
pass the engine PID for RSS samples:

```text
python3 scripts/test/evaluate_tool_calls.py \
  --endpoint http://127.0.0.1:PORT/v1/chat/completions \
  --token '<local-token>' --timeout 90 --engine-pid PID
```

Result JSON contains case IDs, pass/fail/error reasons, latency, and optional
RSS only; prompts and generated text are intentionally excluded.
