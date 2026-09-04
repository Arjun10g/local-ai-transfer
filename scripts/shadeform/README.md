# Shadeform read-only controls

`readonly_preflight.py` adapts the Expert PreFetch operating practices for this
repository. It reads the ignored `.env` only to validate uppercase controls and
never prints credential values. It accepts a local redacted catalogue snapshot or
an explicitly supplied HTTPS GET-only catalogue endpoint, then writes a plan and
optionally an append-only usage ledger.

Shadeform's `hourly_price` API field is cents per hour; the plan preserves the
source cents and reports the converted USD rate. The requested runtime is required
and the plan computes worst-case cost against the remaining project budget after
ledger events. GPU family prefixes, count, minimum VRAM, cloud allow/exclude,
region, interruptible preference, and hourly cap are all applied and rejected
cheaper options are retained in the report.

For the official API, use `https://api.shadeform.ai/v1/instances/types` with
`available=true&sort=price`; the adapter sends the key only in the `X-API-KEY`
header, as required by Shadeform's API documentation. The endpoint is read-only.

The adapter has no provisioning, termination, delete, or account-wide mutation
code. Every plan is bound to an owner and run ID and carries these mandatory
controls: hourly and total caps, no-orphan backstop, salvage-before-teardown, and
Sol review before J1M or any spend. If an instance is eventually approved, the
operator must keep the ledger, set a bounded auto-terminate backstop, salvage
receipts before teardown, and verify ownership before stopping it.

Example with a redacted catalogue snapshot (no network or spend):

```text
python scripts/shadeform/readonly_preflight.py \
  --env-file .env --catalogue catalogue.redacted.json \
  --owner S2 --run-id J1M-preflight-001 \
  --hours 1 --ledger out/evidence/shadeform-cost-ledger.jsonl \
  --output out/evidence/shadeform-readonly-plan.json \
  --record-ledger out/evidence/shadeform-cost-ledger.jsonl
```

The script deliberately does not invent rates. A catalogue profile must supply
`hourly_usd`; missing/invalid rates are excluded from candidates.
