---
name: sidecar-workers
description: Launch and follow local Devin, Claude, or Grok workers through the shared Sidecar service and task-specific preview. Use when the user requests these workers or authorized delegation. Claude and Grok require an explicit provider request.
---

# Sidecar workers

Use the installed `sidecar` CLI. The service starts automatically; never build a server, choose ports, create config files, or inspect provider internals for an ordinary worker launch. The CLI scopes jobs to the current task from the coordinating app's own session variable: `CODEX_THREAD_ID`, then `CODEX_SESSION_ID`, then `CLAUDE_CODE_SESSION_ID`. When none is set, provide the real `--thread-id`.

## Launch

```sh
sidecar start --repo /absolute/repo --title "Inspect the issue" --provider devin --model swe-2-high --mode read_only --prompt "Concrete bounded task"
```

Use `--file /absolute/prompt.txt` for longer prompts. Use `--mode write` when edits are authorized. For parallel writers, first create isolated worktrees and divide ownership. Sidecar does not create worktrees or integrate changes. Respect higher-priority rules about when delegation is allowed.

Devin is the default provider: SWE-2 High for most work, Max for important work and design, Medium only on explicit request. Claude or Grok require an explicit user request for that provider; do not add them automatically for review. Supported choices:

- Devin: `--model swe-2-high`, `swe-2-max`, or `swe-2-medium`; no separate effort flag.
- Claude: `--model claude-opus-5` or `claude-fable-5-1` and `--effort medium|high|xhigh`. Default Opus 5 High.
- Grok: `--model latest --effort high`. The CLI resolves the current model catalog.

Write workers use native Devin Smart / Claude auto / Grok auto. Inspection uses Ask / plan / plan. These are provider permission modes, not OS sandboxes. Preserve user scope and do not enable bypass modes to get around a failure.

### First-use sign-in

`sidecar start` checks the selected provider's CLI and sign-in from the service environment before creating a worker. If it returns `status: needs_auth` and `worker_started: false`, it opens that provider's native login in a visible terminal. Tell the user to complete the provider's browser or terminal sign-in. Do not ask for passwords, tokens, or credential files in chat, and do not send the task prompt to a login command.

Check `sidecar auth status PROVIDER` while waiting. Once it reports `ready`, retry the original `sidecar start` with the same task, options, and returned `request_id` passed as `--request-id`. The login does not itself launch a worker. Repeated attempts reuse an active login instead of opening more windows. If the user cancels, pause the launch rather than repeatedly reopening login.

If opening the terminal fails, show the returned `manual_command` for the user to run in their own terminal. `sidecar auth login PROVIDER` retries the visible login; `--foreground` runs it in the caller's interactive terminal. For a headless session, use `sidecar start --no-login` and guide the user through that manual command on the host machine. Never declare success until a fresh auth status confirms it.

`sidecar doctor` reports `binary`, `status`, and `authenticated` for Devin, Claude, and Grok. `not_installed` means discovery failed; `check_failed` means the check was inconclusive (for example, a network error), not that the user must sign in. Diagnose that failure rather than opening login automatically. Windows Devin discovery includes the per-user and Program Files app bundles; `SIDECAR_DEVIN_BIN` remains the explicit override.

### Connected tools in Devin

Devin loads its own configured MCP servers and authentication. The coordinating app's own plugin and MCP connections are not forwarded by Sidecar. Diagnose with the installed Devin CLI's `mcp list` and `mcp login <server-name>` commands, then verify a fresh Sidecar worker with a minimal read-only call. Never copy credentials into prompts or worker logs.

In the tested Devin ACP version, Ask mode (`--mode read_only`) omits shell execution and the MCP bridge tools. For an authorized task that needs GitHub, Supabase, or another MCP, use `--mode write` (native Smart) and explicitly state the allowed operations in the prompt. For inspection, say to make no file or remote changes and name the read-only calls to perform. Smart is not a read-only sandbox; if the user requires enforced read-only access, use a suitably restricted server or have the parent perform the reads instead. Do not switch to a bypass mode.

Devin skills are separate from MCP connections. Use `devin skills paths` and `devin skills list` to check discovery; install only the relevant skill directories, including their referenced files, into a supported location. Recheck after plugin upgrades and start a fresh worker after authentication or skill changes.

After a successful launch, the result contains `worker_id`, `thread_id`, `job_dir`, and `preview_url`. Open the URL once in the coordinating app's own browser view: `open_in_codex` in Codex, the browser or preview pane in Claude Code. From a terminal-only host, use the operating system's default browser. Reuse that tab; do not create another one for each worker. If using computer-use tools, mark the preview as a deliverable. Each task has a separate URL on the same service.

## Follow through

```sh
sidecar status WORKER_ID
sidecar preview
```

`status` reports compact progress and pending permissions. Add `--full` for all activity. Poll while doing useful parent work. Read the final answer, inspect the actual changes, test appropriately, and integrate before reporting completion. A launch is not completion. Keep results and logs local.

For Devin `needs_attention`, inspect the exact command, repository, and original delegated scope. Approve ordinary actions already authorized by the user without asking again:

```sh
sidecar permission WORKER_ID REQUEST_ID allow_once
```

Use `deny` if it is outside scope, or obtain required authorization. Never approve a queue blindly. Requests expire after five minutes within the overall worker timeout. This coordinating permission queue currently supports Devin only; inspect Claude/Grok blocked results and resolve authorized follow-up work in the parent. Compare each provider's reported model with the requested model.

`sidecar stop WORKER_ID` cancels only that task's named worker and reports `stop_requested: false` when that worker had already finished. Retrying a stop is safe. Reuse an explicit `--request-id UUID` when retrying an uncertain start to avoid duplicate jobs. New starts do not resume previous provider conversations; provide relevant prior context in follow-up jobs. Replies to a pending clarification continue the existing conversation.

If the CLI is missing, report that Sidecar needs installation from the project README. For an installed service problem, use `sidecar doctor`; do not recreate the old per-task preview scripts.


## Clarifications and bounded exploration

New workers may finish a turn with a structured clarification question. Sidecar reports `needs_input` with `questions` containing the question ID and text. Check status while workers are active and answer promptly with context already available to you:

```sh
sidecar reply WORKER_ID QUESTION_ID --answer "Concrete answer and relevant scope"
```

Use `--file` for a longer answer. Never answer an authorization question on the user's behalf without existing authorization. If required information is unavailable, ask the user and keep independent work moving. Polling status is still required: Sidecar cannot wake a finished coordinating turn. Do not leave active workers unattended at turn end.

Replies use Claude/Grok's exact native session ID; Devin continues its open ACP session. They do not start a fresh conversation or change permission mode. Questions and replies survive preview-service restarts. Execution time pauses while awaiting a reply, with a separate `--question-timeout` (default 600 seconds), and a maximum of five questions per worker. Expired or stopped workers cannot receive new replies. Retrying the same answer is safe; changing an already submitted answer is rejected.

Nested delegation is disabled by default (provider tool restriction for Claude/Grok, prompt instruction for Devin). Use `--allow-subagents` only if delegation is authorized and needed. Claude/Grok default to `--max-turns 24` per conversation turn; use `--max-turns N` to adjust it. All providers receive instructions to reserve time for synthesis and verification. These are work limits, not filesystem sandboxes.
