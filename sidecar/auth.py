"""Visible native login, separate from worker execution and worker logs."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

from .common import ROOT, atomic, host_free_env
from .platform import acquire_lock, process_command, windows
from .providers import PROVIDERS, login_command, prepare_command, provider_status


def _paths(data, provider):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider')
    folder = data / 'auth'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder, folder / (provider + '.json')


def open_login(data, provider, binary):
    folder, state_path = _paths(data, provider)
    manual = 'sidecar auth login ' + provider + ' --foreground'
    with acquire_lock(folder / (provider + '.launch.lock')):
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        age = time.time() - state.get('updated', 0)
        if state.get('status') == 'opening' and age < 30:
            return {'status': 'already_open', 'message': 'Complete sign-in in the opened terminal/browser.', 'manual_command': manual}
        if state.get('status') == 'running':
            try:
                running = 'sidecar.auth' in process_command(state['pid'])
            except (KeyError, ValueError, OSError, subprocess.SubprocessError):
                running = False
            if running:
                return {'status': 'already_open', 'message': 'Complete sign-in in the opened terminal/browser.', 'manual_command': manual}
        atomic(state_path, {'status': 'opening', 'updated': time.time()})
        python = Path(sys.executable)
        if windows() and python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        cmd = [str(python), '-m', 'sidecar.auth', provider, binary, str(data)]
        try:
            if windows():
                # An interactive console is required for callbacks and fallback
                # prompts. Do not redirect its output into Sidecar logs.
                child = subprocess.Popen(cmd, cwd=ROOT, env=host_free_env(), close_fds=True,
                                         creationflags=0x00000010)  # CREATE_NEW_CONSOLE
                # Reap the handle if the caller stays alive, without keeping a
                # short-lived CLI invocation open for the interactive login.
                threading.Thread(target=child.wait, daemon=True).start()
            elif sys.platform == 'darwin':
                script = folder / (provider + '-login.command')
                script.write_text('#!/bin/sh\ncd ' + shlex.quote(str(ROOT)) + '\nexec ' + shlex.join(cmd) + '\n', encoding='utf-8')
                script.chmod(0o700)
                subprocess.run(['open', '-a', 'Terminal', str(script)], check=True,
                               capture_output=True, timeout=10)
            else:
                raise OSError('No supported desktop terminal')
        except (OSError, subprocess.SubprocessError):
            atomic(state_path, {'status': 'failed', 'updated': time.time()})
            return {'status': 'manual_required', 'message': 'Could not open a login terminal. Run the manual command in your terminal.', 'manual_command': manual}
    return {'status': 'opened', 'message': 'Complete sign-in in the opened terminal/browser, then retry the worker.', 'manual_command': manual}


def run_login(data, provider, binary):
    folder, state_path = _paths(data, provider)
    try:
        lock = acquire_lock(folder / (provider + '.run.lock'), blocking=False)
    except BlockingIOError:
        print('A sign-in for this provider is already running.')
        return 1
    with lock:
        atomic(state_path, {'status': 'running', 'pid': os.getpid(), 'updated': time.time()})
        print('Sign in to ' + provider + ' in its browser or terminal flow. Sidecar does not store your credentials.', flush=True)
        status = 'failed'
        try:
            cmd, options = prepare_command(login_command(provider, binary))
            options.pop('start_new_session', None)
            options.pop('creationflags', None)
            # Inherit this visible terminal. No pipes, files, or worker logging.
            result = subprocess.run(cmd, **options)
            if result.returncode == 0:
                checked = provider_status(provider, binary=binary)
                if checked['status'] == 'ready':
                    status = 'complete'
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
            pass
        finally:
            atomic(state_path, {'status': status, 'updated': time.time()})
        print('Signed in. Retry your Sidecar worker.' if status == 'complete' else
              'Sign-in was not confirmed. Run sidecar auth status ' + provider + ' to check or retry login.', flush=True)
        return 0 if status == 'complete' else 1


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('Use sidecar auth login PROVIDER')
    raise SystemExit(run_login(Path(sys.argv[3]), sys.argv[1], sys.argv[2]))
