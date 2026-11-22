import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import ledger, recovery, switch, writelock
from lifeline.store import FileStore, StoreError, StoreLeftover

WORK = "22222222-bbbb-4bbb-8bbb-000000000002"
ORG = "44444444-dddd-4ddd-8ddd-000000000004"


def pair(tag, expires=2000000000000):
    return {
        "accessToken": f"access-{tag}-SECRET",
        "refreshToken": f"refresh-{tag}-SECRET",
        "expiresAt": expires,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }


def ended(tag, expires=1000, days=-1):
    end = int((datetime.now(timezone.utc).timestamp() + days * 86400) * 1000)
    return dict(pair(tag, expires), refreshTokenExpiresAt=end)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Crash(BaseException):
    pass


HOLD_LOCK = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from lifeline import writelock\n"
    "with writelock.held(sys.argv[2]):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.readline()\n"
)


class FlakyStore:
    def __init__(self, inner):
        self.inner = inner
        self.put_errors = []
        self.lost_puts = 0
        self.get_errors = 0
        self.puts = []

    def get(self, uuid):
        if self.get_errors:
            self.get_errors -= 1
            raise StoreError("The parked sign-in could not be read.")
        return self.inner.get(uuid)

    def put(self, uuid, oauth):
        self.puts.append(oauth.get("accessToken"))
        if self.put_errors:
            error = self.put_errors.pop(0)
            if isinstance(error, StoreLeftover):
                self.inner.put(uuid, oauth)
            raise error
        if self.lost_puts:
            self.lost_puts -= 1
            return
        self.inner.put(uuid, oauth)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = recovery.folder_for(self.root / "lifeline" / "lifeline.lock")
        self.store = FlakyStore(FileStore(self.root / "signins"))

    def final(self, uuid=WORK):
        return self.folder / f"{uuid}.json"

    def leftover(self, name, content, uuid=WORK):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.folder / f".{uuid}.{name}.tmp"
        path.write_text(content)
        return path

    def test_folder_for_a_lock_path_and_the_default(self):
        self.assertEqual(recovery.folder_for(self.root / "x" / "lifeline.lock"), self.root / "x" / "recovery")
        self.assertEqual(recovery.folder_for(None), writelock.lifeline_folder() / "recovery")
        self.assertEqual(recovery.folder_for(), writelock.lifeline_folder() / "recovery")

    def test_write_is_private_atomic_and_holds_only_the_signin(self):
        self.folder.parent.mkdir()
        self.folder.mkdir(mode=0o755)
        os.chmod(self.folder, 0o755)
        recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(json.loads(self.final().read_text()), {"claudeAiOauth": pair("a")})
        self.assertEqual(mode(self.folder), 0o700)
        self.assertEqual(mode(self.final()), 0o600)
        recovery.write(self.folder, WORK, pair("b"))
        self.assertEqual(json.loads(self.final().read_text()), {"claudeAiOauth": pair("b")})
        self.assertEqual(sorted(os.listdir(self.folder)), [f"{WORK}.json"])

    def test_write_creates_missing_folders(self):
        recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(mode(self.folder), 0o700)
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a"))

    def test_write_refuses_bad_ids_and_incomplete_signins(self):
        for bad in ("../x", "a/b", "", None, "x.json", "x y"):
            with self.subTest(uuid=bad):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.write(self.folder, bad, pair("a"))
        for oauth in ({}, {"accessToken": "a"}, {"accessToken": "", "refreshToken": "r"}, None, ["x"]):
            with self.subTest(oauth=oauth):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.write(self.folder, WORK, oauth)
        self.assertFalse(self.folder.exists())

    def test_write_failure_raises_without_secrets_and_leaves_nothing(self):
        self.folder.parent.mkdir()
        self.folder.write_text("a file where the folder should be")
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.write(self.folder, WORK, pair("a"))
        text = str(caught.exception) + repr(caught.exception.args)
        self.assertNotIn("SECRET", text)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__context__ is None or caught.exception.__suppress_context__)
        self.folder.unlink()
        self.folder.mkdir(mode=0o700)
        with mock.patch.object(recovery.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(recovery.RecoveryError):
                recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(os.listdir(self.folder), [])

    def test_write_refuses_values_that_are_not_json(self):
        broken = dict(pair("a"), expiresAt=float("nan"))
        with self.assertRaises(recovery.RecoveryError):
            recovery.write(self.folder, WORK, broken)
        self.assertFalse(self.final().exists())

    def test_writable_probe_leaves_nothing_behind(self):
        self.assertTrue(recovery.writable(self.folder))
        self.assertEqual(os.listdir(self.folder), [])
        self.assertEqual(mode(self.folder), 0o700)

    def test_writable_is_false_when_the_folder_cannot_be_written(self):
        self.folder.parent.mkdir()
        self.folder.write_text("a file where the folder should be")
        self.assertFalse(recovery.writable(self.folder))
        self.folder.unlink()
        self.folder.mkdir()
        with mock.patch.object(recovery.os, "fsync", side_effect=OSError("read only")):
            self.assertFalse(recovery.writable(self.folder))
        self.assertEqual(os.listdir(self.folder), [])

    def test_accounts_lists_final_files_and_leftovers_only(self):
        recovery.write(self.folder, WORK, pair("a"))
        self.leftover("abc_12", "{}", uuid=ORG)
        for junk in ("notes.txt", "._probe-x1.tmp", ".bad.id.x.tmp.bak", "with space.json", "x.json.tmp"):
            (self.folder / junk).write_text("x")
        self.assertEqual(recovery.accounts(self.folder), sorted([WORK, ORG]))
        self.assertEqual(recovery.accounts(self.root / "missing"), [])

    def test_pending_returns_the_pair_or_none(self):
        self.assertIsNone(recovery.pending(self.folder, WORK))
        recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a"))
        self.assertIsNone(recovery.pending(self.folder, ORG))
        self.assertIsNone(recovery.pending(self.folder, "../etc"))

    def test_pending_finishes_the_newest_complete_leftover(self):
        older = self.leftover("aaa111", json.dumps({"claudeAiOauth": pair("old", expires=1000)}))
        newest = self.leftover("bbb222", json.dumps({"claudeAiOauth": pair("new", expires=3000)}))
        middle = self.leftover("ccc333", json.dumps({"claudeAiOauth": pair("mid", expires=2000)}))
        partial = self.leftover("ddd444", '{"claudeAiOauth": {"accessToken": "acc')
        self.assertEqual(recovery.pending(self.folder, WORK), pair("new", expires=3000))
        self.assertEqual(json.loads(self.final().read_text()), {"claudeAiOauth": pair("new", expires=3000)})
        self.assertFalse(newest.exists())
        self.assertTrue(older.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(partial.exists())
        self.assertEqual(recovery.pending(self.folder, WORK), pair("new", expires=3000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("new", expires=3000))
        self.assertFalse(self.final().exists())
        self.assertTrue(older.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(partial.exists())

    def test_pending_replaces_a_damaged_file_with_a_complete_leftover(self):
        self.folder.mkdir(parents=True)
        self.final().write_text("{broken")
        stray = self.leftover("aaa111", json.dumps({"claudeAiOauth": pair("x")}))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("x"))
        self.assertFalse(stray.exists())
        self.assertFalse(recovery.damaged(self.folder, WORK))
        self.assertEqual(json.loads(self.final().read_text()), {"claudeAiOauth": pair("x")})

    def test_pending_leaves_a_damaged_file_with_no_complete_leftover(self):
        self.folder.mkdir(parents=True)
        self.final().write_text("{broken")
        partial = self.leftover("aaa111", '{"claudeAiOauth": {"accessToken": "a')
        self.assertIsNone(recovery.pending(self.folder, WORK))
        self.assertTrue(partial.exists())
        self.assertTrue(recovery.damaged(self.folder, WORK))
        self.assertEqual(self.final().read_text(), "{broken")

    def test_pending_ignores_leftovers_of_other_accounts_and_incomplete_ones(self):
        self.leftover("aaa111", json.dumps({"claudeAiOauth": pair("x")}), uuid=ORG)
        self.leftover("bbb222", json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        self.assertIsNone(recovery.pending(self.folder, WORK))
        self.assertFalse(self.final().exists())

    def test_links_and_damage(self):
        self.assertFalse(recovery.damaged(self.folder, WORK))
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"claudeAiOauth": pair("x")}))
        self.folder.mkdir(parents=True)
        os.symlink(target, self.final())
        self.assertIsNone(recovery.pending(self.folder, WORK))
        self.assertTrue(recovery.damaged(self.folder, WORK))
        self.final().unlink()
        self.final().write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        self.assertTrue(recovery.damaged(self.folder, WORK))
        recovery.write(self.folder, WORK, pair("a"))
        self.assertFalse(recovery.damaged(self.folder, WORK))

    def test_store_pair_follows_keep_the_newer(self):
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("a", 2000)), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("a", 2000))
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("a", 2000)), recovery.HELD)
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("old", 1000)), recovery.SUPERSEDED)
        self.assertEqual(self.store.inner.get(WORK), pair("a", 2000))
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("b", 3000)), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("b", 3000))
        self.store.inner.put(WORK, {"accessToken": "", "refreshToken": "r", "expiresAt": 9000})
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("c", 4000)), recovery.SAVED)

    def test_store_pair_saves_over_a_sign_in_with_no_expiry(self):
        self.store.inner.put(WORK, {"accessToken": "a", "refreshToken": "r"})
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("c", 4000)), recovery.SAVED)

    def test_store_pair_pending_when_the_store_fails_or_does_not_hold_it(self):
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("a")), recovery.PENDING)
        self.store.lost_puts = 1
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("a")), recovery.PENDING)
        self.store.get_errors = 1
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("a")), recovery.PENDING)
        self.assertIsNone(self.store.inner.get(WORK))
        self.store.put_errors = [StoreLeftover("The new sign-in is saved, but an old copy remains.")]
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("a")), recovery.SAVED)

    def test_remove_deletes_only_the_expected_pair(self):
        self.assertTrue(recovery.remove(self.folder, WORK, pair("a")))
        recovery.write(self.folder, WORK, pair("a"))
        self.assertFalse(recovery.remove(self.folder, WORK, pair("b")))
        self.assertTrue(self.final().exists())
        self.assertTrue(recovery.remove(self.folder, WORK, pair("a")))
        self.assertFalse(self.final().exists())
        self.assertFalse(recovery.remove(self.folder, "../x", pair("a")))

    def test_settle_saves_and_then_removes(self):
        recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("a"))
        self.assertFalse(self.final().exists())
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.ABSENT)

    def test_settle_keeps_the_file_until_the_store_reads_it_back(self):
        recovery.write(self.folder, WORK, pair("a"))
        self.store.lost_puts = 1
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.PENDING)
        self.assertTrue(self.final().exists())
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        self.assertFalse(recovery.import_into(self.store, self.folder, WORK))
        self.assertTrue(self.final().exists())
        self.assertTrue(recovery.import_into(self.store, self.folder, WORK))
        self.assertFalse(self.final().exists())

    def test_settle_keeps_a_file_when_the_store_holds_a_later_signin(self):
        self.store.inner.put(WORK, pair("newer", 5000))
        recovery.write(self.folder, WORK, pair("a", 2000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertEqual(self.store.inner.get(WORK), pair("newer", 5000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a", 2000))
        self.store.inner.put(WORK, pair("older", 1000))
        recovery.write(self.folder, WORK, pair("a", 2000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("a", 2000))

    def test_settle_when_the_store_already_holds_it(self):
        self.store.inner.put(WORK, pair("a"))
        recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.HELD)
        self.assertEqual(self.store.puts, [])
        self.assertFalse(self.final().exists())

    def test_settle_finishes_a_leftover_from_a_crash(self):
        self.leftover("zz9", json.dumps({"claudeAiOauth": pair("a")}))
        self.assertEqual(recovery.accounts(self.folder), [WORK])
        self.assertTrue(recovery.import_into(self.store, self.folder, WORK))
        self.assertEqual(self.store.inner.get(WORK), pair("a"))
        self.assertEqual(os.listdir(self.folder), [])

    def test_settle_leaves_a_damaged_file(self):
        self.folder.mkdir(parents=True)
        self.final().write_text("{broken")
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.ABSENT)
        self.assertTrue(self.final().exists())
        self.assertEqual(self.store.puts, [])

    def test_fingerprint_follows_the_switch_rule(self):
        self.assertEqual(recovery.fingerprint(pair("a")), switch.fingerprint(pair("a")))
        self.assertIsNone(recovery.fingerprint({"accessToken": "a"}))
        self.assertIsNone(recovery.fingerprint(None))

    def test_write_records_the_spent_fingerprint(self):
        recovery.write(self.folder, WORK, pair("new", 1000), spent=pair("old", 2000))
        self.assertEqual(
            json.loads(self.final().read_text()),
            {"claudeAiOauth": pair("new", 1000), "spent": switch.fingerprint(pair("old", 2000))},
        )
        recovery.write(self.folder, WORK, pair("new", 1000), spent=switch.fingerprint(pair("x")))
        self.assertEqual(json.loads(self.final().read_text())["spent"], switch.fingerprint(pair("x")))
        recovery.write(self.folder, WORK, pair("new", 1000), spent={"accessToken": "a"})
        self.assertEqual(json.loads(self.final().read_text()), {"claudeAiOauth": pair("new", 1000)})
        before = self.final().read_text()
        for bad in ("refresh-old-SECRET", 7, ["x"], "A" * 64):
            with self.subTest(spent=bad):
                with self.assertRaises(recovery.RecoveryError) as caught:
                    recovery.write(self.folder, WORK, pair("other"), spent=bad)
                self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.final().read_text(), before)

    def test_store_pair_puts_a_shorter_renewal_over_the_pair_it_spent(self):
        old = pair("old", 7200)
        new = pair("new", 3600)
        self.store.inner.put(WORK, old)
        self.assertEqual(recovery.store_pair(self.store, WORK, new), recovery.SUPERSEDED)
        self.assertEqual(recovery.store_pair(self.store, WORK, new, spent=old), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), new)
        self.store.inner.put(WORK, old)
        self.assertEqual(recovery.store_pair(self.store, WORK, new, spent=switch.fingerprint(old)), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), new)
        self.store.inner.put(WORK, pair("other", 9000))
        self.assertEqual(recovery.store_pair(self.store, WORK, new, spent=old), recovery.SUPERSEDED)
        self.assertTrue(recovery.newer(old, new))
        self.assertFalse(recovery.newer(old, new, spent=old))

    def test_settle_saves_a_shorter_renewal_over_the_pair_it_spent(self):
        old = pair("old", 7200)
        new = pair("new", 3600)
        self.store.inner.put(WORK, old)
        recovery.write(self.folder, WORK, new, spent=old)
        self.assertEqual(recovery.pending(self.folder, WORK), new)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), new)
        self.assertFalse(self.final().exists())
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (new, False))

    def test_choose_offers_a_shorter_renewal_when_the_store_refuses_it(self):
        old = pair("old", 7200)
        new = pair("new", 3600)
        self.store.inner.put(WORK, old)
        recovery.write(self.folder, WORK, new, spent=old)
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (new, True))
        self.assertTrue(self.final().exists())
        self.assertEqual(self.store.inner.get(WORK), old)

    def test_choose_n1_newer_store_wins_when_the_spent_file_cannot_be_deleted(self):
        recovery.write(self.folder, WORK, pair("spent", 1000))
        self.store.inner.put(WORK, pair("working", 5000))
        os.chmod(self.folder, 0o500)
        self.addCleanup(os.chmod, self.folder, 0o700)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual(choice.pair, pair("working", 5000))
        self.assertFalse(choice.from_file)
        self.assertFalse(choice.damaged)
        self.assertEqual(self.store.inner.get(WORK), pair("working", 5000))

    def test_choose_n2_refuses_when_the_store_cannot_be_read(self):
        recovery.write(self.folder, WORK, pair("spent", 1000))
        self.store.inner.put(WORK, pair("working", 5000))
        self.store.get_errors = 2
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.choose(self.store, self.folder, WORK)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(caught.exception.__context__ is None or caught.exception.__suppress_context__)
        self.assertEqual(self.store.inner.get(WORK), pair("working", 5000))
        self.assertTrue(self.final().exists())
        self.assertEqual(self.store.puts, [])

    def test_choose_n3_offers_the_file_pair_and_never_removes_it(self):
        self.store.inner.put(WORK, pair("spent", 1000))
        recovery.write(self.folder, WORK, pair("renewed", 5000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual(choice.pair, pair("renewed", 5000))
        self.assertTrue(choice.from_file)
        self.assertTrue(self.final().exists())
        self.assertEqual(self.store.inner.get(WORK), pair("spent", 1000))
        self.assertTrue(recovery.remove(self.folder, WORK, choice.pair))
        self.assertFalse(self.final().exists())

    def test_choose_with_an_empty_store(self):
        recovery.write(self.folder, WORK, pair("renewed", 5000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (pair("renewed", 5000), True))
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (pair("renewed", 5000), False))
        self.assertEqual(self.store.inner.get(WORK), pair("renewed", 5000))
        self.assertFalse(self.final().exists())

    def test_choose_without_a_file(self):
        self.assertEqual(recovery.choose(self.store, self.folder, WORK), recovery.Choice(None, False, False))
        self.store.inner.put(WORK, pair("a"))
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file, choice.damaged), (pair("a"), False, False))

    def test_a_newer_copy_with_the_same_renewal_key_keeps_no_spare_of_the_old_one(self):
        self.store.inner.put(WORK, pair("a", 1000))
        newer = dict(pair("a", 2000), accessToken="access-a2-SECRET")
        self.assertEqual(recovery.store_pair(self.store, WORK, newer, folder=self.folder), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), newer)
        self.assertEqual(sorted(self.folder.iterdir()) if self.folder.exists() else [], [])

    def test_choose_says_whether_proof_picked_the_pair(self):
        base = self.folder.parent
        self.assertFalse(recovery.choose(self.store, self.folder, WORK).proven)
        self.store.inner.put(WORK, pair("only", 5000))
        self.assertTrue(recovery.choose(self.store, self.folder, WORK).proven)
        recovery.set_aside(self.folder, WORK, pair("kept", 4000))
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual(choice.pair, pair("only", 5000))
        self.assertFalse(choice.proven)
        ledger.add(base, WORK, pair("kept", 4000))
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual(choice.pair, pair("only", 5000))
        self.assertTrue(choice.proven)
        recovery.set_aside(self.folder, WORK, ended("over"))
        self.assertTrue(recovery.choose(self.store, self.folder, WORK).proven)
        recovery.set_aside(self.folder, WORK, pair("newer", 6000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file, choice.proven), (pair("newer", 6000), True, False))
        ledger.add(base, WORK, pair("only", 5000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file, choice.proven), (pair("newer", 6000), True, True))

    def test_choose_reports_a_damaged_file(self):
        self.store.inner.put(WORK, pair("a"))
        self.folder.mkdir(parents=True)
        self.final().write_text("{broken")
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file, choice.damaged, choice.proven), (pair("a"), False, True, False))
        self.assertTrue(self.final().exists())

    def test_choose_and_settle_prefer_the_file_over_a_store_signin_of_unknown_age(self):
        other = dict(pair("other"), expiresAt=None)
        self.store.inner.put(WORK, other)
        recovery.write(self.folder, WORK, pair("renewed", 5000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (pair("renewed", 5000), True))
        self.assertTrue(self.final().exists())
        self.assertEqual(self.store.inner.get(WORK), other)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("renewed", 5000))
        self.assertFalse(self.final().exists())

    def test_a_file_of_unknown_age_is_still_preferred_over_a_dated_store_signin(self):
        self.store.inner.put(WORK, pair("dated", 5000))
        undated = dict(pair("undated"), expiresAt=None)
        recovery.write(self.folder, WORK, undated)
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (undated, True))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), undated)
        self.assertFalse(self.final().exists())

    def test_choose_offers_the_file_over_an_unusable_store_value(self):
        self.store.inner.put(WORK, {"accessToken": "", "refreshToken": "r", "expiresAt": 9000})
        recovery.write(self.folder, WORK, pair("renewed", 5000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (pair("renewed", 5000), True))

    def rescued(self, uuid=WORK):
        if not self.folder.exists():
            return []
        return sorted(p for p in self.folder.iterdir() if p.name.startswith(f"{uuid}.rescue.") and p.name.endswith(".json"))

    def test_rescue_writes_a_new_private_file_every_time(self):
        first = recovery.rescue(self.folder, WORK, pair("a"))
        second = recovery.rescue(self.folder, WORK, pair("a"), spent=pair("old"))
        self.assertNotEqual(first, second)
        self.assertEqual(self.rescued(), sorted([first, second]))
        self.assertEqual(json.loads(first.read_text()), {"claudeAiOauth": pair("a")})
        self.assertEqual(json.loads(second.read_text()), {"claudeAiOauth": pair("a"), "spent": switch.fingerprint(pair("old"))})
        self.assertEqual(mode(self.folder), 0o700)
        self.assertEqual((mode(first), mode(second)), (0o600, 0o600))
        self.assertEqual(sorted(os.listdir(self.folder)), sorted([first.name, second.name]))
        self.assertEqual(recovery.accounts(self.folder), [WORK])
        self.assertFalse(self.final().exists())

    def test_rescue_never_overwrites_a_file(self):
        self.folder.mkdir(parents=True)
        taken = self.folder / f"{WORK}.rescue.{'0' * 16}.json"
        taken.write_text("keep me")
        with mock.patch.object(recovery.secrets, "token_hex", side_effect=["0" * 16, "1" * 16]):
            placed = recovery.rescue(self.folder, WORK, pair("a"))
        self.assertEqual(placed.name, f"{WORK}.rescue.{'1' * 16}.json")
        self.assertEqual(taken.read_text(), "keep me")
        self.assertEqual(json.loads(placed.read_text()), {"claudeAiOauth": pair("a")})

    def test_rescue_refuses_bad_input_and_fails_with_fixed_text(self):
        for bad in ("../x", "", None, "x.json"):
            with self.subTest(uuid=bad):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.rescue(self.folder, bad, pair("a"))
        for oauth in ({}, {"accessToken": "a"}, None):
            with self.subTest(oauth=oauth):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.rescue(self.folder, WORK, oauth)
        with self.assertRaises(recovery.RecoveryError):
            recovery.rescue(self.folder, WORK, pair("a"), spent="refresh-old-SECRET")
        self.assertFalse(self.folder.exists())
        self.folder.parent.mkdir()
        self.folder.write_text("a file where the folder should be")
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.rescue(self.folder, WORK, pair("a"))
        self.assertNotIn("SECRET", str(caught.exception) + repr(caught.exception.args))
        self.assertTrue(caught.exception.__context__ is None or caught.exception.__suppress_context__)
        self.folder.unlink()
        self.folder.mkdir(mode=0o700)
        with mock.patch.object(recovery.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(recovery.RecoveryError):
                recovery.rescue(self.folder, WORK, pair("a"))
        self.assertEqual(os.listdir(self.folder), [])

    def test_rescue_keeps_a_complete_copy_when_the_last_step_fails(self):
        with mock.patch.object(recovery.os, "link", side_effect=PermissionError("not allowed")):
            with self.assertRaises(recovery.RecoveryError):
                recovery.rescue(self.folder, WORK, pair("a"))
        self.assertEqual(self.rescued(), [])
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(os.listdir(self.folder), [])

    def test_rescue_needs_no_write_lock(self):
        lock = self.folder.parent / "lifeline.lock"
        core = Path(recovery.__file__).resolve().parents[1]
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLD_LOCK, str(core), str(lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        fd = os.open(lock, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        done = []
        worker = threading.Thread(target=lambda: done.append(recovery.rescue(self.folder, WORK, pair("a"))))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(done), 1)
        self.assertEqual(json.loads(done[0].read_text()), {"claudeAiOauth": pair("a")})
        self.assertIsNone(holder.poll())
        holder.stdin.write("\n")
        holder.stdin.close()
        self.assertEqual(holder.wait(10), 0)
        with writelock.held(lock):
            self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("a"))
        self.assertEqual(os.listdir(self.folder), [])

    def test_two_rescues_of_different_pairs_newest_wins(self):
        recovery.rescue(self.folder, WORK, pair("x", 3000))
        recovery.rescue(self.folder, WORK, pair("y", 2000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("x", 3000))
        self.assertEqual(len(self.rescued()), 2)
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.PENDING)
        self.assertEqual(len(self.rescued()), 2)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("x", 3000))
        self.assertEqual(self.set_aside(), [pair("y", 2000)])

    def test_two_rescues_the_renewal_wins_over_the_pair_it_spent(self):
        older = pair("x", 7200)
        renewal = pair("y", 3600)
        for order in ((older, None, renewal, older), (renewal, older, older, None)):
            with self.subTest(first=order[0]["accessToken"]):
                if self.store.inner.get(WORK) is not None:
                    self.store.inner.remove(WORK)
                recovery.rescue(self.folder, WORK, order[0], spent=order[1])
                recovery.rescue(self.folder, WORK, order[2], spent=order[3])
                self.assertEqual(recovery.pending(self.folder, WORK), renewal)
                self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
                choice = recovery.choose(self.store, self.folder, WORK)
                self.assertEqual((choice.pair, choice.from_file), (renewal, True))
                self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
                self.assertEqual(self.store.inner.get(WORK), renewal)
                self.assertEqual(os.listdir(self.folder), [])

    def test_rescue_beside_a_newer_final_file(self):
        recovery.write(self.folder, WORK, pair("final", 5000))
        recovery.rescue(self.folder, WORK, pair("rescued", 3000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("final", 5000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("final", 5000))
        self.assertEqual(self.set_aside(), [pair("rescued", 3000)])

    def test_rescue_beside_an_older_final_file(self):
        recovery.write(self.folder, WORK, pair("final", 1000))
        recovery.rescue(self.folder, WORK, pair("rescued", 3000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("rescued", 3000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("rescued", 3000))
        self.assertEqual(self.set_aside(), [pair("final", 1000)])

    def test_rescue_beside_a_spent_final_file(self):
        first = pair("first", 9000)
        spent = pair("final", 7200)
        renewal = pair("rescued", 3600)
        self.store.inner.put(WORK, first)
        recovery.write(self.folder, WORK, spent, spent=first)
        recovery.rescue(self.folder, WORK, renewal, spent=spent)
        self.assertEqual(recovery.pending(self.folder, WORK), renewal)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), renewal)
        self.assertEqual(os.listdir(self.folder), [])

    def test_spent_rescue_beside_the_final_file_that_renewed_it(self):
        spent = pair("rescued", 7200)
        renewal = pair("final", 3600)
        recovery.rescue(self.folder, WORK, spent)
        recovery.write(self.folder, WORK, renewal, spent=spent)
        self.store.inner.put(WORK, spent)
        self.assertEqual(recovery.pending(self.folder, WORK), renewal)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), renewal)
        self.assertEqual(os.listdir(self.folder), [])

    def test_rescue_file_deleted_only_once_the_store_holds_it(self):
        placed = recovery.rescue(self.folder, WORK, pair("a", 3000))
        self.store.lost_puts = 1
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.PENDING)
        self.assertTrue(placed.exists())
        self.store.get_errors = 1
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.PENDING)
        self.assertTrue(placed.exists())
        self.store.inner.put(WORK, pair("a", 3000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.HELD)
        self.assertFalse(placed.exists())
        placed = recovery.rescue(self.folder, WORK, pair("b", 2000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertTrue(placed.exists())
        self.assertEqual(self.store.inner.get(WORK), pair("a", 3000))

    def test_a_spent_record_is_kept_while_its_spent_pair_is_still_on_disk(self):
        first = pair("first", 9000)
        spent = pair("final", 7200)
        renewal = pair("rescued", 3600)
        self.store.inner.put(WORK, first)
        recovery.write(self.folder, WORK, spent, spent=first)
        placed = recovery.rescue(self.folder, WORK, renewal, spent=spent)
        real_unlink = os.unlink

        def stuck(path, *args, **kwargs):
            if Path(path) == self.final():
                raise PermissionError("not allowed")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(recovery.os, "unlink", stuck):
            self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.PENDING)
        self.assertEqual(self.store.inner.get(WORK), renewal)
        self.assertTrue(self.final().exists())
        self.assertTrue(placed.exists())
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (renewal, False))
        self.assertEqual(self.store.inner.get(WORK), renewal)
        self.assertEqual(os.listdir(self.folder), [])

    def test_remove_clears_what_the_loaded_pair_holds_or_supersedes(self):
        spent = pair("final", 7200)
        renewal = pair("rescued", 3600)
        recovery.write(self.folder, WORK, spent)
        placed = recovery.rescue(self.folder, WORK, renewal, spent=spent)
        self.assertFalse(recovery.remove(self.folder, WORK, spent))
        self.assertFalse(self.final().exists())
        self.assertTrue(placed.exists())
        recovery.write(self.folder, WORK, spent)
        self.assertTrue(recovery.remove(self.folder, WORK, renewal))
        self.assertEqual(os.listdir(self.folder), [])

    def test_crash_mid_rescue_leaves_an_incomplete_temp_that_is_ignored(self):
        self.folder.mkdir(parents=True)
        partial = self.folder / f".{WORK}.rescue.abc123de.tmp"
        partial.write_text('{"claudeAiOauth": {"accessToken": "acc')
        self.assertEqual(recovery.accounts(self.folder), [WORK])
        self.assertIsNone(recovery.pending(self.folder, WORK))
        self.assertFalse(recovery.damaged(self.folder, WORK))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.ABSENT)
        self.assertTrue(partial.exists())
        recovery.rescue(self.folder, WORK, pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(os.listdir(self.folder), [partial.name])

    def test_crash_mid_rescue_leaves_a_complete_temp_that_is_recovered(self):
        with mock.patch.object(recovery.os, "link", side_effect=Crash()):
            with self.assertRaises(Crash):
                recovery.rescue(self.folder, WORK, pair("a"), spent=pair("old"))
        temps = [name for name in os.listdir(self.folder) if name.endswith(".tmp")]
        self.assertEqual(len(temps), 1)
        self.assertEqual(self.rescued(), [])
        self.store.inner.put(WORK, dict(pair("old"), expiresAt=9000000000000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("a"))
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_rescue_still_being_written_is_left_alone(self):
        self.folder.mkdir(parents=True, mode=0o700)
        busy = self.folder / f".{WORK}.rescue.abc123de.tmp"
        busy.write_text(json.dumps({"claudeAiOauth": pair("a")}))
        fd = os.open(busy, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertIsNone(recovery.pending(self.folder, WORK))
            self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.ABSENT)
            self.assertTrue(recovery.remove(self.folder, WORK, pair("a")))
            self.assertTrue(busy.exists())
        finally:
            os.close(fd)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertFalse(busy.exists())

    def test_settle_leaves_a_rescue_that_is_still_being_written(self):
        real_link = os.link
        seen = []

        def settle_first(source, target, *args, **kwargs):
            seen.append(recovery.settle(self.store, self.folder, WORK))
            seen.append(recovery.pending(self.folder, WORK))
            seen.append(recovery.remove(self.folder, WORK, pair("a")))
            return real_link(source, target, *args, **kwargs)

        with mock.patch.object(recovery.os, "link", settle_first):
            placed = recovery.rescue(self.folder, WORK, pair("a"))
        self.assertEqual(seen, [recovery.ABSENT, None, True])
        self.assertIsNone(self.store.inner.get(WORK))
        self.assertEqual(json.loads(placed.read_text()), {"claudeAiOauth": pair("a")})
        self.assertEqual(os.listdir(self.folder), [placed.name])
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_damaged_rescue_file_is_reported_and_left(self):
        self.store.inner.put(WORK, pair("a"))
        self.folder.mkdir(parents=True)
        broken = self.folder / f"{WORK}.rescue.0123abcd.json"
        broken.write_text("{broken")
        self.assertTrue(recovery.damaged(self.folder, WORK))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.ABSENT)
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file, choice.damaged), (pair("a"), False, True))
        self.assertTrue(broken.exists())

    def test_write_leaves_a_complete_temp_when_interrupted(self):
        with mock.patch.object(recovery.os, "replace", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                recovery.write(self.folder, WORK, pair("a"))
        self.assertEqual(len(os.listdir(self.folder)), 1)
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(os.listdir(self.folder), [])

    def base(self):
        return self.folder.parent

    def spent_marks(self):
        return ledger.spent(self.base(), WORK)

    def confirm(self, oauth, uuid=WORK):
        switch.record_confirmed(self.base() / ledger.RECORD_NAME, uuid, {"claudeAiOauth": oauth}, datetime.now(timezone.utc))

    def set_aside(self):
        if not self.folder.exists():
            return []
        found = []
        for name in sorted(os.listdir(self.folder)):
            loaded = recovery._load(self.folder / name)
            if loaded is not None:
                found.append(loaded[0])
        return found

    def test_settle_copies_records_into_the_ledger_before_deleting(self):
        recovery.write(self.folder, WORK, pair("new", 1000), spent=pair("old", 2000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertFalse(self.final().exists())
        self.assertEqual(self.spent_marks(), frozenset({switch.fingerprint(pair("old"))}))

    def test_remove_copies_records_into_the_ledger_before_deleting(self):
        recovery.rescue(self.folder, WORK, pair("new", 1000), spent=pair("old", 2000))
        self.assertTrue(recovery.remove(self.folder, WORK, pair("new", 1000)))
        self.assertEqual(self.spent_marks(), frozenset({switch.fingerprint(pair("old"))}))

    def test_records_stay_on_disk_while_the_ledger_cannot_be_written(self):
        recovery.write(self.folder, WORK, pair("new", 1000), spent=pair("old", 2000))
        with mock.patch.object(recovery.ledger, "add", side_effect=ledger.LedgerError("no")):
            self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.PENDING)
            self.assertTrue(self.final().exists())
            self.assertFalse(recovery.remove(self.folder, WORK, pair("new", 1000)))
            self.assertTrue(self.final().exists())
        self.assertEqual(self.store.inner.get(WORK), pair("new", 1000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.HELD)
        self.assertFalse(self.final().exists())
        self.assertEqual(self.spent_marks(), frozenset({switch.fingerprint(pair("old"))}))

    def test_store_pair_puts_over_a_copy_the_ledger_marks_spent_whatever_its_expiry(self):
        self.store.inner.put(WORK, pair("old", 9000))
        ledger.add(self.base(), WORK, pair("old"))
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("new", 1000), folder=self.folder), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("new", 1000))
        self.assertEqual(self.set_aside(), [])

    def test_store_pair_keeps_the_store_copy_when_the_ledger_marks_the_new_pair_spent(self):
        self.store.inner.put(WORK, pair("kept", 1000))
        ledger.add(self.base(), WORK, pair("late"))
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("late", 9000), folder=self.folder), recovery.SUPERSEDED)
        self.assertEqual(self.store.inner.get(WORK), pair("kept", 1000))

    def test_a_record_never_decides_in_the_store_or_the_recovery_folder(self):
        self.store.inner.put(WORK, pair("confirmed", 1000))
        self.confirm(pair("confirmed", 1000))
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("late", 9000), folder=self.folder), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("late", 9000))
        self.assertEqual(self.set_aside(), [pair("confirmed", 1000)])
        self.confirm(pair("short", 500))
        recovery.write(self.folder, WORK, pair("short", 500))
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (pair("late", 9000), False))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("confirmed", 1000))

    def test_store_pair_sets_an_undecided_copy_aside_before_overwriting_it(self):
        self.store.inner.put(WORK, pair("a", 1000))
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("b", 9000), folder=self.folder), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("b", 9000))
        self.assertEqual(self.set_aside(), [pair("a", 1000)])
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("c", 9500), folder=self.folder), recovery.SAVED)
        self.assertEqual(sorted(p["accessToken"] for p in self.set_aside()), [pair("a")["accessToken"], pair("b")["accessToken"]])

    def test_settle_never_lets_a_copy_of_the_store_hide_a_candidate(self):
        self.store.inner.put(WORK, pair("a", 5000))
        recovery.set_aside(self.folder, WORK, pair("a", 5000))
        undated = dict(pair("b"), expiresAt=None)
        recovery.write(self.folder, WORK, undated)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), undated)
        self.assertEqual(self.set_aside(), [pair("a", 5000)])

    def test_an_expiry_tie_goes_to_the_copy_first_seen_live_later(self):
        ledger.see(self.base(), WORK, [pair("old", 5000)])
        ledger.see(self.base(), WORK, [pair("new", 5000)])
        self.store.inner.put(WORK, pair("old", 5000))
        recovery.set_aside(self.folder, WORK, pair("new", 5000))
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual(choice.pair, pair("new", 5000))
        self.assertEqual(self.store.inner.get(WORK), pair("new", 5000))
        self.assertEqual(self.set_aside(), [pair("old", 5000)])
        self.store.inner.remove(WORK)
        for name in os.listdir(self.folder):
            (self.folder / name).unlink()
        recovery.rescue(self.folder, WORK, pair("new", 5000))
        later_file = recovery.rescue(self.folder, WORK, pair("old", 5000))
        os.utime(later_file, (2000000000, 2000000000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("new", 5000))

    def test_kept_copies_go_once_their_sign_in_has_ended(self):
        self.store.inner.put(WORK, pair("stored", 9000))
        gone = recovery.rescue(self.folder, WORK, ended("gone", 1000))
        later = recovery.rescue(self.folder, WORK, ended("later", 2000, days=10))
        unknown = recovery.rescue(self.folder, WORK, pair("unknown", 3000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertFalse(gone.exists())
        self.assertTrue(later.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual(self.store.inner.get(WORK), pair("stored", 9000))
        gone = recovery.rescue(self.folder, WORK, ended("gone", 1000))
        recovery.remove(self.folder, WORK, pair("stored", 9000))
        self.assertFalse(gone.exists())
        self.assertTrue(unknown.exists())

    def age(self, path, days):
        moment = datetime.now(timezone.utc).timestamp() - days * 86400
        os.utime(path, (moment, moment))

    def test_a_kept_copy_with_no_known_end_goes_45_days_after_it_was_written(self):
        self.store.inner.put(WORK, pair("stored", 9000))
        old = recovery.rescue(self.folder, WORK, pair("old", 3000))
        young = recovery.rescue(self.folder, WORK, pair("young", 3500))
        aside = recovery.set_aside(self.folder, WORK, pair("aside", 4000))
        dated = recovery.rescue(self.folder, WORK, ended("dated", 4500, days=10))
        self.age(old, 46)
        self.age(aside, 45.01)
        self.age(young, 44)
        self.age(dated, 60)
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertFalse(old.exists())
        self.assertFalse(aside.exists())
        self.assertTrue(young.exists())
        self.assertTrue(dated.exists())
        self.assertEqual(self.store.inner.get(WORK), pair("stored", 9000))
        again = recovery.rescue(self.folder, WORK, pair("again", 3000))
        self.age(again, 50)
        recovery.remove(self.folder, WORK, pair("stored", 9000))
        self.assertFalse(again.exists())
        self.assertTrue(young.exists())

    def test_a_copy_kept_45_days_with_no_known_end_never_wins_a_choice(self):
        self.store.inner.put(WORK, pair("stored", 1000))
        stale = recovery.rescue(self.folder, WORK, pair("stale", 9000))
        self.age(stale, 46)
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual(choice.pair, pair("stored", 1000))
        self.assertEqual(self.store.inner.get(WORK), pair("stored", 1000))
        self.assertFalse(stale.exists())
        self.store.inner.remove(WORK)
        fresh = recovery.rescue(self.folder, WORK, pair("fresh", 1000))
        stale = recovery.rescue(self.folder, WORK, pair("stale", 9000))
        self.age(stale, 46)
        self.assertEqual(recovery.pending(self.folder, WORK), pair("fresh", 1000))
        self.assertFalse(recovery.waiting(self.store, self.folder, WORK) and recovery.pending(self.folder, WORK) == pair("stale", 9000))
        self.assertTrue(fresh.exists())

    def test_an_ended_store_copy_is_replaced_without_being_set_aside(self):
        self.store.inner.put(WORK, ended("dead", 9000))
        recovery.write(self.folder, WORK, pair("live", 1000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("live", 1000))
        self.assertEqual(os.listdir(self.folder), [])

    def test_set_aside_skips_a_copy_whose_sign_in_has_ended(self):
        self.assertIsNone(recovery.set_aside(self.folder, WORK, ended("dead")))
        self.assertFalse(self.folder.exists() and os.listdir(self.folder))

    def test_a_failed_put_leaves_no_duplicate_set_aside(self):
        self.store.inner.put(WORK, pair("a", 1000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("b", 9000), folder=self.folder), recovery.PENDING)
        self.assertEqual(self.store.inner.get(WORK), pair("a", 1000))
        self.assertEqual(self.set_aside(), [])
        recovery.set_aside(self.folder, WORK, pair("a", 1000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        self.assertEqual(recovery.store_pair(self.store, WORK, pair("b", 9000), folder=self.folder), recovery.PENDING)
        self.assertEqual(self.set_aside(), [pair("a", 1000)])

    def test_store_pair_is_pending_when_the_undecided_copy_cannot_be_set_aside(self):
        self.store.inner.put(WORK, pair("a", 1000))
        with mock.patch.object(recovery, "set_aside", side_effect=recovery.RecoveryError("no")):
            self.assertEqual(recovery.store_pair(self.store, WORK, pair("b", 9000), folder=self.folder), recovery.PENDING)
        self.assertEqual(self.store.inner.get(WORK), pair("a", 1000))
        self.assertEqual(self.store.puts, [])

    def test_settle_sets_an_undecided_store_copy_aside_and_keeps_it(self):
        self.store.inner.put(WORK, pair("a", 1000))
        recovery.write(self.folder, WORK, pair("b", 9000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("b", 9000))
        self.assertFalse(self.final().exists())
        self.assertEqual(self.set_aside(), [pair("a", 1000)])
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertEqual(self.set_aside(), [pair("a", 1000)])
        self.assertEqual(self.store.inner.get(WORK), pair("b", 9000))
        ledger.add(self.base(), WORK, pair("a"))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertEqual(os.listdir(self.folder), [])

    def test_settle_keeps_a_copy_set_aside_earlier(self):
        self.store.inner.put(WORK, pair("a", 1000))
        recovery.set_aside(self.folder, WORK, pair("a", 1000))
        recovery.write(self.folder, WORK, pair("b", 9000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("b", 9000))
        self.assertEqual(self.set_aside(), [pair("a", 1000)])

    def test_a_copy_set_aside_never_swaps_back_on_age_alone(self):
        cases = {
            "equal ages": (pair("a", 5000), pair("b", 5000)),
            "undated file over a dated copy": (pair("a", 5000), dict(pair("b"), expiresAt=None)),
            "dated file over an undated copy": (dict(pair("a"), expiresAt=None), pair("b", 5000)),
        }
        for label, (stored, renewed) in cases.items():
            with self.subTest(label):
                if self.store.inner.get(WORK) is not None:
                    self.store.inner.remove(WORK)
                self.store.inner.put(WORK, stored)
                recovery.write(self.folder, WORK, renewed)
                self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
                self.assertEqual(self.store.inner.get(WORK), renewed)
                self.assertEqual(self.set_aside(), [stored])
                self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
                self.assertEqual(self.store.inner.get(WORK), renewed)
                self.assertEqual(self.set_aside(), [stored])
                for name in os.listdir(self.folder):
                    (self.folder / name).unlink()

    def test_a_copy_set_aside_comes_back_when_it_is_really_newer(self):
        self.store.inner.put(WORK, pair("a", 1000))
        recovery.set_aside(self.folder, WORK, pair("b", 9000))
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SAVED)
        self.assertEqual(self.store.inner.get(WORK), pair("b", 9000))
        self.assertEqual(self.set_aside(), [pair("a", 1000)])
        self.assertEqual(recovery.settle(self.store, self.folder, WORK), recovery.SUPERSEDED)
        self.assertEqual(self.set_aside(), [pair("a", 1000)])

    def test_set_aside_writes_a_marked_private_rescue_file(self):
        placed = recovery.set_aside(self.folder, WORK, pair("a"))
        self.assertTrue(placed.name.startswith(f"{WORK}.rescue."))
        self.assertEqual(json.loads(placed.read_text()), {"claudeAiOauth": pair("a"), "set_aside": True})
        self.assertEqual(mode(placed), 0o600)
        self.assertEqual(recovery.pending(self.folder, WORK), pair("a"))
        with self.assertRaises(recovery.RecoveryError):
            recovery.set_aside(self.folder, WORK, {"accessToken": "a"})
        self.assertEqual(recovery.set_aside(self.folder, WORK, pair("a")), placed)
        recovery.write(self.folder, WORK, pair("b"))
        self.assertEqual(recovery.set_aside(self.folder, WORK, pair("b")), self.final())
        self.assertEqual(len(os.listdir(self.folder)), 2)

    def test_choose_follows_the_ledger_over_expiry(self):
        self.store.inner.put(WORK, pair("spent", 9000))
        ledger.add(self.base(), WORK, pair("spent"))
        recovery.write(self.folder, WORK, pair("renewed", 1000))
        self.store.put_errors = [StoreError("The new parked sign-in could not be saved.")]
        choice = recovery.choose(self.store, self.folder, WORK)
        self.assertEqual((choice.pair, choice.from_file), (pair("renewed", 1000), True))
        self.assertEqual(self.store.inner.get(WORK), pair("spent", 9000))

    def test_pending_ranks_by_the_ledger_first(self):
        recovery.rescue(self.folder, WORK, pair("long", 9000))
        recovery.rescue(self.folder, WORK, pair("short", 1000))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("long", 9000))
        ledger.add(self.base(), WORK, pair("long"))
        self.assertEqual(recovery.pending(self.folder, WORK), pair("short", 1000))

    def test_remove_deletes_only_copies_of_the_loaded_pair_and_proven_spent_ones(self):
        recovery.write(self.folder, WORK, pair("loaded", 5000))
        older = recovery.rescue(self.folder, WORK, pair("older", 1000))
        spent = recovery.rescue(self.folder, WORK, pair("spent", 9000))
        ledger.add(self.base(), WORK, pair("spent"))
        self.assertFalse(recovery.remove(self.folder, WORK, pair("loaded", 5000)))
        self.assertFalse(self.final().exists())
        self.assertFalse(spent.exists())
        self.assertTrue(older.exists())

    def test_waiting_only_for_a_pair_the_store_should_take(self):
        self.assertFalse(recovery.waiting(self.store, self.folder, WORK))
        self.store.inner.put(WORK, pair("stored", 5000))
        recovery.rescue(self.folder, WORK, pair("kept", 1000))
        self.assertFalse(recovery.waiting(self.store, self.folder, WORK))
        recovery.rescue(self.folder, WORK, pair("stored", 5000))
        self.assertFalse(recovery.waiting(self.store, self.folder, WORK))
        ledger.add(self.base(), WORK, pair("stored"))
        self.assertTrue(recovery.waiting(self.store, self.folder, WORK))
        self.store.inner.remove(WORK)
        self.assertTrue(recovery.waiting(self.store, self.folder, WORK))
        self.store.get_errors = 1
        self.assertTrue(recovery.waiting(self.store, self.folder, WORK))

    def test_holds_says_whether_a_file_holds_a_signin(self):
        self.assertFalse(recovery.holds(self.folder, WORK, pair("a")))
        recovery.rescue(self.folder, WORK, pair("a"))
        self.assertTrue(recovery.holds(self.folder, WORK, pair("a")))
        self.assertFalse(recovery.holds(self.folder, WORK, pair("b")))
        self.assertFalse(recovery.holds(self.folder, ORG, pair("a")))

    def test_choice_repr_hides_the_signin(self):
        choice = recovery.Choice(pair("a"), True, False)
        self.assertNotIn("SECRET", repr(choice))
        self.assertNotIn("SECRET", str(choice))

    def test_same_signin_and_newer(self):
        self.assertTrue(recovery.same_signin(pair("a"), dict(pair("a"), scopes=[])))
        self.assertFalse(recovery.same_signin(pair("a"), pair("b")))
        self.assertFalse(recovery.same_signin(None, pair("a")))
        self.assertTrue(recovery.newer(pair("b", 3000), pair("a", 2000)))
        self.assertFalse(recovery.newer(pair("b", 1000), pair("a", 2000)))
        self.assertFalse(recovery.newer(pair("a", 3000), pair("a", 2000)))
        self.assertFalse(recovery.newer({"accessToken": "x"}, pair("a", 2000)))
        self.assertFalse(recovery.newer(dict(pair("b"), expiresAt=None), pair("a", 2000)))
        self.assertFalse(recovery.newer(dict(pair("b"), expiresAt=True), pair("a", 2000)))
        self.assertFalse(recovery.newer(dict(pair("b"), expiresAt=10**400), pair("a", 2000)))


if __name__ == "__main__":
    unittest.main()
