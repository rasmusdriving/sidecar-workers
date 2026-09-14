"""Shared executable discovery and credential-free provider diagnostics."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .common import host_free_env
from .platform import hidden_options, windows

PROVIDERS = ('devin', 'claude', 'grok')


def resolve_binary(provider):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider')
    candidates = []
    if provider == 'devin':
        override = os.environ.get('SIDECAR_DEVIN_BIN')
        if override:
            # A broken explicit override must not silently select another CLI.
            return str(Path(override).expanduser()) if Path(override).expanduser().is_file() else None
    candidates.append(shutil.which(provider))
    if provider == 'devin':
        if windows():
            local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
            roots = [local / 'Programs']
            roots.extend(Path(os.environ[name]) for name in ('ProgramFiles', 'ProgramFiles(x86)') if os.environ.get(name))
            for root in roots:
                for app in ('Devin', 'Devin - Next'):
                    candidates.append(root / app / 'resources/app/extensions/windsurf/devin/bin/devin.exe')
        else:
            for app in ('Devin - Next', 'Devin'):
                candidates.append(Path('/Applications') / (app + '.app') / 'Contents/Resources/app/extensions/windsurf/devin/bin/devin')
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def login_command(provider, binary):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider')
    return [binary, 'login', '--oauth'] if provider == 'grok' else [binary, 'auth', 'login']


def prepare_command(command):
    from .engine import setup
    setup()
    from runtime.platform import prepare_spawn
    env = host_free_env()
    cmd, options = prepare_spawn(command, env=env)
    return cmd, {**options, 'env': env}


def provider_status(provider, check_auth=True, *, binary=None):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider')
    binary = binary or resolve_binary(provider)
    result = {'provider': provider, 'binary': binary, 'status': 'not_installed', 'authenticated': None}
    if not binary:
        result['message'] = ('Devin CLI not found. Install Devin or set SIDECAR_DEVIN_BIN to its agent executable.'
                             if provider == 'devin' else 'Install the provider CLI and add it to PATH, then reinstall Sidecar.')
        return result
    result.update(status='unchecked', login_command='sidecar auth login ' + provider)
    if not check_auth:
        return result
    probe = [binary, 'models'] if provider == 'grok' else [binary, 'auth', 'status']
    cmd, options = prepare_command(probe)
    try:
        completed = subprocess.run(cmd, **{**options, **hidden_options()}, stdin=subprocess.DEVNULL,
                                   capture_output=True, encoding='utf-8', errors='replace', timeout=8)
    except (OSError, subprocess.SubprocessError):
        return {**result, 'status': 'check_failed', 'message': 'Could not check sign-in. Check the provider CLI or connection and retry.'}
    # Parse in memory; never return provider output, account details, or tokens.
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', completed.stdout + '\n' + completed.stderr).lower()
    logged_in = None
    if provider == 'claude':
        try:
            value = json.loads(completed.stdout).get('loggedIn')
            if isinstance(value, bool):
                logged_in = value
        except (ValueError, AttributeError):
            pass
    if any(marker in output for marker in ('not logged in', 'not authenticated', 'authentication required',
                                           'please log in', 'please login', 'please sign in', 'login required',
                                           'unauthorized', 'invalid api key', 'expired token')):
        logged_in = False
    elif logged_in is None and completed.returncode == 0:
        if provider == 'grok' or (provider == 'devin' and ('logged in' in output or 'authenticated' in output)):
            logged_in = True
    if logged_in is False:
        return {**result, 'status': 'needs_auth', 'authenticated': False, 'message': 'Sign in to the provider CLI to start workers.'}
    if logged_in is True and completed.returncode == 0:
        return {**result, 'status': 'ready', 'authenticated': True}
    return {**result, 'status': 'check_failed', 'message': 'Provider sign-in could not be confirmed. Check the CLI and retry.'}


def provider_statuses(check_auth=True):
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        results = pool.map(lambda provider: provider_status(provider, check_auth), PROVIDERS)
        return dict(zip(PROVIDERS, results))
