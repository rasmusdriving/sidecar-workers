import argparse
import fcntl
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
from .common import ROOT, active_workers, atomic, data_dir, identity, thread_dir

LABEL = 'io.sidecar-workers.service'


def endpoint(data):
    config = json.loads((data / 'service.json').read_text())
    return config, f"http://127.0.0.1:{config['port']}"


def healthy(data):
    try:
        config, host = endpoint(data)
        with urlopen(host + '/' + config['token'] + '/health', timeout=1) as response:
            return json.load(response).get('service') == 'sidecar-workers'
    except (OSError, ValueError, HTTPException):
        return False


def ensure(data):
    if healthy(data):
        return
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data / 'startup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy(data):
            return
        with (data / 'service.log').open('a') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'sidecar.service'], cwd=ROOT, env={**os.environ,'SIDECAR_HOME':str(data)}, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
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
        raise ValueError(json.load(exc).get('error', str(exc))) from exc


def install(data):
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
    (bindir / 'sidecar').write_text(launcher)
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
    skill = Path.home() / '.codex/skills/sidecar-workers'
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'skills/sidecar-workers/SKILL.md', skill / 'SKILL.md')
    ensure(data)
    return {'installed':True,'cli':str(bindir/'sidecar'),'launch_agent':str(plist),'data':str(data)}


def main():
    parser = argparse.ArgumentParser(description='Sidecar Workers: shared local worker service for Codex')
    sub = parser.add_subparsers(dest='action',required=True)
    start = sub.add_parser('start')
    start.add_argument('--repo',required=True)
    start.add_argument('--title',required=True)
    start.add_argument('--provider',choices=['devin','claude','grok'],default='devin')
    start.add_argument('--model')
    start.add_argument('--effort')
    start.add_argument('--mode',choices=['read_only','write'],default='read_only')
    start.add_argument('--timeout',type=int,default=600)
    start.add_argument('--request-id',default=None)
    group = start.add_mutually_exclusive_group(required=True)
    group.add_argument('--prompt')
    group.add_argument('--file')
    for action in [start, sub.add_parser('preview'), sub.add_parser('status'), sub.add_parser('permission'), sub.add_parser('stop')]:
        action.add_argument('--thread-id')
        if action.prog.endswith(('status','permission','stop')):
            action.add_argument('worker_id',nargs='?' if action.prog.endswith('status') else None)
        if action.prog.endswith('status'):
            action.add_argument('--full',action='store_true')
        if action.prog.endswith('permission'):
            action.add_argument('request_id')
            action.add_argument('decision',choices=['allow_once','deny'])
    sub.add_parser('doctor')
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
            import signal
            subprocess.run(['launchctl','bootout','gui/'+str(os.getuid())+'/'+LABEL],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if healthy(data):
                cfg,_ = endpoint(data)
                os.kill(cfg['pid'],signal.SIGTERM)
            (Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')).unlink(missing_ok=True)
            (Path.home()/'.local/bin/sidecar').unlink(missing_ok=True)
            (Path.home()/'.codex/skills/sidecar-workers/SKILL.md').unlink(missing_ok=True)
            result = {'uninstalled':True,'data_preserved':str(data)}
        elif args.action == 'serve':
            from .service import serve
            serve(data)
            return
        elif args.action == 'doctor':
            ensure(data)
            cfg, host = endpoint(data)
            result = {'healthy':True,'pid':cfg['pid'],'host':host,'data':str(data),'providers':{p:shutil.which(p) for p in ['claude','grok']}}
        elif args.action == 'import-history':
            source = Path(args.directory).resolve(strict=True)
            tid = identity(source.name)
            target = thread_dir(data,tid)
            target.parent.mkdir(parents=True,exist_ok=True)
            for job in source.glob('devin-*/job.json'):
                if json.loads(job.read_text()).get('thread_id') != tid:
                    raise ValueError('History contains a job belonging to another task')
            if not target.exists():
                target.symlink_to(source,target_is_directory=True)
            elif target.resolve() != source:
                raise ValueError('Task already has a different history directory')
            result = {'thread_id':tid,'imported':str(source)}
        else:
            tid = identity(args.thread_id)
            if args.action == 'start':
                body = vars(args).copy()
                body.update(thread_id=tid, prompt=Path(args.file).read_text() if args.file else args.prompt, request_id=args.request_id or str(uuid.uuid4()))
                result = request(data,'start',body)
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
