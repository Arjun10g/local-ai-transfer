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

   **`os_image` is not purely a provider fact, and this narrows the list.**
   `list_candidates` substitutes `SHADEFORM_IMAGE` for *every* candidate's
   image when that key is set in `.secrets/shadeform.env`
   (`scripts/shadeform_lifecycle.py:2978-2981`). So with, say,
   `SHADEFORM_IMAGE=ubuntu22.04_cuda12.2_shade_os` set, the denvr entry —
   which declares CUDA 12.4 — can never match, and with the primary out of
   stock *which alternate is reachable is decided by an environment variable
   rather than by this list*. That is fail-closed, never a wrong rental, and
   the created image is verified again post-create
   (`scripts/shadeform_lifecycle.py:3659`). It is named distinctly as
   `os_image_env_override` rather than `os_image_mismatch`, because the remedy
   is to unset the override rather than to wait for stock. **Launch
   precondition: leave `SHADEFORM_IMAGE` unset unless every entry the run may
   reach declares that exact image.** A run records which source applied in
   `selected_target.os_image_source`; the plan path reads no environment and
   records the policy and the key name instead, in `os_image_policy`.

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
- A refusal now names which approved entries were considered and, per entry,
  the dimension it failed on — `not_in_catalogue`, `vram_mismatch`,
  `price_mismatch`, `instance_type_mismatch`, `os_image_mismatch`,
  `os_image_env_override`, `interruptible` or `not_approved` — instead of
  reporting one absent pin. Where several catalogue rows could be one entry,
  the reason reported is the deepest dimension any of them reached, because
  "the offer is here but priced differently" is a different fact from "no such
  offer exists". The list is carried on the raised `ShadeformError` as
  `considered` and summarised in its message; on a *successful* selection the
  same list carries the prefix that was skipped to reach it.
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
- A set `SHADEFORM_IMAGE` silently narrows which entries are reachable, as
  §Decision item 2 records. The refusal names it, but nothing prevents an
  operator from setting it.

### Required implementation changes

- Owners/tasks: `J1M-APPROVED-TARGETS-001` (S2), merged by S0 Sol.
- Contracts: `model/conversion/j1m-config.json` replaces `shadeform_target`
  with `shadeform_targets` and adds `budget_policy.per_run_cap_usd`. The
  loader republishes `config["shadeform_target"]` as the primary approved entry
  — bound to the list entry itself, not a copy. Supplying the singular key as
  *input* is refused, so "which entry is primary" cannot become ambiguous. Be
  precise about what that shim is for: on `main` the singular key had five
  readers, and **four of them were rewritten** to explicit accessors
  (`scripts/j1m_dry_run.py:542`, `scripts/j1m_orchestrator.py:906` and `:2067`,
  `scripts/j1m_runner.py:1606` as they stood at `e237bbc`). Each now resolves
  to `primary_shadeform_target` / `shadeform_targets[0]` or to the explicitly
  selected entry, and none reads a different entry, so behaviour is unchanged
  — but the shim itself is exercised only by
  `tests/model/test_comparator_engine.py`, the one reader left. It exists for
  readers outside this branch's diff, not for readers inside it. The cost-event schema
  `local_bmo.shadeform.cost-event.v2` is unchanged: the selected index is
  recorded inside the existing `candidate` object.
- Tests: `tests/performance/test_j1m_approved_targets.py`, registered in
  `scripts/test/run_qa.py`; seven approved-target scenarios and four new
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

> **Corrected 2026-09-14 — this verdict is empirically false for denvr. Read
> the amendment below before relying on any row of the table that follows.**
> The alternates are **unverified**, not approved.

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

## Amendment — 2026-09-14 — the alternate toolchain verdict is empirically false

The verdict above cleared both alternates by reading the pinned requirements.
The reasoning was sound and the conclusion is wrong. The first run in this
program's history ever to select a non-primary entry selected denvr and died at
the first probe.

**Evidence.** Run `j1m-eval-20260914-b`, instance
`a6abd624-5e0a-4024-ae30-0da929068162`, selected `approved_target_index` 1 —
denvr / houston-usa-1 / `A100_sxm4_80G` / `ubuntu22.04_cuda12.4_shade_os` /
USD 1.50 — because the recorded primary was absent from the catalogue at
18:33Z (`selected_target.considered[0].status` is `not_in_catalogue`). It
failed at `eval-stage:remote_toolchain_probe` about eight minutes in, with
`remote toolchain refused: nvcc_unavailable`. Teardown was clean.

**What the table got wrong.** The row asserting nvcc at
`/usr/local/cuda/bin/nvcc` is `image-independent` for both alternates is the
falsified cell. The floor was never the problem: `nvcc >= 12.0` is irrelevant
when there is no `nvcc` on the image at all. The verdict reasoned about a
*version* and the failure was an *absence*, which no amount of reading the
pinned minima could have surfaced — only renting the machine could.

**Scope of the correction.**

- **denvr — REFUTED.** Not merely unverified: measured, and it fails. It must
  not be selected again until an image carrying nvcc is identified for it.
- **crusoe — UNTESTED.** It has never been selected by any run. Its clearance
  rests on the same reasoning that proved false for denvr, so it carries no
  more evidential weight than denvr's did at 18:32Z. Note the mitigating fact
  that crusoe's `ubuntu22.04_cuda12.2_shade_os` is the same image name the
  primary runs, which denvr's was not — that is a real difference, and it is
  still an argument rather than a measurement.
- **hyperstack (index 0) — the only empirically proven entry.** Every prior
  run that recorded a `selected_target` used it: `20260911-d/e/f/g/h`,
  `20260912-a/b/c`, `20260914-a` and `20260914-c`.

**Cost of learning it, and why the figure is misleading.** The run booked
USD 3.637207. The provider's own `cost_estimate` for that instance is
**USD 0.0414** — an 87.9x overstatement, the worst row in the ledger, caused by
the ceiling-settlement defect recorded separately. The true price of this
finding was four cents. The intuitive reading that denvr "fails more expensively
because it costs more per hour" is false: B cost an order of magnitude *less*
than the 25-minute hyperstack run before it, because it died in eight minutes.
Real cost tracks duration, not rate; only the phantom figure tracked rate.

**No pre-launch guard would have prevented it.** A procedural check was
published here and withdrawn the same day once it was found to query nothing;
see §Pre-launch target check — WITHDRAWN below. Nothing available before the
spend reports which target will be rented.

**This amendment records a measurement; it does not re-decide the list.**
Changing the recorded order, removing denvr, or promoting crusoe is Sol's
decision and is not taken here. (The fail-closed guard below does not re-decide
the list either: denvr stays in its recorded position, and is refused because it
is unverified rather than because it is unapproved. Verifying its image, or
crusoe's, is what would make either launchable again.)

## Pre-launch target check — WITHDRAWN, see the correction at the end of this section

> **This rule was published mandatory on 2026-09-14 and withdrawn the same day.
> It does not work. Read §Corrected twice below before acting on any of it.**

**Before every `--execute`, run the same command without `--execute` and read
`selected_target.approved_target_index`.**

```
SOL_J1M_REVIEWED=1 <python> scripts/j1m_orchestrator.py --mode eval
```

The plan path performs the read-only catalogue query and prints the exact entry
the launch will rent. It mutates nothing, mints no key, and costs USD 0.00 —
`main()` returns before the `SOL_J1M_REVIEWED` gate is even reached when
`--execute` is absent.

**If the index is not 0, do not launch.** Index 0 is the only empirically proven
entry; index 1 is refuted and index 2 is untested. A non-zero index means the
primary is absent from the catalogue, which is a condition to report, not to
spend through.

### Corrected twice, 2026-09-14 — THE CHECK ABOVE IS VACUOUS. DO NOT RELY ON IT.

**The rule stated above does not work and cannot work. It is retained only so
that this correction has something to point at. There is currently no
pre-launch signal about target availability at all.**

The first correction to this section blamed a check-then-act race. That was
wrong too, and the real cause is simpler and worse: **the plan path never
queries the provider.** `build_plan` defaults `target_index` to the first
approved entry and returns (`scripts/j1m_runner.py:2006-2008`):

```
    if target_index is None:
        target_index = approved[0][0]
```

The orchestrator's only live catalogue query is
`sf.list_candidates(...)` at `scripts/j1m_orchestrator.py:2446`, which is inside
`execute()` (lines 2388-3188). Without `--execute`, `main()` prints the plan and
returns before reaching it. The dry run therefore reports
`approved_target_index: 0` **as a constant**, never as a measurement.

The condition "if the index is not 0" can never be true, so the rule above
refuses nothing, ever. It is not a weak guard; it is not a guard.

**Evidence, code and observation agreeing.** A plan run taken while hyperstack
was demonstrably absent from the catalogue — runs D, E and F had all just landed
on denvr — still printed `approved_target_index: 0`, hyperstack,
montreal-canada-2, and emitted **no `considered` key at all**. A real selection
populates `considered` because it examines candidates; the plan path omits it
because it examines none. Compare run D's lifecycle receipt, which recorded
`considered: [{"approved_target_index": 0, "cloud": "hyperstack", "status":
"not_in_catalogue"}]` and then selected index 1.

**A separate defect the same evidence exposes.** The plan object reports
`target_selection: "first approved entry that exactly matches a live catalogue
candidate; refused if none does"`. The plan path performs no such match and
issues no such refusal. A plan artifact that describes provider-dependent
behaviour it never performs is how three operators in one day concluded they had
checked something they had not. Either the plan path should perform the query it
claims, or it should stop reporting a `selected_target` and a `target_selection`
that imply one.

**Consequence for the fail-closed refusal.** A refusal inside the orchestrator
after selection and before the billable create is therefore not the *robust*
option against a weak procedural one — **it is the only guard that can exist**,
because selection happens exactly once, inside `execute()`, after the operator's
last opportunity to intervene.

**Decided and implemented (2026-09-14, later the same day).** The operator chose
the fail-closed refusal, and it landed as `f3d30c4`. Each approved entry now
carries `toolchain_verified`; `execute()` refuses an unverified entry after
selection and before the ephemeral key or the billable create. Absent means
unverified, and only a literal `True` counts. The recorded values are the
observations above: hyperstack `true`, denvr `false`, crusoe `false` (never run).
It was exercised on a real launch the same evening: run `j1m-eval-20260914-h`
drew denvr and was refused at USD 0.00 with no instance, no key and no ledger
row. The offline dry-run gate proves the same property with a dedicated
scenario (25 checks, up from 24).

**Consequence for retrying.** With provider-truth settlement live a wrong-target
landing is cheap — run D booked **USD 0.031488** against a denvr ceiling of
3.6372, a 115x reduction, matching the provider's own `cost_estimate` of
0.0314884417. But cheap retrying is not a strategy here: because the plan path
is blind, relaunching does not resample a signal, it just re-enters the same
selection with the same catalogue. Runs B, D, E and F landing on denvr is that
pattern, not bad luck.

**What actually establishes which target was rented:** only the lifecycle
receipt's own `selected_target`, after the money. Nothing before the spend
reports it, and "pre-checked, index 0" must not be recorded as evidence that a
run was on the primary.

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
