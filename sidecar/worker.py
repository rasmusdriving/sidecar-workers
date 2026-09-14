"""Detached supervisor: outlives the UI and shared HTTP service."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from .common import atomic
from .platform import spawn_detached, stop_child
from .engine import command, setup


def main():
    job, data = map(Path, sys.argv[1:3])
    (job / 'supervisor.pid').write_text(str(os.getpid()), encoding='utf-8')
    config = json.loads((job / 'job.json').read_text(encoding='utf-8'))
    child = None
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    code, error = 1, None
    try:
        cmd, env = command(config, job, data)
        with (job / 'stream.jsonl').open('w') as out, (job / 'stderr.log').open('w') as err:
            child = spawn_detached(cmd, stdout=out, stderr=err, env=env)
            (job / 'worker.pid').write_text(str(child.pid), encoding='utf-8')
            setup()
            from runtime.coordinator import paused_seconds
            started = time.monotonic()
            while child.poll() is None:
                if (job / 'stop-requested.json').exists():
                    raise KeyboardInterrupt
                if time.monotonic() - started - paused_seconds(job / 'questions') > config['timeout'] + 60:
                    raise TimeoutError('Worker exceeded its execution budget')
                time.sleep(.1)
            code = child.returncode
    except BaseException as exc:
        error = 'Worker cancelled' if isinstance(exc, KeyboardInterrupt) else str(exc)
        if child and child.poll() is None:
            stop_child(child)
        (job / 'stderr.log').write_text(error, encoding='utf-8')
    finally:
        setup()
        from runtime.coordinator import close_pending
        close_pending(job / 'questions')
        atomic(job / 'done.json', {'status': 'complete' if code == 0 else 'failed', 'exit_code': code, 'finished': time.time(), **({'error': error} if error else {})})

if __name__ == '__main__':
    main()
