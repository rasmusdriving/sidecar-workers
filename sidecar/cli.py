import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .common import ROOT, active_workers, atomic, data_dir, identity, install_skill, remove_skill, thread_dir

from .platform import acquire_lock, spawn_detached, link_history, windows

LABEL = 'io.sidecar-workers.service'


def endpoint(data):
    config = json.loads((data / 'service.json').read_text(encoding='utf-8'))
    return config, f"http://127.0.0.1:{config['port']}"


def healthy(data):
    try:
        config, host = endpoint(data)
        with urlopen(host + '/' + config['token'] + '/health', timeout=1) as response:
            return json.load(response).get('service') == 'sidecar-workers'
    except (OSError, ValueError, HTTPException):
        return False


def ensure(data, *, runtime=ROOT):
    if healthy(data):
        return
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with acquire_lock(data / 'startup.lock'):
        if healthy(data):
            return
        with (data / 'service.log').open('a') as log:
            proc = spawn_detached([sys.executable, '-m', 'sidecar.service'], cwd=runtime, env={**os.environ,'SIDECAR_HOME':str(data)}, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        for _ in range(100):
            if healthy(data):
                return
            if proc.poll() not in (None, 0):
                raise RuntimeError('Sidecar failed to start. Inspect ' + str(data / 'service.log'))
            time.sleep(.05)
        raise RuntimeError('Sidecar did not become ready within five seconds')


def request(data, action, body):
    ensure(data)
    config, host = endpoint(data)
    req = Request(host + '/' + action, json.dumps(body).encode(), {'Authorization':'Bearer '+config['token'], 'Content-Type':'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        with exc:
            message = json.load(exc).get('error', str(exc))
        raise ValueError(message) from exc


def install(data):
    if windows():
        from .windows_install import install as install_windows
        return install_windows(data)
    if sys.platform != 'darwin':
        raise ValueError('LaunchAgent installation is macOS-only')
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = Path.home() / 'Library/Application Support/SidecarWorkers/runtime'
    if ROOT != runtime:
        for name in ('sidecar', 'vendor', 'skills'):
            shutil.copytree(ROOT / name, runtime / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    logdir = Path.home() / 'Library/Logs/SidecarWorkers'
    logdir.mkdir(parents=True, exist_ok=True)
    bindir = Path.home() / '.local/bin'
    bindir.mkdir(parents=True, exist_ok=True)
    import shlex
    launcher = '#!/bin/sh\nexport SIDECAR_HOME=' + shlex.quote(str(data)) + '\ncd ' + shlex.quote(str(runtime)) + '\nexec ' + shlex.quote(sys.executable) + ' -m sidecar "$@"\n'
    (bindir / 'sidecar').write_text(launcher, encoding='utf-8')
    (bindir / 'sidecar').chmod(0o755)
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    env = {'SIDECAR_HOME':str(data), 'PATH':os.environ.get('PATH','/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin')}
    if os.environ.get('SIDECAR_DEVIN_BIN'):
        env['SIDECAR_DEVIN_BIN'] = os.environ['SIDECAR_DEVIN_BIN']
    value = {'Label':LABEL, 'ProgramArguments':[sys.executable,'-m','sidecar.supervise'], 'WorkingDirectory':str(runtime), 'EnvironmentVariables':env, 'RunAtLoad':True, 'StartInterval':15, 'ProcessType':'Background', 'AbandonProcessGroup':True, 'StandardOutPath':str(logdir/'launchd.log'), 'StandardErrorPath':str(logdir/'launchd.log')}
    plist.write_bytes(plistlib.dumps(value))
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl','bootout',domain+'/'+LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # bootout can return while launchd is still removing a running service.
    for _ in range(100):
        state = subprocess.run(['launchctl','print',domain+'/'+LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if state.returncode != 0:
            break
        time.sleep(.05)
    else:
        raise RuntimeError('Previous LaunchAgent is still unloading; retry installation shortly.')
    subprocess.run(['launchctl','bootstrap',domain,str(plist)], check=True)
    skills = install_skill(ROOT / 'skills/sidecar-workers/SKILL.md')
    ensure(data)
    return {'installed':True,'cli':str(bindir/'sidecar'),'launch_agent':str(plist),'data':str(data),'skills':skills}


def main():
    parser = argparse.ArgumentParser(description='Sidecar Workers: shared local worker service for Codex and Claude Code')
    sub = parser.add_subparsers(dest='action',required=True)
    start = sub.add_parser('start')
    start.add_argument('--repo',required=True)
    start.add_argument('--title',required=True)
    start.add_argument('--provider',choices=['devin','claude','grok'],default='devin')
    start.add_argument('--model')
    start.add_argument('--effort')
    start.add_argument('--mode',choices=['read_only','write'],default='read_only')
    start.add_argument('--timeout',type=int,default=600)
    start.add_argument('--question-timeout',type=int,default=600)
    start.add_argument('--max-turns',type=int,default=24)
    start.add_argument('--allow-subagents',action='store_true')
    start.add_argument('--request-id',default=None)
    group = start.add_mutually_exclusive_group(required=True)
    group.add_argument('--prompt')
    group.add_argument('--file')
    for action in [start, sub.add_parser('preview'), sub.add_parser('status'), sub.add_parser('permission'), sub.add_parser('stop'), sub.add_parser('reply')]:
        action.add_argument('--thread-id')
        if action.prog.endswith(('status','permission','stop','reply')):
            action.add_argument('worker_id',nargs='?' if action.prog.endswith('status') else None)
        if action.prog.endswith('reply'):
            action.add_argument('question_id')
            group = action.add_mutually_exclusive_group(required=True)
            group.add_argument('--answer')
            group.add_argument('--file')
        if action.prog.endswith('status'):
            action.add_argument('--full',action='store_true')
        if action.prog.endswith('permission'):
            action.add_argument('request_id')
            action.add_argument('decision',choices=['allow_once','deny'])
    doctor = sub.add_parser('doctor')
    doctor.add_argument('--skip-auth', action='store_true', help='Only check executable discovery')
    auth = sub.add_parser('auth')
    auth.add_argument('operation', choices=['status', 'login'])
    auth.add_argument('provider', choices=['devin', 'claude', 'grok'])
    auth.add_argument('--foreground', action='store_true', help='Sign in using this interactive terminal')
    start.add_argument('--no-login', action='store_true', help='Report missing sign-in without opening a terminal')
    sub.add_parser('uninstall')
    setup = sub.add_parser('install')
    setup.add_argument('--data-dir')
    imp = sub.add_parser('import-history')
    imp.add_argument('directory')
    sub.add_parser('serve')
    args = parser.parse_args()
    data = data_dir()
    try:
        if args.action == 'install':
            result = install(Path(args.data_dir).expanduser().resolve() if args.data_dir else data)
        elif args.action == 'uninstall':
            if active_workers(data):
                raise ValueError('Workers are active. Finish or stop them before uninstalling.')
            if windows():
                from .windows_install import uninstall
                uninstall(data)
            else:
                subprocess.run(['launchctl','bootout','gui/'+str(os.getuid())+'/'+LABEL],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if healthy(data):
                    request(data, 'shutdown', {})
                (Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')).unlink(missing_ok=True)
                (Path.home()/'.local/bin/sidecar').unlink(missing_ok=True)
                remove_skill()
            result = {'uninstalled':True,'data_preserved':str(data)}
        elif args.action == 'serve':
            from .service import serve
            serve(data)
            return
        elif args.action == 'doctor':
            ensure(data)
            cfg, host = endpoint(data)
            result = {'healthy':True,'pid':cfg['pid'],'host':host,'data':str(data),
                      'providers':request(data, 'provider-status', {'check_auth': not args.skip_auth})}
        elif args.action == 'auth':
            result = request(data, 'provider-status', {'provider': args.provider})
            if args.operation == 'login' and result['binary'] and result['status'] != 'ready':
                from .auth import open_login, run_login
                if args.foreground:
                    raise SystemExit(run_login(data, args.provider, result['binary']))
                result['login'] = open_login(data, args.provider, result['binary'])
        elif args.action == 'import-history':
            source = Path(args.directory).resolve(strict=True)
            tid = identity(source.name)
            target = thread_dir(data,tid)
            target.parent.mkdir(parents=True,exist_ok=True)
            for job in source.glob('devin-*/job.json'):
                if json.loads(job.read_text(encoding='utf-8')).get('thread_id') != tid:
                    raise ValueError('History contains a job belonging to another task')
            if not target.exists():
                link_history(target, source)
            elif target.resolve() != source:
                raise ValueError('Task already has a different history directory')
            result = {'thread_id':tid,'imported':str(source)}
        else:
            tid = identity(args.thread_id)
            if args.action == 'start':
                body = vars(args).copy()
                body.update(thread_id=tid, prompt=Path(args.file).read_text(encoding='utf-8') if args.file else args.prompt, request_id=args.request_id or str(uuid.uuid4()))
                # Check from the service's environment, exactly where workers
                # resolve their CLI. No worker or prompt log exists before login.
                result = request(data, 'provider-status', {'provider': args.provider})
                if result['status'] == 'ready':
                    result = request(data,'start',body)
                else:
                    result.update(thread_id=tid, request_id=body['request_id'], worker_started=False)
                    if result['status'] == 'needs_auth' and not args.no_login:
                        from .auth import open_login
                        result['login'] = open_login(data, args.provider, result['binary'])
            elif args.action == 'reply':
                result = request(data,'reply',{**vars(args),'thread_id':tid,'answer':Path(args.file).read_text(encoding='utf-8') if args.file else args.answer})
            elif args.action in ('permission','stop'):
                result = request(data,args.action,{**vars(args),'thread_id':tid})
            else:
                ensure(data)
                cfg, host = endpoint(data)
                url = host+'/'+cfg['token']+'/thread/'+tid+'/'
                result = {'thread_id':tid,'preview_url':url}
                if args.action == 'status':
                    with urlopen(url+'state',timeout=10) as response:
                        result = json.load(response)
                    if args.worker_id:
                        result['workers'] = [w for w in result['workers'] if w['id']==args.worker_id]
                        if not result['workers']:
                            raise ValueError('Worker not found in this task')
                    if not args.full:
                        for w in result['workers']:
                            items = w.pop('items',[])
                            w.pop('prompt',None)
                            w['latest_message'] = next((i['text'][-3000:] for i in reversed(items) if i.get('type')=='message'), '')
                            w['permissions'] = [p for p in w.get('permissions',[]) if p['status']=='pending']
        print(json.dumps(result))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)}),file=sys.stderr)
        raise SystemExit(1)

if __name__ == '__main__':
    main()
