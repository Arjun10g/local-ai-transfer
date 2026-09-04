# Shadeform cost ledger

This append-only ledger is empty until Sol approves a provider action. Every
row must identify one phase-owned resource and must be settled only after exact
deletion. A `pending` cost blocks all subsequent provisioning.

| date | phase | instance id | gpu | $/hr | purpose | status | cost logged | idle min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
