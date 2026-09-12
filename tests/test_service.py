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
from sidecar.cli import ensure, healthy, endpoint
from sidecar.service import Manager, thread_state


class LifecycleTests(unittest.TestCase):
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
        self.assertTrue(launch.call_args.kwargs['start_new_session'])

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
            with patch('sidecar.cli.Path.home', return_value=home), patch('sidecar.cli.sys.platform','darwin'), patch('sidecar.cli.subprocess.run', side_effect=responses) as run, patch('sidecar.cli.ensure'), patch('sidecar.cli.time.sleep'):
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
            os.kill(cfg['pid'],signal.SIGTERM)
            deadline = time.monotonic()+3
            while healthy(self.data) and time.monotonic()<deadline:
                time.sleep(.02)

    def launch_service(self, grace=.25):
        flag = self.data/'app-open'
        code = 'from pathlib import Path; from sidecar.service import serve; import sys; serve(Path(sys.argv[1]),app_probe=lambda: Path(sys.argv[1],"app-open").exists(),grace=float(sys.argv[2]),check_interval=.05)'
        log = self.data / 'test-service.log'
        with log.open('w') as output:
            p = subprocess.Popen([sys.executable,'-c',code,str(self.data),str(grace)],cwd=ROOT, stderr=output)
        self.procs.append(p)
        deadline=time.monotonic()+3
        while not healthy(self.data) and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertTrue(healthy(self.data), log.read_text())
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
        code='import sys,os; import sidecar.worker as w; w.command=lambda *a: ([sys.executable,"-c","import time; time.sleep(3); print(42)"],dict(os.environ)); w.main()'
        worker=subprocess.Popen([sys.executable,'-c',code,str(job),str(self.data)],cwd=ROOT,start_new_session=True)
        self.procs.append(worker)
        p=self.launch_service()
        time.sleep(.6)
        self.assertTrue(worker_alive(job))
        self.assertIsNone(p.poll())
        p.terminate();p.wait(timeout=3)
        self.assertIsNone(worker.poll())
        p2=self.launch_service()
        worker.wait(timeout=5)
        self.assertEqual(json.loads((job/'done.json').read_text())['status'],'complete')
        p2.wait(timeout=3)

    def test_concurrent_ensure_singleton(self):
        env={**os.environ,'SIDECAR_HOME':str(self.data)}
        processes=[subprocess.Popen([sys.executable,'-m','sidecar','doctor'],cwd=ROOT,env=env,stdout=subprocess.PIPE,text=True) for _ in range(4)]
        values=[]
        for p in processes:
            out,_=p.communicate(timeout=10)
            self.assertEqual(p.returncode,0, (self.data/'test-service.log').read_text() if (self.data/'test-service.log').exists() else '')
            values.append(json.loads(out))
        self.assertEqual(len({v['pid'] for v in values}),1)

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

if __name__=='__main__':
    unittest.main()
