import hashlib
import json
import os
import stat
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import ledger, switch, writelock

WORK = "22222222-bbbb-4bbb-8bbb-000000000002"
ORG = "44444444-dddd-4ddd-8ddd-000000000004"
DAY = 86400
NOW = 1_800_000_000


def pair(tag, expires=2000000000000):
    return {
        "accessToken": f"access-{tag}-SECRET",
        "refreshToken": f"refresh-{tag}-SECRET",
        "expiresAt": expires,
    }


def mark(oauth):
    return hashlib.sha256(oauth["refreshToken"].encode("utf-8")).hexdigest()


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "lifeline"
        self.path = self.base / ledger.NAME

    def test_path_sits_beside_the_lock_and_the_record(self):
        lock = self.base / "lifeline.lock"
        self.assertEqual(ledger.path_for(lock), self.base / "spent.json")
        self.assertEqual(ledger.path_for(None), writelock.lifeline_folder() / "spent.json")
        self.assertEqual(ledger.path_for(lock).parent, switch.record_path_for(lock).parent)

    def test_add_then_read_per_account(self):
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW), frozenset())
        self.assertTrue(ledger.add(self.base, WORK, pair("a"), now=NOW))
        self.assertTrue(ledger.add(self.base, ORG, mark(pair("b")), now=NOW))
        self.assertTrue(ledger.add(self.base, WORK, [pair("c"), mark(pair("d"))], now=NOW))
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW), frozenset({mark(pair("a")), mark(pair("c")), mark(pair("d"))}))
        self.assertEqual(ledger.spent(self.base, ORG, now=NOW), frozenset({mark(pair("b"))}))
        self.assertFalse(ledger.add(self.base, WORK, pair("a"), now=NOW + 10))

    def test_file_holds_fingerprints_and_times_and_never_a_key(self):
        ledger.add(self.base, WORK, pair("a"), now=NOW)
        text = self.path.read_text()
        self.assertNotIn("SECRET", text)
        self.assertEqual(json.loads(text), {"accounts": {WORK: {mark(pair("a")): NOW}}})
        ledger.add(self.base, WORK, pair("a"), now=NOW + DAY)
        self.assertEqual(json.loads(self.path.read_text())["accounts"][WORK][mark(pair("a"))], NOW)

    def test_file_is_private_and_written_whole(self):
        self.base.mkdir(mode=0o700)
        ledger.add(self.base, WORK, pair("a"), now=NOW)
        self.assertEqual(mode(self.path), 0o600)
        self.assertEqual(sorted(os.listdir(self.base)), ["spent.json"])
        before = self.path.read_bytes()
        with mock.patch.object(ledger.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(ledger.LedgerError) as caught:
                ledger.add(self.base, WORK, pair("b"), now=NOW)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.base)), ["spent.json"])
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__context__ is None or caught.exception.__suppress_context__)

    def test_a_missing_folder_is_made_private(self):
        ledger.add(self.base, WORK, pair("a"), now=NOW)
        self.assertEqual(mode(self.base), 0o700)

    def test_entries_are_kept_45_days(self):
        ledger.add(self.base, WORK, pair("old"), now=NOW)
        ledger.add(self.base, WORK, pair("new"), now=NOW + 40 * DAY)
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW + 45 * DAY), frozenset({mark(pair("old")), mark(pair("new"))}))
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW + 45 * DAY + 1), frozenset({mark(pair("new"))}))
        ledger.add(self.base, ORG, pair("x"), now=NOW + 46 * DAY)
        self.assertEqual(json.loads(self.path.read_text())["accounts"][WORK], {mark(pair("new")): NOW + 40 * DAY})

    def test_a_damaged_file_reads_as_empty_and_is_never_replaced(self):
        self.base.mkdir()
        for text in ("{broken", "[1]", "", "null"):
            with self.subTest(text=text):
                self.path.write_text(text)
                self.assertEqual(ledger.spent(self.base, WORK, now=NOW), frozenset())
                self.assertEqual(ledger.seen(self.base, WORK, now=NOW), {})
                self.assertTrue(ledger.problem(self.base))
                with self.assertRaises(ledger.LedgerError):
                    ledger.add(self.base, WORK, pair("a"), now=NOW)
                with self.assertRaises(ledger.LedgerError):
                    ledger.see(self.base, WORK, [pair("a")], now=NOW)
                self.assertEqual(self.path.read_text(), text)

    def test_leftover_temporary_files_older_than_five_minutes_are_removed_when_writing(self):
        self.base.mkdir(mode=0o700)
        old = self.base / ".spent-abc123de.tmp"
        fresh = self.base / ".spent-fgh456ij.tmp"
        other = self.base / ".other-abc123de.tmp"
        folder = self.base / ".spent-folder01.tmp"
        for path in (old, fresh, other):
            path.write_text("{}")
        folder.mkdir()
        moment = time.time()
        for path in (old, other, folder):
            os.utime(path, (moment - 301, moment - 301))
        os.utime(fresh, (moment - 200, moment - 200))
        ledger.add(self.base, WORK, pair("a"), now=NOW)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())
        self.assertTrue(folder.is_dir())
        fresh_time = os.stat(fresh).st_mtime
        os.utime(fresh, (fresh_time - 200, fresh_time - 200))
        ledger.see(self.base, WORK, [pair("b")], now=NOW)
        self.assertFalse(fresh.exists())

    def test_strange_entries_are_skipped(self):
        self.base.mkdir()
        good = mark(pair("a"))
        self.path.write_text(
            json.dumps(
                {
                    "accounts": {
                        WORK: {good: NOW, "not-a-fingerprint": NOW, mark(pair("b")): "soon", mark(pair("c")): True},
                        "../x": {mark(pair("d")): NOW},
                        ORG: ["list"],
                    }
                }
            )
        )
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW), frozenset({good}))
        self.assertEqual(ledger.spent(self.base, ORG, now=NOW), frozenset())

    def test_a_linked_or_unreadable_file_is_never_replaced(self):
        self.base.mkdir()
        target = self.base / "elsewhere.json"
        target.write_text(json.dumps({"accounts": {WORK: {mark(pair("a")): NOW}}}))
        os.symlink(target, self.path)
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW), frozenset())
        with self.assertRaises(ledger.LedgerError):
            ledger.add(self.base, WORK, pair("b"), now=NOW)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(target.read_text()), {"accounts": {WORK: {mark(pair("a")): NOW}}})

    def test_problem_names_a_file_that_cannot_be_read_safely(self):
        self.assertFalse(ledger.problem(self.base))
        ledger.add(self.base, WORK, pair("a"), now=NOW)
        self.assertFalse(ledger.problem(self.base))
        self.path.write_text("{broken")
        self.assertTrue(ledger.problem(self.base))
        self.path.unlink()
        target = self.base / "elsewhere.json"
        target.write_text("{}")
        os.symlink(target, self.path)
        self.assertTrue(ledger.problem(self.base))
        self.path.unlink()
        self.path.mkdir()
        self.assertTrue(ledger.problem(self.base))
        self.path.rmdir()
        self.assertFalse(ledger.problem(self.base))
        os.chmod(self.base, 0o500)
        self.addCleanup(os.chmod, self.base, 0o700)
        self.assertTrue(ledger.problem(self.base))

    def test_seen_times_are_first_sightings_kept_45_days(self):
        self.assertEqual(ledger.seen(self.base, WORK, now=NOW), {})
        self.assertTrue(ledger.see(self.base, WORK, [pair("a")], now=NOW))
        self.assertFalse(ledger.see(self.base, WORK, [pair("a")], now=NOW + DAY))
        self.assertTrue(ledger.see(self.base, WORK, [pair("b")], now=NOW + 2 * DAY))
        self.assertEqual(ledger.seen(self.base, WORK, now=NOW + 2 * DAY), {mark(pair("a")): NOW, mark(pair("b")): NOW + 2 * DAY})
        self.assertEqual(ledger.seen(self.base, ORG, now=NOW), {})
        ledger.add(self.base, WORK, pair("x"), now=NOW + 3 * DAY)
        self.assertEqual(ledger.seen(self.base, WORK, now=NOW + 3 * DAY)[mark(pair("a"))], NOW)
        self.assertEqual(ledger.spent(self.base, WORK, now=NOW + 3 * DAY), frozenset({mark(pair("x"))}))
        self.assertEqual(ledger.seen(self.base, WORK, now=NOW + 46 * DAY), {mark(pair("b")): NOW + 2 * DAY})
        self.assertNotIn("SECRET", self.path.read_text())
        with self.assertRaises(ledger.LedgerError):
            ledger.see(self.base, "../x", [pair("a")], now=NOW)

    def test_bad_input_is_refused_with_fixed_text(self):
        for bad in ("../x", "", None, "a b"):
            with self.subTest(uuid=bad):
                with self.assertRaises(ledger.LedgerError):
                    ledger.add(self.base, bad, pair("a"), now=NOW)
        for bad in ("refresh-a-SECRET", "A" * 64, 7, {"accessToken": "only"}):
            with self.subTest(spent=bad):
                with self.assertRaises(ledger.LedgerError) as caught:
                    ledger.add(self.base, WORK, bad, now=NOW)
                self.assertNotIn("SECRET", str(caught.exception))
        self.assertFalse(self.path.exists())
        self.assertFalse(ledger.add(self.base, WORK, [], now=NOW))
        self.assertFalse(self.path.exists())

    def test_record_entry_reads_the_switch_record(self):
        record = self.base / ledger.RECORD_NAME
        self.assertIsNone(ledger.record_entry(record))
        switch.record_confirmed(record, WORK, {"claudeAiOauth": pair("a")}, datetime.now(timezone.utc))
        self.assertEqual(ledger.record_entry(record), {"uuid": WORK, "refresh_sha256": mark(pair("a")), "proven": False})
        self.assertEqual(switch.read_record_entry(record), ledger.record_entry(record))
        switch.record_confirmed(record, WORK, {"claudeAiOauth": pair("a")}, datetime.now(timezone.utc), proven=True)
        self.assertEqual(ledger.record_entry(record), {"uuid": WORK, "refresh_sha256": mark(pair("a")), "proven": True})
        switch.write_record(record, WORK)
        self.assertEqual(ledger.record_entry(record), {"uuid": WORK, "refresh_sha256": None, "proven": False})
        record.write_text(json.dumps({"uuid": WORK, "refresh_sha256": mark(pair("a")), "proven": "yes"}))
        self.assertFalse(ledger.record_entry(record)["proven"])
        record.write_text("[1]")
        self.assertIsNone(ledger.record_entry(record))


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "lifeline"
        self.record = self.base / ledger.RECORD_NAME

    def confirm(self, uuid, oauth, proven=True):
        switch.record_confirmed(self.record, uuid, {"claudeAiOauth": oauth}, datetime.now(timezone.utc), proven=proven)

    def test_gather_reads_the_ledger_and_the_record_for_one_account(self):
        ledger.add(self.base, WORK, pair("old"))
        ledger.add(self.base, ORG, pair("org"))
        self.confirm(WORK, pair("new"))
        found = ledger.gather(self.base, WORK)
        self.assertEqual(found.spent, frozenset({mark(pair("old"))}))
        self.assertEqual(found.confirmed, mark(pair("new")))
        other = ledger.gather(self.base, ORG)
        self.assertEqual(other.spent, frozenset({mark(pair("org"))}))
        self.assertIsNone(other.confirmed)
        switch.write_record(self.record, WORK)
        self.assertIsNone(ledger.gather(self.base, WORK).confirmed)
        extra = ledger.gather(self.base, WORK, extra=[mark(pair("x"))])
        self.assertIn(mark(pair("x")), extra.spent)

    def test_a_record_written_without_proof_proves_nothing(self):
        self.confirm(WORK, pair("b", 1000), proven=False)
        found = ledger.gather(self.base, WORK)
        self.assertIsNone(found.confirmed)
        live = pair("b", 1000)
        self.assertIsNone(found.judge(pair("a", 9000), live, live=live))

    def test_the_record_proves_nothing_while_the_ledger_cannot_be_read(self):
        live = pair("b", 1000)
        self.confirm(WORK, live)
        path = self.base / ledger.NAME
        target = self.base / "elsewhere.json"
        target.write_text("{}")
        cases = {
            "damaged": lambda: path.write_text("{broken"),
            "linked": lambda: os.symlink(target, path),
            "unreadable": lambda: (path.write_text("{}"), os.chmod(path, 0)),
        }
        for name, spoil in cases.items():
            with self.subTest(name=name):
                spoil()
                found = ledger.gather(self.base, WORK)
                self.assertTrue(found.blind)
                self.assertIsNone(found.judge(pair("a", 500), live, live=live))
                self.assertIsNone(found.by_record(live, pair("a", 500), live=live))
                if not path.is_symlink():
                    os.chmod(path, 0o600)
                path.unlink()
        found = ledger.gather(self.base, WORK)
        self.assertFalse(found.blind)
        self.assertEqual(found.judge(pair("a", 500), live, live=live), ledger.SECOND)

    def test_the_ledger_decides_first(self):
        ledger.add(self.base, WORK, pair("long", 9000))
        self.confirm(WORK, pair("long", 9000))
        found = ledger.gather(self.base, WORK)
        live = pair("long", 9000)
        self.assertEqual(found.judge(pair("long", 9000), pair("short", 1000), live=live), ledger.SECOND)
        self.assertEqual(found.judge(pair("short", 1000), pair("long", 9000), live=live), ledger.FIRST)
        self.assertEqual(found.by_ledger(pair("short", 1000), pair("long", 9000)), ledger.FIRST)
        self.assertEqual(found.by_record(pair("short", 1000), pair("long", 9000), live=live), ledger.SECOND)

    def test_the_record_decides_only_for_the_live_pair(self):
        self.confirm(WORK, pair("b", 9000))
        found = ledger.gather(self.base, WORK)
        live = pair("b", 9000)
        self.assertEqual(found.judge(pair("a", 1000), pair("b", 9000), live=live), ledger.SECOND)
        self.assertEqual(found.judge(pair("b", 9000), pair("a", 1000), live=live), ledger.FIRST)
        self.assertIsNone(found.by_ledger(pair("b", 9000), pair("a", 1000)))
        self.assertIsNone(found.judge(pair("a", 1000), pair("b", 9000)))
        self.assertIsNone(found.judge(pair("a", 1000), pair("b", 9000), live=pair("a", 1000)))
        self.assertIsNone(found.by_record(pair("a", 1000), pair("b", 9000), live=pair("c", 5000)))

    def test_the_record_never_proves_the_live_pair_against_a_copy_that_ends_later(self):
        live = pair("live", 5000)
        self.confirm(WORK, live)
        found = ledger.gather(self.base, WORK)
        self.assertIsNone(found.by_record(pair("later", 5001), live, live=live))
        self.assertIsNone(found.by_record(live, pair("later", 5001), live=live))
        self.assertIsNone(found.judge(pair("later", 5001), live, live=live))
        self.assertEqual(found.by_record(pair("same", 5000), live, live=live), ledger.SECOND)
        self.assertEqual(found.by_record(live, pair("sooner", 4999), live=live), ledger.FIRST)
        unknown = {key: value for key, value in pair("unknown").items() if key != "expiresAt"}
        self.assertEqual(found.by_record(unknown, live, live=live), ledger.SECOND)

    def test_the_record_never_proves_the_live_pair_against_a_copy_seen_live_after_it(self):
        self.confirm(WORK, pair("live", 1000))
        ledger.see(self.base, WORK, [pair("live", 1000)])
        ledger.see(self.base, WORK, [pair("later", 1000)])
        found = ledger.gather(self.base, WORK)
        live = pair("live", 1000)
        self.assertIsNone(found.by_record(pair("later", 1000), live, live=live))
        self.assertEqual(found.by_record(pair("never", 1000), live, live=live), ledger.SECOND)

    def test_nothing_decides_without_evidence(self):
        found = ledger.gather(self.base, WORK)
        self.assertIsNone(found.judge(pair("a", 9000), pair("b", 1000)))
        ledger.add(self.base, WORK, [pair("a"), pair("b")])
        self.assertIsNone(ledger.gather(self.base, WORK).by_ledger(pair("a"), pair("b")))
        self.assertIsNone(found.judge(pair("a"), dict(pair("a"), expiresAt=1)))

    def test_an_unusable_copy_always_loses(self):
        found = ledger.gather(self.base, WORK)
        broken = {"accessToken": "", "refreshToken": "refresh-x-SECRET", "expiresAt": 9000}
        self.assertEqual(found.judge(broken, pair("a")), ledger.SECOND)
        self.assertEqual(found.judge(pair("a"), None), ledger.FIRST)
        self.assertIsNone(found.judge(None, broken))

    def test_an_expiry_tie_goes_to_the_copy_first_seen_live_later(self):
        ledger.see(self.base, WORK, [pair("old")], now=NOW)
        ledger.see(self.base, WORK, [pair("new")], now=NOW + 60)
        found = ledger.gather(self.base, WORK)
        self.assertEqual(found.by_seen(pair("old"), pair("new")), ledger.SECOND)
        self.assertEqual(found.by_seen(pair("new"), pair("old")), ledger.FIRST)
        self.assertIsNone(found.by_seen(pair("new"), pair("never")))
        self.assertIsNone(found.judge(pair("old"), pair("new")))

    def test_is_spent_and_adding(self):
        found = ledger.gather(self.base, WORK)
        self.assertFalse(found.is_spent(pair("a")))
        more = found.adding({mark(pair("a"))})
        self.assertTrue(more.is_spent(pair("a")))
        self.assertFalse(found.is_spent(pair("a")))
        self.assertFalse(more.is_spent(None))

    def test_evidence_repr_holds_no_key(self):
        self.confirm(WORK, pair("a"))
        text = repr(ledger.gather(self.base, WORK))
        self.assertNotIn("SECRET", text)


if __name__ == "__main__":
    unittest.main()
