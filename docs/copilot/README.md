# Letting GitHub Copilot delegate chores to BMO

`lae-mcp.mjs` is a small stdio MCP server. GitHub Copilot starts it on the laptop, and it forwards small jobs to the BMO host that is already running there, over `127.0.0.1` only. It holds no model and no tools of its own. Each job still needs your approval in the BMO window on the laptop.

The design and its sources are in [`docs/research/COPILOT_MCP_COMPATIBILITY.md`](../research/COPILOT_MCP_COMPATIBILITY.md).

## What works and what does not

Works:
- **VS Code agent mode.** VS Code uses the older `initialize` handshake (protocol 2025-11-25).
- **Copilot CLI 1.0.81 and later.** The CLI uses the newer stateless protocol (2026-07-28). The bridge serves both from one process.
- **Visual Studio 17.14+ / 2026.** It reads the same `servers` format. Its MCP tools are off by default, so enable them in the tool picker. Untested here.
- **JetBrains, Xcode and Eclipse Copilot.** They accept local stdio servers. Untested here. JetBrains reportedly asks before every call, even for read-only tools.

Does not work, by design:
- **The Copilot cloud agent, Copilot code review, and github.com chat.** They run on GitHub's servers and cannot reach the laptop. Do not try to expose BMO to them: that would need an internet-facing tunnel, which the project's loopback-only rule forbids.
- **Anything without your approval on the laptop.** Copilot's "Allow" button is not BMO's consent.
- **Long blocking calls.** Every tool call returns within 20 s. Longer jobs hand back a `job_id`, and Copilot polls it with `bmo_job_status`.

## Tools

| Tool | What it does | Copilot asks first? |
|---|---|---|
| `bmo_ask` | Starts a job and waits up to 18 s. Returns the answer, or a `job_id` to poll. | Yes: not read-only |
| `bmo_job_status` | Waits up to 15 s for a job, then returns its status, and the answer once completed. | VS Code: no (read-only) |
| `bmo_job_cancel` | Cancels a job. Safe to repeat. | Yes |
| `bmo_health` | Shows whether BMO is running, the queue length, and the approval mode. | VS Code: no (read-only) |

BMO's answers come from a small local model. They are returned in an `answer` field, labelled as untrusted data.

How jobs behave on the laptop:
- **Queue cap.** BMO holds at most 3 jobs, counting the ones awaiting approval. Beyond that, starts fail with `queue_full`.
- **Your own chat comes first.** Chatting with BMO on the laptop preempts a running job. The job is requeued, shown with the phase `waiting_for_operator_chat`. After 3 interruptions it fails with the code `preempted`.
- **Phases.** Results and progress messages show the host's `phase`: `waiting_for_operator`, `waiting_for_engine`, `waiting_for_operator_chat`, `starting`, `generating`, `reading_files` or `using_tool`.

## Setup order (Windows laptop)

1. **Start BMO with delegation enabled:** `Start-BMO.ps1 -Mode app -EnableDelegation`. Delegation is off by default. Only while it is on and the host is running does the host write `%LOCALAPPDATA%\BMO\host.json`, the file that tells the bridge its loopback port. With no `host.json`, every tool says "BMO is not running, or delegation is turned off".
2. **Show the delegate key** on the laptop with `Start-BMO.ps1 -ShowDelegateKey`, or with `python local\bmo_local.py delegate-key`. Copy it, and never paste it into a file in a repository. `Start-BMO.ps1 -RotateDelegateKey` issues a new key; after that, re-enter it in every client.
3. **Add the server to your client.** Pick one of these:
   - **VS Code.** Run **MCP: Open User Configuration** and merge in [`vscode-mcp.json`](vscode-mcp.json). Replace the placeholder with the absolute path to `lae-mcp.mjs`. Keep `node.exe` as an absolute path. On first start VS Code prompts for the key (a password box) and keeps it in its secret storage, not in the file.
   - **Copilot CLI.** Save [`copilot-cli-mcp.json`](copilot-cli-mcp.json) outside any repository, for example as `C:\bmo\copilot-cli-mcp.json`, and fix the path in it. Then, in PowerShell:
     ```powershell
     $env:BMO_DELEGATE_KEY = '<paste key>'   # this shell session only; nothing is written to disk
     copilot --additional-mcp-config "@C:\bmo\copilot-cli-mcp.json" --allow-tool "bmo(bmo_job_status)" --allow-tool "bmo(bmo_health)"
     ```
     The CLI passes only `PATH` to servers, so the config lists `BMO_DELEGATE_KEY` under `env`. The `${BMO_DELEGATE_KEY}` placeholder expands from the CLI's own environment (see "Unverified" below). Do not use `--allow-all` and do not pre-approve `bmo_ask`.
4. **Approve the first delegated job on the laptop.** Ask Copilot something small, for example "Ask BMO to reword this sentence: …". Copilot asks to run `bmo_ask`, and then the BMO window shows an approval card.
   - Every job needs your **Approve** on the laptop, unless you have granted time-limited approval there.
   - A job may wait up to about 120 s for approval. If you have not approved it within 18 s, Copilot receives `{job_id, status: "awaiting_approval", phase: "waiting_for_operator"}`, with a note that a human must approve it on the laptop, and keeps polling `bmo_job_status`.

Optional: copy [`copilot-instructions-snippet.md`](copilot-instructions-snippet.md) into your **user-level** instructions. For Copilot CLI that is `~/.copilot/instructions/bmo.instructions.md`; for VS Code, use a user-profile instructions file. Use [`agents-md-snippet.md`](agents-md-snippet.md) only in the `AGENTS.md` of a personal, unshared repository. Copy [`agents/bmo-delegate.agent.md`](agents/bmo-delegate.agent.md) into `~/.copilot/agents/` (read by both VS Code and Copilot CLI). Keep all of them out of shared repositories: they advertise a personal server to every contributor.

## 10-minute verification

1. Record the VS Code version (Help → About), the Copilot Chat extension version, the output of `copilot --version`, and your Copilot plan. The "MCP servers in Copilot" policy applies only to Business and Enterprise plans.
2. In VS Code, run **MCP: List Servers** → `bmo` → **Show Output**. Expect a line like `lae-mcp … info ready key_configured=true`, and then `initialize version=2025-11-25`. If you see `modern_request version=2026-07-28` instead, the client chose the newer protocol. Both are fine; record which one appeared.
3. Open the tool picker in Agent mode. Expect exactly `bmo_ask`, `bmo_job_status`, `bmo_job_cancel` and `bmo_health`.
4. Ask "call bmo_health". Expect no confirmation prompt in VS Code, and `ready: true` in the result.
5. Ask "use bmo_ask to summarise: <a paragraph>". Expect a Copilot confirmation, then the approval card on the laptop, then the answer with a `provenance` note.
6. Ask for something slow, approve it late, and watch Copilot poll `bmo_job_status`. Then start another job and press **Stop** in Chat while it waits. The BMO window should show that job as cancelled.
7. Copilot CLI: start it as in setup step 3, run `/mcp`, and confirm that `bmo` is connected with 4 tools. Repeat steps 4–6. `bmo_health` should not prompt, because of `--allow-tool`. Then start once with `--deny-tool "bmo"` and confirm that no bmo tools appear.
8. Keep the client logs (VS Code Output panel, CLI session log) as receipts. Use them to update the "Unverified" list below.

## Troubleshooting (the `error.code` in a tool result)

| Code | Meaning | Fix |
|---|---|---|
| `bmo_not_running` | BMO is not running or delegation is off: no valid `host.json`, the host's process is gone, or the connection was refused | Run `Start-BMO.ps1 -Mode app -EnableDelegation` |
| `no_key` | `BMO_DELEGATE_KEY` is missing, malformed, or an unexpanded `${…}` placeholder | VS Code: clear the stored input so it prompts again. CLI: set `$env:BMO_DELEGATE_KEY` before starting `copilot` |
| `unauthorized` | The host rejected the key; it may have been rotated | Run `Start-BMO.ps1 -ShowDelegateKey` and re-enter the key |
| `auth_rate_limited` | Too many wrong keys were tried | Wait a minute, then fix the key |
| `queue_full`, `rate_limited` | 3 jobs are already queued or awaiting approval, or job starts are rate-limited. `retry_after_s` says how long to wait | Wait, or cancel stale jobs |
| `busy` | More than 4 calls are in flight in this bridge | Wait for one to finish |
| `task_too_large`, `context_too_large`, `job_too_large`, `too_large` | The request does not fit BMO's limits | Send less text |
| `approval_expired`, `denied` | Nobody approved the job in time, or you declined it | Retry only if you meant to |
| `preempted` | You kept chatting with BMO, which interrupted the job 3 times | Retry later |
| `timeout`, `host_timeout` | The host did not answer in time | Check `bmo_health` and the BMO window |
| `unknown_job` | The `job_id` expired or never existed | Start a new job |

The bridge logs only metadata to stderr: event names, tool names, durations and hashed job ids. It never logs the key, tasks, context or answers. Set `BMO_MCP_LOG=debug|info|error|off` in `env` to change the level.

## Security notes

- The key comes only from the `BMO_DELEGATE_KEY` environment variable. The bridge refuses to start if it is given any command-line argument, and it removes the key from its own environment after reading it.
- The bridge connects only to `127.0.0.1:<port from host.json>`. It sends no `Origin` header, follows no redirects, ignores proxy settings, and reads at most 256 KiB per response.
- On macOS and Linux the bridge ignores a `host.json` that is a symlink, is owned by another user, or is group- or world-writable. **On Windows** Node cannot inspect ACLs, so protection rests on `%LOCALAPPDATA%` being per-user. The host should still write the file with a user-only ACL.
- Residual risk: if BMO crashed without removing `host.json`, its pid has been reused, and another local program listens on the old port, the bridge would send the key to that program. A host self-authentication step would close this gap (see the interface notes in the implementation report).

## Unverified (check on the laptop)

- Whether Copilot CLI expands `${BMO_DELEGATE_KEY}` in `env`. This is supported per github/copilot-cli#1403, which reported and closed a regression in 0.0.407. If it is not expanded, the bridge reports `no_key`. The fallback is `copilot mcp add bmo --env BMO_DELEGATE_KEY=<key> -- "C:\Program Files\nodejs\node.exe" <path>\lae-mcp.mjs`, but that may store the key in the CLI's config or keychain.
- Whether Copilot CLI's stripped environment (`PATH` only) affects Node networking on Windows. If `bmo_health` says `host_unreachable` under the CLI but works in VS Code, add `"SystemRoot": "C:\\Windows"` to `env`.
- Whether VS Code also loads `~/.copilot/mcp-config.json`. If it does and both files define `bmo`, keep only one, because duplicate tool IDs make VS Code drop tools.
- Which protocol generation each client uses first, whether VS Code sends a `progressToken`, and real client timeouts.
