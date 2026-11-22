import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lifeline import writelock
from lifeline.writelock import WriteLockTimeout, default_lock_path, held

CORE = Path(__file__).resolve().parents[1]

HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from lifeline.writelock import held\n"
    "with held(Path(sys.argv[1]), timeout=5):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.readline()\n"
)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class WriteLockCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "data" / "lifeline.lock"

    def holder(self):
        env = dict(os.environ, PYTHONPATH=str(CORE))
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )

        def stop():
            if child.poll() is None:
                child.kill()
            child.wait(5)
            child.stdin.close()
            child.stdout.close()

        self.addCleanup(stop)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def release(self, child):
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertEqual(child.wait(5), 0)


class ExclusionTests(WriteLockCase):
    def test_second_process_cannot_hold_it(self):
        child = self.holder()
        started = time.monotonic()
        with self.assertRaises(WriteLockTimeout):
            with held(self.path, timeout=0.3):
                self.fail("both processes held the lock")
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.release(child)
        with held(self.path, timeout=1):
            pass

    def test_waits_then_takes_it_when_the_other_process_lets_go(self):
        child = self.holder()
        timer = threading.Timer(0.3, self.release, args=(child,))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with held(self.path, timeout=5):
            waited = time.monotonic() - started
        timer.join()
        self.assertGreaterEqual(waited, 0.25)

    def test_freed_when_the_holding_process_dies(self):
        child = self.holder()
        child.kill()
        child.wait(5)
        with held(self.path, timeout=1):
            pass

    def test_two_threads_in_one_process_exclude_each_other(self):
        entered = threading.Event()
        leave = threading.Event()

        def hold():
            with held(self.path, timeout=1):
                entered.set()
                leave.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(WriteLockTimeout):
                with held(self.path, timeout=0.2):
                    self.fail("two threads held the lock")
        finally:
            leave.set()
            worker.join(5)
        with held(self.path, timeout=1):
            pass

    def test_nested_use_in_one_thread_does_not_reenter(self):
        with held(self.path, timeout=1):
            with self.assertRaises(WriteLockTimeout):
                with held(self.path, timeout=0.2):
                    self.fail("the lock was taken twice")

    def test_released_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with held(self.path, timeout=1):
                raise ValueError("boom")
        child = self.holder()
        self.release(child)
        with held(self.path, timeout=0.2):
            pass

    def test_timeout_is_a_plain_sentence(self):
        child = self.holder()
        with self.assertRaises(WriteLockTimeout) as caught:
            with held(self.path, timeout=0.1):
                pass
        message = str(caught.exception)
        self.assertTrue(message.endswith("."))
        self.assertIn("Lifeline", message)
        self.assertNotIn(str(self.path), message)
        self.assertIsNone(caught.exception.__context__)
        self.release(child)

    def test_yields_the_lock_path(self):
        with held(self.path, timeout=1) as where:
            self.assertEqual(where, self.path)


class FileTests(WriteLockCase):
    def test_file_is_600_and_new_folder_is_700(self):
        with held(self.path, timeout=1):
            self.assertEqual(mode(self.path), 0o600)
            self.assertEqual(mode(self.path.parent), 0o700)

    def test_loose_file_mode_is_tightened(self):
        self.path.parent.mkdir()
        self.path.write_text("")
        os.chmod(self.path, 0o644)
        with held(self.path, timeout=1):
            self.assertEqual(mode(self.path), 0o600)

    def test_existing_caller_folder_is_left_alone(self):
        self.path.parent.mkdir(mode=0o755)
        os.chmod(self.path.parent, 0o755)
        with held(self.path, timeout=1):
            pass
        self.assertEqual(mode(self.path.parent), 0o755)

    def test_refuses_a_linked_lock_file(self):
        self.path.parent.mkdir()
        target = self.root / "elsewhere"
        target.write_text("keep")
        os.chmod(target, 0o644)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            with held(self.path, timeout=1):
                self.fail("followed a link")
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(mode(target), 0o644)

    def test_file_stays_after_release(self):
        with held(self.path, timeout=1):
            pass
        self.assertTrue(self.path.is_file())


class DefaultPathTests(WriteLockCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mac_path(self):
        with mock.patch.object(writelock.ident.sys, "platform", "darwin"):
            self.assertEqual(
                default_lock_path(),
                self.home / "Library" / "Application Support" / "com.example.lifeline" / "lifeline.lock",
            )

    def test_linux_path(self):
        with mock.patch.object(writelock.ident.sys, "platform", "linux"):
            self.assertEqual(
                default_lock_path(), self.home / ".local" / "share" / "com.example.lifeline" / "lifeline.lock"
            )

    def test_default_folder_is_made_700_even_when_it_already_existed(self):
        with mock.patch.object(writelock.ident.sys, "platform", "linux"):
            folder = default_lock_path().parent
            folder.mkdir(parents=True)
            os.chmod(folder, 0o755)
            with held(timeout=1) as where:
                self.assertEqual(where, default_lock_path())
                self.assertEqual(mode(where), 0o600)
            self.assertEqual(mode(folder), 0o700)


if __name__ == "__main__":
    unittest.main()
