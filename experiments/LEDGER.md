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
| 2026-09-11 | j1m-eval-20260911-remote-e | 576c7f56-1d5e-4805-945a-b0157b2eae24 | A100_80G | $1.3500 | J1M-EVAL-37E | deleted | $3.2735 | 0.0 |
| 2026-09-11 | j1m-eval-20260911-remote-f | b8b2f935-1f6d-42f3-86cf-2022e43ab301 | A100_80G | $1.3500 | J1M-EVAL-37F | deleted | $3.2735 | 0.0 |
| 2026-09-11 | j1m-eval-20260911-remote-g | 3741a88d-9644-4656-b10e-f727392bf21e | A100_80G | $1.3500 | J1M-EVAL-37G | deleted | $3.2735 | 0.0 |
| 2026-09-11 | j1m-eval-20260911-remote-h | 0233d85d-14ac-4b64-bdbe-5f9fb48455bb | A100_80G | $1.5000 | J1M-EVAL-37H | deleted | $3.6372 | 0.0 |
| 2026-09-12 | j1m-eval-20260911-remote-h | 478c254f-af51-4343-93a6-82968d9beb5b | A100_80G | $1.3500 | J1M-EVAL-37H | deleted | $3.2735 | 0.0 |
| 2026-09-12 | j1m-eval-20260912-a | 56889450-9b0c-470e-80be-fcaa55e2f3a2 | A100_80G | $1.3500 | J1M-EVAL-38A | deleted | $3.2735 | 0.0 |
| 2026-09-12 | j1m-eval-20260912-b | b928e587-73ad-4646-a489-0418c1b9d98f | A100_80G | $1.3500 | J1M-EVAL-38B | deleted | $3.2735 | 0.0 |
| 2026-09-12 | j1m-eval-20260912-c | 6158ddd6-86c5-4572-bb75-225507b6a27b | A100_80G | $1.3500 | J1M-EVAL-38C | deleted | $3.2735 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-a | 766fcf10-ec25-436d-831d-7460d1b56b8f | A100_80G | $1.3500 | J1M-EVAL-37A | deleted | $3.2735 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-b | a6abd624-5e0a-4024-ae30-0da929068162 | A100_80G | $1.5000 | J1M-EVAL-37B | deleted | $3.6372 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-c | 3b18e2ac-1613-4d86-b421-8384e4d6d4f3 | A100_80G | $1.3500 | J1M-EVAL-37C | deleted | $0.4398 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-d | 0d66d87a-07bb-46ab-a37c-999d7a4391f6 | A100_80G | $1.5000 | J1M-EVAL-37D | deleted | $0.0315 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-e | 4a0c0afb-6774-427f-bea6-f86c28d2b5fb | A100_80G | $1.5000 | J1M-EVAL-37E | deleted | $0.0288 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-f | 509e358d-54ba-472f-81bb-67621b17a607 | A100_80G | $1.5000 | J1M-EVAL-37F | deleted | $0.0311 | 0.0 |
| 2026-09-14 | j1m-eval-20260914-g | 83e9d5fa-cbab-4960-a408-8be9089244ce | A100_80G | $1.3500 | J1M-EVAL-37G | deleted | $0.0000 | 0.0 |
| 2026-09-16 | j1m-eval-20260916-a | d4f2bb71-e6ab-47a8-b887-909a1ba392eb | A100_80G | $1.3500 | J1M-EVAL-37I | deleted | $0.4510 | 0.0 |
| 2026-10-03 | j1m-eval-20261003-a | d8c956c0-af12-43c9-8ef1-4f0323f0b8ff | A100_80G | $1.3500 | J1M-EVAL-37A | delete-failed | $0.0000 | 0.0 |
| 2026-10-03 | j1m-eval-20261003-b | c2e6dfeb-9743-4f8a-9353-c597275f2ce0 | A100_80G | $1.3500 | J1M-EVAL-37B | deleted | $0.4370 | 0.0 |
| 2026-10-03 | j1m-eval-20261003-c | 599e1bda-1409-4bd3-97fc-33c2ee33e191 | A100_80G | $1.3500 | J1M-EVAL-37C | deleted | $0.0000 | 0.0 |
| 2026-10-03 | j1m-eval-20261003-d | 3f2cbbce-dbeb-4ff7-852e-86f231d28796 | A100_80G | $1.3500 | J1M-EVAL-37D | deleted | $0.8093 | 0.0 |
| 2026-10-03 | j1m-eval-20261003-e | 030b4915-a690-450d-afd3-471092a6e6c3 | A100_80G | $1.3500 | J1M-EVAL-37E | deleted | $0.5322 | 0.0 |
| 2026-10-03 | j1m-eval-20261003-f | a1776531-3d3f-4ca6-bca4-51fd71fe792c | A100_80G | $1.3500 | J1M-EVAL-37F | deleted | $0.5664 | 0.0 |
