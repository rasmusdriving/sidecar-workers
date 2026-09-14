"""One loopback server for all coordinating tasks, with independent worker supervisors."""
import hmac
import json
import os
import re
import secrets
import socket
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from .common import ROOT, Lifecycle, active_workers, app_running, atomic, data_dir, identity, thread_dir, worker_alive
from .platform import acquire_lock, spawn_detached, windows
from .activity import snapshot
from .models import resolve_model
from .providers import provider_status, provider_statuses


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer normally performs reverse DNS here. This service only uses
        # 127.0.0.1; DNS must not delay startup or graceful shutdown.
        if windows():
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def prepare(data):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    (data / 'threads').mkdir(exist_ok=True)
    path = data / 'service.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    value = {'token': secrets.token_urlsafe(32), 'port': 0}
    atomic(path, value)
    path.chmod(0o600)
    return value


def thread_state(data, tid):
    workers = []
    for p in sorted(thread_dir(data, tid).glob('devin-*/job.json')):
        if json.loads(p.read_text(encoding='utf-8')).get('thread_id') != tid:
            continue
        state = snapshot(p.parent)
        state['id'] = p.parent.name
        workers.append(state)
    return {'thread_id': tid, 'workers': sorted(workers, key=lambda w: w.get('started', 0))}


def worker_prompt(config):
    delegation = 'Do not spawn or delegate to other agents.' if not config['allow_subagents'] else 'Keep any delegated work inside the execution budget.'
    return config['prompt'] + '\n\nSidecar coordination contract:\n' + delegation + f" You have {config['timeout']} seconds of execution time. Limit initial exploration to relevant files, reserve at least half the budget for synthesis and verification, and return useful partial findings with limitations if necessary. Do not repeatedly re-explore established facts. " + 'If a missing answer prevents progress, finish your turn with ONLY a JSON object of this form: {"sidecar_question":"Your concise question with enough context for the coordinating agent"}. Sidecar will pause and deliver the reply in this same conversation. Do not use this format for rhetorical questions or final findings. Ask at most five questions. Permission approval is separate; a clarification does not expand the authorized scope.'


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
        question_timeout = int(body.get('question_timeout', 600))
        max_turns = int(body.get('max_turns', 24))
        if not 10 <= question_timeout <= 86400 or not 1 <= max_turns <= 200:
            raise ValueError('Question timeout must be 10..86400; max turns must be 1..200')
        # Caller supplies a stable request ID so retries do not duplicate paid work.
        request_id = uuid.UUID(body['request_id']).hex
        with self.lock:
            folder = thread_dir(self.data, tid)
            folder.mkdir(parents=True, exist_ok=True)
            job = folder / ('devin-' + request_id)
            if not job.exists():
                job.mkdir()
                config = dict(thread_id=tid, title=body.get('title', 'Worker'), repo=repo, provider=provider, model=model, effort=effort, mode=mode, timeout=timeout, started=time.time(), prompt=body['prompt'], question_timeout=question_timeout, max_turns=max_turns, allow_subagents=bool(body.get('allow_subagents', False)))
                atomic(job / 'job.json', config)
                (job / 'prompt.txt').write_text(worker_prompt(config), encoding='utf-8')
                try:
                    with (job / 'worker.log').open('a') as log:
                        proc = spawn_detached([sys.executable, '-m', 'sidecar.worker', str(job), str(self.data)], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                    (job / 'supervisor.pid').write_text(str(proc.pid), encoding='utf-8')
                    self.children.append(proc)
                except Exception as exc:
                    atomic(job / 'done.json', {'status':'failed','exit_code':1,'finished':time.time(),'error':str(exc)})
                    raise
            return {'worker_id': job.name, 'thread_id': tid, 'job_dir': str(job), 'preview_url': self.preview(tid)}

    def job(self, tid, wid):
        if not re.fullmatch(r'devin-[a-f0-9]+', wid):
            raise ValueError('Invalid worker ID')
        job = thread_dir(self.data, tid) / wid
        config = json.loads((job / 'job.json').read_text(encoding='utf-8'))
        if config.get('thread_id') != tid:
            raise ValueError('Worker belongs to another task')
        return job

    def permission(self, body):
        job = self.job(identity(body['thread_id']), body['worker_id'])
        rid = uuid.UUID(body['request_id']).hex
        path = job / 'permissions' / (rid + '.request.json')
        request = json.loads(path.read_text(encoding='utf-8'))
        if request['status'] != 'pending' or (job / 'done.json').exists():
            raise ValueError('Permission is no longer pending')
        decision = body['decision']
        if decision not in ('allow_once', 'deny'):
            raise ValueError('Decision must be allow_once or deny')
        option = next(o['optionId'] for o in request['request']['options'] if o['kind'] == 'allow_once') if decision == 'allow_once' else None
        atomic(path.with_name(rid + '.decision.json'), {'optionId': option})
        return {'id': rid, 'decision': decision}

    def reply(self, body):
        with self.lock:
            job = self.job(identity(body['thread_id']), body['worker_id'])
            rid = uuid.UUID(body['question_id']).hex
            path = job / 'questions' / (rid + '.request.json')
            question = json.loads(path.read_text(encoding='utf-8'))
            answer = body.get('answer')
            if not isinstance(answer, str) or not 0 < len(answer.strip()) <= 16000:
                raise ValueError('Answer must contain 1..16000 characters')
            reply = path.with_name(rid + '.reply.json')
            if reply.exists():
                if json.loads(reply.read_text(encoding='utf-8'))['answer'] != answer:
                    raise ValueError('Question already has a different answer')
                return {'id': rid, 'status': 'answered'}
            if question['status'] != 'pending' or time.time() >= question['deadline'] or (job / 'done.json').exists() or (job / 'stop-requested.json').exists() or not worker_alive(job):
                raise ValueError('Question is no longer pending on a live worker')
            atomic(reply, {'answer': answer, 'created': time.time()})
            return {'id': rid, 'status': 'answered'}

    def stop(self, body):
        job = self.job(identity(body['thread_id']), body['worker_id'])
        # Never gate the request on a liveness probe: it shells out to
        # PowerShell on Windows and a transient empty answer would silently
        # leave a paid worker running while reporting a cancellation. The file
        # is inert once a supervisor has exited, so requesting is always safe.
        requested = not (job / 'done.json').exists()
        if requested:
            atomic(job / 'stop-requested.json', {'created':time.time()})
        return {'worker_id': job.name, 'stop_requested': requested}


def serve(data=None, *, app_probe=app_running, grace=60, check_interval=2):
    data = data or data_dir()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        lock = acquire_lock(data / 'service.lock', blocking=False)
    except BlockingIOError:
        return
    config = prepare(data)
    stopping = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

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
                elif self.path == '/provider-status':
                    result = (provider_status(body['provider'], body.get('check_auth', True))
                              if body.get('provider') else provider_statuses(body.get('check_auth', True)))
                elif self.path == '/permission':
                    result = manager.permission(body)
                elif self.path == '/reply':
                    result = manager.reply(body)
                elif self.path == '/shutdown':
                    if active_workers(data):
                        raise ValueError('Workers are active. Finish or stop them before shutting down.')
                    stopping.set()
                    result = {'stopping': True}
                elif self.path == '/stop':
                    result = manager.stop(body)
                else:
                    return self.reply(404, {'error':'Not found'})
                self.reply(200, result)
            except (KeyError, ValueError, OSError, StopIteration, subprocess.SubprocessError) as exc:
                self.reply(400, {'error':str(exc)})

    server = LoopbackServer(('127.0.0.1', config['port']), Handler)
    # Finish in-flight responses before interpreter shutdown. Client timeouts
    # keep shutdown bounded even if a connection stops sending data.
    server.daemon_threads = False
    config.update(port=server.server_port, pid=os.getpid())
    manager = Manager(data, f'http://127.0.0.1:{server.server_port}/' + config['token'])
    atomic(data / 'service.json', config)
    (data / 'service.json').chmod(0o600)
    lifecycle = Lifecycle(grace)
    # Windows presence/liveness probes launch subprocesses and can take seconds.
    # Accept HTTP requests independently so health checks, previews, and worker
    # cancellation remain available throughout those probes.
    http_thread = threading.Thread(target=server.serve_forever,
                                   kwargs={'poll_interval': .1}, name='sidecar-http')
    http_thread.start()
    try:
        while not stopping.is_set():
            checked_at = time.monotonic()
            manager.reap()
            if lifecycle.should_exit(app_probe(), active_workers(data), checked_at):
                # Recheck immediately before exiting; reopening cancels shutdown.
                if lifecycle.should_exit(app_probe(), active_workers(data), time.monotonic()):
                    break
            stopping.wait(max(0, check_interval - (time.monotonic() - checked_at)))
    finally:
        server.shutdown()
        http_thread.join()
        server.server_close()
        lock.close()

if __name__ == '__main__':
    serve()
