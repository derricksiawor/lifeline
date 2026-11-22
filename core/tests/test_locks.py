import errno
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lifeline import locks
from lifeline.locks import LockTimeout, SessionLocks


class LockCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / ".claude").mkdir()
        self.refresh = self.home / ".claude" / ".oauth_refresh.lock"
        self.main = self.home / ".claude.lock"
        self.config = self.home / ".claude.json.lock"

    def age(self, path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t))

    def no_leftovers(self):
        names = [p.name for p in self.home.iterdir()] + [p.name for p in (self.home / ".claude").iterdir()]
        self.assertEqual([n for n in names if ".stale." in n], [])

    def remaining(self):
        return [p for p in (self.refresh, self.main, self.config) if p.exists()]


class TakingTests(LockCase):
    def test_takes_locks_in_order(self):
        order = []
        real = os.mkdir

        def spy(path, *a, **k):
            real(path, *a, **k)
            order.append(Path(path))

        with mock.patch.object(locks.os, "mkdir", spy):
            with SessionLocks(self.home, timeout=1, poll=0.01):
                self.assertEqual(self.remaining(), [self.refresh, self.main, self.config])
        self.assertEqual(order, [self.refresh, self.main, self.config])
        self.assertEqual(self.remaining(), [])

    def test_missing_dot_folder_raises_and_creates_nothing(self):
        self.refresh.parent.rmdir()
        with self.assertRaises(FileNotFoundError):
            with SessionLocks(self.home, timeout=1, poll=0.01):
                self.fail("must not enter")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_missing_home_raises_and_creates_nothing(self):
        gone = self.home / "nothing"
        with self.assertRaises(FileNotFoundError):
            with SessionLocks(gone, timeout=1, poll=0.01):
                pass
        self.assertFalse(gone.exists())

    def test_waits_while_fresh_lock_held(self):
        self.main.mkdir()
        started = time.monotonic()
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.3, poll=0.02):
                self.fail("must not enter")
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertTrue(self.main.exists())

    def test_waits_then_acquires_when_other_releases(self):
        self.main.mkdir()
        threading.Timer(0.2, self.main.rmdir).start()
        with SessionLocks(self.home, timeout=3, poll=0.02):
            self.assertEqual(self.remaining(), [self.refresh, self.main, self.config])
        self.assertEqual(self.remaining(), [])

    def test_breaks_stale_lock(self):
        self.refresh.mkdir()
        self.age(self.refresh, 61)
        with SessionLocks(self.home, timeout=1, poll=0.01):
            self.assertTrue(self.refresh.is_dir())
            self.assertLess(time.time() - self.refresh.stat().st_mtime, 5)
        self.assertFalse(self.refresh.exists())
        self.no_leftovers()

    def test_stale_limits_per_lock(self):
        for path, fresh_age, stale_age in (
            (self.refresh, 59, 61),
            (self.main, 59, 61),
            (self.config, 9, 11),
        ):
            with self.subTest(path=path.name):
                path.mkdir()
                self.age(path, fresh_age)
                with self.assertRaises(LockTimeout):
                    with SessionLocks(self.home, timeout=0.1, poll=0.01):
                        pass
                self.age(path, stale_age)
                with SessionLocks(self.home, timeout=1, poll=0.01):
                    pass
                self.assertFalse(path.exists())

    def test_config_lock_fresh_after_ten_seconds_is_stale_but_main_is_not(self):
        self.main.mkdir()
        self.age(self.main, 11)
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.1, poll=0.01):
                pass

    def test_future_mtime_is_not_stale(self):
        self.config.mkdir()
        t = time.time() + 3600
        os.utime(self.config, (t, t))
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.1, poll=0.01):
                pass


class ReleaseTests(LockCase):
    def test_releases_on_exception(self):
        with self.assertRaises(ValueError):
            with SessionLocks(self.home, timeout=1, poll=0.01):
                raise ValueError("boom")
        self.assertEqual(self.remaining(), [])

    def test_timeout_raises_and_releases_taken_locks(self):
        self.config.mkdir()
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.2, poll=0.01):
                pass
        self.assertFalse(self.refresh.exists())
        self.assertFalse(self.main.exists())
        self.assertTrue(self.config.exists())

    def test_never_removes_foreign_lock_after_timeout(self):
        self.main.mkdir()
        before = self.main.stat().st_ino
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.1, poll=0.01):
                pass
        self.assertEqual(self.main.stat().st_ino, before)

    def test_does_not_remove_lock_replaced_by_another_owner(self):
        with SessionLocks(self.home, timeout=1, poll=0.01):
            moved = self.home / "moved"
            os.rename(self.main, moved)
            self.main.mkdir()
            foreign = self.main.stat().st_ino
        self.assertTrue(self.main.exists())
        self.assertEqual(self.main.stat().st_ino, foreign)
        self.assertFalse(self.refresh.exists())
        self.assertFalse(self.config.exists())

    def test_release_tolerates_lock_already_gone(self):
        with SessionLocks(self.home, timeout=1, poll=0.01):
            self.main.rmdir()
        self.assertEqual(self.remaining(), [])

    def test_reverse_order_release(self):
        order = []
        real = os.rmdir

        def spy(path, *a, **k):
            real(path, *a, **k)
            order.append(Path(path))

        with mock.patch.object(locks.os, "rmdir", spy):
            with SessionLocks(self.home, timeout=1, poll=0.01):
                order.clear()
        self.assertEqual(order, [self.config, self.main, self.refresh])

    def test_reusable_after_exit(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        with guard:
            pass
        with guard:
            self.assertEqual(len(self.remaining()), 3)
        self.assertEqual(self.remaining(), [])

    def test_second_holder_blocks_until_first_exits(self):
        entered = threading.Event()
        leave = threading.Event()
        second = []

        def first():
            with SessionLocks(self.home, timeout=2, poll=0.01):
                entered.set()
                leave.wait(3)

        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(entered.wait(2))
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.15, poll=0.01):
                second.append(1)
        leave.set()
        t.join(3)
        with SessionLocks(self.home, timeout=1, poll=0.01):
            second.append(2)
        self.assertEqual(second, [2])


class RaceTests(LockCase):
    def test_six_threads_never_overlap(self):
        inside = []
        overlaps = []
        done = []

        def work():
            with SessionLocks(self.home, timeout=10, poll=0.002):
                inside.append(1)
                if len(inside) > 1:
                    overlaps.append(1)
                time.sleep(0.01)
                inside.pop()
            done.append(1)

        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(overlaps, [])
        self.assertEqual(len(done), 6)
        self.assertEqual(self.remaining(), [])

    def test_four_threads_racing_for_stale_lock_only_one_wins(self):
        self.refresh.mkdir()
        self.age(self.refresh, 120)
        inside = []
        overlaps = []
        done = []

        def work():
            with SessionLocks(self.home, timeout=10, poll=0.002):
                inside.append(1)
                if len(inside) > 1:
                    overlaps.append(1)
                time.sleep(0.02)
                inside.pop()
            done.append(1)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(overlaps, [])
        self.assertEqual(len(done), 4)
        self.assertEqual(self.remaining(), [])
        self.no_leftovers()

    def test_stale_takeover_does_not_remove_a_fresh_replacement(self):
        self.main.mkdir()
        self.age(self.main, 120)
        real = locks.os.stat
        swapped = []

        def swap_after_stat(path, *a, **k):
            result = real(path, *a, **k)
            if Path(path) == self.main and not swapped:
                swapped.append(1)
                os.rename(self.main, self.home / "old")
                os.mkdir(self.main)
            return result

        with mock.patch.object(locks.os, "stat", swap_after_stat):
            with self.assertRaises(LockTimeout):
                with SessionLocks(self.home, timeout=0.2, poll=0.01):
                    pass
        self.assertTrue(self.main.exists())
        self.assertFalse(self.refresh.exists())


class TouchTests(LockCase):
    def test_touch_thread_refreshes_held_locks(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        guard.touch_interval = 0.05
        with guard:
            for p in (self.refresh, self.main, self.config):
                self.age(p, 30)
            with guard._mutex:
                guard._held = [(p, locks._identity(p)) for p, _ in guard._held]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if all(time.time() - p.stat().st_mtime < 5 for p in (self.refresh, self.main, self.config)):
                    break
                time.sleep(0.02)
            for p in (self.refresh, self.main, self.config):
                self.assertLess(time.time() - p.stat().st_mtime, 5)

    def test_default_interval_is_five_seconds(self):
        self.assertEqual(SessionLocks(self.home).touch_interval, 5.0)
        self.assertEqual(SessionLocks(self.home).timeout, 30.0)
        self.assertEqual(SessionLocks(self.home).poll, 0.1)

    def test_touch_thread_stops_on_exit(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        guard.touch_interval = 0.02
        with guard:
            thread = guard._thread
            self.assertTrue(thread.is_alive())
        self.assertFalse(thread.is_alive())

    def test_touch_thread_stops_after_exception(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        with self.assertRaises(RuntimeError):
            with guard:
                thread = guard._thread
                raise RuntimeError
        self.assertFalse(thread.is_alive())

    def test_touch_thread_stops_after_timeout(self):
        self.config.mkdir()
        guard = SessionLocks(self.home, timeout=0.1, poll=0.01)
        seen = []
        real = threading.Thread.start

        def capture(thread):
            seen.append(thread)
            real(thread)

        with mock.patch.object(threading.Thread, "start", capture):
            with self.assertRaises(LockTimeout):
                with guard:
                    pass
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].is_alive())

    def test_touch_skips_lock_taken_by_another_owner(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        guard.touch_interval = 0.02
        with guard:
            os.rename(self.main, self.home / "moved")
            self.main.mkdir()
            self.age(self.main, 30)
            time.sleep(0.2)
            self.assertGreater(time.time() - self.main.stat().st_mtime, 25)


def make_fresh_after_rename(case, third_party, record=None):
    real = os.rename

    def wrapped(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if ".stale." in str(dst):
            now = time.time()
            os.utime(dst, (now, now))
            if third_party:
                os.mkdir(src)
                if record is not None:
                    record.append(os.stat(src).st_ino)

    return mock.patch.object(locks.os, "rename", wrapped)


class TakeoverTests(LockCase):
    def run_takeover(self):
        self.main.mkdir()
        original = self.main.stat().st_ino
        self.age(self.main, 120)
        third = []
        with make_fresh_after_rename(self, True, third):
            with self.assertRaises(LockTimeout):
                with SessionLocks(self.home, timeout=0.2, poll=0.01):
                    pass
        self.assertEqual(len(third), 1)
        self.assertNotEqual(original, third[0])
        return original, third[0]

    def test_fresh_set_aside_never_removed_when_third_party_took_path(self):
        original, third = self.run_takeover()
        asides = [p for p in self.home.iterdir() if ".stale." in p.name]
        self.assertEqual(len(asides), 1)
        self.assertEqual(asides[0].stat().st_ino, original)
        self.assertFalse(self.refresh.exists())

    def test_third_party_folder_keeps_its_inode(self):
        original, third = self.run_takeover()
        self.assertEqual(self.main.stat().st_ino, third)
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.1, poll=0.01):
                pass
        self.assertEqual(self.main.stat().st_ino, third)

    def test_noreplace_call_is_loaded(self):
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            self.assertIsNotNone(locks._NOREPLACE)

    def test_unsupported_noreplace_falls_back_to_guarded_rename(self):
        a = self.home / "a"
        b = self.home / "b"
        a.mkdir()
        fake = mock.Mock(return_value=-1)
        with mock.patch.object(locks, "_NOREPLACE", fake):
            for code in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                with mock.patch.object(locks.ctypes, "get_errno", lambda c=code: c):
                    locks._rename_noreplace(a, b)
                    self.assertTrue(b.is_dir())
                    self.assertFalse(a.exists())
                    os.rename(b, a)
            b.mkdir()
            with mock.patch.object(locks.ctypes, "get_errno", lambda: errno.EINVAL):
                with self.assertRaises(FileExistsError):
                    locks._rename_noreplace(a, b)
            with mock.patch.object(locks.ctypes, "get_errno", lambda: errno.EEXIST):
                with self.assertRaises(FileExistsError):
                    locks._rename_noreplace(a, b)

    def test_touch_survives_clock_stepping_back(self):
        real = os.utime
        calls = []

        def stepped_back(path, times=None, **k):
            calls.append(path)
            past = time.time() - 3600
            real(path, (past, past))

        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        guard.touch_interval = 0.02
        with mock.patch.object(locks.os, "utime", stepped_back):
            with guard:
                deadline = time.monotonic() + 3
                while len(calls) < 9 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertGreaterEqual(len(calls), 9)
                self.assertFalse(guard.compromised)

    def test_fresh_set_aside_is_restored_when_path_free(self):
        self.main.mkdir()
        inode = self.main.stat().st_ino
        self.age(self.main, 120)
        with make_fresh_after_rename(self, False):
            with self.assertRaises(LockTimeout):
                with SessionLocks(self.home, timeout=0.2, poll=0.01):
                    pass
        self.assertEqual(self.main.stat().st_ino, inode)
        self.no_leftovers()

    def test_noreplace_rename_refuses_existing_folder(self):
        a = self.home / "a"
        b = self.home / "b"
        a.mkdir()
        b.mkdir()
        ino = b.stat().st_ino
        with self.assertRaises(OSError):
            locks._rename_noreplace(a, b)
        self.assertEqual(b.stat().st_ino, ino)
        self.assertTrue(a.is_dir())


class CompromisedTests(LockCase):
    def test_false_in_normal_use(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        with guard:
            self.assertFalse(guard.compromised)
        self.assertFalse(guard.compromised)

    def test_release_detects_replaced_lock(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        with guard:
            os.rename(self.main, self.home / "moved")
            self.main.mkdir()
        self.assertTrue(guard.compromised)

    def test_release_detects_missing_lock(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        with guard:
            self.main.rmdir()
        self.assertTrue(guard.compromised)

    def test_touch_detects_lost_lock_while_held(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        guard.touch_interval = 0.02
        with guard:
            self.config.rmdir()
            deadline = time.monotonic() + 3
            while not guard.compromised and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(guard.compromised)

    def test_flag_resets_on_next_entry(self):
        guard = SessionLocks(self.home, timeout=1, poll=0.01)
        with guard:
            self.main.rmdir()
        with guard:
            self.assertFalse(guard.compromised)


class WaitLoopTests(LockCase):
    def test_zero_timeout_still_tries_once_then_times_out(self):
        with SessionLocks(self.home, timeout=0, poll=0.01):
            pass
        self.main.mkdir()
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0, poll=0.01):
                pass

    def test_endless_takeover_loop_still_times_out(self):
        guard = SessionLocks(self.home, timeout=0.2, poll=0.01)
        self.main.mkdir()
        with mock.patch.object(SessionLocks, "_break_if_stale", lambda self, path, stale: True):
            started = time.monotonic()
            with self.assertRaises(LockTimeout):
                with guard:
                    pass
            self.assertLess(time.monotonic() - started, 2)


CHILD = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from pathlib import Path\n"
    "from lifeline.locks import SessionLocks\n"
    "g = SessionLocks(Path(sys.argv[2]), timeout=5, poll=0.01)\n"
    "g.__enter__()\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


class CrashTests(LockCase):
    def test_killed_holder_leaves_locks_that_expire(self):
        import signal
        import subprocess
        import sys

        core = str(Path(locks.__file__).resolve().parent.parent)
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, core, str(self.home)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.stdout.close)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        child.send_signal(signal.SIGKILL)
        child.wait()
        self.assertEqual(self.remaining(), [self.refresh, self.main, self.config])
        for p in self.remaining():
            self.assertLess(time.time() - p.stat().st_mtime, 5)
        with self.assertRaises(LockTimeout):
            with SessionLocks(self.home, timeout=0.1, poll=0.01):
                pass
        guard = SessionLocks(self.home, timeout=5, poll=0.02)
        guard._specs = tuple((rel, 0.3) for rel, _ in locks._LOCKS)
        oldest = min(p.stat().st_mtime for p in self.remaining())
        with guard:
            self.assertGreater(time.time(), oldest + 0.3)
            self.assertEqual(len(self.remaining()), 3)
        self.assertEqual(self.remaining(), [])
        self.no_leftovers()



class ClaudeStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.folder = self.home / ".claude"
        self.folder.mkdir()

    def test_the_folder_is_always_the_one_that_goes_with_the_plain_keychain_item(self):
        from lifeline import slot

        self.assertEqual(slot.SERVICE, "Claude Code-credentials")
        for environ in (
            {},
            {"CLAUDE_CONFIG_DIR": str(self.home / "other")},
            {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.home / "secure")},
            {"CLAUDE_CONFIG_DIR": "relative", "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""},
            {"CLAUDE_CONFIG_DIR": ""},
        ):
            with self.subTest(environ=environ), mock.patch.dict(os.environ, environ):
                self.assertEqual(locks.claude_folder(self.home), self.folder)
        decomposed = Path("/home/Rene\u0301")
        self.assertEqual(str(locks.claude_folder(decomposed)), "/home/Ren\u00e9/.claude")

    def test_a_relative_folder_is_never_doubled(self):
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.home)
        with locks.StorageLock(Path(".claude"), timeout=1, poll=0.01):
            self.assertTrue((self.folder / ".storage-write.lock").is_dir())
        self.assertFalse((self.folder / ".claude").exists())

    def test_the_storage_lock_is_claude_codes_own_lock_folder(self):
        path = self.folder / ".storage-write.lock"
        self.assertEqual((locks.STORAGE_STALE, locks.STORAGE_LOCK), (15.0, ".storage-write.lock"))
        guard = locks.StorageLock(self.folder, timeout=1, poll=0.01)
        with guard:
            self.assertTrue(path.is_dir())
            self.assertFalse((self.folder / ".oauth_refresh.lock").exists())
        self.assertFalse(path.exists())

    def test_a_storage_lock_claude_code_holds_makes_lifeline_wait_then_give_up(self):
        path = self.folder / ".storage-write.lock"
        path.mkdir()
        old = time.time() - 10
        os.utime(path, (old, old))
        started = time.monotonic()
        with self.assertRaises(LockTimeout):
            with locks.StorageLock(self.folder, timeout=0.3, poll=0.01):
                self.fail("the lock was taken while Claude Code held it")
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(path.is_dir())

    def test_a_storage_lock_older_than_15_seconds_is_stale(self):
        path = self.folder / ".storage-write.lock"
        path.mkdir()
        old = time.time() - 16
        os.utime(path, (old, old))
        with locks.StorageLock(self.folder, timeout=1, poll=0.01):
            self.assertLess(time.time() - path.stat().st_mtime, 5)
        self.assertFalse(path.exists())

    def test_a_long_hold_keeps_the_storage_lock_fresh(self):
        path = self.folder / ".storage-write.lock"
        guard = locks.StorageLock(self.folder, timeout=1, poll=0.01)
        self.assertLess(guard.touch_interval, locks.STORAGE_STALE / 2)
        guard.touch_interval = 0.05
        with guard:
            first = path.stat().st_mtime_ns
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and path.stat().st_mtime_ns == first:
                time.sleep(0.02)
            self.assertGreater(path.stat().st_mtime_ns, first)

    def test_a_missing_folder_is_not_made(self):
        missing = self.home / "missing"
        with self.assertRaises(FileNotFoundError):
            with locks.StorageLock(missing, timeout=0.2, poll=0.01):
                pass
        self.assertFalse(missing.exists())

    def test_the_nudge_only_moves_the_sign_in_files_time(self):
        path = self.folder / ".credentials.json"
        path.write_bytes(b"left alone")
        os.chmod(path, 0o600)
        old = time.time() - 86400
        os.utime(path, (old, old))
        before = path.stat()
        with mock.patch("builtins.open", side_effect=AssertionError("opened")), mock.patch.object(locks.os, "open", side_effect=AssertionError("opened")):
            self.assertEqual(locks.nudge_sessions(self.folder), "moved")
        after = path.stat()
        self.assertEqual(path.read_bytes(), b"left alone")
        self.assertEqual((after.st_ino, after.st_mode, after.st_size), (before.st_ino, before.st_mode, before.st_size))
        self.assertLess(time.time() - after.st_mtime, 5)

    def test_the_nudge_never_makes_a_file(self):
        self.assertEqual(locks.nudge_sessions(self.folder), "absent")
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(locks.nudge_sessions(self.home / "missing"), "absent")
        self.assertFalse((self.home / "missing").exists())

    def test_a_nudge_that_fails_says_so(self):
        path = self.folder / ".credentials.json"
        path.write_bytes(b"x")
        with mock.patch.object(locks.os, "utime", side_effect=PermissionError()):
            self.assertEqual(locks.nudge_sessions(self.folder), "failed")


if __name__ == "__main__":
    unittest.main()
