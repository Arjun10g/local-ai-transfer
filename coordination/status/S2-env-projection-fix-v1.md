# Status Packet

- **Session/role:** Luna Model/Performance — Shadeform environment projection repair
- **Branch/worktree:** `luna/env-projection-fix-v1` / `wt-env-projection-fix-v1`
- **Exact base/task:** `main@9c2d15f95618bfe85ad5c7a21d6f43a20d025215`; `SHADEFORM-ENV-PROJECTION-FIX`
- **Objective:** diagnose and narrowly repair the offline mixed-donor projection refusal without exposing donor values or weakening selected-key, filesystem, or publication invariants.
- **Dependencies:** accepted Shadeform environment hardening `e3b7238`, integrated source `d195235`, current governance reconciliation `9c2d15f`, and `SOL-003`.
- **Observed metadata:** the external donor is a current-user mode-`0600`, single-link regular file under non-group/world-writable ancestors. Descriptor-bound reading succeeds. Parsing refuses an unrelated key on line 47 because it has trailing ASCII whitespace before `=`; all selected Shadeform key names are canonical.
- **Assumption under test:** conventional surrounding ASCII whitespace on an unrelated donor key can be ignored safely only for classification. Any whitespace alias of an allowlisted key, internal key whitespace, malformed assignment, duplicate selected key, or noncanonical selected value must remain refused.
- **Safety boundary:** metadata, line numbers, key names, and sanitized error taxonomy only. No donor value is printed or returned; no donor or repository `.env` is modified; no network, provider, model, native build, or lifecycle mutation is authorized.
- **State:** `IN_PROGRESS`; implementation and focused adversarial evidence pending.
