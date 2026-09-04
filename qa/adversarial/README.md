# SEC-001 threat inventory

`THREAT_INVENTORY.json` maps the initial localhost/path/process/HTTP/tool/URL
threats to runnable deterministic checks. Run them with:

```text
python -m qa.adversarial.run --output out/evidence/security-fixtures.json
```

The inventory explicitly marks real-engine, offline socket, model-integrity,
and orphan-process checks as skipped until their producers exist. A skip is not
a pass and cannot satisfy the corresponding release gate.
