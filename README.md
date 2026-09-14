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

## Install on Windows

Requires Windows 10/11, native Python 3.10+, Git, Windows PowerShell, and an authenticated provider CLI that works in your Windows terminal. Install the provider's Windows prerequisites first. WSL provider installations are separate and are not used by this native Windows service.

From PowerShell:

```powershell
git clone https://github.com/rasmusdriving/sidecar-workers.git
cd sidecar-workers
py -3 -m sidecar install
& "$env:LOCALAPPDATA\SidecarWorkers\bin\sidecar.cmd" doctor
```

Add `%LOCALAPPDATA%\SidecarWorkers\bin` to your user PATH to use `sidecar` from any terminal. For the current PowerShell session, run:

```powershell
$env:Path += ";$env:LOCALAPPDATA\SidecarWorkers\bin"
sidecar doctor
```

Installation copies the runtime to `%LOCALAPPDATA%\SidecarWorkers\runtime`, installs the Codex skill, and creates a per-user `SidecarWorkers-...` scheduled task. The task checks for Codex or ChatGPT every minute while you are signed in. It starts a detached service and exits; CLI commands also start the service immediately. The task uses your current account with limited privileges and stores no password. If your organization blocks Task Scheduler registration, the installer reports the error; you can still run `py -3 -m sidecar` from the checkout.

Windows launches request process breakaway so the service and workers can outlive the host app. If a host blocks breakaway, Sidecar reports the startup failure. Run the installed `SidecarWorkers-...` task from Task Scheduler while Codex is open, then retry the CLI. Windows process job restrictions are described in [Microsoft's job object documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

To keep histories on another drive, install with `py -3 -m sidecar install --data-dir "D:\Sidecar Data"`. The runtime remains in Local App Data so the scheduled check can start reliably. That data drive must be available when Sidecar runs. Rerun installation after updating the checkout or changing provider PATH entries. Finish or stop workers before reinstalling.

Devin is discovered on PATH, or through `$env:SIDECAR_DEVIN_BIN = 'C:\path\to\devin.exe'` set before installation. Claude and Grok use the Windows CLIs on PATH, including npm `.cmd` launchers. Sidecar preserves each provider's existing login.

Commands use the same options on both platforms. Use native Windows repository paths and UTF-8 prompt files for long prompts or text with shell metacharacters:

```powershell
sidecar start --repo 'D:\Work\my-repo' --title 'Investigate failing test' --provider claude --mode read_only --file '.\prompt.txt'
sidecar status WORKER_ID
sidecar stop WORKER_ID
```

Outside Codex, add `--thread-id` with a UUID to task commands. Windows history imports use directory junctions, so Developer Mode and symlink privileges are not required. Use local history folders; junctions cannot target network shares.

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

## Worker questions

A worker can ask its coordinating agent for missing context. Status becomes `needs_input` and includes a question ID and text. Reply from the same Codex task:

```sh
sidecar reply WORKER_ID QUESTION_ID --answer "Use the existing customer timezone."
```

Use `--file answer.txt` for a longer reply, or `--thread-id UUID` outside the original task. Answers are persisted before being delivered, and identical retries are safe. The preview displays questions and answers; mutations stay in the authenticated CLI.

Claude and Grok resume the exact native conversation ID. Devin continues the same open ACP session. Provider permission modes remain unchanged. This is a question/reply channel, not arbitrary messages into a running tool call. The orchestrating agent must check status; Sidecar cannot wake an idle Codex task.

Execution time pauses during clarification. `--question-timeout` defaults to 600 seconds per question, with up to five questions. Service restarts preserve pending questions and workers; machine restarts do not resume paid work automatically. Replies after expiry or stop are rejected.

Nested agents are disabled by default. Claude/Grok enforce this through their provider flags, while Devin receives a prompt instruction. Pass `--allow-subagents` only when needed and authorized. Claude/Grok use `--max-turns 24` per provider turn; this can be adjusted from 1 to 200. Every worker is instructed to reserve at least half its execution budget for synthesis and verification. Provider errors, including cancelled tools and exhausted turn budgets, are preserved in status and saved results.

## Process lifetime

- macOS launchd checks for the Codex app every 15 seconds; Windows Task Scheduler checks every minute while you are signed in. When the app is closed and no workers are active, the check exits immediately instead of leaving a resident watcher.
- One service remains available while Codex is open. Browser inactivity does not expire a task's URL. Hidden tabs pause polling.
- Closing Codex starts a 60-second grace period only when no workers are active.
- Reopening Codex or finding active workers cancels pending shutdown. Conditions are checked again immediately before exit.
- Worker supervisors run independently. App and service restarts do not kill them. A restarted service discovers their saved status and process identities.
- Once Codex stays closed, workers have finished, and the grace period expires, the service exits. The next app opening or CLI call starts it again at the saved port and URL.

A machine restart cannot preserve running processes. Afterward, unfinished jobs without a matching live supervisor are marked failed and their files remain. There is no automatic replay of paid work. If the saved port is occupied by another application, Sidecar reports startup failure rather than silently moving old URLs. Data on an external volume requires that volume to be mounted.

The default data directory is `~/Library/Application Support/SidecarWorkers` on macOS or `%LOCALAPPDATA%\SidecarWorkers` on Windows. It contains a local authorization token, thread job folders, and service logs. Keep custom data directories private to your OS account; Windows uses the directory's inherited access permissions. The preview binds only to loopback. HTTP mutation routes require CLI authorization and reject browser-origin requests. Do not expose this service through a tunnel. All tasks run as the same local OS user; the token is not a multi-user security boundary.

Worker logs accumulate on disk, not in a permanent in-memory history. No logs are automatically deleted. Existing preview histories can be registered without copying using `sidecar import-history /path/to/threads/UUID`; the original folder must remain available. Old per-task preview servers should be stopped after moving their tabs to the new URL.

## Development

```sh
python3 -m unittest discover -v
```

Tests cover singleton startup, request replay, task isolation, HTTP access checks, the app-close/reopen grace period, service restart, and a detached worker surviving restart. Lifecycle tests simulate app presence; they do not close your actual Codex app. Provider integration also needs an authenticated live smoke test.

CI runs the suite on macOS and Windows with Python 3.10 and 3.12. Platform tests cover installation, file locks, Unicode paths, worker cancellation and descendant cleanup. The Windows launcher and junction test runs only on Windows; Task Scheduler registration is mocked in automated tests. Check a real installation with `sidecar doctor`, a provider worker, stop, and restart before relying on it for paid work.

Platform references: [Python file locking](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking), [Python subprocess creation flags](https://docs.python.org/3/library/subprocess.html#windows-constants), and [Microsoft Task Scheduler options](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).

Sidecar wraps a vendored, MIT-licensed MCO execution engine with local provider adaptations. See [vendor/mco/ORIGIN.md](vendor/mco/ORIGIN.md) and [vendor/mco/LICENSE](vendor/mco/LICENSE). The shared service and CLI are separate from that engine. No MCP server is required.

## Uninstall

Run `sidecar uninstall` to remove the macOS LaunchAgent or Windows scheduled task, shut down the service, and remove the installed command and Codex skill. It refuses while workers are active. Saved histories and the source checkout are retained. On Windows, remove the Sidecar bin entry from your user PATH if you added it.

## Status

Early release focused on Codex, with native macOS and Windows support. Windows live provider execution and Task Scheduler registration still need validation on a Windows machine. This is an independent project, not an official OpenAI, Anthropic, xAI, or Cognition product.
