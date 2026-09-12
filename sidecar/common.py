import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir():
    return Path(os.environ.get('SIDECAR_HOME', Path.home() / 'Library/Application Support/SidecarWorkers')).expanduser().resolve()


def atomic(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def identity(value=None):
    return str(uuid.UUID(value or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or ''))


def thread_dir(data, thread):
    return data / 'threads' / identity(thread)


def jobs(data):
    return list((data / 'threads').glob('*/devin-*/job.json'))


def worker_alive(job):
    try:
        pid = int((job / 'supervisor.pid').read_text())
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        return 'sidecar.worker' in command and str(job) in command
    except (ValueError, OSError, subprocess.CalledProcessError):
        # Imported legacy worker: use its MCO PID and exact artifact directory.
        try:
            pid = int((job / 'worker.pid').read_text())
            command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True)
            return 'mco' in command and str(job / 'artifacts') in command
        except (ValueError, OSError, subprocess.CalledProcessError):
            return False


def active_workers(data):
    active = False
    for path in jobs(data):
        job = path.parent
        if (job / 'done.json').exists():
            continue
        if worker_alive(job):
            active = True
        elif time.time() - path.stat().st_mtime > 10:
            atomic(job / 'done.json', {'status': 'failed', 'exit_code': -1, 'finished': time.time(), 'error': 'Worker process ended without a result; files are preserved.'})
        else:
            active = True  # Allow a newly created supervisor to initialize.
    return active


class Lifecycle:
    def __init__(self, grace=60):
        self.grace = grace
        self.absent_since = None

    def should_exit(self, app_open, workers_active, now):
        if app_open or workers_active:
            self.absent_since = None
            return False
        if self.absent_since is None:
            self.absent_since = now
        return now - self.absent_since >= self.grace


def app_running():
    commands = subprocess.check_output(['ps', '-axo', 'comm='], text=True).splitlines()
    return any(c.strip().endswith(('/ChatGPT.app/Contents/MacOS/ChatGPT', '/Codex.app/Contents/MacOS/Codex')) for c in commands)
