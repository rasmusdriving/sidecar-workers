# Sidecar Workers

Local background AI workers with one shared preview service for Codex. Launch Devin, Claude, or Grok from a small CLI and review each task's activity in its own browser page.

Sidecar keeps worker execution separate from the Codex app and preview server. Restarting the app or HTTP service does not interrupt detached workers. Results are saved on disk.

## Install on macOS

Requires Python 3.10+, Git, and an authenticated provider CLI. Sidecar does not supply provider accounts or API credits. Keep the checkout in a permanent location, preferably an external workspace if internal disk space is limited.

```sh
git clone https://github.com/rasmusdriving/sidecar-workers.git
cd sidecar-workers
python3 -m sidecar install
```

Add `~/.local/bin` to PATH if needed. Installation creates the `sidecar` command, a Codex skill, and a macOS LaunchAgent. For another data location, use `python3 -m sidecar install --data-dir /path/to/sidecar-data`. No root access is required. Installation copies a small runtime into `~/Library/Application Support/SidecarWorkers/runtime` and keeps launch logs in `~/Library/Logs/SidecarWorkers`, so launchd does not need to create its own logs on a removable volume. Run the install command again after updating the checkout.

Devin is discovered in `/Applications/Devin - Next.app` or `/Applications/Devin.app`. Set `SIDECAR_DEVIN_BIN` before installation for a different location. Claude and Grok use their existing signed-in CLIs on PATH. No credentials are copied into Sidecar.

## Use

```sh
sidecar start --repo /path/to/repo --title "Investigate failing test" \
  --provider devin --model swe-2-high --mode read_only \
  --prompt "Inspect the failing test and explain the cause. Do not edit files."
sidecar status WORKER_ID
sidecar preview
sidecar doctor
```

Codex task identity comes from `CODEX_THREAD_ID` or `CODEX_SESSION_ID`. Outside Codex, pass `--thread-id UUID`. Use `--mode write` for authorized edits, `--file prompt.txt` for a longer prompt, and `--timeout SECONDS` to bound a worker. The CLI prints JSON and returns immediately after dispatch. Open the returned `preview_url` in the Codex browser. Workers within a task share the same page; all pages share one server.

The CLI checks the local service automatically. Warm launches need no manual server setup. Cold starts use a singleton lock, including when several tasks connect at once. Reuse `--request-id UUID` for an uncertain retry to avoid dispatching the same job twice.

### Providers

| Provider | Default model | Write permissions | Inspection permissions |
| --- | --- | --- | --- |
| Devin | SWE-2 High | Smart | Ask |
| Claude | Opus 5, High effort | auto | plan |
| Grok | Latest numbered model, High effort | auto | plan |

Devin model choices are `swe-2-medium`, `swe-2-high`, and `swe-2-max`. Claude choices are `claude-opus-5` and `claude-fable-5-1`, with medium/high/xhigh effort. Availability depends on your installed CLI and account. Sidecar verifies Devin's model and mode; Claude/Grok reported model identity is displayed for review. A provider may reject actions in its native auto mode.

Devin's remaining permission requests appear in status and the preview:

```sh
sidecar permission WORKER_ID REQUEST_ID allow_once
# or: sidecar permission WORKER_ID REQUEST_ID deny
sidecar stop WORKER_ID
```

Inspect the exact action before approving it. The permission queue is currently Devin-only. Permissions are not OS sandboxes. Use separate worktrees for concurrent writes and keep delegation within the user's authorized scope.

## Process lifetime

- launchd checks for the Codex app every 15 seconds. When it is closed and no workers are active, the check exits immediately instead of leaving a resident watcher.
- One service remains available while Codex is open. Browser inactivity does not expire a task's URL. Hidden tabs pause polling.
- Closing Codex starts a 60-second grace period only when no workers are active.
- Reopening Codex or finding active workers cancels pending shutdown. Conditions are checked again immediately before exit.
- Worker supervisors run independently. App and service restarts do not kill them. A restarted service discovers their saved status and process identities.
- Once Codex stays closed, workers have finished, and the grace period expires, the service exits. The next app opening or CLI call starts it again at the saved port and URL.

A machine restart cannot preserve running processes. Afterward, unfinished jobs without a matching live supervisor are marked failed and their files remain. There is no automatic replay of paid work. If the saved port is occupied by another application, Sidecar reports startup failure rather than silently moving old URLs. Data on an external volume requires that volume to be mounted.

The default data directory is `~/Library/Application Support/SidecarWorkers`. It contains a local authorization token, thread job folders, and service logs. The preview binds only to loopback. HTTP mutation routes require CLI authorization and reject browser-origin requests. Do not expose this service through a tunnel. All tasks run as the same local OS user; the token is not a multi-user security boundary.

Worker logs accumulate on disk, not in a permanent in-memory history. No logs are automatically deleted. Existing preview histories can be registered without copying using `sidecar import-history /path/to/threads/UUID`; the original folder must remain available. Old per-task preview servers should be stopped after moving their tabs to the new URL.

## Development

```sh
python3 -m unittest discover -v
```

Tests cover singleton startup, request replay, task isolation, HTTP access checks, the app-close/reopen grace period, service restart, and a detached worker surviving restart. Lifecycle tests simulate app presence; they do not close your actual Codex app. Provider integration also needs an authenticated live smoke test.

Sidecar wraps a vendored, MIT-licensed MCO execution engine with local provider adaptations. See [vendor/mco/ORIGIN.md](vendor/mco/ORIGIN.md) and [vendor/mco/LICENSE](vendor/mco/LICENSE). The shared service and CLI are separate from that engine. No MCP server is required.

## Uninstall

Run `sidecar uninstall` to unload the LaunchAgent and remove the installed command and Codex skill. It refuses while workers are active. Saved histories and the source checkout are retained.

## Status

Early macOS release focused on Codex. Claude and Grok execution are supported, but other host-app lifecycle integrations are not implemented. This is an independent project, not an official OpenAI, Anthropic, xAI, or Cognition product.
