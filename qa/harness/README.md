# QA evidence harness

The harness is Python standard-library-only and is designed to run from a
clean checkout. `runner.py run` writes a metadata-only `evidence.v1` manifest;
`runner.py validate` checks it. `runner.py audit-env` is deliberately stricter:
it emits only `present: true|false` for explicitly allowlisted keys.

```text
python qa/harness/runner.py run --build-id fixture-local --output out/evidence/fixture-local/manifest.json
python qa/harness/runner.py audit-env --input qa/fixtures/security/si-001.env --allow SAFE_MODE --output out/evidence/fixture-local/environment.json
python qa/harness/runner.py validate out/evidence/fixture-local/manifest.json
```

This local runner produces fixture/static evidence only. It does not provision
Shadeform, load model weights, or establish native Windows/Intel evidence.
