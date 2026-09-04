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
