# Sidecar Workers

Local background AI workers with one shared preview service. Launch Devin, Claude, Grok, or Codex from a small CLI in Codex or Claude Code, and review each task's activity in its own browser page.

Sidecar keeps worker execution separate from the coordinating app and preview server. Restarting the app or HTTP service does not interrupt detached workers. Results are saved on disk.

## Install on macOS

Requires Python 3.10+, Git, and an authenticated provider CLI. Sidecar does not supply provider accounts or API credits. Keep the checkout in a permanent location, preferably an external workspace if internal disk space is limited.

```sh
git clone https://github.com/rasmusdriving/sidecar-workers.git
cd sidecar-workers
python3 -m sidecar install
```

Add `~/.local/bin` to PATH if needed. Installation creates the `sidecar` command, a skill for Codex (`~/.codex/skills`) and Claude Code (`~/.claude/skills`), and a macOS LaunchAgent. For another data location, use `python3 -m sidecar install --data-dir /path/to/sidecar-data`. No root access is required. Installation copies a small runtime into `~/Library/Application Support/SidecarWorkers/runtime` and keeps launch logs in `~/Library/Logs/SidecarWorkers`, so launchd does not need to create its own logs on a removable volume. Run the install command again after updating the checkout.

Devin is discovered on PATH or in `/Applications/Devin - Next.app` or `/Applications/Devin.app`. Set `SIDECAR_DEVIN_BIN` before installation for a different location. Claude, Grok, and Codex use their CLIs on PATH. No credentials are copied into Sidecar.

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

Installation copies the runtime to `%LOCALAPPDATA%\SidecarWorkers\runtime`, installs the skill for Codex (`%USERPROFILE%\.codex\skills`) and Claude Code (`%USERPROFILE%\.claude\skills`), and creates a per-user `SidecarWorkers-...` scheduled task. The task checks for Codex, ChatGPT, or Claude every minute while you are signed in. It starts a detached service and exits; CLI commands also start the service immediately. The task uses your current account with limited privileges and stores no password. If your organization blocks Task Scheduler registration, the installer reports the error; you can still run `py -3 -m sidecar` from the checkout.

Windows launches request process breakaway so the service and workers can outlive the host app. If a managed host blocks breakaway, Sidecar falls back to console detachment and prints a warning: host-wide process cleanup can still stop those workers. For app-independent startup, run the installed `SidecarWorkers-...` task from Task Scheduler while the coordinating app is open before launching work. Windows process job restrictions are described in [Microsoft's job object documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

To keep histories on another drive, install with `py -3 -m sidecar install --data-dir "D:\Sidecar Data"`. The runtime remains in Local App Data so the scheduled check can start reliably. That data drive must be available when Sidecar runs. Rerun installation after updating the checkout or changing provider PATH entries. Finish or stop workers before reinstalling.

Devin is discovered on PATH, or through `$env:SIDECAR_DEVIN_BIN = 'C:\path\to\devin.exe'` set before installation. Claude, Grok, and Codex use the Windows CLIs on PATH, including npm `.cmd` launchers. Sidecar preserves each provider's existing login.

Commands use the same options on both platforms. Use native Windows repository paths and UTF-8 prompt files for long prompts or text with shell metacharacters:

```powershell
sidecar start --repo 'D:\Work\my-repo' --title 'Investigate failing test' --provider claude --mode read_only --file '.\prompt.txt'
sidecar status WORKER_ID
sidecar stop WORKER_ID
```

Outside a recognized app session, add `--thread-id` with a UUID to task commands. Windows history imports use directory junctions, so Developer Mode and symlink privileges are not required. Use local history folders; junctions cannot target network shares.

Devin's agent CLI is discovered inside `Devin` or `Devin - Next` under `%LOCALAPPDATA%\Programs`, `%ProgramFiles%`, or `%ProgramFiles(x86)%`, at `resources\app\extensions\windsurf\devin\bin\devin.exe`. The `devin-desktop` launcher is not the agent CLI. `SIDECAR_DEVIN_BIN` takes priority over PATH and the bundled locations; an invalid explicit override is reported rather than silently ignored.

Worker processes use `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` on Windows for both ACP and shim transports. This hides worker consoles while retaining process-group handling. Interactive sign-in deliberately uses a separate visible console.

## First-use sign-in

Before starting a worker, Sidecar checks the selected provider's sign-in. If the CLI is signed out, Sidecar opens its native login in a visible Windows console or macOS Terminal. Complete the provider's browser or terminal flow, then have your coordinating agent retry the original request. Signing into the desktop app does not necessarily sign into its separate agent CLI.

The initial start returns `status: needs_auth`, `worker_started: false`, and a `request_id`. It creates no worker or prompt log. The agent checks `sidecar auth status devin` (or `claude`/`grok`/`codex`) and retries the original start with that same `--request-id` once the status is `ready`. Login does not automatically execute a saved prompt. Repeated attempts reuse a pending login.

```sh
sidecar doctor
sidecar auth status devin
sidecar auth login devin
```

`doctor` reports discovery and sign-in for all four providers using the service's environment. `ready`, `needs_auth`, `not_installed`, and `check_failed` are distinct states; a connection or configuration error does not automatically trigger login. `doctor --skip-auth` checks discovery only. For a terminal without a desktop, use `sidecar start --no-login` and run `sidecar auth login devin --foreground` yourself on the host machine. The login response includes this fallback if the visible terminal cannot open.

Provider login output stays in that visible terminal, outside worker logs. Sidecar stores only login progress and process metadata; credentials remain managed by the provider CLI. After updating an existing installation, rerun `python3 -m sidecar install` on macOS or `py -3 -m sidecar install` on Windows so the service and both agent skills receive the new flow.

## Coordinating apps

One installation serves Codex and Claude Code, on macOS and Windows. Installation writes the skill to both `~/.codex/skills/sidecar-workers` and `~/.claude/skills/sidecar-workers`, whether or not both apps are present, so the agent discovers `sidecar` wherever you work. In Claude Code this covers the desktop app, its terminal sessions, and the CLI, since they share the same user skill directory.

Each task is scoped by the session identifier its app publishes: `CODEX_THREAD_ID`, then `CODEX_SESSION_ID`, then `CLAUDE_CODE_SESSION_ID`. Anything else, including a plain shell, needs `--thread-id UUID`. Histories from different apps are separate task folders under the same data directory and the same service.

Presence detection covers the Codex, ChatGPT, and Claude desktop apps: `Codex.app`, `ChatGPT.app`, and `Claude.app` on macOS, `codex.exe`, `chatgpt.exe`, and `claude.exe` on Windows. The service stays available while any of them is open.

Workers never inherit the coordinating session. Sidecar removes the app's session and messaging variables from the worker environment, so a `claude` worker launched from Claude Code starts its own provider conversation instead of attaching to yours. Provider credentials, `ANTHROPIC_*` settings, and `PATH` are untouched.

## Use

```sh
sidecar start --repo /path/to/repo --title "Investigate failing test" \
  --provider devin --model swe-2-high --mode read_only \
  --prompt "Inspect the failing test and explain the cause. Do not edit files."
sidecar status WORKER_ID
sidecar preview
sidecar doctor
```

Task identity comes from `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, or `CLAUDE_CODE_SESSION_ID`, in that order. Without one, pass `--thread-id UUID`. Use `--mode write` for authorized edits, `--file prompt.txt` for a longer prompt, and `--timeout SECONDS` to bound a worker. The CLI prints JSON and returns immediately after dispatch. Open the returned `preview_url` in the coordinating app's browser view. Workers within a task share the same page; all pages share one server.

The CLI checks the local service automatically. Warm launches need no manual server setup. Cold starts use a singleton lock, including when several tasks connect at once. Reuse `--request-id UUID` for an uncertain retry to avoid dispatching the same job twice.

### Providers

| Provider | Default model | Write permissions | Inspection permissions |
| --- | --- | --- | --- |
| Devin | SWE-2 High | Smart | Ask |
| Claude | Opus 5, High effort | auto | plan |
| Grok | Latest numbered model, High effort | auto | plan |
| Codex | Astra 6, Medium effort | workspace-write sandbox | read-only sandbox |

Devin model choices are `swe-2-medium`, `swe-2-high`, and `swe-2-max`. Claude choices are `claude-opus-5` and `claude-fable-5-1`, with medium/high/xhigh effort. Availability depends on your installed CLI and account. Sidecar verifies Devin's model and mode; Claude/Grok reported model identity is displayed for review. A provider may reject actions in its native auto mode.

Codex supports `gpt-6-astra` with `low`, `medium`, `high`, or `xhigh` effort and `gpt-5.6-sol` with `medium`, `high`, or `xhigh`. `light` is accepted as an alias for `low` on Astra. Both default to Medium. Install a current native Codex CLI on PATH (validated with 0.153.4); Sidecar checks `codex login status` and opens `codex login` on first use when needed. Availability depends on your Codex account. Workers use their own conversations, and clarification replies resume the exact same session. Codex's CLI may omit the model from its event stream, so the preview labels it as requested unless the provider reports it.

From Claude Code or Codex:

```sh
sidecar start --repo /path/to/repo --title "Inspect the issue" --provider codex --model gpt-6-astra --effort low --mode read_only --prompt "Inspect the issue and explain the cause."
sidecar start --repo /path/to/repo --title "Implement the fix" --provider codex --model gpt-5.6-sol --effort high --mode write --prompt "Implement the agreed fix and test it."
```

Codex uses `--ask-for-approval never` within the selected sandbox. If an operation needs broader access, it fails rather than waiting for terminal approval. Sidecar's permission queue supports Devin only. Nested Codex agents are disabled unless `--allow-subagents` is explicitly set. See the [Codex CLI reference](https://developers.openai.com/codex/cli/reference).

Devin's remaining permission requests appear in status and the preview:

```sh
sidecar permission WORKER_ID REQUEST_ID allow_once
# or: sidecar permission WORKER_ID REQUEST_ID deny
sidecar stop WORKER_ID
```

Inspect the exact action before approving it. The permission queue is currently Devin-only. Permissions are not OS sandboxes. Use separate worktrees for concurrent writes and keep delegation within the user's authorized scope.

## Worker questions

A worker can ask its coordinating agent for missing context. Status becomes `needs_input` and includes a question ID and text. Reply from the same task:

```sh
sidecar reply WORKER_ID QUESTION_ID --answer "Use the existing customer timezone."
```

Use `--file answer.txt` for a longer reply, or `--thread-id UUID` outside the original task. Answers are persisted before being delivered, and identical retries are safe. The preview displays questions and answers; mutations stay in the authenticated CLI.

Claude, Grok, and Codex resume the exact native conversation ID. Devin continues the same open ACP session. Provider permission modes remain unchanged. This is a question/reply channel, not arbitrary messages into a running tool call. The orchestrating agent must check status; Sidecar cannot wake an idle coordinating task.

Execution time pauses during clarification. `--question-timeout` defaults to 600 seconds per question, with up to five questions. Service restarts preserve pending questions and workers; machine restarts do not resume paid work automatically. Replies after expiry or stop are rejected.

Nested agents are disabled by default. Claude/Grok/Codex enforce this through their provider flags, while Devin receives a prompt instruction. Pass `--allow-subagents` only when needed and authorized. Claude/Grok use `--max-turns 24` per provider turn; this can be adjusted from 1 to 200. Every worker is instructed to reserve at least half its execution budget for synthesis and verification. Provider errors, including cancelled tools and exhausted turn budgets, are preserved in status and saved results.

## Process lifetime

- macOS launchd checks for a coordinating app every 15 seconds; Windows Task Scheduler checks every minute while you are signed in. Codex, ChatGPT, and Claude all count. When every one of them is closed and no workers are active, the check exits immediately instead of leaving a resident watcher.
- One service remains available while any coordinating app is open. Browser inactivity does not expire a task's URL. Hidden tabs pause polling.
- Closing the last coordinating app starts a 60-second grace period only when no workers are active.
- Reopening one of them or finding active workers cancels pending shutdown. Conditions are checked again immediately before exit.
- Worker supervisors run independently. App and service restarts do not kill them. A restarted service discovers their saved status and process identities.
- Once they all stay closed, workers have finished, and the grace period expires, the service exits. The next app opening or CLI call starts it again at the saved port and URL.

A machine restart cannot preserve running processes. Afterward, unfinished jobs without a matching live supervisor are marked failed and their files remain. There is no automatic replay of paid work. If the saved port is occupied by another application, Sidecar reports startup failure rather than silently moving old URLs. Data on an external volume requires that volume to be mounted.

The default data directory is `~/Library/Application Support/SidecarWorkers` on macOS or `%LOCALAPPDATA%\SidecarWorkers` on Windows. It contains a local authorization token, thread job folders, and service logs. Keep custom data directories private to your OS account; Windows uses the directory's inherited access permissions. The preview binds only to loopback. HTTP mutation routes require CLI authorization and reject browser-origin requests. Do not expose this service through a tunnel. All tasks run as the same local OS user; the token is not a multi-user security boundary.

Worker logs accumulate on disk, not in a permanent in-memory history. No logs are automatically deleted. Existing preview histories can be registered without copying using `sidecar import-history /path/to/threads/UUID`; the original folder must remain available. Old per-task preview servers should be stopped after moving their tabs to the new URL.

## Development

```sh
python3 -m unittest discover -v
```

Tests cover singleton startup, request replay, task isolation, HTTP access checks, the app-close/reopen grace period, service restart, and a detached worker surviving restart. Lifecycle tests simulate app presence; they do not close your actual coordinating app. Provider integration also needs an authenticated live smoke test.

CI runs the suite on macOS and Windows with Python 3.10 and 3.12. Platform tests cover installation, file locks, Unicode paths, worker cancellation and descendant cleanup. The Windows launcher and junction test runs only on Windows; Task Scheduler registration is mocked in automated tests. Check a real installation with `sidecar doctor`, a provider worker, stop, and restart before relying on it for paid work.

Platform references: [Python file locking](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking), [Python subprocess creation flags](https://docs.python.org/3/library/subprocess.html#windows-constants), and [Microsoft Task Scheduler options](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).

Sidecar wraps a vendored, MIT-licensed MCO execution engine with local provider adaptations. See [vendor/mco/ORIGIN.md](vendor/mco/ORIGIN.md) and [vendor/mco/LICENSE](vendor/mco/LICENSE). The shared service and CLI are separate from that engine. No MCP server is required.

## Uninstall

Run `sidecar uninstall` to remove the macOS LaunchAgent or Windows scheduled task, shut down the service, and remove the installed command and both installed skill copies. It refuses while workers are active. Saved histories and the source checkout are retained. On Windows, remove the Sidecar bin entry from your user PATH if you added it.

## Status

Early release for Codex and Claude Code, with native macOS and Windows support. Windows live provider execution, Task Scheduler registration, and the `claude.exe` presence check still need validation on a Windows machine. Until then, an unrecognized Claude process name on Windows only affects how long the idle service stays up; the CLI and workers are unaffected. This is an independent project, not an official OpenAI, Anthropic, xAI, or Cognition product.
