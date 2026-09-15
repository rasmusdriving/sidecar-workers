import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from sidecar.common import ROOT, Lifecycle, atomic, worker_alive
from sidecar.cli import ensure, healthy, endpoint, request
from sidecar.platform import detached_options, spawn_detached, process_command
from sidecar.service import Manager, thread_state


class LifecycleTests(unittest.TestCase):
    def test_existing_port_cannot_be_taken_over(self):
        from http.server import BaseHTTPRequestHandler
        from sidecar.service import LoopbackServer
        with LoopbackServer(('127.0.0.1', 0), BaseHTTPRequestHandler) as first:
            with self.assertRaises(OSError):
                LoopbackServer(first.server_address, BaseHTTPRequestHandler)

    def test_server_startup_never_performs_reverse_dns(self):
        from http.server import BaseHTTPRequestHandler
        from sidecar.service import LoopbackServer
        with patch('socket.getfqdn', side_effect=AssertionError('unexpected DNS')):
            with LoopbackServer(('127.0.0.1', 0), BaseHTTPRequestHandler) as server:
                self.assertEqual(server.server_name, '127.0.0.1')
                self.assertGreater(server.server_port, 0)

    def test_partial_health_response_during_shutdown_is_not_healthy(self):
        from http.client import IncompleteRead
        with patch('sidecar.cli.endpoint', return_value=({'token':'t'},'http://127.0.0.1:1')), patch('sidecar.cli.urlopen', side_effect=IncompleteRead(b'',3)):
            self.assertFalse(healthy(Path('/unused')))

    def test_reopening_cancels_shutdown(self):
        life = Lifecycle(60)
        self.assertFalse(life.should_exit(False, False, 0))
        self.assertFalse(life.should_exit(False, False, 59))
        self.assertFalse(life.should_exit(True, False, 60))
        self.assertFalse(life.should_exit(False, False, 100))
        self.assertTrue(life.should_exit(False, False, 160))

    def test_active_workers_cancel_shutdown(self):
        life = Lifecycle(60)
        life.should_exit(False, False, 0)
        self.assertFalse(life.should_exit(False, True, 1000))
        self.assertFalse(life.should_exit(False, False, 1001))
        self.assertTrue(life.should_exit(False, False, 1061))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = Manager(self.root,'http://127.0.0.1:1234/token')
        self.tid = str(uuid.uuid4())
        self.body = dict(thread_id=self.tid, repo=str(self.root), prompt='test', provider='claude', request_id=str(uuid.uuid4()))

    def test_retried_launch_does_not_duplicate_worker(self):
        with patch('sidecar.service.subprocess.Popen', return_value=Mock(pid=123)) as launch:
            first = self.manager.start(self.body)
            second = self.manager.start(self.body)
        self.assertEqual(first, second)
        launch.assert_called_once()
        for key, value in detached_options().items():
            self.assertEqual(launch.call_args.kwargs[key], value)

    def test_stop_is_requested_without_consulting_the_liveness_probe(self):
        with patch('sidecar.service.subprocess.Popen', return_value=Mock(pid=123)):
            job = Path(self.manager.start(self.body)['job_dir'])
        # The probe shells out to PowerShell on Windows and can transiently
        # answer "gone" for a live worker. Skipping the request on its word
        # reports a cancellation that never reached the paid worker.
        with patch('sidecar.service.worker_alive', return_value=False) as probe:
            result = self.manager.stop(dict(thread_id=self.tid, worker_id=job.name))
        self.assertTrue(result['stop_requested'])
        self.assertTrue((job / 'stop-requested.json').exists())
        probe.assert_not_called()

    def test_finished_worker_reports_that_nothing_was_stopped(self):
        with patch('sidecar.service.subprocess.Popen', return_value=Mock(pid=123)):
            job = Path(self.manager.start(self.body)['job_dir'])
        atomic(job / 'done.json', {'status': 'complete', 'exit_code': 0})
        result = self.manager.stop(dict(thread_id=self.tid, worker_id=job.name))
        self.assertFalse(result['stop_requested'])
        self.assertFalse((job / 'stop-requested.json').exists())

    def test_thread_isolation_and_path_rejection(self):
        with patch('sidecar.service.subprocess.Popen',return_value=Mock(pid=123)):
            result = self.manager.start(self.body)
        other = str(uuid.uuid4())
        self.assertEqual(thread_state(self.root,other)['workers'],[])
        with self.assertRaises(FileNotFoundError):
            self.manager.job(other,result['worker_id'])
        with self.assertRaises(ValueError):
            self.manager.job(self.tid,'../../escape')
        self.assertEqual(len(thread_state(self.root,self.tid)['workers']),1)


class InstallTests(unittest.TestCase):
    def test_reinstall_waits_for_launchd_unload(self):
        from sidecar.cli import install
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            responses = [Mock(returncode=0), Mock(returncode=0), Mock(returncode=0), Mock(returncode=113), Mock(returncode=0)]
            with patch('sidecar.cli.Path.home', return_value=home), patch('sidecar.cli.sys.platform','darwin'), patch('sidecar.cli.os.getuid', return_value=501, create=True), patch('sidecar.cli.subprocess.run', side_effect=responses) as run, patch('sidecar.cli.ensure'), patch('sidecar.cli.time.sleep'):
                result = install(home / 'data')
            self.assertTrue(result['installed'])
            self.assertEqual([call.args[0][1] for call in run.call_args_list], ['bootout','print','print','print','bootstrap'])
            self.assertTrue((home/'.local/bin/sidecar').exists())
            self.assertTrue((home/'.codex/skills/sidecar-workers/SKILL.md').exists())
            self.assertTrue((home/'Library/Application Support/SidecarWorkers/runtime/sidecar/service.py').exists())


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if healthy(self.data):
            cfg,_ = endpoint(self.data)
            request(self.data, 'shutdown', {})
            # Losing HTTP readiness does not mean the process has closed its
            # log yet. Windows refuses to remove files still held by a process.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if 'sidecar.service' not in process_command(cfg['pid']):
                        break
                except subprocess.CalledProcessError:
                    break  # POSIX ps exits nonzero when the process is gone.
                time.sleep(.02)
            else:
                self.fail('test service did not exit after shutdown')

    def launch_service(self, grace=.25):
        starting = self.data / 'starting-service'
        starting.touch()
        # Keep the simulated app open until readiness is observed. A failed
        # connection to the previous port can take longer than the idle grace.
        code = 'from pathlib import Path; from sidecar.service import serve; import sys; serve(Path(sys.argv[1]),app_probe=lambda: any(Path(sys.argv[1],name).exists() for name in ("app-open","starting-service")),grace=float(sys.argv[2]),check_interval=.05)'
        log = self.data / 'test-service.log'
        with log.open('w') as output:
            p = subprocess.Popen([sys.executable,'-c',code,str(self.data),str(grace)],cwd=ROOT, stderr=output)
        self.procs.append(p)
        # A cold interpreter start on a loaded Windows runner needs more than a
        # couple of seconds. Stop early when the child is gone, and say whether
        # it exited and how long it took: an empty stderr on its own cannot
        # distinguish a slow start from a service that returned immediately.
        # Assert on the readiness actually observed. Re-probing here made the
        # check flaky on Windows when lifecycle probes blocked request handling.
        # A separate regression test checks responsiveness during those probes.
        started = time.monotonic()
        deadline = started + 20
        ready = False
        while not ready and time.monotonic() < deadline and p.poll() is None:
            ready = healthy(self.data)
            if not ready:
                time.sleep(.02)
        starting.unlink()
        self.assertTrue(ready, 'service exit={} after {:.1f}s, stderr: {}'.format(
            p.poll(), time.monotonic() - started, log.read_text() or '(empty)'))
        return p

    def test_server_shutdown_and_same_url_restart(self):
        p=self.launch_service()
        first,_=endpoint(self.data)
        p.wait(timeout=3)
        self.assertFalse(healthy(self.data))
        p2=self.launch_service()
        second,_=endpoint(self.data)
        self.assertEqual(first['port'],second['port'])
        self.assertEqual(first['token'],second['token'])
        self.assertNotEqual(first['pid'],second['pid'])

    def test_health_and_stop_respond_while_lifecycle_probe_is_blocked(self):
        tid = str(uuid.uuid4())
        worker_id = 'devin-' + uuid.uuid4().hex
        job = self.data / 'threads' / tid / worker_id
        job.mkdir(parents=True)
        atomic(job / 'job.json', {'thread_id': tid})
        entered = self.data / 'probe-entered'
        release = self.data / 'release-probe'
        code = '''
import sys, time
from pathlib import Path
from sidecar.service import serve
data = Path(sys.argv[1])
def probe():
    (data / 'probe-entered').touch()
    while not (data / 'release-probe').exists():
        time.sleep(.01)
    return True
serve(data, app_probe=probe, check_interval=.05)
'''
        process = subprocess.Popen([sys.executable, '-c', code, str(self.data)], cwd=ROOT)
        self.procs.append(process)
        self.addCleanup(release.touch)
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            healthy(self.data)  # Also triggers the lifecycle check on the old server.
            time.sleep(.02)
        self.assertTrue(entered.exists(), 'lifecycle probe never started')
        self.assertTrue(healthy(self.data), 'health blocked behind lifecycle probe')
        config, host = endpoint(self.data)
        body = json.dumps({'thread_id': tid, 'worker_id': worker_id}).encode()
        with urlopen(Request(host + '/stop', body, {
            'Authorization': 'Bearer ' + config['token'],
        }, method='POST'), timeout=1) as response:
            self.assertTrue(json.load(response)['stop_requested'])
        self.assertTrue((job / 'stop-requested.json').exists())
        release.touch()
        atomic(job / 'done.json', {'status': 'complete'})
        request(self.data, 'shutdown', {})
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_running_app_keeps_service_and_reopen_cancels_exit(self):
        flag=self.data/'app-open';flag.touch()
        p=self.launch_service(grace=.8)
        time.sleep(1)
        self.assertIsNone(p.poll())
        flag.unlink();time.sleep(.2);flag.touch();time.sleep(1)
        self.assertIsNone(p.poll())
        flag.unlink();p.wait(timeout=3)
        self.assertEqual(p.returncode,0, (self.data/'test-service.log').read_text() if (self.data/'test-service.log').exists() else '')

    def test_worker_survives_service_restart(self):
        tid=str(uuid.uuid4());job=self.data/'threads'/tid/('devin-'+uuid.uuid4().hex)
        job.mkdir(parents=True)
        atomic(job/'job.json',dict(thread_id=tid,timeout=10))
        # Exercise the real detached supervisor with a harmless fake execution engine.
        # A release file keeps this deterministic even when Windows process queries are slow.
        release = self.data / 'finish-worker'
        self.addCleanup(release.touch)
        engine = 'import sys,time\nfrom pathlib import Path\nwhile not Path(sys.argv[1]).exists(): time.sleep(.05)\nprint(42)'
        code=f'import sys,os; import sidecar.worker as w; w.command=lambda *a: ([sys.executable,"-c",{engine!r},{str(release)!r}],dict(os.environ)); w.main()'
        worker=spawn_detached([sys.executable,'-c',code,str(job),str(self.data)],cwd=ROOT)
        self.procs.append(worker)
        p=self.launch_service()
        time.sleep(.6)
        self.assertTrue(worker_alive(job))
        self.assertIsNone(p.poll())
        p.terminate();p.wait(timeout=3)
        self.assertIsNone(worker.poll())
        p2=self.launch_service()
        release.touch()
        worker.wait(timeout=10)
        self.assertEqual(json.loads((job/'done.json').read_text())['status'],'complete')
        p2.wait(timeout=10)

    def test_concurrent_ensure_singleton(self):
        env={**os.environ,'SIDECAR_HOME':str(self.data)}
        processes=[subprocess.Popen([sys.executable,'-m','sidecar','doctor','--skip-auth'],cwd=ROOT,env=env,stdout=subprocess.PIPE,text=True) for _ in range(4)]
        values=[]
        for p in processes:
            out,_=p.communicate(timeout=10)
            self.assertEqual(p.returncode,0, (self.data/'test-service.log').read_text() if (self.data/'test-service.log').exists() else '')
            values.append(json.loads(out))
        self.assertEqual(len({v['pid'] for v in values}),1)

    def test_cli_compact_and_filtered_full_status_over_http(self):
        (self.data / 'app-open').touch()
        self.launch_service()
        tid = str(uuid.uuid4())
        wid = 'devin-' + uuid.uuid4().hex
        job = self.data / 'threads' / tid / wid
        job.mkdir(parents=True)
        atomic(job / 'job.json', {'thread_id': tid, 'prompt': 'test prompt'})
        atomic(job / 'done.json', {'status': 'complete'})
        atomic(job / 'stream.jsonl', {'type': 'output_delta', 'delta': 'Finished'})
        other = job.parent / ('devin-' + uuid.uuid4().hex)
        other.mkdir()
        (other / 'job.json').write_text('unreadable unrelated worker')
        for full in (False, True):
            cmd = [sys.executable, '-m', 'sidecar', 'status', wid, '--thread-id', tid]
            if full:
                cmd.append('--full')
            result = subprocess.run(cmd, cwd=ROOT, env={**os.environ, 'SIDECAR_HOME': str(self.data)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(result.stdout)
            self.assertEqual(state['thread_id'], tid)
            self.assertEqual(len(state['workers']), 1)
            worker = state['workers'][0]
            self.assertEqual(worker['status'], 'complete')
            if full:
                self.assertEqual(worker['items'][0]['text'], 'Finished')
            else:
                self.assertEqual(worker['latest_message'], 'Finished')
                self.assertNotIn('items', worker)

    def test_browser_cannot_launch_workers(self):
        (self.data/'app-open').touch();self.launch_service()
        cfg,host=endpoint(self.data)
        for headers in ({},{'Authorization':'Bearer '+cfg['token'],'Origin':'https://example.com'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(host+'/start',b'{}',headers,method='POST'))
            self.assertEqual(error.exception.code,403)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(host+'/wrong/thread/'+str(uuid.uuid4())+'/state')
        self.assertEqual(error.exception.code,404)
        error.exception.close()

    def test_shutdown_requires_authorization_and_no_active_workers(self):
        (self.data/'app-open').touch()
        process = self.launch_service()
        cfg, host = endpoint(self.data)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(host+'/shutdown', b'{}', method='POST'))
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        job = self.data / 'threads' / str(uuid.uuid4()) / ('devin-' + uuid.uuid4().hex)
        job.mkdir(parents=True)
        atomic(job / 'job.json', {})  # Supervisor initialization grace counts as active.
        with self.assertRaisesRegex(ValueError, 'Workers are active'):
            request(self.data, 'shutdown', {})
        atomic(job / 'done.json', {'status': 'complete'})
        self.assertEqual(request(self.data, 'shutdown', {}), {'stopping': True})
        process.wait(timeout=5)

if __name__=='__main__':
    unittest.main()
