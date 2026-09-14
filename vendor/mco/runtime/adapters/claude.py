from __future__ import annotations

import os

from typing import List

from ..contracts import CapabilitySet, TaskInput
from .shim import ShimAdapterBase
from ..message_stream import decode_messages


class ClaudeAdapter(ShimAdapterBase):
    def __init__(self) -> None:
        super().__init__(
            provider_id="claude",
            binary_name="claude",
            capability_set=CapabilitySet(
                tiers=["C0", "C1", "C2", "C3", "C4", "C5", "C6"],
                supports_native_async=False,
                supports_poll_endpoint=False,
                supports_resume_after_restart=True,
                supports_schema_enforcement=True,
                min_supported_version="2.1.59",
                tested_os=["macos"],
            ),
        )

    def _auth_check_command(self, binary: str) -> List[str]:
        return [binary, "auth", "status"]

    def supported_permission_keys(self) -> List[str]:
        return ["permission_mode"]

    def supported_model_keys(self) -> List[str]:
        return ["model", "effort"]

    def supported_context_keys(self) -> List[str]:
        return ["context_files"]

    def _build_command(self, input_task: TaskInput) -> List[str]:
        permission_mode = "plan"
        raw_permissions = input_task.metadata.get("provider_permissions")
        if isinstance(raw_permissions, dict):
            value = raw_permissions.get("permission_mode")
            if isinstance(value, str) and value.strip():
                permission_mode = value.strip()
        cmd = [
            "claude",
            "-p",
            "--permission-mode",
            permission_mode,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        # Context policy is opt-in: only apply when provider_context key is present.
        if "provider_context" in input_task.metadata:
            ctx = input_task.metadata.get("provider_context", {})
            if not isinstance(ctx, dict):
                ctx = {}
            if ctx.get("context_files") is not True:
                cmd.extend(["--safe-mode", "--disable-slash-commands"])
        for key in ("model", "effort"):
            value = input_task.metadata.get(key)
            if value:
                if key == "effort" and value not in ("medium", "high", "xhigh"):
                    raise ValueError("unsupported Claude effort: " + str(value))
                cmd.extend(["--" + key, str(value)])
        if os.environ.get('SIDECAR_ALLOW_SUBAGENTS') == '0':
            cmd.extend(['--disallowedTools', 'Agent,Task'])
        if os.environ.get('SIDECAR_MAX_TURNS'):
            cmd.extend(['--max-turns', os.environ['SIDECAR_MAX_TURNS']])
        cmd.append(input_task.prompt)
        return cmd

    def _build_command_for_record(self) -> List[str]:
        return ["claude", "-p", "--permission-mode", "plan", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "<prompt>"]

    def decode_transport(self, raw: str):
        return decode_messages(raw)

    def _is_success(self, return_code: int, stdout_text: str, stderr_text: str) -> bool:
        if return_code != 0 or decode_messages(stdout_text).status == "failed":
            return False
        text = f"{stdout_text}\n{stderr_text}".lower()
        return "api error" not in text
