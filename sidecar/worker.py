"""Detached supervisor: outlives the UI and shared HTTP service."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from .common import atomic
from .engine import command


def main():
    job, data = map(Path, sys.argv[1:3])
    (job / 'supervisor.pid').write_text(str(os.getpid()))
    config = json.loads((job / 'job.json').read_text())
    child = None
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    code, error = 1, None
    try:
        cmd, env = command(config, job, data)
        with (job / 'stream.jsonl').open('w') as out, (job / 'stderr.log').open('w') as err:
            child = subprocess.Popen(cmd, stdout=out, stderr=err, env=env)
            (job / 'worker.pid').write_text(str(child.pid))
            code = child.wait(timeout=config['timeout'] + 60)
    except BaseException as exc:
        error = 'Worker cancelled' if isinstance(exc, KeyboardInterrupt) else str(exc)
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        (job / 'stderr.log').write_text(error)
    finally:
        atomic(job / 'done.json', {'status': 'complete' if code == 0 else 'failed', 'exit_code': code, 'finished': time.time(), **({'error': error} if error else {})})

if __name__ == '__main__':
    main()
