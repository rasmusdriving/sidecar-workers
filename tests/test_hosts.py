import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sidecar import platform as host
from sidecar.common import HOST_ENV, SKILL_HOMES, identity, install_skill, remove_skill
from sidecar.engine import command


class TaskIdentityTests(unittest.TestCase):
    def setUp(self):
        cleared = patch.dict(os.environ, {})
        cleared.start()
        self.addCleanup(cleared.stop)
        for name in HOST_ENV:
            os.environ.pop(name, None)

    def test_each_app_supplies_its_own_task_identity(self):
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_CODE_SESSION_ID'):
            tid = str(uuid.uuid4())
            with patch.dict(os.environ, {name: tid}):
                self.assertEqual(identity(), tid)

    def test_explicit_thread_id_outranks_codex_which_outranks_claude(self):
        explicit, thread, session, claude = (str(uuid.uuid4()) for _ in range(4))
        with patch.dict(os.environ, CODEX_THREAD_ID=thread, CODEX_SESSION_ID=session,
                        CLAUDE_CODE_SESSION_ID=claude):
            self.assertEqual(identity(explicit), explicit)
            self.assertEqual(identity(), thread)
        with patch.dict(os.environ, CODEX_SESSION_ID=session, CLAUDE_CODE_SESSION_ID=claude):
            self.assertEqual(identity(), session)

    def test_missing_and_malformed_identities_are_rejected(self):
        with self.assertRaises(ValueError):
            identity()
        # The Claude Code host session ID is not a bare UUID and must not be used.
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID='local_' + str(uuid.uuid4())):
            with self.assertRaises(ValueError):
                identity()


class AppPresenceTests(unittest.TestCase):
    def probe(self, *processes, windows=False):
        with patch('sidecar.platform.windows', return_value=windows), \
             patch('sidecar.platform.subprocess.check_output',
                   return_value=''.join(p + '\n' for p in processes)):
            return host.app_running()

    def test_macos_probe_covers_every_coordinating_app(self):
        for app in ('Codex', 'ChatGPT', 'Claude'):
            self.assertTrue(self.probe(f'/Applications/{app}.app/Contents/MacOS/{app}'))
        self.assertFalse(self.probe(
            '/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper',
            '/Applications/NotClaude.app/Contents/MacOS/Claude Code',
            '/usr/bin/login'))

    def test_windows_probe_uses_exact_executable_names(self):
        for name in ('Codex.exe', 'ChatGPT.exe', 'Claude.exe'):
            self.assertTrue(self.probe(f'"{name}","123","Console"', windows=True))
        self.assertFalse(self.probe('"Claude-helper.exe","1"', '"NotClaude.exe","2"', windows=True))


class SkillInstallTests(unittest.TestCase):
    def test_skill_is_published_to_and_removed_from_every_app(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            source = home / 'SKILL.md'
            source.write_text('---\nname: sidecar-workers\n---\n', encoding='utf-8')
            with patch('sidecar.common.Path.home', return_value=home):
                installed = install_skill(source)
                targets = [home / skills / 'sidecar-workers' / 'SKILL.md' for skills in SKILL_HOMES]
                self.assertEqual(installed, [str(t.parent) for t in targets])
                for target in targets:
                    self.assertEqual(target.read_text(encoding='utf-8'), source.read_text(encoding='utf-8'))
                remove_skill()
                self.assertFalse(any(t.exists() for t in targets))
                remove_skill()  # Removing an already uninstalled skill is not an error.


class WorkerEnvironmentTests(unittest.TestCase):
    def test_worker_does_not_inherit_the_coordinating_session(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            config = dict(provider='claude', repo=root, model='claude-opus-5',
                          mode='read_only', timeout=60)
            session = {name: 'host-value' for name in HOST_ENV}
            with patch.dict(os.environ, {**session, 'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}):
                _, env = command(config, data, data)
            self.assertFalse([name for name in HOST_ENV if name in env])
            self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.anthropic.com')
            self.assertEqual(env['PATH'], os.environ['PATH'])


if __name__ == '__main__':
    unittest.main()
