# Status packet — ledger canonical display-prefix constant fix

- Role: Luna implementation worker.
- Branch/worktree: `luna/ledger-prefix-constant-fix-v1` in sibling worktree
  `wt-ledger-prefix-constant-fix-v1`.
- First claimed task: replace repository-file display-prefix schema discovery
  with immutable approved canonical bytes while preserving secure evidence
  validation and refusal behavior.
- Dependencies: current ledger migration preflight implementation and its
  canonical receipt/display contracts at base `83cffab0511b0cb647dcfc6031b45514bc4fc671`.
- Assumptions: the producer-approved six-line preamble/header/separator is the
  immutable contract already represented by the checked-in producer/tests;
  source-only focused tests are sufficient for this bounded repair.
- Uncertainties: no compile, Windows, provider, live evidence, or target
  execution is permitted or attempted; exact runtime behavior remains gated.
