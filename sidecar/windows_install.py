"""Per-user Windows installation with a short, interactive scheduled check."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .common import ROOT, active_workers
from .platform import install_home, hidden_options


def task_name():
    user = os.environ.get('USERDOMAIN', '') + '\\' + getpass.getuser()
    return 'SidecarWorkers-' + hashlib.sha256(user.encode()).hexdigest()[:12]


def task_command(*args, check=True):
    result = subprocess.run(['schtasks.exe', *args], capture_output=True,
                            text=True, errors='replace', **hidden_options())
    if check and result.returncode:
        raise RuntimeError('Task Scheduler: ' + (result.stderr.strip() or result.stdout.strip()))
    return result


def stop_service(data):
    from .cli import healthy, request
    if healthy(data):
        request(data, 'shutdown', {})
        deadline = time.monotonic() + 10
        while healthy(data) and time.monotonic() < deadline:
            time.sleep(.05)
        if healthy(data):
            raise RuntimeError('Sidecar is still shutting down; retry shortly.')


def install(data):
    from .cli import ensure
    # Replacing imported runtime files beneath a paid worker is unsafe.
    base = install_home()
    settings = base / 'installation.json'
    previous = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    old_data = Path(previous.get('data', data))
    if active_workers(old_data) or (old_data != data and active_workers(data)):
        raise ValueError('Workers are active. Finish or stop them before installing.')
    if previous:
        task_command('/Delete', '/TN', previous['task'], '/F')
    stop_service(old_data)
    if old_data != data:
        stop_service(data)
    data.mkdir(parents=True, exist_ok=True)
    runtime = base / 'runtime'
    if ROOT != runtime:
        for name in ('sidecar', 'vendor', 'skills'):
            shutil.copytree(ROOT / name, runtime / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    bindir = base / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    env = {'SIDECAR_HOME': str(data), 'PATH': os.environ.get('PATH', ''), 'PYTHONUTF8': '1'}
    if os.environ.get('SIDECAR_DEVIN_BIN'):
        env['SIDECAR_DEVIN_BIN'] = os.environ['SIDECAR_DEVIN_BIN']
    # Keep paths and settings as Python literals, never interpolated shell code.
    bootstrap = base / 'launcher.py'
    bootstrap.write_text(
        'import os, runpy, sys\n'
        f'os.environ.update({env!r})\n'
        f'sys.path.insert(0, {str(runtime)!r})\n'
        "supervise = sys.argv[1:2] == ['--supervise']\n"
        'if supervise:\n'
        ' sys.argv.pop(1)\n'
        f" sys.stdout = sys.stderr = open({str(base / 'scheduler.log')!r}, 'a', encoding='utf-8')\n"
        "runpy.run_module('sidecar.supervise' if supervise else 'sidecar', run_name='__main__')\n",
        encoding='utf-8')
    # CMD's percent expansion also happens inside double quotes.
    python = str(Path(sys.executable)).replace('%', '%%')
    launcher = bindir / 'sidecar.cmd'
    launcher.write_text('@echo off\nsetlocal DisableDelayedExpansion\n'
                        'for /f "tokens=2 delims=:" %%C in (\'chcp\') do set "SIDECAR_CODEPAGE=%%C"\n'
                        'chcp 65001 >nul\n'
                        f'"{python}" -X utf8 "%~dp0..\\launcher.py" %*\n'
                        'set "SIDECAR_EXIT=%ERRORLEVEL%"\n'
                        'chcp %SIDECAR_CODEPAGE% >nul\n'
                        'exit /b %SIDECAR_EXIT%\n',
                        encoding='utf-8')
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    background_python = str(pythonw if pythonw.exists() else Path(sys.executable))
    task = task_name()
    task_command('/Create', '/TN', task, '/SC', 'MINUTE', '/MO', '1',
                 '/TR', subprocess.list2cmdline([background_python, '-X', 'utf8', str(bootstrap), '--supervise']),
                 '/IT', '/RL', 'LIMITED', '/F')
    settings.write_text(json.dumps({'data': str(data), 'task': task}), encoding='utf-8')
    skill = Path.home() / '.codex/skills/sidecar-workers'
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(runtime / 'skills/sidecar-workers/SKILL.md', skill / 'SKILL.md')
    ensure(data, runtime=runtime)
    return {'installed': True, 'cli': str(launcher), 'scheduled_task': task,
            'data': str(data), 'add_to_path': str(bindir)}


def uninstall(data):
    if active_workers(data):
        raise ValueError('Workers are active. Finish or stop them before uninstalling.')
    base = install_home()
    settings = base / 'installation.json'
    previous = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    saved_data = Path(previous.get('data', data))
    if saved_data != data and active_workers(saved_data):
        raise ValueError('Workers are active in the installed data directory.')
    if previous:
        task_command('/Delete', '/TN', previous['task'], '/F')
    stop_service(data)
    if saved_data != data:
        stop_service(saved_data)
    (base / 'bin/sidecar.cmd').unlink(missing_ok=True)
    (base / 'launcher.py').unlink(missing_ok=True)
    settings.unlink(missing_ok=True)
    (Path.home() / '.codex/skills/sidecar-workers/SKILL.md').unlink(missing_ok=True)
