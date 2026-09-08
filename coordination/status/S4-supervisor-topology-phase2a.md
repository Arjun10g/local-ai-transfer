# Status packet — Design A supervisor topology phase 2a

- Role: Luna implementation worker (`/root/remote_canary_hardening`).
- Branch/worktree: `luna/design-a-supervisor-single-owner-v1` in the fresh
  sibling worktree `wt-design-a-supervisor-single-owner-v1`.
- First claimed task: replace the supervisor-parallel process/journal seam
  with one supervisor-owned helper owner and a permanently refused phase-2a
  process path.
- Dependencies: the existing private action-journal owner/lease headers;
  future phase-2b OS-handle evidence verifier remains absent.
- Assumptions: current immutable gates remain false; controller arbitration and
  product registries remain outside this inert source boundary.
- Uncertainties: Windows SDK compilation and target race evidence are not
  available or attempted in this source-only task.
