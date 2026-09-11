# Status Packet

- **Session:** S2
- **Required model:** GPT-5.6 Luna
- **Role:** Model/Performance — approved Shadeform target selection
- **Timestamp (UTC):** 2026-09-11
- **Branch/worktree:** `luna/j1m-approved-targets-v1` / `wt-j1m-approved-targets-v1`
- **Current phase:** J1M launch-path unblocking (no provider, no network, no spend)
- **Primary task ID:** J1M-APPROVED-TARGETS-001
- **Task state:** READY_FOR_REVIEW
- **Base `main` commit:** `e237bbcb44c45e9a05b7d9cf6c4d10853f79d8b2`

## Objective for this work interval

The pinned hyperstack A100_80G at USD 1.35 is absent from the 2026-09-11
catalogue, so a single `shadeform_target` makes the eval lane unlaunchable
rather than merely expensive. Sol decided the config should record an *ordered
list* of approved exact targets and the orchestrator should take the first one
the catalogue still offers exactly. This interval implements that decision with
tests and the offline gate, so it can be reviewed and merged.

This advances no gate. It changes which machines may be rented and nothing
about what is built, what is evaluated, or whether anything may be launched.

## Inputs and dependencies

- `AGENTS.md` — "never silently fall back to ... a different backend" and the
  stop condition on silently switching a reviewed input. That rule is why the
  answer is an approved *list* rather than a widened match.
- `coordination/BLOCKERS.md` B-004 — the prescribed non-A100 canary is
  unexecutable by current code, and Sol's recorded deviation runs the built-in
  probes on the A100 itself. Hence: A100-only entries.
- `coordination/adrs/ADR-0005-spend-authorization-and-caps.md` — program cap
  USD 50; per-run caps recorded per run (`remote-eval-20260911-b`: USD 10.00 /
  4 h). Only the program cap was enforced in source.
- Sol's read-only catalogue observation, 2026-09-11 ~19:00Z: crusoe /
  culpeper-usa-1 / A100_80G / `A100_80G` / USD 1.65 / 80 GiB /
  `ubuntu22.04_cuda12.2_shade_os`; denvr / houston-usa-1 / A100_80G /
  `A100_sxm4_80G` / USD 1.50 / 80 GiB / `ubuntu22.04_cuda12.4_shade_os`;
  paperspace / newyork-usa-1 / A100_80G / USD 3.28; hyperstack /
  montreal-canada-2 now H100 only at USD 2.50.
- `model/conversion/j1m-config.json`, `scripts/j1m_orchestrator.py`,
  `scripts/j1m_runner.py`, `scripts/j1m_dry_run.py`,
  `scripts/shadeform_lifecycle.py`, `scripts/shadeform_watchdog.py`.

## Work completed

- **Config** `model/conversion/j1m-config.json` — `shadeform_target` becomes
  `shadeform_targets`, three entries ordered cheapest first. Entry 0 is the
  pre-existing hyperstack entry with every value unchanged; entries 1 and 2 are
  denvr at USD 1.50 and crusoe at USD 1.65, each declaring `instance_type`,
  `os_image`, `interruptible: false` and its own derived
  `active_run_cost_usd` (USD 0.375 and 0.4125 for a 0.25 h proving run). Each
  entry carries an explicit `approved` boolean. `budget_policy.per_run_cap_usd`
  records ADR-0005's USD 10.00.
- **Validation** `scripts/j1m_runner.py` — `_validate_shadeform_targets` fails
  closed on a missing field, an unknown field, a non-A100 GPU, a multi-GPU or
  sub-80 GiB entry, an entry whose cost does not follow from its own rate, a
  list that is not cheapest-first, a duplicated exact identity, an entry that
  changes the shared clock envelope, an interruptible entry, an unapproved
  entry with no reason, an approved entry carrying one, a list with nothing
  approved, and a config supplying the singular key as input. Each mode's
  `active_cost_usd` is checked against the primary rate, so mode cost and
  target rate cannot drift apart unnoticed.
- **Selection** `scripts/j1m_orchestrator.py` — `select_approved_target` walks
  the recorded order and takes the first entry matching exactly on cloud,
  region, GPU, VRAM and hourly rate, plus `instance_type` and `os_image`
  wherever the entry declares them, and never an interruptible offer. Nothing
  approved in the catalogue raises `ShadeformError("no approved J1M target is
  an eligible current catalogue candidate")` before `ephemeral_key_directory`
  and before every provider POST.
- **Cost** — `_comparator_budget` takes the selected rate instead of reading a
  constant, and reports `hourly_usd` alongside its projection;
  `mode_active_cost_usd` computes a mode's active cost at a given rate.
  `_eval_deadline_ceiling` is untouched and remains purely time-based.
- **Per-run cap** — `_assert_selected_target_within_per_run_cap` refuses
  pre-spend when the selected entry's worst case exceeds the recorded cap. The
  worst case is the longer of this mode's provider backstop and
  `SHADEFORM_AUTO_TERMINATE_HOURS`, times the selected rate. The new
  `sf.configured_auto_terminate_hours` reads only the key name and the numeric
  ceiling, and `_auto_delete` now calls it instead of re-parsing.
- **Recording** — the selected entry and its list index reach the plan
  (`approved_targets`, `selected_target`, `target_selection`), the lifecycle
  receipt (`selected_target`, including the cost projection), the ledger
  reservation's existing `candidate` object, and the watchdog argv
  (`--approved-target-index`, parsed and bounded, provenance only). The
  cost-event field set and schema `local_bmo.shadeform.cost-event.v2` are
  unchanged.
- **Dry run** `scripts/j1m_dry_run.py` — seven approved-target scenarios and
  three new checks; `drive()` accepts a fake catalogue and `_candidate()`
  builds a row for any entry with per-dimension overrides, so a near-miss is
  built from the real entry rather than hand-typed.
- **Tests** `tests/performance/test_j1m_approved_targets.py` — 57 cases,
  registered in `scripts/test/run_qa.py` `TEST_INVENTORY` as `lifecycle`.

## Backward compatibility and default-plan equivalence

`load_config` republishes `config["shadeform_target"]` as the primary approved
entry, bound to the list entry itself rather than to a copy, so all five
pre-existing readers are untouched. Supplying the singular key as *input* is
refused, so "which entry is primary" cannot become ambiguous.

With the primary present the plan is the plan it was. Asserted against the
figures the single-target config produced, written down rather than recomputed
from the new code (`MAIN_PLAN_COSTS`):

| mode | active_run_cost_usd | provider_backstop_cost_usd |
|---|---:|---:|
| prove | 0.3375 | 0.4219 |
| build | 1.6875 | 2.1094 |
| eval | 2.619 | 3.2738 |
| canary | 0.3375 | 0.4219 |

The plan gains exactly three keys — `approved_targets`, `selected_target`,
`target_selection` — and `candidate` is still the primary target object.
`build_plan(config, "build")["commands"]` is still `command_plan(config)`, the
eval upload count still equals `_eval_deadline_ceiling`'s `upload_count`, and
no approved entry's cloud, region or rate appears anywhere in any remote
command. A target is a machine to rent; it never enters a command.

## Toolchain and image verdict — both alternates approved

Read rather than assumed, because Sol's instruction was not to ship an
alternate that fails after the money is gone. **Nothing in this lane pins a
CUDA minor version.**

- `scripts/test/remote_toolchain_probe.py:20` — the floor is `nvcc >= 12.0`.
  Re-verified identically at `scripts/j1m_orchestrator.py:1464` and
  `scripts/test/remote_model_eval.py:970`. CUDA 12.2 and 12.4 both clear it.
- `scripts/test/remote_toolchain_probe.py:112-113` — nvcc must be exactly
  `/usr/local/cuda/bin/nvcc`, which is image-independent within the
  `*_shade_os` CUDA family. Re-verified at `scripts/j1m_orchestrator.py:1480`
  and `scripts/test/remote_model_eval.py:982`.
- `scripts/j1m_orchestrator.py:749` and `scripts/j1m_runner.py:1517` —
  `CMAKE_CUDA_ARCHITECTURES=80`. sm_80 *is* the A100, and every approved entry
  is a single 80 GiB A100.
- `scripts/test/cuda_device_probe.py:67-68` — exactly one GPU, at least
  70000 MiB, name containing `a100`. An A100_80G reports 81920 MiB.
  Re-verified at `scripts/j1m_orchestrator.py:1453`.
- `scripts/j1m_orchestrator.py:1459` and `scripts/test/remote_model_eval.py:963`
  — the driver version is length-bounded and recorded, **never compared**. A
  driver pin is precisely what would have made an image choice a silent
  post-spend failure, and there is none.
- `scripts/test/remote_toolchain_probe.py:15-21` — `python3 >= 3.8`,
  `git >= 2.30`, `cmake >= 3.18`, `g++ >= 9.0`; Ubuntu 22.04 ships 3.10, 2.34,
  3.22 and 11.x. The six packages `dpkg-query` demands (`:22`, `:92-107`) are
  the six the bootstrap installs at `scripts/j1m_orchestrator.py:724`.

Crusoe's `ubuntu22.04_cuda12.2_shade_os` is **the same image the approved
primary already runs** (`tests/performance/test_j1m_lifecycle.py:587`), so it
adds no toolchain surface whatsoever. Denvr's
`ubuntu22.04_cuda12.4_shade_os` differs only above the floor, and that image
family is already exercised at
`tests/performance/test_remote_external_tools_lifecycle.py:256`.
`AlternateImageToolchainTests` pins this verdict so it breaks if the floor is
raised above either image, if a driver comparison appears, or if the bootstrap
stops installing a demanded package.

Neither entry is marked `approved: false`. The field and its mandatory
`unapproved_reason` exist so a future withdrawal is recorded rather than
deleted.

## Evidence (reproduced in this worktree at the branch tip)

- `python3 -m unittest discover -s tests -t . -p 'test_*.py'` → **801 tests,
  OK**. The same command on `main@e237bbc` in this worktree gives 743 OK, so
  the 58 new cases are 57 in `test_j1m_approved_targets.py` plus one in
  `test_j1m_dry_run.py`.
- `python3 scripts/j1m_dry_run.py` → **PASS**, **142 argv recorded / 0 refused**
  — unchanged from the 142 baseline, because every scenario run uses its own
  recorder exactly as the pre-existing scenario runs do. 15 runs now, up from
  8. All three new checks PASS:
  - `approved_target_selection_follows_the_list` — primary only → #0; denvr
    only → #1; crusoe only → #2; all three present in reverse catalogue order
    → #0.
  - `unapproved_catalogue_is_refused_pre_spend` — an H100-only catalogue, denvr
    at USD 1.55, and crusoe in `culpeper-usa-2` each refuse with no key
    directory, no cost row and no teardown call.
  - `selected_target_prices_every_recorded_figure` — #0 @ USD 1.35/h reserved
    USD 0.421875; #1 @ USD 1.50/h reserved USD 0.46875; #2 @ USD 1.65/h
    reserved USD 0.515625; each worst case inside the USD 10.00 cap.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -` →
  `BLOCKED`, **0 missing / 0 unknown**, 71 discovered (70 on `main@e237bbc`).
  `BLOCKED` is the standing evidence state and is unrelated to this branch.
- `git diff --check main...HEAD` → **exit 0**. Working tree clean.

Two operator preconditions had to be satisfied in this worktree before the
baseline was green, and neither is a source change: `artifacts/` and
`artifacts/qwen35-9b/` are `0755` as checked in, and the dry run's
`operator_artifact_destination_is_salvage_ready` check correctly fails until
they are `0700`. `scripts/j1m_dry_run.py:1180-1196` already records this as an
operator precondition rather than a defect. The same `chmod` is required in any
worktree before a launch.

## Decisions taken here, standing for review

1. **`approved` is explicit and required on every entry, including the
   primary.** Sol asked for the hyperstack entry "unchanged"; this adds exactly
   one key to it. The alternative — treating an absent `approved` as true —
   is fail-open on the one field that decides whether a machine may be rented,
   which `AGENTS.md` forbids. The added key is the one documented difference in
   the plan's `candidate` block.
2. **The approved list must be ordered cheapest first, and a list that is not
   is refused rather than re-sorted.** The order is the policy, so a reordered
   list is a changed policy and should be reviewed, not silently normalised.
3. **Every approved entry must share one clock envelope.** A target is a
   machine, not a schedule; an entry that moved the backstop or watchdog would
   change the safety envelope the modes were reviewed against.
4. **An exact match compares `os_image` where the entry declares it.** Not in
   Sol's stated match tuple, and the catalogue's `os_image` is partly derived
   from `SHADEFORM_IMAGE`. Included because the image is what the toolchain
   verdict is *about*: an entry approved on CUDA 12.2 should not match an
   offer that arrives on something else. The primary declares no image, so its
   behaviour is unchanged.
5. **ADR-0005's per-run cap is now enforced in source.** It was recorded but
   never checked; only the program cap was. The check is deliberately
   conservative — it uses the auto-terminate ceiling rather than the effective
   1.25x backstop — so it bounds the bill by the longest the provider could
   hold the instance.
6. **`--approved-target-index` on the watchdog is provenance only.** It is
   parsed and bounded but drives no code path; the recovery profile the
   watchdog acts on is unchanged.

## Blockers

None for this slice. B-004 remains OPEN and is not touched: this branch
supplies no activation evidence for any target, and the prescribed non-A100
canary is still unexecutable (`scripts/j1m_orchestrator.py:2123`;
`scripts/test/cuda_device_probe.py:67-68`). A successful run on an alternate
would be evidence about that alternate only.

## Sol action requested

1. Review and merge `luna/j1m-approved-targets-v1`; the merge ratifies
   ADR-0006.
2. Confirm the three recorded entries are the ones intended, since the list is
   now the whole of what may be rented.
3. Note decision 1 above — the primary entry gains an explicit
   `"approved": true` — and decision 4, the `os_image` comparison, both of
   which go slightly beyond the stated brief.
4. Before any launch from a fresh worktree: `chmod 700 artifacts
   artifacts/qwen35-9b`, then re-run `python3 scripts/j1m_dry_run.py`.
