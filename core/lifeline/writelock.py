import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from lifeline import ident

LOCK_NAME = "lifeline.lock"
DEFAULT_TIMEOUT = 30.0
POLL_SECONDS = 0.05
BUSY = "Lifeline is busy with another change. Try again in a moment."


class WriteLockTimeout(Exception):
    pass


def lifeline_folder():
    return ident.data_folder()


def default_lock_path():
    return lifeline_folder() / LOCK_NAME


def _prepare(folder, own):
    existed = folder.is_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if own or not existed:
        os.chmod(folder, 0o700)


def _try_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def held(path=None, timeout=DEFAULT_TIMEOUT, poll=POLL_SECONDS):
    target = Path(path) if path is not None else default_lock_path()
    _prepare(target.parent, path is None or target == default_lock_path())
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WriteLockTimeout(BUSY)
            time.sleep(min(poll, remaining))
        try:
            yield target
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
