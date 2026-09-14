import errno
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest.mock import Mock, patch

from sidecar import platform as host
from sidecar.common import ROOT, atomic, data_dir, worker_alive
from sidecar.engine import command
from sidecar.service import Manager
from sidecar import windows_install as installer


class PlatformTests(unittest.TestCase):
    def test_atomic_state_retries_windows_reader_contention(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'state.json'
            original = Path.replace
            attempts = []
            def replace(source, target):
                attempts.append(source)
                if len(attempts) < 3:
                    raise PermissionError('reader still has the file open')
                return original(source, target)
            with patch('pathlib.Path.replace', replace), patch('sidecar.common.time.sleep'):
                atomic(path, {'text': 'å 日本語'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'text': 'å 日本語'})
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_windows_paths_and_override(self):
        with tempfile.TemporaryDirectory() as root, patch('sidecar.platform.windows', return_value=True):
            with patch.dict(os.environ, LOCALAPPDATA=root, SIDECAR_HOME=str(Path(root) / 'custom')):
                self.assertEqual(host.install_home(), Path(root) / 'SidecarWorkers')
                self.assertEqual(data_dir(), (Path(root) / 'custom').resolve())
            with patch.dict(os.environ, LOCALAPPDATA=root):
                os.environ.pop('SIDECAR_HOME', None)
                self.assertEqual(data_dir(), (Path(root) / 'SidecarWorkers').resolve())

    def test_windows_detachment_has_no_unix_options(self):
        with patch('sidecar.platform.windows', return_value=True):
            self.assertEqual(host.detached_options(), {'creationflags': 0x01000208})

    def test_restricted_windows_job_does_not_silently_inherit_host_lifetime(self):
        with patch('sidecar.platform.windows', return_value=True), patch('sidecar.platform.subprocess.Popen', side_effect=PermissionError('Access denied')) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'host may forbid process breakaway'):
                host.spawn_detached(['python', '-m', 'sidecar.service'])
            spawn.assert_called_once()

    def test_windows_lock_contention_and_retry(self):
        crt = types.SimpleNamespace(LK_NBLCK=2, locking=Mock())
        with tempfile.TemporaryDirectory() as root, patch('sidecar.platform.windows', return_value=True), patch.dict(sys.modules, msvcrt=crt):
            path = Path(root) / 'lock'
            crt.locking.side_effect = OSError(errno.EACCES, 'locked')
            with self.assertRaises(BlockingIOError):
                host.acquire_lock(path, blocking=False)
            crt.locking.side_effect = [OSError(errno.EACCES, 'locked'), None]
            with patch('sidecar.platform.time.sleep'), host.acquire_lock(path):
                pass
            crt.locking.side_effect = OSError(errno.EBADF, 'bad descriptor')
            with self.assertRaises(OSError) as error:
                host.acquire_lock(path)
            self.assertEqual(error.exception.errno, errno.EBADF)

    def test_windows_app_probe_uses_exact_executable_names(self):
        with patch('sidecar.platform.windows', return_value=True), patch('sidecar.platform.subprocess.check_output') as output:
            output.return_value = '"Codex-helper.exe","1"\n"NotCodex.exe","2"'
            self.assertFalse(host.app_running())
            for name in ('Codex.exe', 'ChatGPT.exe'):
                output.return_value = f'"{name}","123","Console"\n'
                self.assertTrue(host.app_running())

    def test_worker_identity_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as root:
            job = Path(root) / 'worker å'
            job.mkdir()
            (job / 'supervisor.pid').write_text('123')
            with patch('sidecar.common.process_command', return_value='python -m sidecar.worker somewhere-else'):
                self.assertFalse(worker_alive(job))
            with patch('sidecar.platform.windows', return_value=True), patch('sidecar.platform.subprocess.check_output', return_value=json.dumps(f'python -m sidecar.worker "{job}"')):
                self.assertTrue(worker_alive(job))

    def test_windows_stop_targets_engine_and_descendants(self):
        child = Mock(pid=123)
        with patch('sidecar.platform.windows', return_value=True), patch('sidecar.platform.subprocess.run', return_value=Mock(returncode=0)) as run:
            host.stop_child(child)
        self.assertEqual(run.call_args.args[0], ['taskkill.exe', '/PID', '123', '/T', '/F'])
        child.wait.assert_called_once()

    def test_devin_on_path_and_unicode_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            binary = data / 'Devin å' / 'devin.exe'
            binary.parent.mkdir()
            binary.touch()
            config = dict(provider='devin', repo=root, model='swe-2-high', mode='read_only', timeout=10)
            with patch.dict(os.environ, SIDECAR_DEVIN_BIN=''), patch('sidecar.engine.shutil.which', return_value=str(binary)):
                _, env = command(config, data, data)
            line = (data / 'engine-config/agents.yaml').read_text(encoding='utf-8').split('    command: ')[1]
            self.assertEqual(shlex.split(json.loads(line)), [str(binary), 'acp'])
            self.assertEqual(env['PYTHONIOENCODING'], 'utf-8')


class WindowsInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'Local App Data' / 'SidecarWorkers'
        for target, value in [('sidecar.windows_install.install_home', self.base),
                              ('sidecar.windows_install.Path.home', self.home)]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_install_reinstall_uninstall_preserves_history(self):
        data = self.home / 'external data'
        with patch('sidecar.windows_install.task_command') as scheduler, patch('sidecar.windows_install.stop_service') as stop, patch('sidecar.cli.ensure') as ensure:
            result = installer.install(data)
            ensure.assert_called_once_with(data, runtime=self.base / 'runtime')
            self.assertTrue(Path(result['cli']).exists())
            self.assertTrue((self.base / 'runtime/sidecar/service.py').exists())
            self.assertTrue((self.home / '.codex/skills/sidecar-workers/SKILL.md').exists())
            call = scheduler.call_args.args
            self.assertIn('/IT', call)
            self.assertIn('LIMITED', call)
            self.assertNotIn('/RP', call)
            self.assertIn('--supervise', call[call.index('/TR') + 1])
            compile((self.base / 'launcher.py').read_text(encoding='utf-8'), 'launcher.py', 'exec')
            history = data / 'keep.txt'
            history.write_text('saved history')
            installer.install(data)
            self.assertTrue(any(c.args[0] == '/Delete' for c in scheduler.call_args_list))
            installer.uninstall(data)
            self.assertEqual(history.read_text(), 'saved history')
            self.assertFalse(Path(result['cli']).exists())
            stop.assert_called_with(data)

    def test_install_and_uninstall_refuse_active_workers(self):
        with patch('sidecar.windows_install.active_workers', return_value=True), patch('sidecar.windows_install.task_command') as scheduler:
            for action in (installer.install, installer.uninstall):
                with self.assertRaisesRegex(ValueError, 'Workers are active'):
                    action(self.home / 'data')
            scheduler.assert_not_called()

    def test_installed_launcher_preserves_callers_working_directory(self):
        data = self.home / 'data'
        source = self.home / str(uuid.uuid4())
        source.mkdir()
        with patch('sidecar.windows_install.task_command'), patch('sidecar.windows_install.stop_service'), patch('sidecar.cli.ensure'):
            installer.install(data)
        run = subprocess.run([sys.executable, '-X', 'utf8', str(self.base / 'launcher.py'),
                              'import-history', source.name], cwd=self.home,
                             capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['imported'], str(source.resolve()))
        link = data / 'threads' / source.name
        if sys.platform == 'win32':
            link.rmdir()
        else:
            link.unlink()

    def test_scheduler_failure_is_actionable(self):
        with patch('sidecar.windows_install.subprocess.run', return_value=Mock(returncode=1, stderr='Access is denied.', stdout='')):
            with self.assertRaisesRegex(RuntimeError, 'Task Scheduler: Access is denied'):
                installer.task_command('/Create')

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows launcher check')
    def test_native_launcher_and_history_junction(self):
        from runtime.platform import prepare_spawn
        data = self.home / 'data å'
        with patch('sidecar.windows_install.task_command'), patch('sidecar.windows_install.stop_service'), patch('sidecar.cli.ensure'):
            result = installer.install(data)
        args, options = prepare_spawn([result['cli'], '--help'])
        run = subprocess.run(args, **options, capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Sidecar Workers', run.stdout)
        source = self.home / "history & å's"
        source.mkdir()
        (source / 'keep.txt').write_text('history')
        target = self.home / 'linked history'
        host.link_history(target, source)
        self.assertEqual(target.resolve(), source.resolve())
        # Remove only the junction, leaving the source intact.
        target.rmdir()
        self.assertTrue((source / 'keep.txt').exists())


class PortableProcessTests(unittest.TestCase):
    def test_native_lock_is_exclusive_and_released_after_exit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'lock'
            script = ('from pathlib import Path; import sys; from sidecar.platform import acquire_lock; '
                      'lock=acquire_lock(Path(sys.argv[1]), blocking=False)')
            with host.acquire_lock(path):
                run = subprocess.run([sys.executable, '-c', script, str(path)], cwd=ROOT, capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn(b'BlockingIOError', run.stderr)
            run = subprocess.run([sys.executable, '-c', script, str(path)], cwd=ROOT, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_stop_writes_final_state_and_stops_descendants(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            tid = str(uuid.uuid4())
            job = data / 'threads' / tid / ('devin-' + uuid.uuid4().hex)
            job.mkdir(parents=True)
            atomic(job / 'job.json', dict(thread_id=tid, timeout=60))
            heartbeat = data / 'heartbeat'
            grandchild = data / 'grandchild.py'
            grandchild.write_text('import sys,time\nfrom pathlib import Path\n'
                                 'while True:\n Path(sys.argv[1]).write_text(str(time.time()))\n time.sleep(.05)\n')
            engine = data / 'fake_engine.py'
            engine.write_text('import subprocess,sys,time\n'
                              'subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n'
                              'time.sleep(60)\n')
            script = (f'import sys,os; import sidecar.worker as w; '
                      f'w.command=lambda *a: ([sys.executable,{str(engine)!r},{str(grandchild)!r},{str(heartbeat)!r}],dict(os.environ)); w.main()')
            worker = subprocess.Popen([sys.executable, '-c', script, str(job), str(data)],
                                      cwd=ROOT, **host.detached_options())
            try:
                deadline = time.monotonic() + 15
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(heartbeat.exists())
                manager = Manager(data, 'http://127.0.0.1:1/token')
                result = manager.stop(dict(thread_id=tid, worker_id=job.name))
                self.assertTrue(result['stop_requested'])
                worker.wait(timeout=15)
                done = json.loads((job / 'done.json').read_text())
                self.assertEqual(done['status'], 'failed')
                self.assertEqual(done['error'], 'Worker cancelled')
                previous = heartbeat.read_text()
                time.sleep(.25)
                self.assertEqual(heartbeat.read_text(), previous)
            finally:
                if worker.poll() is None:
                    atomic(job / 'stop-requested.json', {'created': time.time()})
                    worker.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
