"""One loopback server for all Codex tasks, with independent worker supervisors."""
import fcntl
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .common import ROOT, Lifecycle, active_workers, app_running, atomic, data_dir, identity, thread_dir, worker_alive
from .activity import snapshot
from .models import resolve_model


def prepare(data):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    (data / 'threads').mkdir(exist_ok=True)
    path = data / 'service.json'
    if path.exists():
        return json.loads(path.read_text())
    value = {'token': secrets.token_urlsafe(32), 'port': 0}
    atomic(path, value)
    path.chmod(0o600)
    return value


def thread_state(data, tid):
    workers = []
    for p in sorted(thread_dir(data, tid).glob('devin-*/job.json')):
        if json.loads(p.read_text()).get('thread_id') != tid:
            continue
        state = snapshot(p.parent)
        state['id'] = p.parent.name
        workers.append(state)
    return {'thread_id': tid, 'workers': sorted(workers, key=lambda w: w.get('started', 0))}


class Manager:
    def __init__(self, data, base_url):
        self.data, self.base_url = data, base_url
        self.children = []
        self.lock = threading.Lock()

    def preview(self, tid):
        return self.base_url + '/thread/' + identity(tid) + '/'

    def reap(self):
        self.children[:] = [p for p in self.children if p.poll() is None]

    def start(self, body):
        tid = identity(body['thread_id'])
        provider = body.get('provider', 'devin')
        if provider not in ('devin', 'claude', 'grok'):
            raise ValueError('Unknown provider')
        mode = body.get('mode', 'read_only')
        if mode not in ('read_only', 'write'):
            raise ValueError('Mode must be read_only or write')
        model, effort = resolve_model(provider, body.get('model'), body.get('effort'))
        repo = str(Path(body['repo']).expanduser().resolve(strict=True))
        if not Path(repo).is_dir():
            raise ValueError('Repository must be a directory')
        timeout = int(body.get('timeout', 600))
        if not 10 <= timeout <= 86400:
            raise ValueError('Timeout must be between 10 and 86400 seconds')
        if not isinstance(body.get('prompt'), str) or not body['prompt'].strip():
            raise ValueError('Prompt is required')
        # Caller supplies a stable request ID so retries do not duplicate paid work.
        request_id = uuid.UUID(body['request_id']).hex
        with self.lock:
            folder = thread_dir(self.data, tid)
            folder.mkdir(parents=True, exist_ok=True)
            job = folder / ('devin-' + request_id)
            if not job.exists():
                job.mkdir()
                config = dict(thread_id=tid, title=body.get('title', 'Worker'), repo=repo, provider=provider, model=model, effort=effort, mode=mode, timeout=timeout, started=time.time(), prompt=body['prompt'])
                atomic(job / 'job.json', config)
                (job / 'prompt.txt').write_text(config['prompt'])
                try:
                    with (job / 'worker.log').open('a') as log:
                        proc = subprocess.Popen([sys.executable, '-m', 'sidecar.worker', str(job), str(self.data)], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                    (job / 'supervisor.pid').write_text(str(proc.pid))
                    self.children.append(proc)
                except Exception as exc:
                    atomic(job / 'done.json', {'status':'failed','exit_code':1,'finished':time.time(),'error':str(exc)})
                    raise
            return {'worker_id': job.name, 'thread_id': tid, 'job_dir': str(job), 'preview_url': self.preview(tid)}

    def job(self, tid, wid):
        if not re.fullmatch(r'devin-[a-f0-9]+', wid):
            raise ValueError('Invalid worker ID')
        job = thread_dir(self.data, tid) / wid
        config = json.loads((job / 'job.json').read_text())
        if config.get('thread_id') != tid:
            raise ValueError('Worker belongs to another task')
        return job

    def permission(self, body):
        job = self.job(identity(body['thread_id']), body['worker_id'])
        rid = uuid.UUID(body['request_id']).hex
        path = job / 'permissions' / (rid + '.request.json')
        request = json.loads(path.read_text())
        if request['status'] != 'pending' or (job / 'done.json').exists():
            raise ValueError('Permission is no longer pending')
        decision = body['decision']
        if decision not in ('allow_once', 'deny'):
            raise ValueError('Decision must be allow_once or deny')
        option = next(o['optionId'] for o in request['request']['options'] if o['kind'] == 'allow_once') if decision == 'allow_once' else None
        atomic(path.with_name(rid + '.decision.json'), {'optionId': option})
        return {'id': rid, 'decision': decision}

    def stop(self, body):
        job = self.job(identity(body['thread_id']), body['worker_id'])
        if not (job / 'done.json').exists() and worker_alive(job):
            pid = int((job / 'supervisor.pid').read_text())
            if os.getpgid(pid) != pid:
                raise ValueError('Worker process group did not match; refusing to stop it')
            os.killpg(pid, signal.SIGTERM)
        return {'worker_id': job.name, 'stop_requested': True}


def serve(data=None, *, app_probe=app_running, grace=60, check_interval=2):
    data = data or data_dir()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (data / 'service.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    config = prepare(data)
    stopping = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data, mime='application/json'):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {'error': 'Invalid host'})
            prefix = '/' + config['token']
            if self.path == prefix + '/health':
                return self.reply(200, {'service':'sidecar-workers','pid':os.getpid()})
            match = re.fullmatch(re.escape(prefix) + r'/thread/([0-9a-f-]{36})/(state)?', self.path)
            if not match:
                return self.reply(404, {'error':'Not found'})
            try:
                tid = identity(match[1])
                if match[2]:
                    return self.reply(200, thread_state(data, tid))
                self.reply(200, Path(__file__).with_name('preview.html').read_bytes(), 'text/html; charset=utf-8')
            except (ValueError, OSError) as exc:
                self.reply(400, {'error':str(exc)})

        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin') or not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer ' + config['token']):
                return self.reply(403, {'error':'CLI authorization required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('Invalid request size')
                self.connection.settimeout(10)
                body = json.loads(self.rfile.read(size))
                if self.path == '/start':
                    result = manager.start(body)
                elif self.path == '/permission':
                    result = manager.permission(body)
                elif self.path == '/stop':
                    result = manager.stop(body)
                else:
                    return self.reply(404, {'error':'Not found'})
                self.reply(200, result)
            except (KeyError, ValueError, OSError, StopIteration, subprocess.SubprocessError) as exc:
                self.reply(400, {'error':str(exc)})

    server = ThreadingHTTPServer(('127.0.0.1', config['port']), Handler)
    server.daemon_threads = True
    server.timeout = .5
    config.update(port=server.server_port, pid=os.getpid())
    manager = Manager(data, f'http://127.0.0.1:{server.server_port}/' + config['token'])
    atomic(data / 'service.json', config)
    (data / 'service.json').chmod(0o600)
    lifecycle = Lifecycle(grace)
    last_check = 0
    try:
        while not stopping.is_set():
            server.handle_request()
            if time.monotonic() - last_check >= check_interval:
                last_check = time.monotonic()
                manager.reap()
                if lifecycle.should_exit(app_probe(), active_workers(data), last_check):
                    # Recheck immediately before exiting; reopening cancels shutdown.
                    if lifecycle.should_exit(app_probe(), active_workers(data), time.monotonic()):
                        break
    finally:
        server.server_close()
        lock.close()

if __name__ == '__main__':
    serve()
