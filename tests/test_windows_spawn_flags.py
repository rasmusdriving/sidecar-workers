import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from sidecar.engine import setup
setup()
from runtime import platform
from runtime.models import _run_model_probe
from runtime.acp.transport import JsonRpcTransport
from runtime.adapters.claude import ClaudeAdapter
from runtime.adapters.codex import CodexAdapter
from runtime.adapters.grok import GrokAdapter
from runtime.contracts import TaskInput
from sidecar import auth, providers


class WindowsSpawnFlagsTests(unittest.TestCase):
    def assert_hidden_group(self, options):
        flags = options['creationflags']
        self.assertTrue(flags & 0x08000000, 'missing CREATE_NO_WINDOW')
        self.assertTrue(flags & 0x00000200, 'missing CREATE_NEW_PROCESS_GROUP')
        self.assertFalse(flags & 0x00000008, 'DETACHED_PROCESS conflicts with CREATE_NO_WINDOW')
        self.assertFalse(flags & 0x00000010, 'worker must not create a visible console')

    def windows(self):
        # Mock only this module's view of the OS, preserving native Path/locks.
        return patch.object(platform, 'os', types.SimpleNamespace(name='nt', environ=os.environ))

    def test_executable_and_batch_workers_hide_console_and_retain_group(self):
        with self.windows(), patch.object(platform, 'resolve_spawn_arg', side_effect=lambda cmd: cmd):
            for binary in ['devin.exe', 'claude.cmd', 'grok.exe', 'codex.cmd']:
                with self.subTest(binary=binary):
                    _, options = platform.prepare_spawn([binary, 'test'])
                    self.assert_hidden_group(options)

    def test_acp_transport_passes_both_flags_to_popen(self):
        with self.windows(), patch.object(platform, 'resolve_spawn_arg', side_effect=lambda cmd: cmd), \
             patch('runtime.acp.transport.subprocess.Popen') as spawn, \
             patch('runtime.acp.transport.threading.Thread'):
            transport = JsonRpcTransport()
            transport.start(['devin.exe', 'acp'])
            self.assert_hidden_group(spawn.call_args.kwargs)

    def test_all_shim_providers_pass_both_flags_to_popen(self):
        with tempfile.TemporaryDirectory() as root, self.windows(), \
             patch.object(platform, 'resolve_spawn_arg', side_effect=lambda cmd: cmd), \
             patch('runtime.adapters.shim.subprocess.Popen', return_value=Mock(pid=123)) as spawn:
            for cls in (ClaudeAdapter, GrokAdapter, CodexAdapter):
                adapter = cls()
                task = TaskInput(task_id=adapter.id, prompt='test', repo_root=root, target_paths=[],
                                 metadata={'artifact_root': root})
                ref = adapter.run(task)
                try:
                    self.assert_hidden_group(spawn.call_args.kwargs)
                finally:
                    adapter._close_io(adapter._runs.pop(ref.run_id))

    def test_probe_merges_flag_bits_and_keeps_other_options(self):
        with patch('sidecar.providers.resolve_binary', return_value='devin.exe'), \
             patch('sidecar.providers.prepare_command', return_value=(['devin.exe'], {'creationflags': 0x200, 'env': {}})), \
             patch('sidecar.providers.hidden_options', return_value={'creationflags': 0x08000000}), \
             patch('sidecar.providers.subprocess.run', return_value=Mock(returncode=0, stdout='Logged in', stderr='')) as run:
            self.assertEqual(providers.provider_status('devin')['status'], 'ready')
            self.assert_hidden_group(run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs['env'], {})

    def test_runtime_probes_and_stop_command_do_not_flash_consoles(self):
        with self.windows(), patch.object(platform, 'resolve_spawn_arg', side_effect=lambda cmd: cmd), \
             patch('subprocess.run', return_value=Mock(returncode=0, stdout='ok', stderr='')) as run:
            _run_model_probe('codex.exe', ['debug', 'models'])
            self.assert_hidden_group(run.call_args.kwargs)
            CodexAdapter()._probe_version('codex.exe')
            self.assert_hidden_group(run.call_args.kwargs)
            CodexAdapter()._probe_auth('codex.exe')
            self.assert_hidden_group(run.call_args.kwargs)
            platform._taskkill(Mock(pid=123), force=True)
            self.assertTrue(run.call_args.kwargs['creationflags'] & 0x08000000)

    def test_interactive_login_keeps_a_visible_console(self):
        with tempfile.TemporaryDirectory() as root, patch('sidecar.auth.windows', return_value=True), \
             patch('sidecar.auth.subprocess.Popen') as spawn:
            auth.open_login(Path(root), 'codex', 'codex.exe')
            flags = spawn.call_args.kwargs['creationflags']
            self.assertEqual(flags, 0x10)
            self.assertFalse(flags & 0x08000000)


if __name__ == '__main__':
    unittest.main()
