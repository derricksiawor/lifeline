import ctypes
import ctypes.util
import errno
import os
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path


class LockTimeout(Exception):
    pass


_LOCKS = (
    (Path(".claude") / ".oauth_refresh.lock", 60.0),
    (Path(".claude.lock"), 60.0),
    (Path(".claude.json.lock"), 10.0),
)

STORAGE_LOCK = ".storage-write.lock"
STORAGE_STALE = 15.0
STORAGE_TIMEOUT = 20.0
SIGNIN_FILE = ".credentials.json"


def claude_folder(home):
    return Path(unicodedata.normalize("NFC", str(Path(home) / ".claude")))


def nudge_sessions(folder):
    try:
        os.utime(Path(folder) / SIGNIN_FILE)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "failed"
    return "moved"


def _identity(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino, getattr(st, "st_birthtime", None))


def _load_noreplace():
    name = ctypes.util.find_library("c")
    if not name:
        return None
    try:
        libc = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        def call(src, dst):
            return libc.renamex_np(os.fsencode(src), os.fsencode(dst), 0x4)
        return call
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        def call(src, dst):
            return libc.renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1)
        return call
    return None


_NOREPLACE = _load_noreplace()
_ATTEMPT = threading.Lock()


def _rename_guarded(src, dst):
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    os.rename(src, dst)


def _rename_noreplace(src, dst):
    if _NOREPLACE is None:
        _rename_guarded(src, dst)
        return
    if _NOREPLACE(src, dst) != 0:
        code = ctypes.get_errno()
        if code in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
            _rename_guarded(src, dst)
            return
        raise OSError(code, os.strerror(code), str(dst))


class SessionLocks:
    touch_interval = 5.0

    def __init__(self, home, timeout=30.0, poll=0.1):
        self.home = Path(home)
        self.timeout = timeout
        self.poll = poll
        self._specs = _LOCKS
        self._parent = self.home / ".claude"
        self._held = []
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._compromised = False

    @property
    def compromised(self):
        return self._compromised

    def __enter__(self):
        if self._held or self._thread is not None:
            raise RuntimeError("locks already held")
        parent = self._parent
        if not parent.is_dir():
            raise FileNotFoundError(f"{parent} does not exist, so there is nothing to lock")
        self._compromised = False
        deadline = time.monotonic() + self.timeout
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._keep_fresh, args=(self._stop,), daemon=True)
        try:
            self._thread.start()
            for relative, stale in self._specs:
                path = self.home / relative
                identity = self._acquire(path, stale, deadline)
                with self._mutex:
                    self._held.append((path, identity))
        except BaseException:
            self._finish()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self._finish()
        return False

    def _finish(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join()
        self._release()

    def _acquire(self, path, stale, deadline):
        first = True
        while True:
            if not first and time.monotonic() >= deadline:
                raise LockTimeout(f"timed out waiting for {path}")
            first = False
            with _ATTEMPT:
                try:
                    os.mkdir(path)
                except FileExistsError:
                    if self._break_if_stale(path, stale):
                        continue
                else:
                    try:
                        return _identity(path)
                    except FileNotFoundError:
                        continue
            time.sleep(max(0.0, min(self.poll, deadline - time.monotonic())))

    def _break_if_stale(self, path, stale):
        try:
            seen = os.stat(path).st_mtime
        except FileNotFoundError:
            return True
        if time.time() - seen <= stale:
            return False
        aside = Path(f"{path}.stale.{os.getpid()}.{uuid.uuid4().hex}")
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            return True
        try:
            moved = os.stat(aside).st_mtime
        except FileNotFoundError:
            return True
        if time.time() - moved > stale:
            try:
                os.rmdir(aside)
            except OSError:
                pass
            return True
        try:
            _rename_noreplace(aside, path)
        except OSError:
            pass
        return False

    def _release(self):
        with self._mutex:
            held, self._held = self._held, []
            for path, identity in reversed(held):
                try:
                    if _identity(path) == identity:
                        os.rmdir(path)
                    else:
                        self._compromised = True
                except FileNotFoundError:
                    self._compromised = True
                except OSError:
                    pass

    def _keep_fresh(self, stop):
        while not stop.wait(self.touch_interval):
            with self._mutex:
                for index, (path, identity) in enumerate(self._held):
                    try:
                        if _identity(path) == identity:
                            os.utime(path, None)
                            again = _identity(path)
                            if again[:2] == identity[:2]:
                                self._held[index] = (path, again)
                        else:
                            self._compromised = True
                    except FileNotFoundError:
                        self._compromised = True
                    except OSError:
                        pass


class StorageLock(SessionLocks):
    def __init__(self, folder, timeout=STORAGE_TIMEOUT, poll=0.1):
        super().__init__(folder, timeout=timeout, poll=poll)
        self._parent = Path(folder)
        self._specs = ((Path(STORAGE_LOCK), STORAGE_STALE),)
