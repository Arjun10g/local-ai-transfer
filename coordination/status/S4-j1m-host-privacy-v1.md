# Status packet — J1M remote host privacy

- **Role:** Luna implementation worker (S4).
- **Branch/worktree:** `luna/j1m-host-privacy-v1` in sibling worktree
  `wt-j1m-host-privacy-v1`.
- **Base:** exact `main@a0aa02cbf1be459df0eda1c4ecff2ae2e9741f9d`. `main` was
  not modified, and nothing under its `.secrets/`, `experiments/` or
  `artifacts/` was touched. No provider, network, orchestrator, lifecycle, ssh,
  or model command was run. No spend.
- **Claimed task:** `J1M-HOST-PRIVACY-001` — make every remote J1M command
  create owner-private host state, so a paid eval run can return its receipts.
- **Dependencies:** `J1M-KEY-HANDLE-001`, B-004, ADR-0005. Review owners S0, S2.
- **Design note:** `scripts/shadeform/HOST_PRIVACY.md`, cross-referenced from
  `scripts/shadeform/SALVAGE_TRANSPORT.md`.
- **State:** `READY_FOR_REVIEW`. Source claim only; no gate approval, and the
  next paid launch remains Sol's decision.

## 1. The incident this slice exists for

Run `j1m-eval-20260911-remote-d`, the first paid Shadeform eval run.

| Fact | Value |
| --- | --- |
| Instance | `d75747f8-4801-4f72-8a5f-5713ef333905` |
| Cloud / GPU / region | hyperstack, `A100_80G` ×1, montreal-canada-2 |
| Created / active | 2026-09-11T20:24:07Z / 20:28:44Z |
| Booked cost | **USD 3.273486** (`deletion.actual_cost_usd`) |
| Failed stage | `eval-stage:j1m_runner` |
| Salvage | **8 requested, 0 completed** |
| Teardown | deletion confirmed, no orphan, ephemeral key removed |

Eval stages 1–4 completed. Stage 5 (`j1m_runner.py --run`) failed `remote_exit`
exit **2** with an **empty** `stderr_tail`. Stage 7 failed 127
(`/scratch/j1m/venv/bin/python: No such file or directory`) because the venv
stage inside the runner never ran. Salvage recorded seven
`salvage_transport_failed` (exit 1, files never written) and one
`salvage_not_private_regular_file` for `toolchain-receipt.json`, written `0644`.

## 2. Root cause

Two independent defects, neither visible to any local validator. Every command
passed `validate_persisted_argv`; `scripts/j1m_dry_run.py` passed.

1. **The remote login shell's umask.** `ssh_base(...) + argv` is run by the
   host's login shell, which carried the image default `022`. `mkdir -p`
   produced `0755` directories; the probes wrote `0644` receipts.
   `_private_ancestor_snapshot` requires every descendant of the trusted root
   to be fully owner-private (`& 0o077`), so `run_commands`' very first write —
   `/scratch/j1m/artifacts/command-receipt.json`, before any stage — raised
   `private ancestor is unsafe`, and `_safe_cli` exited 2 with its typed
   refusal on stdout. The `0644` receipt was then correctly refused by salvage.
2. **A progress directory no plan created.** Found while proving fix 1, and
   independently fatal. On the host the uploaded runner is
   `/scratch/j1m/j1m_runner.py`, so `ROOT`/`PRIVATE_OUTPUT_ROOT` is
   **`/scratch`**, not `/scratch/j1m` — Sol's "verify which root" question:
   verified, it is `/scratch`. Its progress file is therefore
   `/scratch/experiments/runtime/J1M.progress.json`, and no remote plan ever
   created `/scratch/experiments`. Even a correct `0700` artifact tree would
   have refused with `private ancestor is unavailable`.

## 3. What changed

No privacy predicate or salvage policy was weakened.
`_private_ancestor_snapshot`, `_private_atomic_write` and the salvage refusal
codes are byte-unchanged, and `tests/performance/test_j1m_host_privacy.py`
`NoWeakenedPolicyTests` pins that.

**The umask prefix lives in `scripts/shadeform_lifecycle.py`:**
`REMOTE_SHELL_UMASK = "077"` and `remote_shell_prefix() -> ["umask","077","&&"]`,
appended by `ssh_base` after the `user@ip` destination. One place; every remote
command in the tree inherits it, including
`scripts/shadeform/remote_external_tools.py`. `&&` not `;`, so a shell that
cannot set the umask never runs the command that would create world-readable
state. `scp_base` is deliberately untouched — scp runs no login shell, and a
`umask` operand there would be a filename.

**Exact remote plan diff.**

`_remote_workspace_stages(ssh_user, remote_root, progress_relative)` is now a
module-level function; 5 stages → 6:

```
  scratch_root              sudo mkdir -p /scratch                       (unchanged)
  scratch_owner             sudo chown <user> /scratch                   (unchanged)
- remote_workspace          mkdir -p /scratch/j1m
+ remote_workspace          mkdir -p /scratch/j1m /scratch/j1m/artifacts \
+                                    /scratch/experiments /scratch/experiments/runtime
+ remote_workspace_private  chmod 700 /scratch/j1m /scratch/j1m/artifacts \
+                                     /scratch/experiments /scratch/experiments/runtime
  scratch_df                df -P -k /scratch                            (unchanged)
  scratch_writable          test -w /scratch                             (unchanged)
```

`_eval_remote_commands` 14 stages → 15; stage 0 rewritten and stage 1 added,
stages 2–14 byte-identical to the old 1–13:

```
- mkdir -p /scratch/j1m/model /scratch/j1m/engine/native \
           /scratch/j1m/engine/vendor /scratch/j1m/engine/scripts \
           /scratch/j1m/engine/tests/native /scratch/j1m/artifacts
+ mkdir -p /scratch/j1m/model /scratch/j1m/engine /scratch/j1m/engine/native \
           /scratch/j1m/engine/vendor /scratch/j1m/engine/scripts \
           /scratch/j1m/engine/tests /scratch/j1m/engine/tests/native \
           /scratch/j1m/artifacts
+ chmod 700 <the same eight paths>
```

The two `engine`/`engine/tests` parents are now named explicitly because
`chmod` is not recursive and `mkdir -p` leaves a pre-existing directory alone.
`/scratch` itself is **not** forced to `0700`: it is the trusted root, checked
with `& 0o022` only, so an image's root-owned `0755` `/scratch` is acceptable
once `scratch_owner` chowns it.

Consequences of the extra stages: `eval_commands[:3]/[3:]` → `[:4]/[4:]`;
`bootstrap_timeouts` `(30,120,270)` → `_EVAL_BOOTSTRAP_TIMEOUTS =
(30,30,120,270)`; `fixed_setup`/`host_shutdown_seconds` now multiply by
`_WORKSPACE_STAGE_COUNT` (6) instead of a literal 5. Static comparator slack
`run_seconds - ceiling_seconds` 309.0 s → **249.0 s**; the
`ceiling < run < host_shutdown < watchdog < provider` envelope still holds and
`fits_static_worst_case` is unchanged (already `false`).

**How receipts are written now.** The five uploaded host-side writers —
`remote_toolchain_probe.py`, `cuda_device_probe.py`, `remote_eval_prepare.py`,
`remote_model_eval.py` (startup preflight *and* eval receipt) and
`remote_comparator_eval.py` — each publish through an identical local
`_publish_private_receipt(output, encoded)`: create the parent, `chmod 0700` it,
`mkstemp` + `fchmod 0600` + `fsync` in that directory, then `os.replace` over
the final name. Duplicated per file rather than shared, because these scripts
are uploaded individually with no shared module — the same pattern
`_run_identity` already uses. Result is exactly the shape
`_salvage_staged_bytes` requires: `0600`, single link, owner `= uid`, atomic.

**Failed-stage diagnosis.** `_remote` now records `stdout_tail` for a failed
stage and a transport timeout via `_failed_stage_output_tail`, which applies
the usual `_redacted_output_tail` screening *and* re-validates the truncated
tail, so a JSON fragment the persisted-value validator would reject becomes
`<redacted>` instead of a receipt that cannot be written.

## 4. Offline host simulation — the proof

`scripts/j1m_dry_run.py:host_tree_simulation()`. `tempfile.mkdtemp()` stands in
for `/scratch`, chmodded **0755** (the shape the image leaves after
`scratch_owner`). The directory-creating commands are taken from the production
builders — never retyped — filtered to `{mkdir, chmod, touch}`, `/scratch` is
substituted for the temporary root, and each is executed as
`sh -c "umask 077 && <command>"`: the exact text `ssh_base` will send. The
result is judged by `_private_ancestor_snapshot` over all 11 receipt paths the
widest comparator selection can publish, then by a real `_private_atomic_write`
of the progress file and the command receipt. A synthetic `touch` answers
"would a file created by this shell be `0600`?" from the shell rather than by
assertion. A path outside the stand-in root raises `DryRunError`.

| | this branch | `main@a0aa02c`'s plan (the control) |
| --- | --- | --- |
| replayed commands | 5 | 3 |
| umask | `umask 077 &&` | `umask 022 &&` |
| receipt paths accepted | **11 / 11** | **0 / 11** |
| `_private_atomic_write` published | 2 / 2 | **0 / 2** |
| refusals | none | **13** — `private ancestor is unavailable` (progress) and `private ancestor is unsafe` (artifacts) |
| file created by the plan's own shell | **`0600`** | **`0644`** |

The control's commands are `_RUN_D_HOST_COMMANDS`, transcribed verbatim from
`main@a0aa02cbf1be459df0eda1c4ecff2ae2e9741f9d`, so the check demonstrably
fails on `main`'s plan and passes on this one, and both halves of the incident
are reproduced with distinct typed errors. Both checks run in the gate:
`host_tree_simulation_accepts_every_private_write` and
`host_tree_simulation_reproduces_the_run_d_refusal`.

This local `sh` invocation is the only thing `j1m_dry_run.py` executes; the
module docstring now says so. Still no network, no provider call, no spend.

## 5. Deliberate contract changes for review

1. **`_remote` retains failed-stage stdout.** Two tests previously pinned "a
   failed remote receipt does not retain stdout"
   (`test_remote_failure_does_not_retain_stdout_evidence`,
   `test_orchestrator_failed_remote_receipt_does_not_retain_stdout`). They are
   rewritten to the new contract: a completed stage still retains nothing, the
   tail is bounded to `_STDERR_TAIL_LIMIT` = 1200 bytes, and credential-shaped
   output is still `<redacted>`. **Residual:** ordinary non-credential stdout
   of a *failed* stage is now persisted in the lifecycle receipt. The evaluator
   stages print a single JSON summary (`schema`, `status`, `metrics`) with no
   prompt or response text, so the practical exposure is the typed refusal this
   change exists to surface — but it is a relaxation of the previous invariant
   and is Sol's call to confirm.
2. **Plan-pinning tests updated:** `len(_eval_remote_commands)` 14 → 15 and the
   new `chmod` stage asserted (`test_comparator_eval`); static comparator slack
   309.0 → 249.0 s (`test_comparator_engine`); `eval_commands[:3]/[3:]` →
   `[:4]/[4:]` and the bootstrap index shift (`test_j1m_lifecycle`). Each
   carries the reason at the call site.

## 6. Evidence

Reproduced in `wt-j1m-host-privacy-v1`, after the documented dry-run
precondition `chmod 700 artifacts artifacts/qwen35-9b`:

| Command | Result |
| --- | --- |
| `python3 -m unittest discover -s tests -t . -p 'test_*.py'` | **Ran 839 tests, OK** (811 on base `a0aa02c`; +28 in `tests/performance/test_j1m_host_privacy.py`) |
| `python3 scripts/j1m_dry_run.py` | **PASS** — **148** argv recorded (none executed), **24** checks, 0 failed, network none, provider calls none, spend $0.00 |
| `python3 scripts/test/run_qa.py --root . --skip-native --output -` | inventory **0 missing / 0 unknown**; overall status `BLOCKED` (safe mode, unchanged from base) |
| `git diff --check main...HEAD` | exit **0** |
| `git status --porcelain` | clean |

Commits on `luna/j1m-host-privacy-v1`:

- `security:` the source fix (umask prefix, explicit `chmod 700`, derived
  progress directories, private receipt publication, failed-stage `stdout_tail`)
- `qa:` the host-tree simulation, the new suite, the `run_qa.py` registration
  and the deliberate pinning-test updates
- `docs:` `scripts/shadeform/HOST_PRIVACY.md`, the `SALVAGE_TRANSPORT.md`
  cross-reference, the claims row and this packet

## 7. Open / not proved

- The simulation uses this machine's filesystem semantics. It does not prove
  the provider image's `/scratch` is a real directory, on a mode-honouring
  filesystem, or that `sudo chown` succeeds there. Those stay with the
  `scratch_root` / `scratch_owner` / `scratch_writable` preflight stages, each
  fail-closed.
- `prove` and `build` mode were repaired incidentally (both write under
  `/scratch/j1m/artifacts` and the progress path, which only the eval plan used
  to create) but neither has been exercised on a host.
- `sudo`'s umask union behaviour (`sudo` never lowers the caller's umask) is
  relied on for `sudo mkdir -p /scratch`; the subsequent explicit `chown` plus
  the `& 0o022` root check make the run safe either way.
- Item 5.1 above needs an explicit Sol decision before merge.
