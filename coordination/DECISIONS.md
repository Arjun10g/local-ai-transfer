# Decision Index

| ADR | Status | Decision |
|---|---|---|
| [ADR-0001](adrs/ADR-0001.md) | Accepted | Qwen3.5-9B text-only Q4_K_M; official HF source; CPU/Vulkan/SYCL ladder; local host architecture |
| [ADR-0003](adrs/ADR-0003-action-journal-event-cap.md) | Accepted for source correction | Retain inactive ActionJournal v0.1.0 and align every operation/detail cap to the already-authoritative 16-event store limit |
| [ADR-0004](adrs/ADR-0004-inert-contract-additive-extension.md) | Accepted for source correction | Additive status-code/predicate extensions to inert, activation-refused, transport-less contracts may retain their version when no existing value, identifier, envelope, on-disk byte, limit, or gate changes; a separate frozen contract is required for the v2 WAL boundary before any transport, import, package, or activation path exists |
| [ADR-0005](adrs/ADR-0005-spend-authorization-and-caps.md) | Accepted | Standing user authorization to launch bounded provider runs for this program; program hard cap USD 50; per-run cost and wall-clock caps recorded per run (`remote-eval-20260911-b`: USD 10.00 / 4 h); lifecycle sequence unchanged and still fail-closed at every step, with post-run verification exact-instance by deletion receipt rather than account-wide. Spend authorization only — not phase, release, capability, provider, credential, or artifact approval |
