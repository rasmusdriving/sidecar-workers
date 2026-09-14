"""OS-scheduled app checks; no resident watcher while every coordinating app is closed."""
from .common import active_workers, app_running, data_dir
from .service import serve
from .platform import windows

if __name__ == '__main__':
    data = data_dir()
    if app_running() or active_workers(data):
        if windows():
            # Scheduled checks exit promptly; the service has an independent lifetime.
            from .cli import ensure
            ensure(data)
        else:
            serve(data)
