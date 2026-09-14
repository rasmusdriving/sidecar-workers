"""Small OS boundary for the service; provider spawning lives in the engine."""
import csv
import base64
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def windows():
    return sys.platform == 'win32'


def install_home():
    if windows():
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'SidecarWorkers'
    return Path.home() / 'Library/Application Support/SidecarWorkers'


def detached_options():
    if windows():
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB.
        # Console detachment alone still inherits an app's kill-on-close job.
        return {'creationflags': 0x00000008 | 0x00000200 | 0x01000000}
    return {'start_new_session': True}


def spawn_detached(*args, **kwargs):
    try:
        return subprocess.Popen(*args, **kwargs, **detached_options())
    except PermissionError as exc:
        if windows():
            raise RuntimeError('Windows denied independent process startup. The host may forbid '
                               'process breakaway; start the installed Sidecar scheduled task '
                               'from Task Scheduler, then retry. Also check executable access.') from exc
        raise


def hidden_options():
    return {'creationflags': 0x08000000} if windows() else {}


def acquire_lock(path, blocking=True):
    """Return an open locked file. Closing it releases the process-wide lock."""
    lock = path.open('a+b')
    try:
        if windows():
            import msvcrt
            # Lock beyond EOF is supported, so no initialization write is needed.
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise BlockingIOError('Sidecar lock is already held') from exc
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return lock
    except BaseException:
        lock.close()
        raise


def process_command(pid):
    pid = int(pid)
    if pid <= 0:
        raise ValueError('Invalid process ID')
    if windows():
        script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                  f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | "
                  'Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress')
        result = subprocess.check_output(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            encoding='utf-8', timeout=10, **hidden_options())
        return json.loads(result) if result.strip() else ''
    return subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()


def app_running():
    if windows():
        result = subprocess.check_output(['tasklist.exe', '/FO', 'CSV', '/NH'],
                                         text=True, errors='replace', timeout=10,
                                         **hidden_options())
        return any(row and row[0].lower() in ('codex.exe', 'chatgpt.exe')
                   for row in csv.reader(result.splitlines()))
    commands = subprocess.check_output(['ps', '-axo', 'comm='], text=True).splitlines()
    return any(c.strip().endswith(('/ChatGPT.app/Contents/MacOS/ChatGPT', '/Codex.app/Contents/MacOS/Codex')) for c in commands)


def stop_child(child):
    """Stop the engine and its providers while the supervisor can save a result."""
    if windows():
        result = subprocess.run(['taskkill.exe', '/PID', str(child.pid), '/T', '/F'],
                                capture_output=True, **hidden_options())
        if result.returncode and child.poll() is None:
            raise OSError('Could not stop worker process tree')
    else:
        import signal
        os.killpg(child.pid, signal.SIGINT)  # MCO handles this by cancelling providers.
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if windows():
            raise RuntimeError('Worker process tree did not stop')
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def link_history(target, source):
    if not windows():
        target.symlink_to(source, target_is_directory=True)
        return
    # Junctions work without symlink privileges. Encode the script and quote
    # literal paths so spaces, apostrophes and shell metacharacters stay data.
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = ("$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction "
              f'-Path {quote(target)} -Target {quote(source)} | Out-Null')
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                    '-EncodedCommand', encoded], check=True, capture_output=True,
                   **hidden_options())
