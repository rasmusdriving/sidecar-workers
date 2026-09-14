import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from sidecar import auth, cli, providers
from sidecar.common import ROOT, atomic
from sidecar.cli import healthy, request


class DiscoveryTests(unittest.TestCase):
    def test_windows_bundle_discovery_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            local, machine = root / 'Local å', root / 'Program Files'
            suffix = 'Devin/resources/app/extensions/windsurf/devin/bin/devin.exe'
            per_user = local / 'Programs' / suffix
            per_machine = machine / suffix
            for path in (per_user, per_machine):
                path.parent.mkdir(parents=True)
                path.touch()
            env = {'LOCALAPPDATA': str(local), 'ProgramFiles': str(machine), 'SIDECAR_DEVIN_BIN': ''}
            with patch.dict(os.environ, env), patch('sidecar.providers.windows', return_value=True), \
                 patch('sidecar.providers.shutil.which', return_value=None):
                self.assertEqual(providers.resolve_binary('devin'), str(per_user))
                per_user.unlink()
                self.assertEqual(providers.resolve_binary('devin'), str(per_machine))
                with patch.dict(os.environ, SIDECAR_DEVIN_BIN=str(root / 'missing')):
                    self.assertIsNone(providers.resolve_binary('devin'))
                with patch.dict(os.environ, SIDECAR_DEVIN_BIN=str(per_machine)):
                    self.assertEqual(providers.resolve_binary('devin'), str(per_machine))


class StatusTests(unittest.TestCase):
    def probe(self, provider, stdout='', stderr='', code=0):
        with patch('sidecar.providers.resolve_binary', return_value=sys.executable), \
             patch('sidecar.providers.subprocess.run', return_value=Mock(stdout=stdout, stderr=stderr, returncode=code)) as run:
            result = providers.provider_status(provider)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['timeout'], 8)
            return result

    def test_signed_out_devin_is_detected_even_with_success_exit(self):
        result = self.probe('devin', 'Not logged in.\nCredentials path: private-location')
        self.assertEqual(result['status'], 'needs_auth')
        self.assertFalse(result['authenticated'])
        self.assertNotIn('private-location', json.dumps(result))

    def test_signed_in_devin_and_claude_do_not_expose_account_details(self):
        self.assertEqual(self.probe('devin', 'Logged in as secret@example.test')['status'], 'ready')
        result = self.probe('claude', json.dumps({'loggedIn': True, 'email': 'secret@example.test', 'token': 'secret-token'}))
        self.assertEqual(result['status'], 'ready')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(self.probe('claude', '{"loggedIn":false}')['status'], 'needs_auth')

    def test_network_and_unknown_failures_do_not_trigger_login(self):
        self.assertEqual(self.probe('grok', stderr='Connection timed out', code=1)['status'], 'check_failed')
        self.assertEqual(self.probe('devin', 'Unrecognized output')['status'], 'check_failed')
        self.assertEqual(self.probe('grok', stderr='Please log in', code=1)['status'], 'needs_auth')
        with patch('sidecar.providers.resolve_binary', return_value=sys.executable), \
             patch('sidecar.providers.subprocess.run', side_effect=subprocess.TimeoutExpired('probe', 8)):
            self.assertEqual(providers.provider_status('devin')['status'], 'check_failed')

    def test_missing_and_unchecked_are_distinct_from_authenticated(self):
        with patch('sidecar.providers.resolve_binary', return_value=None):
            self.assertEqual(providers.provider_status('devin')['status'], 'not_installed')
        with patch('sidecar.providers.resolve_binary', return_value=sys.executable), patch('sidecar.providers.subprocess.run') as run:
            self.assertEqual(providers.provider_status('devin', False)['status'], 'unchecked')
            run.assert_not_called()


class LoginTests(unittest.TestCase):
    def test_windows_login_opens_one_visible_console_and_strips_host_session(self):
        with tempfile.TemporaryDirectory() as root, patch('sidecar.auth.windows', return_value=True), \
             patch('sidecar.auth.subprocess.Popen') as launch, patch.dict(os.environ, CLAUDECODE='1'):
            data = Path(root)
            first = auth.open_login(data, 'devin', 'C:/Program Files/Devin/devin.exe')
            second = auth.open_login(data, 'devin', 'C:/Program Files/Devin/devin.exe')
            self.assertEqual(first['status'], 'opened')
            self.assertEqual(second['status'], 'already_open')
            launch.assert_called_once()
            options = launch.call_args.kwargs
            self.assertEqual(options['creationflags'], 0x10)
            self.assertNotIn('CLAUDECODE', options['env'])
            for stream in ('stdout', 'stderr', 'stdin'):
                self.assertNotIn(stream, options)

    def test_macos_login_script_quotes_paths_and_uses_terminal(self):
        with tempfile.TemporaryDirectory(prefix="auth ' å ") as root, \
             patch('sidecar.auth.windows', return_value=False), \
             patch('sidecar.auth.sys', Mock(platform='darwin', executable=sys.executable)), \
             patch('sidecar.auth.subprocess.run') as launch:
            binary = "/Applications/Devin ' test.app/devin"
            result = auth.open_login(Path(root), 'devin', binary)
            self.assertEqual(result['status'], 'opened')
            command = launch.call_args.args[0]
            self.assertEqual(command[:3], ['open', '-a', 'Terminal'])
            script = Path(command[3]).read_text(encoding='utf-8')
            self.assertEqual(shlex.split(script.split('exec ', 1)[1]),
                             [sys.executable, '-m', 'sidecar.auth', 'devin', binary, root])

    def test_failed_terminal_launch_can_be_retried(self):
        with tempfile.TemporaryDirectory() as root, patch('sidecar.auth.windows', return_value=True), \
             patch('sidecar.auth.subprocess.Popen', side_effect=[OSError('no desktop'), Mock()]):
            self.assertEqual(auth.open_login(Path(root), 'devin', 'devin')['status'], 'manual_required')
            self.assertEqual(auth.open_login(Path(root), 'devin', 'devin')['status'], 'opened')

    def test_running_login_is_reused_and_dead_login_is_replaced(self):
        with tempfile.TemporaryDirectory() as root, patch('sidecar.auth.windows', return_value=True), \
             patch('sidecar.auth.subprocess.Popen') as launch:
            data = Path(root)
            folder, state = auth._paths(data, 'devin')
            atomic(state, {'status': 'running', 'pid': 123, 'updated': 0})
            with patch('sidecar.auth.process_command', return_value='python -m sidecar.auth devin'):
                self.assertEqual(auth.open_login(data, 'devin', 'devin')['status'], 'already_open')
                launch.assert_not_called()
            with patch('sidecar.auth.process_command', return_value=''):
                self.assertEqual(auth.open_login(data, 'devin', 'devin')['status'], 'opened')

    def test_native_login_is_rechecked_without_capturing_credentials(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('sidecar.auth.subprocess.run', return_value=Mock(returncode=0)) as run, \
             patch('sidecar.auth.provider_status', return_value={'status': 'ready'}) as checked, \
             contextlib.redirect_stdout(io.StringIO()):
            data = Path(root)
            self.assertEqual(auth.run_login(data, 'devin', sys.executable), 0)
            self.assertNotIn('capture_output', run.call_args.kwargs)
            self.assertNotIn('start_new_session', run.call_args.kwargs)
            checked.assert_called_once_with('devin', binary=sys.executable)
            self.assertEqual(json.loads((data / 'auth/devin.json').read_text())['status'], 'complete')

    def test_cancelled_login_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('sidecar.auth.subprocess.run', side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(io.StringIO()):
            data = Path(root)
            self.assertEqual(auth.run_login(data, 'devin', sys.executable), 1)
            self.assertEqual(json.loads((data / 'auth/devin.json').read_text())['status'], 'failed')


class StartPreflightTests(unittest.TestCase):
    def invoke(self, status, extra=()):
        tid, rid = str(uuid.uuid4()), str(uuid.uuid4())
        args = ['sidecar', 'start', '--repo', str(ROOT), '--title', 'test', '--provider', 'devin',
                '--thread-id', tid, '--request-id', rid, '--prompt', 'private prompt', *extra]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), patch('sidecar.cli.request') as request, \
             patch('sidecar.auth.open_login', return_value={'status': 'opened'}) as login, \
             contextlib.redirect_stdout(output):
            request.side_effect = [dict(provider='devin', binary='devin.exe', status=status), {'worker_id': 'devin-' + uuid.UUID(rid).hex}]
            cli.main()
            return json.loads(output.getvalue()), request, login, rid

    def test_first_attempt_opens_login_without_starting_or_logging_a_worker(self):
        result, request, login, rid = self.invoke('needs_auth')
        self.assertFalse(result['worker_started'])
        self.assertEqual(result['request_id'], rid)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], 'provider-status')
        login.assert_called_once()
        self.assertNotIn('private prompt', json.dumps(result))

    def test_ready_worker_starts_with_original_request_id(self):
        result, request, login, rid = self.invoke('ready')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[1], 'start')
        self.assertEqual(request.call_args.args[2]['request_id'], rid)
        login.assert_not_called()

    def test_unavailable_or_unknown_and_no_login_do_not_open_browser(self):
        for status, extra in [('check_failed', ()), ('not_installed', ()), ('needs_auth', ('--no-login',))]:
            with self.subTest(status=status, extra=extra):
                _, request, login, _ = self.invoke(status, extra)
                self.assertEqual(request.call_count, 1)
                login.assert_not_called()


class NativeAuthFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sidecar-auth-')
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        source = self.data / 'fake_provider.py'
        source.write_text('''
import sys
from pathlib import Path
marker = Path(__file__).with_name('signed-in')
if sys.argv[1:] == ['auth', 'login']:
    marker.touch()
    print('Test provider login complete')
elif sys.argv[1:] == ['auth', 'status']:
    print('Logged in' if marker.exists() else 'Not logged in.')
else:
    raise SystemExit('Unexpected provider command')
''', encoding='utf-8')
        if sys.platform == 'win32':
            self.binary = self.data / 'devin.cmd'
            self.binary.write_text('@echo off\n' + subprocess.list2cmdline([sys.executable, str(source)]) + ' %*\n', encoding='utf-8')
        else:
            self.binary = self.data / 'devin'
            self.binary.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(source)]) + ' "$@"\n', encoding='utf-8')
            self.binary.chmod(0o700)
        self.env = {**os.environ, 'SIDECAR_HOME': str(self.data), 'SIDECAR_DEVIN_BIN': str(self.binary)}

    def call(self, *args):
        p = subprocess.run([sys.executable, '-m', 'sidecar', *args], cwd=ROOT,
                           env=self.env, capture_output=True, encoding='utf-8', timeout=25)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_signed_out_login_and_retry_start_exactly_one_worker(self):
        # Exercise the actual CLI, authenticated HTTP route, login subprocess,
        # and detached worker. Replace only paid inference with a harmless print.
        code = '''
import sys
from pathlib import Path
import sidecar.service as service
from sidecar.platform import spawn_detached
worker_code = 'import sys,os; import sidecar.worker as w; w.command=lambda *a: ([sys.executable,"-c","print(42)"],dict(os.environ)); w.main()'
def spawn(command, **options):
    return spawn_detached([sys.executable, '-c', worker_code, *command[-2:]], **options)
service.spawn_detached = spawn
service.serve(Path(sys.argv[1]), app_probe=lambda: True, check_interval=.05)
'''
        log = (self.data / 'service-test.log').open('w')
        service = subprocess.Popen([sys.executable, '-c', code, str(self.data)],
                                   cwd=ROOT, env=self.env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not healthy(self.data) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(healthy(self.data))
            tid, rid = str(uuid.uuid4()), str(uuid.uuid4())
            args = ('start', '--repo', str(ROOT), '--title', 'Login test', '--provider', 'devin',
                    '--prompt', 'Do not send this prompt to the login process.', '--thread-id', tid,
                    '--request-id', rid, '--no-login')
            first = json.loads(self.call(*args))
            self.assertEqual(first['status'], 'needs_auth')
            self.assertFalse(first['worker_started'])
            self.assertFalse(list((self.data / 'threads').glob('*/devin-*')))
            self.call('auth', 'login', 'devin', '--foreground')
            self.assertEqual(json.loads(self.call('auth', 'status', 'devin'))['status'], 'ready')
            worker = json.loads(self.call(*args))
            duplicate = json.loads(self.call(*args))
            self.assertEqual(worker['worker_id'], duplicate['worker_id'])
            done = Path(worker['job_dir']) / 'done.json'
            deadline = time.monotonic() + 15
            while not done.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(json.loads(done.read_text())['status'], 'complete')
            self.assertEqual(len(list((self.data / 'threads').glob('*/devin-*'))), 1)
            doctor = json.loads(self.call('doctor'))
            self.assertEqual(doctor['providers']['devin']['binary'], str(self.binary))
            self.assertEqual(doctor['providers']['devin']['status'], 'ready')
            request(self.data, 'shutdown', {})
            service.wait(timeout=15)
        finally:
            if service.poll() is None:
                service.terminate()
                try:
                    service.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)
            log.close()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows visible login console')
    def test_windows_native_login_console_completes_without_worker_logs(self):
        with patch.dict(os.environ, self.env):
            result = auth.open_login(self.data, 'devin', str(self.binary))
        self.assertEqual(result['status'], 'opened')
        state_path = self.data / 'auth/devin.json'
        deadline = time.monotonic() + 20
        state = {}
        while time.monotonic() < deadline:
            state = json.loads(state_path.read_text())
            if state['status'] in ('complete', 'failed'):
                break
            time.sleep(.05)
        self.assertEqual(state['status'], 'complete')
        self.assertTrue((self.data / 'signed-in').exists())
        self.assertFalse(list((self.data / 'auth').glob('*.log')))


if __name__ == '__main__':
    unittest.main()
