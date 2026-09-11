# ADR-0006 — Ordered list of approved exact Shadeform targets

- **Status:** Accepted — records which exact provider offers may be rented and
  in what order; it approves no run, advances no phase or release gate, and
  changes no capability state
- **Date:** 2026-09-11
- **Decision owner:** S0 Sol
- **Authors/reviewers:** S0 Sol (decision); S2 (implementation and evidence);
  S4 (lifecycle and cost review)
- **Related tasks/risks:** B-004, ADR-0005, `J1M-APPROVED-TARGETS-001`,
  `MODEL-COMPARATOR-EVAL-001`; RISK register entries for remote spend and
  provider lifecycle
- **Provenance:** decision made by S0 Sol and relayed for recording. Sol's
  merge of this branch to `main` is the ratifying act. Bookkeeping is not gate
  approval: this ADR records an approved-target list and the semantics for
  choosing between its entries, and nothing else.

## Context

Until 2026-09-11 the evaluation lane could rent exactly one machine.
`model/conversion/j1m-config.json` carried a single `shadeform_target` —
hyperstack / montreal-canada-2 / A100_80G / USD 1.35 per hour — and
`execute()` matched it against the live catalogue with one `next(...)`,
refusing with `approved J1M target is not an eligible current catalogue
candidate` when it was absent.

Two facts changed on 2026-09-11 and together make an ordered list decidable:

1. **The pinned offer is gone.** In Sol's read-only catalogue query at
   approximately 19:00Z the hyperstack A100_80G at USD 1.35 is absent;
   hyperstack / montreal-canada-2 now lists only an H100 at USD 2.50. A single
   pin therefore makes the lane unlaunchable rather than expensive.
2. **Approved alternates exist at a known price.** The same catalogue lists
   denvr / houston-usa-1 / A100_80G / `A100_sxm4_80G` / USD 1.50 / 80 GiB /
   `ubuntu22.04_cuda12.4_shade_os` and crusoe / culpeper-usa-1 / A100_80G /
   `A100_80G` / USD 1.65 / 80 GiB / `ubuntu22.04_cuda12.2_shade_os`, both
   non-interruptible. paperspace lists an A100_80G at USD 3.28, which is inside
   no reviewed budget worth spending.

The obvious wrong fix is to widen the match — take the cheapest A100 the
catalogue happens to offer. `AGENTS.md` forbids exactly that: "never silently
fall back to a cloud model, a different weight file, a different backend", and
renting an unreviewed provider in an unreviewed region on an unreviewed image
is the same class of substitution. The decision is therefore about *which
alternates are approved*, not about loosening what "approved" means.

## Fixed constraints

- Target: a single NVIDIA A100 with 80 GiB, on Shadeform, non-interruptible.
- Model: unchanged. No target entry may influence what is built or evaluated.
- Offline: selection is decided from one read-only catalogue listing and the
  recorded list; no other network input participates.
- Memory: 80 GiB VRAM is a floor, not a preference — the Q4 rebuild and the
  bf16 comparator arm are sized against it.
- Security: the selection runs before ephemeral key generation and before any
  provider POST, so a refusal costs USD 0.00 and mints nothing.
- Dependency: ADR-0005's program cap of USD 50 and its per-run caps.
- Schedule: B-004's activation-reliability question is still untested, and
  nothing here tests it.

## Decision criteria

Ranked before the options were written.

1. No unapproved machine can ever be rented, under any catalogue state.
2. A refusal must happen pre-spend, with a typed reason, at USD 0.00.
3. The cheapest approved entry that is actually available must win.
4. Every recorded dollar figure must follow the entry that was rented.
5. With the primary available, the run must be byte-for-byte the run it was.

## Options considered

### Option A — Keep the single pin and wait for restock

- Description: change nothing; relaunch when hyperstack restocks.
- Benefits: zero source change; criteria 1, 2, 4 and 5 hold trivially.
- Costs: the lane is blocked for an unbounded period on a third party's
  inventory. B-004 stays untested and no quality evidence is produced.
- Risks: waiting is indistinguishable from being blocked, and the blocker
  register already carries a lane that has spent USD 0.00 across two attempts.
- Evidence: the catalogue observation above.

### Option B — Widen the match to any in-budget A100_80G

- Description: drop the exact identity match and take the cheapest catalogue
  A100_80G under a price ceiling.
- Benefits: always launchable while any A100 exists; satisfies criterion 3.
- Costs: fails criterion 1 outright. `list_candidates` already sorts by price
  (`scripts/shadeform_lifecycle.py:3001-3016`), so the code would silently rent
  whichever provider, region and OS image happened to be cheapest that minute.
- Risks: the image is not incidental. The eval builds CUDA from source against
  `/usr/local/cuda/bin/nvcc` and refuses anything else
  (`scripts/test/remote_toolchain_probe.py:112-113`); an unreviewed image that
  places CUDA elsewhere fails *after* the instance is paid for.
- Evidence: `AGENTS.md` "Fixed technical decisions" and its stop condition on
  silently switching a reviewed input.

### Option C — Ordered list of approved exact targets

- Description: replace `shadeform_target` with `shadeform_targets`, an ordered
  list of exact entries Sol has approved individually. Walk it in order, take
  the first entry with an exact live match, refuse if none matches.
- Benefits: satisfies all five criteria. Order encodes "cheapest approved
  first" as data rather than as a sort at run time, so criterion 3 is met
  without reintroducing a preference the catalogue could steer.
- Costs: adding an alternate is a decision that has to be recorded, which is
  the point rather than a cost. Three readers of the singular key had to be
  reconciled.
- Risks: an entry recorded carelessly is now rentable. Mitigated by validating
  every entry fails-closed at config load and by requiring each entry's own
  cost arithmetic to follow from its own rate.
- Evidence: `tests/performance/test_j1m_approved_targets.py` (57 cases) and the
  seven approved-target scenarios in `scripts/j1m_dry_run.py`.

## Decision

Option C, with five recorded semantics.

1. **The approved list is ordered and the order is the policy.**
   `model/conversion/j1m-config.json` records `shadeform_targets` as three
   entries, cheapest first: hyperstack / montreal-canada-2 / A100_80G at
   USD 1.35 (the pre-existing entry, its values unchanged); denvr /
   houston-usa-1 / A100_80G / `A100_sxm4_80G` / `ubuntu22.04_cuda12.4_shade_os`
   at USD 1.50; crusoe / culpeper-usa-1 / A100_80G / `A100_80G` /
   `ubuntu22.04_cuda12.2_shade_os` at USD 1.65. The list is walked in recorded
   order, never in catalogue order, and a list that is not cheapest-first is
   refused at load rather than re-sorted.

2. **A match is exact on every dimension the entry declares.** Cloud, region,
   GPU, VRAM and the hourly rate to the cent must agree, and `instance_type`
   and `os_image` must agree wherever the entry states them. An interruptible
   offer never matches. The primary entry declares neither `instance_type` nor
   `os_image`, which is why its matching behaviour — and therefore the default
   plan — is exactly what it was before this ADR. A near-miss is not a bargain:
   denvr at USD 1.55, or crusoe in `culpeper-usa-2`, is a *different offer*
   than the one that was approved and is refused.

3. **No approved entry available means the run is refused pre-spend.**
   `select_approved_target` raises `ShadeformError("no approved J1M target is
   an eligible current catalogue candidate")` before ephemeral key generation
   and before every provider POST. A cheaper unapproved substitute is never
   taken, at any price.

4. **Cost derives per entry, clocks do not.** Each entry carries its own
   `active_run_cost_usd` derived from its own rate — USD 0.3375, 0.375 and
   0.4125 for a 0.25 h proving run — and the loader refuses an entry whose
   arithmetic does not follow. The comparator budget, the mode active cost and
   the ledger reservation are all computed from the *selected* entry's rate.
   The clock envelope is the opposite: every approved entry must share one
   reviewed `provider_backstop_hours` / `host_shutdown_backstop_hours` /
   `external_watchdog_seconds`, and `_eval_deadline_ceiling` is time-based and
   unchanged, so an alternate cannot move the safety clocks.

5. **ADR-0005's per-run cap becomes a refusal.** ADR-0005 §Decision item 3
   records per-run caps — USD 10.00 for `remote-eval-20260911-b` — but only the
   program cap was ever enforced in source. `budget_policy.per_run_cap_usd` now
   records USD 10.00 and `_assert_selected_target_within_per_run_cap` refuses
   pre-spend when the selected entry's worst case exceeds it. The worst case is
   the longer of this mode's provider backstop and the standing
   `SHADEFORM_AUTO_TERMINATE_HOURS` ceiling, times the selected rate: at the
   ceiling of 3 h that is 3 x USD 1.35 = USD 4.05, 3 x USD 1.50 = USD 4.50 and
   3 x USD 1.65 = USD 4.95, each inside USD 10.

## Rationale

Criterion 1 is satisfied structurally rather than by care. The approved list is
the only source of candidates the selector will consider, and each comparison
is an equality against a recorded value, so there is no code path on which an
unapproved machine can be chosen — including the path where it is cheaper. The
dry run drives that case deliberately: an offer identical to the crusoe entry
except priced at USD 0.99 is refused.

The ordering matters more than it looks. `list_candidates` already sorts the
catalogue by price (`scripts/shadeform_lifecycle.py:3001-3016`) for reasons
recorded in its own comment, so a selector that walked the *catalogue* would
have inherited the provider's cheapest-first ordering as its preference. The
approved list is walked instead, and the dry run proves catalogue order is
irrelevant by presenting all three entries in reverse and still selecting the
primary.

Per-entry cost derivation is what stops a rate change from becoming a quiet
under-report. Before this branch, `_comparator_budget` read a literal
`config["shadeform_target"]["hourly_usd"]`, so a run on a USD 1.65 machine
would have recorded USD 1.35 arithmetic — a projected marginal comparator cost
understated by 22%, and a reservation understated by the same. The clock
figures are deliberately left alone: `fits_static_worst_case` is `False` for
every selection and whether the comparator phase runs at all is decided at run
time by `_comparator_clock_available`, exactly as B-004 records. This ADR does
not change that, and an alternate target does not make the comparator phase any
more likely to fit.

Making ADR-0005's per-run cap executable is the smallest change that turns a
recorded number into a guard. It is strictly conservative: the check uses the
auto-terminate ceiling rather than the effective 1.25x backstop, so it bounds
the bill by the longest the provider could hold the instance rather than by how
long the run intends to take.

## Consequences

### Positive

- The lane is launchable on an approved machine while the primary is out of
  stock, without widening what "approved" means.
- A refusal now names which approved entries were considered and why each was
  skipped, instead of reporting one absent pin.
- Every recorded dollar figure is the one that will actually be billed.
- ADR-0005's per-run cap is enforced rather than noted, for the first time.
- The ledger reservation, the lifecycle receipt, the plan and the watchdog argv
  all say which approved entry was rented and at what list position.

### Negative / accepted trade-offs

- Three machines are now rentable where one was. This is the decision, not a
  side effect, and each was approved individually by Sol against an observed
  catalogue listing.
- The approved list is a snapshot of a catalogue that changes. An entry that
  goes out of stock is skipped silently — correctly — so a list that has gone
  entirely stale presents as a refusal rather than as a stale-list warning.
- `budget_policy.per_run_cap_usd` is recorded per config rather than per run,
  so a run needing a different cap requires an edit and a review rather than an
  operator flag. That is deliberate.
- An `approved: false` entry stays in the list with its reason. Nothing in this
  branch sets one; the field exists so a withdrawal is recorded rather than
  deleted.

### Required implementation changes

- Owners/tasks: `J1M-APPROVED-TARGETS-001` (S2), merged by S0 Sol.
- Contracts: `model/conversion/j1m-config.json` replaces `shadeform_target`
  with `shadeform_targets` and adds `budget_policy.per_run_cap_usd`. The
  loader republishes `config["shadeform_target"]` as the primary approved entry
  — bound to the list entry itself, not a copy — so every pre-existing reader
  is unchanged. Supplying the singular key as *input* is refused, so "which
  entry is primary" cannot become ambiguous. The cost-event schema
  `local_bmo.shadeform.cost-event.v2` is unchanged: the selected index is
  recorded inside the existing `candidate` object.
- Tests: `tests/performance/test_j1m_approved_targets.py`, registered in
  `scripts/test/run_qa.py`; seven approved-target scenarios and three new
  checks in `scripts/j1m_dry_run.py`.
- Migration/rollback: rollback is deleting the two alternate entries. The
  primary entry's values are unchanged, so a one-entry list behaves exactly as
  the single pin did.

## Relationship to B-004

B-004's workaround prescribes "a cheaper non-A100 activation/SSH/CUDA canary
with no model download" before any HF-backed evaluator run. **That canary
remains unexecutable by current code, and this ADR does not change that.**
`execute()` refuses canary mode at `scripts/j1m_orchestrator.py:2123`, and the
canary plan invokes `scripts/test/cuda_device_probe.py`, which raises
`expected_single_a100_80g_not_proven` unless it finds exactly one A100 with at
least 70000 MiB (`:67-68`). Sol has already recorded the accepted deviation:
the built-in probes are run on the A100 itself as the activation canary.

Two consequences follow and are recorded here rather than left implicit.

- **The approved list is A100-only, by validation and not by convention.** The
  loader refuses an entry whose `gpu` does not contain `a100`, or that is not a
  single 80 GiB GPU, because such an entry could only fail after the money is
  spent. A non-A100 entry cannot be added to this list without first making the
  probes able to pass on it.
- **This ADR supplies no activation evidence.** Recording two alternates does
  not test activation reliability on either of them, and B-004's question is
  still open. A first successful run on an alternate is evidence about that
  alternate only.

## Toolchain and image verdict

Sol's instruction was not to ship an alternate that will predictably fail once
the money is gone, so the pinned requirements were read rather than assumed.
**Both alternates are approved.** Nothing in this lane pins a CUDA minor
version.

| requirement | where it is pinned | denvr 12.4 | crusoe 12.2 |
|---|---|---|---|
| `nvcc >= 12.0` | `scripts/test/remote_toolchain_probe.py:20`; re-verified `scripts/j1m_orchestrator.py:1464` and `scripts/test/remote_model_eval.py:970` | passes | passes |
| nvcc at `/usr/local/cuda/bin/nvcc` | `scripts/test/remote_toolchain_probe.py:112-113`; `scripts/j1m_orchestrator.py:1480`; `scripts/test/remote_model_eval.py:982` | image-independent | image-independent |
| `CMAKE_CUDA_ARCHITECTURES=80` | `scripts/j1m_orchestrator.py:749`; `scripts/j1m_runner.py:1517` | sm_80 is the A100 | sm_80 is the A100 |
| one A100 with >= 70000 MiB | `scripts/test/cuda_device_probe.py:67-68`; re-verified `scripts/j1m_orchestrator.py:1453` | 81920 MiB | 81920 MiB |
| driver version | `scripts/j1m_orchestrator.py:1459`; `scripts/test/remote_model_eval.py:963` | recorded, never compared | recorded, never compared |
| `python3 >= 3.8`, `git >= 2.30`, `cmake >= 3.18`, `g++ >= 9.0` | `scripts/test/remote_toolchain_probe.py:15-21` | Ubuntu 22.04 defaults clear all four | same |
| six host packages present to `dpkg-query` | `scripts/test/remote_toolchain_probe.py:22`, `:92-107` | installed by the bootstrap apt stage, `scripts/j1m_orchestrator.py:724` | same |

Crusoe's `ubuntu22.04_cuda12.2_shade_os` is **the same image the approved
primary already runs** — it is the image name in the existing lifecycle fixture
at `tests/performance/test_j1m_lifecycle.py:587` and the dry run's own default
candidate — so it introduces no new toolchain surface at all. Denvr's
`ubuntu22.04_cuda12.4_shade_os` differs only in a CUDA minor version above the
12.0 floor, and the same image family is already exercised at
`tests/performance/test_remote_external_tools_lifecycle.py:256`.

`tests/performance/test_j1m_approved_targets.py::AlternateImageToolchainTests`
pins this verdict so it fails if the floor is later raised above either image,
if a driver comparison is introduced, or if the bootstrap stops installing a
package the probe demands.

## Validation

- **Experiment/test:** `python3 -m unittest discover -s tests -t . -p
  'test_*.py'`; `python3 scripts/j1m_dry_run.py`; `python3
  scripts/test/run_qa.py --root . --skip-native --output -`.
- **Acceptance threshold:** every approved-target scenario selects the entry
  the list requires; every refusal scenario ends with no key directory, no cost
  row and a typed reason; the default plan's commands, uploads and costs are
  the figures the single-target config produced.
- **Artifact:** `coordination/status/S2-j1m-approved-targets-v1.md` and
  `experiments/runtime/dry-run-receipt.json`.
- **Revisit trigger:** any of — a fourth entry proposed for the list; an entry
  needing a different clock envelope; a non-A100 entry becoming necessary
  (which requires B-004's canary question to be reopened first); a run's actual
  cost exceeding its recorded per-run cap; or a change to the exact-match
  dimensions in §Decision item 2.

## Approval

- Sol:
- Affected Luna acknowledgements:
