---
name: sidecar-workers
description: Launch and follow local Devin, Claude, or Grok workers through the shared Sidecar service and task-specific preview. Use when the user requests these workers or authorized delegation. Claude and Grok require an explicit provider request.
---

# Sidecar workers

Use the installed `sidecar` CLI. The service starts automatically; never build a server, choose ports, create config files, or inspect provider internals for an ordinary worker launch. The CLI automatically uses `CODEX_THREAD_ID` (fallback `CODEX_SESSION_ID`) to scope jobs to this Codex task. Outside Codex, provide the real `--thread-id`.

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

The result contains `worker_id`, `thread_id`, `job_dir`, and `preview_url`. Open the URL once with Codex's `open_in_codex` browser tool, or reuse the matching in-app tab. Do not create another tab for each worker. If using computer-use tools, mark the preview as a deliverable. Each Codex task has a separate URL on the same service.

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

`sidecar stop WORKER_ID` cancels only that task's named worker. Reuse an explicit `--request-id UUID` when retrying an uncertain start to avoid duplicate jobs. New sessions do not resume previous provider conversations; provide relevant prior context in follow-up jobs.

If the CLI is missing, report that Sidecar needs installation from the project README. For an installed service problem, use `sidecar doctor`; do not recreate the old per-task preview scripts.
