import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from .platform import app_running, install_home, process_command

ROOT = Path(__file__).resolve().parent.parent

# Task identity published by the coordinating app, most specific first.
TASK_ENV = ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_CODE_SESSION_ID')
# The coordinating session itself, including its control channel. A worker runs
# as its own provider session and must never inherit the conversation that
# launched it; a provider CLI that is also the host would otherwise attach there.
HOST_ENV = TASK_ENV + ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_HOST_SESSION_ID',
                       'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ATTENDED',
                       'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
                       'CLAUDE_PID', 'CLAUDE_EFFORT')
# Where each coordinating app discovers user skills.
SKILL_HOMES = ('.codex/skills', '.claude/skills')


def data_dir():
    return Path(os.environ.get('SIDECAR_HOME', install_home())).expanduser().resolve()


def atomic(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value), encoding='utf-8')
    try:
        for attempt in range(20):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                # Windows readers can briefly hold a handle without delete sharing.
                if attempt == 19:
                    raise
                time.sleep(.01)
    finally:
        tmp.unlink(missing_ok=True)


def identity(value=None):
    return str(uuid.UUID(value or next((os.environ[name] for name in TASK_ENV if os.environ.get(name)), '')))


def host_free_env(env=None):
    return {k: v for k, v in (os.environ if env is None else env).items() if k not in HOST_ENV}


def install_skill(source):
    """Publish the coordination skill to every supported app, installed or not."""
    targets = []
    for home in SKILL_HOMES:
        target = Path.home() / home / 'sidecar-workers'
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target / 'SKILL.md')
        targets.append(str(target))
    return targets


def remove_skill():
    for home in SKILL_HOMES:
        (Path.home() / home / 'sidecar-workers' / 'SKILL.md').unlink(missing_ok=True)


def thread_dir(data, thread):
    return data / 'threads' / identity(thread)


def jobs(data):
    return list((data / 'threads').glob('*/devin-*/job.json'))


def worker_alive(job):
    try:
        pid = int((job / 'supervisor.pid').read_text(encoding='utf-8'))
        command = process_command(pid) or ''
        return 'sidecar.worker' in command and str(job) in command
    except (ValueError, OSError, subprocess.SubprocessError):
        # Imported legacy worker: use its MCO PID and exact artifact directory.
        try:
            pid = int((job / 'worker.pid').read_text(encoding='utf-8'))
            command = process_command(pid) or ''
            return 'mco' in command and str(job / 'artifacts') in command
        except (ValueError, OSError, subprocess.SubprocessError):
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
