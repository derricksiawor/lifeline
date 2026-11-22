import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lifeline import accountfile
from lifeline.accountfile import (
    AccountFileError,
    prune_backups,
    read_oauth_account,
    replace_oauth_account,
)


def account(tag):
    return {f"field{i}": f"{tag}-value-{i}" for i in range(20)}


def build_config(newline=True):
    data = {
        "numStartups": 412,
        "installMethod": "native",
        "oauthAccount": account("old"),
        "mcpServers": {
            f"server{i}": {
                "type": "http",
                "url": f"https://example.invalid/mcp/{i}",
                "headers": {"Authorization": f"Bearer token-{i}-é中"},
                "args": ["--flag", "café", "\U0001f680"],
            }
            for i in range(12)
        },
        "cachedUsageUtilization": {
            "five_hour": {"utilization": 41.5, "resets_at": "2026-11-19T15:10:00Z"},
            "seven_day": {"utilization": 12.0, "resets_at": None},
        },
        "projects": {
            f"/home/test/project-{i}": {
                "allowedTools": [],
                "history": [{"display": f"note üñî {i} \U0001f600", "pastedContents": {}}],
                "lastCost": i / 7,
                "hasTrustDialogAccepted": i % 2 == 0,
                "exampleFiles": [f"file-{i}-{j}.py" for j in range(8)],
                "mcpContextUris": [],
            }
            for i in range(950)
        },
        "tipsHistory": {"tip": 3},
    }
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if newline:
        text += "\n"
    return data, text.encode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.backups = self.root / "backups"
        self.data, self.raw = build_config()
        self.path.write_bytes(self.raw)
        os.chmod(self.path, 0o600)

    def names(self):
        return sorted(p.name for p in self.root.iterdir())


class SyntheticFileTests(Base):
    def test_file_is_about_500kb(self):
        self.assertGreater(len(self.raw), 450_000)
        self.assertLess(len(self.raw), 600_000)


class ReadTests(Base):
    def test_reads_account(self):
        self.assertEqual(read_oauth_account(self.path), account("old"))

    def test_absent_key_returns_none(self):
        del self.data["oauthAccount"]
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertIsNone(read_oauth_account(self.path))

    def test_missing_file_raises(self):
        with self.assertRaises(AccountFileError):
            read_oauth_account(self.root / "nope.json")

    def test_empty_file_raises(self):
        self.path.write_bytes(b"")
        with self.assertRaises(AccountFileError):
            read_oauth_account(self.path)

    def test_corrupt_file_raises(self):
        self.path.write_bytes(self.raw[:-200])
        with self.assertRaises(AccountFileError):
            read_oauth_account(self.path)

    def test_top_level_array_raises(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(AccountFileError):
            read_oauth_account(self.path)


class ReplaceTests(Base):
    def test_only_oauth_account_changes(self):
        new = account("new")
        replace_oauth_account(self.path, new, self.backups)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        before = dict(self.data)
        self.assertEqual(after["oauthAccount"], new)
        del before["oauthAccount"]
        del after["oauthAccount"]
        self.assertEqual(before, after)

    def test_bytes_identical_apart_from_account(self):
        new = account("new")
        replace_oauth_account(self.path, new, self.backups)
        expected = dict(self.data)
        expected["oauthAccount"] = new
        text = json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
        self.assertEqual(self.path.read_bytes(), text.encode("utf-8"))

    def test_key_order_kept(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(after), list(self.data))
        self.assertEqual(list(after["projects"]), list(self.data["projects"]))

    def test_unicode_and_emoji_unescaped(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        raw = self.path.read_bytes()
        self.assertIn("\U0001f680".encode("utf-8"), raw)
        self.assertIn("café".encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)
        self.assertNotIn(b"\\ud83d", raw)

    def test_new_account_with_unicode(self):
        new = {"displayName": "Renée \U0001f600", "n": 1}
        replace_oauth_account(self.path, new, self.backups)
        self.assertIn("Renée \U0001f600".encode("utf-8"), self.path.read_bytes())
        self.assertEqual(read_oauth_account(self.path), new)

    def test_trailing_newline_kept_when_present(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertTrue(self.path.read_bytes().endswith(b"}\n"))

    def test_no_trailing_newline_when_absent(self):
        _, raw = build_config(newline=False)
        self.path.write_bytes(raw)
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertTrue(self.path.read_bytes().endswith(b"}"))

    def test_absent_key_added_at_end(self):
        del self.data["oauthAccount"]
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_oauth_account(self.path, account("new"), self.backups)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(after)[-1], "oauthAccount")
        self.assertEqual(list(after)[:-1], list(self.data))

    def test_existing_key_keeps_its_position(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(after).index("oauthAccount"), 2)

    def test_permissions_kept(self):
        os.chmod(self.path, 0o640)
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_permissions_600_kept(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_no_temp_file_left(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.names(), ["backups", "config.json"])

    def test_result_read_back(self):
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(read_oauth_account(self.path), account("new"))

    def test_new_must_be_dict(self):
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, "x", self.backups)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_same_account_is_stable(self):
        replace_oauth_account(self.path, account("old"), self.backups)
        self.assertEqual(self.path.read_bytes(), self.raw)


class BackupTests(Base):
    def test_backup_holds_exact_original_bytes(self):
        backup = replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(backup.read_bytes(), self.raw)
        self.assertEqual(backup.parent, self.backups)

    def test_backup_modes(self):
        backup = replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_backup_modes_under_loose_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        backup = replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_backup_name_has_no_forbidden_word(self):
        backup = replace_oauth_account(self.path, account("new"), self.backups)
        self.assertRegex(backup.name, r"^accountfile-\d{20}\.json$")

    def test_backup_written_first(self):
        seen = {}

        def spy(src, dst):
            seen["backups"] = [p.read_bytes() for p in self.backups.iterdir()]
            seen["target"] = self.path.read_bytes()
            return real(src, dst)

        real = os.replace
        with mock.patch.object(accountfile.os, "replace", side_effect=spy):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(seen["backups"], [self.raw])
        self.assertEqual(seen["target"], self.raw)

    def test_backup_exists_even_if_write_fails(self):
        with mock.patch.object(accountfile.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
        files = list(self.backups.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), self.raw)

    def test_two_replacements_same_second_keep_both(self):
        with mock.patch.object(accountfile.time, "time_ns", return_value=1_800_000_000_123_456_789):
            first = replace_oauth_account(self.path, account("a"), self.backups)
            second = replace_oauth_account(self.path, account("b"), self.backups)
        self.assertNotEqual(first, second)
        self.assertLess(first.name, second.name)
        self.assertEqual(len(list(self.backups.iterdir())), 2)
        self.assertEqual(first.read_bytes(), self.raw)
        self.assertEqual(
            json.loads(second.read_bytes())["oauthAccount"], account("a")
        )

    def test_names_sort_by_time(self):
        stamps = [1_700_000_000_000_000_000, 1_800_000_000_000_000_000, 1_900_000_000_000_000_000]
        made = []
        for stamp in stamps:
            with mock.patch.object(accountfile.time, "time_ns", return_value=stamp):
                made.append(replace_oauth_account(self.path, account(str(stamp)), self.backups).name)
        self.assertEqual(made, sorted(made))

    def test_clock_going_backwards_still_sorts_and_never_collides(self):
        with mock.patch.object(accountfile.time, "time_ns", return_value=1_800_000_000_000_000_000):
            a = replace_oauth_account(self.path, account("a"), self.backups)
        with mock.patch.object(accountfile.time, "time_ns", return_value=1_700_000_000_000_000_000):
            b = replace_oauth_account(self.path, account("b"), self.backups)
        self.assertNotEqual(a, b)
        self.assertLess(a.name, b.name)

    def test_rapid_real_replacements_all_kept(self):
        for i in range(8):
            replace_oauth_account(self.path, account(str(i)), self.backups)
        self.assertEqual(len(list(self.backups.iterdir())), 8)


class SafetyTests(Base):
    def test_failure_before_replace_leaves_original_and_no_temp(self):
        with mock.patch.object(accountfile.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.names(), ["backups", "config.json"])

    def test_fsync_failure_on_temp_file_leaves_original_and_no_temp(self):
        real = os.fsync
        failed = []

        def fail_temp_only(fd):
            info = os.fstat(fd)
            known = {p.stat().st_ino for p in self.backups.iterdir()}
            if stat.S_ISREG(info.st_mode) and info.st_ino not in known:
                failed.append(fd)
                raise OSError("disk")
            return real(fd)

        with mock.patch.object(accountfile.os, "fsync", side_effect=fail_temp_only):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.names(), ["backups", "config.json"])

    def test_unserialisable_account_changes_nothing(self):
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, {"bad": object()}, self.backups)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.names(), ["config.json"])
        self.assertFalse(self.backups.exists())

    def test_nan_in_new_account_changes_nothing(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, {"n": value}, self.backups)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())

    def test_repeated_failures_leave_no_backups(self):
        for _ in range(60):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, {"bad": object()}, self.backups)
        self.assertFalse(self.backups.exists())

    def test_non_json_numbers_in_file_raise_and_write_nothing(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e400}', '{"a": -1e400}'):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(AccountFileError):
                read_oauth_account(self.path)
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
            self.assertEqual(self.path.read_text(encoding="utf-8"), text)
        self.assertFalse(self.backups.exists())

    def test_symlink_refused(self):
        link = self.root / "link.json"
        os.symlink(self.path, link)
        with self.assertRaises(AccountFileError):
            replace_oauth_account(link, account("new"), self.backups)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())

    def test_hard_link_refused(self):
        other = self.root / "other.json"
        os.link(self.path, other)
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(other.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())

    def test_atomic_rename(self):
        before = self.path.stat().st_ino
        seen = {}
        real = os.replace

        def spy(src, dst):
            seen["src"] = Path(src)
            seen["dst"] = Path(dst)
            seen["src_inode"] = os.stat(src).st_ino
            return real(src, dst)

        with mock.patch.object(accountfile.os, "replace", side_effect=spy):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(seen["src"].parent, self.path.parent)
        self.assertEqual(seen["dst"], self.path)
        self.assertTrue(seen["src"].name.startswith(".accountfile-"))
        self.assertNotEqual(before, self.path.stat().st_ino)
        self.assertEqual(seen["src_inode"], self.path.stat().st_ino)
        self.assertFalse(seen["src"].exists())

    def test_write_by_another_process_before_rename_is_kept(self):
        theirs = dict(self.data)
        theirs["numStartups"] = 999
        theirs_bytes = (json.dumps(theirs, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        real = accountfile._write_backup

        def backup_then_other_write(backups, raw):
            result = real(backups, raw)
            self.path.write_bytes(theirs_bytes)
            return result

        with mock.patch.object(accountfile, "_write_backup", side_effect=backup_then_other_write):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_bytes(), theirs_bytes)
        self.assertEqual(self.names(), ["backups", "config.json"])

    def test_retry_after_a_changed_file_succeeds(self):
        real = accountfile._write_backup
        state = {"first": True}

        def once(backups, raw):
            result = real(backups, raw)
            if state["first"]:
                state["first"] = False
                changed = dict(self.data)
                changed["numStartups"] = 5
                self.path.write_text(json.dumps(changed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return result

        with mock.patch.object(accountfile, "_write_backup", side_effect=once):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
            replace_oauth_account(self.path, account("new"), self.backups)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["numStartups"], 5)
        self.assertEqual(after["oauthAccount"], account("new"))

    def test_corrupt_file_raises_and_writes_nothing(self):
        broken = self.raw[: len(self.raw) // 2]
        self.path.write_bytes(broken)
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_bytes(), broken)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.names(), ["config.json"])

    def test_empty_file_raises_and_writes_nothing(self):
        self.path.write_bytes(b"")
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertFalse(self.backups.exists())

    def test_missing_file_raises_and_creates_nothing(self):
        gone = self.root / "gone.json"
        with self.assertRaises(AccountFileError):
            replace_oauth_account(gone, account("new"), self.backups)
        self.assertFalse(gone.exists())
        self.assertFalse(self.backups.exists())

    def test_invalid_utf8_raises(self):
        self.path.write_bytes(b'{"a": "\xff"}')
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_bytes(), b'{"a": "\xff"}')

    def test_top_level_array_raises(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(AccountFileError):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[]")

    def test_verification_byte_mismatch_raises_and_keeps_backup(self):
        real = os.replace

        def replace_then_tamper(src, dst):
            real(src, dst)
            Path(dst).write_bytes(Path(dst).read_bytes().replace(b"new-value-3", b"new-value-9"))

        with mock.patch.object(accountfile.os, "replace", side_effect=replace_then_tamper):
            with self.assertRaises(AccountFileError) as caught:
                replace_oauth_account(self.path, account("new"), self.backups)
        self.assertIn("did not match", str(caught.exception))
        self.assertNotIn("restore", str(caught.exception).lower())
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.raw)

    def test_verification_key_mismatch_raises_and_keeps_backup(self):
        real = accountfile._load
        calls = []

        def second_read_differs(path):
            calls.append(path)
            if len(calls) == 1:
                return real(path)
            raw, data = real(path)
            data["numStartups"] = 0
            return raw, data

        with mock.patch.object(accountfile, "_load", side_effect=second_read_differs):
            with self.assertRaises(AccountFileError):
                replace_oauth_account(self.path, account("new"), self.backups)
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.raw)

    def test_a_failed_prune_after_the_write_keeps_the_change_and_says_so(self):
        for error in (PermissionError(13, "denied"), OSError(5, "io")):
            with self.subTest(error=type(error).__name__):
                self.path.write_bytes(self.raw)
                notes = []
                with mock.patch.object(accountfile, "prune_backups", side_effect=error):
                    backup = replace_oauth_account(self.path, account("new"), self.backups, notes=notes)
                self.assertEqual(read_oauth_account(self.path), account("new"))
                self.assertEqual(Path(backup).read_bytes(), self.raw)
                self.assertEqual(
                    notes, [f"Lifeline could not remove old copies of Claude Code's settings file from {self.backups}."]
                )

    def test_a_read_back_that_fails_after_the_write_keeps_the_change_and_says_so(self):
        notes = []
        with mock.patch.object(accountfile, "_read_back", side_effect=OSError(5, "io")):
            backup = replace_oauth_account(self.path, account("new"), self.backups, notes=notes)
        self.assertEqual(read_oauth_account(self.path), account("new"))
        self.assertEqual(Path(backup).read_bytes(), self.raw)
        self.assertEqual(notes, ["Lifeline could not read Claude Code's settings file back to check it."])

    def test_without_a_notes_list_a_failed_prune_still_keeps_the_change(self):
        with mock.patch.object(accountfile, "prune_backups", side_effect=PermissionError(13, "denied")):
            replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(read_oauth_account(self.path), account("new"))


class StringifyShapeTests(Base):
    def fixture(self, floats):
        lines = [
            '  "controls": "tab\\t nl\\n cr\\r bs\\b ff\\f one\\u0001 us\\u001f quote\\" slash\\\\",',
            '  "lone": "before \\ud83d after",',
            '  "lowlone": "x \\ude00 y",',
            '  "pair": "😀 café",',
        ]
        if floats:
            lines += ['  "small": 0.0000525,', '  "tiny": 1e-7,', '  "huge": 1e+21,']
        lines += ['  "oauthAccount": {', '    "emailAddress": "a@b.c"', '  }']
        return "{\n" + "\n".join(lines) + "\n}\n"

    def test_lone_surrogate_round_trips_byte_identical(self):
        text = self.fixture(floats=False)
        self.path.write_bytes(text.encode("utf-8"))
        before = self.path.read_bytes()
        replace_oauth_account(self.path, {"emailAddress": "a@b.c"}, self.backups)
        self.assertEqual(self.path.read_bytes(), before)
        replace_oauth_account(self.path, {"emailAddress": "x@y.z"}, self.backups)
        after = self.path.read_bytes()
        self.assertEqual(after, before.replace(b"a@b.c", b"x@y.z"))
        self.assertIn(b"\\ud83d", after)
        self.assertIn(b"\\ude00", after)

    def test_lone_surrogate_in_new_account(self):
        replace_oauth_account(self.path, {"name": "bad \ud83d end"}, self.backups)
        self.assertIn(b"bad \\ud83d end", self.path.read_bytes())
        self.assertEqual(read_oauth_account(self.path), {"name": "bad \ud83d end"})

    def test_meaning_kept_for_stringify_numbers(self):
        text = self.fixture(floats=True)
        self.path.write_bytes(text.encode("utf-8"))
        replace_oauth_account(self.path, {"emailAddress": "x@y.z"}, self.backups)
        before = json.loads(text)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after.pop("oauthAccount"), {"emailAddress": "x@y.z"})
        before.pop("oauthAccount")
        self.assertEqual(before, after)
        self.assertEqual(after["small"], 0.0000525)
        self.assertEqual(after["tiny"], 1e-7)
        self.assertEqual(after["huge"], 1e21)


class PruneTests(Base):
    def test_prune_ignores_look_alike_names(self):
        self.make(60)
        decoys = ["accountfile-1.json", "accountfile-00000000000000000001-0000.json", "accountfile-00000000000000000001.json.bak"]
        for name in decoys:
            (self.backups / name).write_text("x", encoding="utf-8")
        prune_backups(self.backups)
        for name in decoys:
            self.assertTrue((self.backups / name).exists())

    def test_prune_never_removes_protected_file(self):
        names = self.make(5)
        prune_backups(self.backups, keep=1, protect=self.backups / names[0])
        self.assertTrue((self.backups / names[0]).exists())
        self.assertTrue((self.backups / names[-1]).exists())

    def test_just_written_backup_survives_even_with_future_stamps(self):
        self.backups.mkdir(mode=0o700)
        for i in range(60):
            stamp = 9_000_000_000_000_000_000 + i
            (self.backups / f"accountfile-{stamp:020d}.json").write_text("x", encoding="utf-8")
        backup = replace_oauth_account(self.path, account("new"), self.backups)
        self.assertTrue(backup.exists())
        self.assertGreater(backup.name, "accountfile-09000000000000000059.json")

    def make(self, count):
        self.backups.mkdir(mode=0o700, exist_ok=True)
        names = []
        for i in range(count):
            name = f"accountfile-{i:020d}.json"
            (self.backups / name).write_text(str(i), encoding="utf-8")
            names.append(name)
        return names

    def test_prune_keeps_newest_10(self):
        names = self.make(25)
        prune_backups(self.backups)
        left = sorted(p.name for p in self.backups.iterdir())
        self.assertEqual(left, names[-10:])

    def test_prune_under_limit_removes_nothing(self):
        names = self.make(5)
        prune_backups(self.backups)
        self.assertEqual(len(list(self.backups.iterdir())), 5)

    def test_prune_exactly_at_limit(self):
        self.make(10)
        prune_backups(self.backups)
        self.assertEqual(len(list(self.backups.iterdir())), 10)

    def test_prune_custom_keep(self):
        names = self.make(8)
        prune_backups(self.backups, keep=3)
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), names[-3:])

    def test_prune_missing_folder_is_quiet(self):
        prune_backups(self.root / "absent")

    def test_prune_leaves_foreign_files(self):
        self.make(60)
        other = self.backups / "notes.txt"
        other.write_text("keep me", encoding="utf-8")
        prune_backups(self.backups)
        self.assertTrue(other.exists())
        self.assertEqual(len(list(self.backups.iterdir())), 11)

    def test_prune_rejects_negative_keep(self):
        self.make(3)
        with self.assertRaises(ValueError):
            prune_backups(self.backups, keep=-1)
        self.assertEqual(len(list(self.backups.iterdir())), 3)

    def test_replace_prunes_to_10(self):
        self.make(60)
        replace_oauth_account(self.path, account("new"), self.backups)
        self.assertEqual(len(list(self.backups.iterdir())), 10)

    def test_new_backup_survives_prune(self):
        self.make(60)
        backup = replace_oauth_account(self.path, account("new"), self.backups)
        self.assertTrue(backup.exists())


if __name__ == "__main__":
    unittest.main()
