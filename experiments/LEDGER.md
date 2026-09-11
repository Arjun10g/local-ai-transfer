# Shadeform cost ledger

This append-only ledger is empty until Sol approves a provider action. Every
row must identify one phase-owned resource and must be settled only after exact
deletion. A `pending` cost blocks all subsequent provisioning.

| date | phase | instance id | gpu | $/hr | purpose | status | cost logged | idle min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-04 | j1m-prove-20260904-a | 72242e54-3a43-4183-9089-cc5862b983ad | A100_80G | $1.3500 | J1M-prove | deleted | $0.1053 | 0.0 |
| 2026-09-04 | j1m-prove-20260904-b | 8fda7c57-d543-484a-a354-9fa7ffb8c0e3 | A100_80G | $1.3500 | J1M-prove | deleted | $0.1247 | 0.0 |
| 2026-09-04 | j1m-prove-20260904-c | 0e0ce4e9-2eda-4f80-aff0-265e1f38c197 | A100_80G | $1.3500 | J1M-prove | deleted | $0.1019 | 0.0 |
| 2026-09-04 | j1m-build-20260904-a | c6171b35-1859-4b08-b7af-afb3da0aa4f6 | A100_80G | $1.3500 | J1M-build | deleted | $0.1149 | 0.0 |
| 2026-09-04 | j1m-build-20260904-b | 99f65289-0225-4925-83cf-d088384015de | A100_80G | $1.3500 | J1M-build | deleted | $0.1237 | 0.0 |
| 2026-09-04 | j1m-build-20260904-c | 30f1c1d2-e450-4fab-902d-692bb1bdfd1c | A100_80G | $1.3500 | J1M-build | deleted | $0.1280 | 0.0 |
| 2026-09-04 | j1m-build-20260904-d | fdfc734d-40dc-4d8d-97d3-b4602a027ca6 | A100_80G | $1.3500 | J1M-build | deleted | $0.4013 | 0.0 |
| 2026-09-04 | j1m-build-20260904-e | 49f3abda-a7f8-4bc5-a568-1580eca775e0 | A100_80G | $1.3500 | J1M-build | deleted | $0.3996 | 0.0 |
| 2026-09-04 | j1m-proving-run | 9401af8b-3ec1-4380-9a50-b2a09203abd4 | A100_80G | $1.3500 | J1M | deleted | $0.3448 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-b | 77b5be12-585c-4373-aba4-74b1addb9b59 | A100_80G | $1.3500 | J1M-eval-remote-b | deleted | $0.1112 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-c | 45c809df-281f-47c5-8b05-8ec4acfb8062 | A100_80G | $1.3500 | J1M-eval-remote-c | deleted | $0.3373 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-d | 1f7c5b9b-9049-4f27-95f7-7ffe2cd778ec | A100_80G | $1.3500 | J1M-eval-remote-d | deleted | $0.3398 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-e | 667e54a8-6a32-45f5-a0f7-efc40fc07db4 | A100_80G | $1.3500 | J1M-eval-remote-e | deleted | $0.6333 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-f | e8798a85-a20c-4cb4-ab2b-8ca032f696cb | A100_80G | $1.3500 | J1M-eval-remote-f | deleted | $0.4840 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-g | 390b8a1f-ee8a-4fc8-b342-7033dee75837 | A100_80G | $1.3500 | J1M-eval-remote-g | deleted | $0.4824 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-h | 7acf8844-31ad-4a34-ac97-6e08550b2141 | A100_80G | $1.3500 | J1M-eval-remote-h | deleted | $0.4997 | 0.0 |
| 2026-09-04 | j1m-eval-20260904-remote-i | af29daca-960e-4bca-af95-48ed7f132b09 | A100_80G | $1.3500 | J1M-eval-remote-i | deleted | $0.5264 | 0.0 |
| 2026-09-05 | j1m-eval-20260904-remote-j | e3ae8aa9-c5a8-419a-9fe6-5f8ad15eb2d0 | A100_80G | $1.3500 | J1M-eval-remote-j | deleted | $0.5152 | 0.0 |
| 2026-09-05 | j1m-eval-20260904-remote-k | c89aeb37-1b58-49b1-8787-a27ecc49202b | A100_80G | $1.3500 | J1M-eval-remote-k | deleted | $0.5221 | 0.0 |
| 2026-09-05 | j1m-eval-20260904-remote-l | bbbcedee-9bcb-4bd7-ae7b-b285f431592b | A100_80G | $1.3500 | J1M-eval-remote-l | deleted | $0.2355 | 0.0 |
| 2026-09-05 | j1m-eval-20260904-remote-m | 9fac5b8f-36d2-47d3-a89e-f3a9aee16a61 | A100_80G | $1.3500 | J1M-eval-remote-m | deleted | $0.2367 | 0.0 |
| 2026-09-11 | j1m-eval-20260911-remote-d | d75747f8-4801-4f72-8a5f-5713ef333905 | A100_80G | $1.3500 | J1M-EVAL-37D | deleted | $3.2735 | 0.0 |
