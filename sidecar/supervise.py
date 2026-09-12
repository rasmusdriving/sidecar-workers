"""launchd checks every 15s; no resident watcher while Codex is closed."""
from .common import active_workers, app_running, data_dir
from .service import serve

if __name__ == '__main__':
    data = data_dir()
    if app_running() or active_workers(data):
        serve(data)
