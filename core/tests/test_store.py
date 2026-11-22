import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lifeline import slot, store
from lifeline.slot import SecurityError
from lifeline.store import FileStore, MacKeychainStore, Store, StoreError, StoreLeftover

ACCESS = "sk-ant-oat01-ACCESSSECRETVALUE"
REFRESH = "sk-ant-ort01-REFRESHSECRETVALUE"
UUID = "a7a7a7a7-aaaa-4bbb-8ccc-0123456789ab"
PREVIOUS = UUID + ".previous"


def oauth(tag="one"):
    return {
        "accessToken": ACCESS + tag,
        "refreshToken": REFRESH + tag,
        "expiresAt": 1789000000000,
        "scopes": ["user:inference", "user:profile"],
    }


class FakeKeychain:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.hook = None

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        if args[0] == "-i":
            words = shlex.split(stdin.split("\n")[0])
            account = words[words.index("-a") + 1]
            service = words[words.index("-s") + 1]
            digits = words[words.index("-X") + 1]
            return self._write(service, account, digits)
        service = args[args.index("-s") + 1]
        account = args[args.index("-a") + 1]
        if args[0] == "add-generic-password":
            return self._write(service, account, args[args.index("-X") + 1])
        if args[0] == "find-generic-password":
            self._maybe_fail(args[0], account)
            if (service, account) not in self.items:
                raise SecurityError(args[0], 44, "The specified item could not be found in the keychain.")
            return self.items[(service, account)] + "\n"
        if args[0] == "delete-generic-password":
            self._maybe_fail(args[0], account)
            if (service, account) not in self.items:
                raise SecurityError(args[0], 44, "")
            del self.items[(service, account)]
            return ""
        raise AssertionError(args)

    def _maybe_fail(self, command, account):
        if self.hook:
            self.hook(command, account, self)

    def _write(self, service, account, digits):
        self._maybe_fail("write", account)
        self.items[(service, account)] = bytes.fromhex(digits).decode("utf-8")
        return ""

    def text(self, account):
        return self.items.get(("com.example.lifeline", account))

    def set(self, account, value):
        self.items[("com.example.lifeline", account)] = value if isinstance(value, str) else json.dumps(value)


def line_length(name, value):
    digits = slot._compact({"claudeAiOauth": value}).encode().hex()
    return len(f'add-generic-password -U -a "{name}" -s "com.example.lifeline" -X {digits} {slot._quote(slot.login_keychain())}\n'.encode())


def edge_oauth():
    value = oauth()
    value["extra"] = ""
    count = 0
    while True:
        value["extra"] = "x" * (count + 1)
        if line_length(UUID, value) > slot.STDIN_LIMIT:
            break
        count += 1
    value["extra"] = "x" * count
    assert line_length(UUID, value) <= slot.STDIN_LIMIT < line_length(PREVIOUS, value)
    return value


def failing(command, account, error=None):
    def hook(work, acct, fake):
        if work == command and acct == account:
            raise error or SecurityError(command, 1)

    return hook


def chain_text(error):
    parts = []
    while error is not None:
        parts.append(str(error) + repr(error))
        error = error.__cause__ or error.__context__
    return " ".join(parts)


class KeychainStoreTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKeychain()
        self.store = MacKeychainStore(runner=self.fake)

    def test_interface_and_default_service(self):
        self.assertIsInstance(self.store, Store)
        self.assertEqual(self.store.service, "com.example.lifeline")
        self.assertEqual(MacKeychainStore(runner=self.fake, service="Other").service, "Other")

    def test_get_missing_is_none(self):
        self.assertIsNone(self.store.get(UUID))

    def test_put_then_get_round_trips(self):
        self.store.put(UUID, oauth())
        self.assertEqual(self.store.get(UUID), oauth())

    def test_only_the_oauth_object_is_stored_wrapped(self):
        self.store.put(UUID, oauth())
        stored = json.loads(self.fake.text(UUID))
        self.assertEqual(stored, {"claudeAiOauth": oauth()})
        self.assertNotIn("mcpOAuth", self.fake.text(UUID))

    def test_each_account_is_its_own_item(self):
        other = "aaaaaaaa-0000-4000-8000-000000000000"
        self.store.put(UUID, oauth("one"))
        self.store.put(other, oauth("two"))
        self.assertEqual(self.store.get(UUID), oauth("one"))
        self.assertEqual(self.store.get(other), oauth("two"))

    def test_put_rejects_non_dict(self):
        for bad in (None, "text", ["a"], 5):
            with self.assertRaises(StoreError):
                self.store.put(UUID, bad)
        self.assertEqual(self.fake.calls, [])

    def test_put_keeps_previous_until_verified(self):
        self.store.put(UUID, oauth("old"))
        seen = {}

        def hook(command, account, fake):
            if command == "write" and account == UUID:
                seen["previous"] = fake.text(PREVIOUS)
                seen["main"] = fake.text(UUID)

        self.fake.hook = hook
        self.store.put(UUID, oauth("new"))
        self.assertEqual(json.loads(seen["previous"]), {"claudeAiOauth": oauth("old")})
        self.assertEqual(json.loads(seen["main"]), {"claudeAiOauth": oauth("old")})
        self.assertEqual(self.store.get(UUID), oauth("new"))
        self.assertIsNone(self.fake.text(PREVIOUS))

    def test_put_order_is_copy_then_write_then_remove_previous(self):
        self.store.put(UUID, oauth("old"))
        self.fake.calls.clear()
        self.store.put(UUID, oauth("new"))
        steps = []
        for argv, stdin in self.fake.calls:
            if argv[0] == "-i":
                words = shlex.split(stdin.split("\n")[0])
                steps.append(("write", words[words.index("-a") + 1]))
            elif argv[0] == "delete-generic-password":
                steps.append(("delete", argv[argv.index("-a") + 1]))
        self.assertEqual(steps, [("write", PREVIOUS), ("write", UUID), ("delete", PREVIOUS)])

    def test_first_put_has_nothing_to_keep(self):
        self.store.put(UUID, oauth())
        written = [a for a, _ in self.fake.calls if a[0] == "-i"]
        self.assertEqual(len(written), 1)
        self.assertIsNone(self.fake.text(PREVIOUS))

    def test_failure_copying_previous_leaves_old_value_untouched(self):
        self.store.put(UUID, oauth("old"))
        self.fake.hook = failing("write", PREVIOUS)
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.fake.hook = None
        self.assertEqual(self.store.get(UUID), oauth("old"))

    def test_failure_writing_new_value_keeps_previous_and_old_readable(self):
        self.store.put(UUID, oauth("old"))
        self.fake.hook = failing("write", UUID)
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.fake.hook = None
        self.assertEqual(json.loads(self.fake.text(PREVIOUS)), {"claudeAiOauth": oauth("old")})
        self.assertEqual(self.store.get(UUID), oauth("old"))

    def test_write_that_damages_main_then_fails_still_reads_old_from_previous(self):
        self.store.put(UUID, oauth("old"))

        def hook(command, account, fake):
            if command == "write" and account == UUID:
                fake.set(UUID, "{broken")
                raise SecurityError("-i", 1)

        self.fake.hook = hook
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.fake.hook = None
        self.fake.calls.clear()
        self.assertEqual(self.store.get(UUID), oauth("old"))
        self.assertEqual(self.fake.text(UUID), "{broken")
        self.assertIsNotNone(self.fake.text(PREVIOUS))
        self.assertTrue(all(a[0] == "find-generic-password" for a, _ in self.fake.calls))

    def test_write_that_does_not_read_back_keeps_previous(self):
        self.store.put(UUID, oauth("old"))

        original = self.fake._write

        def dropping(service, account, digits):
            if account == UUID:
                return ""
            return original(service, account, digits)

        self.fake._write = dropping
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.fake._write = original
        self.assertEqual(self.store.get(UUID), oauth("old"))
        self.assertIsNotNone(self.fake.text(PREVIOUS))

    def test_failure_removing_previous_retries_once_then_says_an_old_copy_remains(self):
        self.store.put(UUID, oauth("old"))
        self.fake.hook = failing("delete-generic-password", PREVIOUS)
        self.fake.calls.clear()
        with self.assertRaises(StoreLeftover) as caught:
            self.store.put(UUID, oauth("new"))
        self.assertIn("old copy remains", str(caught.exception))
        deletes = [a for a, _ in self.fake.calls if a[0] == "delete-generic-password"]
        self.assertEqual(len(deletes), 2)
        self.fake.hook = None
        self.assertEqual(self.store.get(UUID), oauth("new"))

    def test_first_removal_failure_is_retried_and_put_succeeds(self):
        self.store.put(UUID, oauth("old"))
        state = {"n": 0}

        def hook(command, account, fake):
            if command == "delete-generic-password" and account == PREVIOUS:
                state["n"] += 1
                if state["n"] == 1:
                    raise SecurityError(command, 1)

        self.fake.hook = hook
        self.store.put(UUID, oauth("new"))
        self.assertEqual(state["n"], 2)
        self.assertIsNone(self.fake.text(PREVIOUS))

    def test_damaged_main_without_previous_raises_and_does_not_hide_it(self):
        self.fake.set(UUID, "{not json")
        with self.assertRaises(StoreError) as caught:
            self.store.get(UUID)
        self.assertNotIn("not json", chain_text(caught.exception))

    def test_main_without_oauth_object_counts_as_damaged(self):
        for value in ({"other": 1}, {"claudeAiOauth": "text"}, {"claudeAiOauth": None}, []):
            self.fake.set(UUID, value)
            with self.assertRaises(StoreError):
                self.store.get(UUID)

    def test_damaged_main_reads_from_previous_and_get_writes_nothing(self):
        self.fake.set(UUID, "{broken")
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.calls.clear()
        self.assertEqual(self.store.get(UUID), oauth("old"))
        self.assertEqual(self.fake.text(UUID), "{broken")
        self.assertIsNotNone(self.fake.text(PREVIOUS))
        self.assertTrue(all(a[0] == "find-generic-password" for a, _ in self.fake.calls))

    def test_missing_main_with_leftover_previous_is_none(self):
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.calls.clear()
        self.assertIsNone(self.store.get(UUID))
        self.assertTrue(all(a[0] == "find-generic-password" for a, _ in self.fake.calls))
        self.assertIsNone(self.fake.text(UUID))

    def test_get_never_overwrites_a_newer_put(self):
        self.fake.set(UUID, "{broken")
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("A")})
        self.assertEqual(self.store.get(UUID), oauth("A"))
        self.store.put(UUID, oauth("B"))
        self.assertEqual(self.store.get(UUID), oauth("B"))
        self.assertIsNone(self.fake.text(PREVIOUS))

    def test_both_copies_damaged_raises(self):
        self.fake.set(UUID, "{broken")
        self.fake.set(PREVIOUS, "{also broken")
        with self.assertRaises(StoreError):
            self.store.get(UUID)

    def test_good_main_wins_over_a_stale_previous(self):
        self.fake.set(UUID, {"claudeAiOauth": oauth("new")})
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.assertEqual(self.store.get(UUID), oauth("new"))

    def test_put_over_damaged_main_keeps_the_good_previous(self):
        self.fake.set(UUID, "{broken")
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.hook = failing("write", UUID)
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.fake.hook = None
        self.assertEqual(self.store.get(UUID), oauth("old"))

    def test_read_failure_during_get_raises(self):
        self.store.put(UUID, oauth())
        self.fake.hook = failing("find-generic-password", UUID, SecurityError("find-generic-password", 51))
        with self.assertRaises(StoreError):
            self.store.get(UUID)

    def test_unreadable_main_stops_put_before_anything_is_written(self):
        self.store.put(UUID, oauth("old"))
        self.fake.calls.clear()
        self.fake.hook = failing("find-generic-password", UUID, SecurityError("find-generic-password", 51))
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.fake.hook = None
        self.assertFalse([a for a, _ in self.fake.calls if a[0] in ("-i", "add-generic-password")])
        self.assertEqual(self.store.get(UUID), oauth("old"))

    def test_unreadable_main_never_falls_back_to_a_stale_previous(self):
        self.fake.set(UUID, {"claudeAiOauth": oauth("new")})
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.hook = failing("find-generic-password", UUID, SecurityError("find-generic-password", 51))
        with self.assertRaises(StoreError):
            self.store.get(UUID)
        self.fake.hook = None
        self.assertEqual(json.loads(self.fake.text(UUID)), {"claudeAiOauth": oauth("new")})

    def test_remove_deletes_both_items(self):
        self.store.put(UUID, oauth())
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.calls.clear()
        self.store.remove(UUID)
        self.assertIsNone(self.fake.text(UUID))
        self.assertIsNone(self.fake.text(PREVIOUS))
        self.assertIn(
            ["delete-generic-password", "-s", "com.example.lifeline", "-a", UUID, slot.login_keychain()],
            [a for a, _ in self.fake.calls],
        )

    def test_remove_of_missing_item_is_done(self):
        self.store.remove(UUID)
        self.assertIsNone(self.store.get(UUID))

    def test_remove_deletes_previous_first_and_stops_if_that_fails(self):
        self.store.put(UUID, oauth())
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.hook = failing("delete-generic-password", PREVIOUS)
        self.fake.calls.clear()
        with self.assertRaises(StoreError):
            self.store.remove(UUID)
        self.assertIsNotNone(self.fake.text(UUID))
        accounts = [a[a.index("-a") + 1] for a, _ in self.fake.calls if a[0] == "delete-generic-password"]
        self.assertEqual(accounts, [PREVIOUS])

    def test_remove_order_is_previous_then_main(self):
        self.store.put(UUID, oauth())
        self.fake.set(PREVIOUS, {"claudeAiOauth": oauth("old")})
        self.fake.calls.clear()
        self.store.remove(UUID)
        accounts = [a[a.index("-a") + 1] for a, _ in self.fake.calls if a[0] == "delete-generic-password"]
        self.assertEqual(accounts, [PREVIOUS, UUID])

    def test_remove_failure_on_main_raises(self):
        self.store.put(UUID, oauth())
        self.fake.hook = failing("delete-generic-password", UUID)
        with self.assertRaises(StoreError):
            self.store.remove(UUID)

    def test_oversize_oauth_is_refused_with_zero_runner_calls(self):
        big = oauth()
        big["extra"] = "x" * 6000
        with self.assertRaises(StoreError) as caught:
            self.store.put(UUID, big)
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn("SECRETVALUE", chain_text(caught.exception))

    def test_size_that_fits_the_main_name_but_not_the_previous_name_is_refused_up_front(self):
        edge = edge_oauth()
        with self.assertRaises(StoreError):
            self.store.put(UUID, edge)
        self.assertEqual(self.fake.calls, [])

    def test_value_that_would_overflow_only_as_previous_is_refused_before_any_write(self):
        self.fake.set(UUID, {"claudeAiOauth": edge_oauth()})
        self.fake.calls.clear()
        with self.assertRaises(StoreError):
            self.store.put(UUID, oauth("new"))
        self.assertFalse([a for a, _ in self.fake.calls if a[0] in ("-i", "add-generic-password")])
        self.assertEqual(json.loads(self.fake.text(UUID)), {"claudeAiOauth": edge_oauth()})

    def test_non_json_oauth_writes_nothing(self):
        for bad in ({"a": object()}, {"a": {1, 2}}, {"a": float("nan")}, {(1, 2): "x"}):
            with self.assertRaises(StoreError):
                self.store.put(UUID, bad)
        self.assertEqual(self.fake.calls, [])

    def test_no_secret_in_any_exception_text(self):
        for command in ("write", "find-generic-password", "delete-generic-password"):
            for account in (UUID, PREVIOUS):
                fake = FakeKeychain()
                keychain = MacKeychainStore(runner=fake)
                keychain.put(UUID, oauth("old"))
                fake.hook = failing(command, account)
                for action in (
                    lambda: keychain.put(UUID, oauth("new")),
                    lambda: keychain.get(UUID),
                    lambda: keychain.remove(UUID),
                ):
                    try:
                        action()
                    except StoreError as error:
                        text = chain_text(error)
                        self.assertNotIn("SECRETVALUE", text)
                        self.assertNotIn("sk-ant", text)
                    fake.hook = failing(command, account)

    def test_bad_uuids_never_reach_the_keychain(self):
        bad = [
            "",
            "..",
            "../x",
            "a/b",
            "a\\b",
            "a.b",
            UUID + ".previous",
            "a b",
            "a\nb",
            "a\0b",
            'a"b',
            "a;b",
            "é",
            "ａｂｃ",
            UUID + "\n",
            None,
            5,
            b"abc",
        ]
        for value in bad:
            for action in (
                lambda v=value: self.store.put(v, oauth()),
                lambda v=value: self.store.get(v),
                lambda v=value: self.store.remove(v),
            ):
                with self.assertRaises(StoreError, msg=repr(value)):
                    action()
        self.assertEqual(self.fake.calls, [])

    def test_good_uuid_shapes_are_accepted(self):
        for value in (UUID, "abc", "ABC-123", "0", "a-b-c"):
            self.store.put(value, oauth())
            self.assertEqual(self.store.get(value), oauth())

    def test_every_runner_call_is_for_service_lifeline(self):
        self.store.put(UUID, oauth("old"))
        self.store.put(UUID, oauth("new"))
        self.store.get(UUID)
        self.store.remove(UUID)
        self.assertTrue(self.fake.calls)
        for argv, stdin in self.fake.calls:
            if argv[0] == "-i":
                words = shlex.split(stdin.split("\n")[0])
                self.assertEqual(words[words.index("-s") + 1], "com.example.lifeline")
            else:
                self.assertEqual(argv[argv.index("-s") + 1], "com.example.lifeline")
                self.assertNotIn("Claude Code-credentials", argv)


class RealRunnerTests(unittest.TestCase):
    def test_store_never_writes_vault(self):
        calls = []
        fake = FakeKeychain()

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            args = argv[1:]
            output = fake(args, kwargs.get("input"))
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

        def raising_run(argv, **kwargs):
            try:
                return fake_run(argv, **kwargs)
            except SecurityError as error:
                return subprocess.CompletedProcess(argv, error.returncode, stdout="", stderr="")

        with mock.patch.object(slot.subprocess, "run", raising_run):
            keychain = MacKeychainStore()
            keychain.put(UUID, oauth("old"))
            keychain.put(UUID, oauth("new"))
            self.assertEqual(keychain.get(UUID), oauth("new"))
            self.assertTrue(fake.items)
            self.assertTrue(all(service == "com.example.lifeline" for service, _ in fake.items))
            keychain.remove(UUID)
        self.assertTrue(calls)
        stdin_lines = [i for a, i in fake.calls if a[0] == "-i"]
        self.assertTrue(stdin_lines)
        for line in stdin_lines:
            words = shlex.split(line.split("\n")[0])
            self.assertEqual(words[words.index("-s") + 1], "com.example.lifeline")
        for argv in calls:
            self.assertEqual(argv[0], "/usr/bin/security")
            self.assertFalse(any("vault" in part.lower() for part in argv))
            blob = " ".join(argv)
            for secret in (ACCESS, REFRESH):
                for tag in ("old", "new"):
                    self.assertNotIn(secret + tag, blob)
                    self.assertNotIn((secret + tag).encode().hex(), blob)
        self.assertFalse([a for a in calls if a[1] == "add-generic-password"])

    def test_module_has_no_vault_or_other_writers(self):
        source = Path(store.__file__).read_text(encoding="utf-8")
        self.assertNotIn("vault", source.lower())
        self.assertNotIn("subprocess", source)


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.home / "signins"

    def make(self):
        return FileStore(self.folder)

    def test_default_folder_is_created_700_under_config(self):
        file_store = FileStore()
        default = self.home / ".config" / "com.example.lifeline" / "signins"
        self.assertEqual(file_store.folder, default)
        self.assertTrue(default.is_dir())
        self.assertEqual(stat.S_IMODE(default.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(default.parent.stat().st_mode), 0o700)

    def test_default_is_never_inside_the_slot_folder(self):
        FileStore()
        self.assertFalse((self.home / ".claude").exists())

    def test_round_trip_and_file_naming_and_modes(self):
        file_store = self.make()
        file_store.put(UUID, oauth())
        path = self.folder / (UUID + ".json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(file_store.get(UUID), oauth())
        self.assertEqual(json.loads(path.read_text()), {"claudeAiOauth": oauth()})
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [UUID + ".json"])

    def test_missing_is_none(self):
        self.assertIsNone(self.make().get(UUID))

    def test_put_overwrite_leaves_one_file_and_new_value(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        file_store.put(UUID, oauth("new"))
        self.assertEqual(file_store.get(UUID), oauth("new"))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [UUID + ".json"])

    def test_failed_write_keeps_previous_and_old_value(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        real = os.replace

        def replace(src, dst):
            if str(dst).endswith(UUID + ".json"):
                raise OSError("disk full")
            return real(src, dst)

        with mock.patch.object(slot.os, "replace", replace):
            with self.assertRaises(StoreError) as caught:
                file_store.put(UUID, oauth("new"))
        self.assertNotIn("SECRETVALUE", chain_text(caught.exception))
        self.assertTrue((self.folder / (PREVIOUS + ".json")).exists())
        self.assertEqual(file_store.get(UUID), oauth("old"))

    def test_failure_copying_previous_keeps_old_value(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        real = os.replace

        def replace(src, dst):
            if str(dst).endswith(PREVIOUS + ".json"):
                raise OSError("denied")
            return real(src, dst)

        with mock.patch.object(slot.os, "replace", replace):
            with self.assertRaises(StoreError):
                file_store.put(UUID, oauth("new"))
        self.assertEqual(file_store.get(UUID), oauth("old"))

    def test_failure_removing_previous_retries_then_says_an_old_copy_remains(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith(PREVIOUS + ".json"):
                raise OSError("busy")
            return real(path, *args, **kwargs)

        with mock.patch.object(store.os, "unlink", unlink):
            with self.assertRaises(StoreLeftover):
                file_store.put(UUID, oauth("new"))
        self.assertEqual(file_store.get(UUID), oauth("new"))
        self.assertTrue((self.folder / (PREVIOUS + ".json")).exists())

    def test_damaged_main_reads_previous_and_get_writes_nothing(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        (self.folder / (PREVIOUS + ".json")).write_text(json.dumps({"claudeAiOauth": oauth("old")}))
        (self.folder / (UUID + ".json")).write_text("{broken")
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        self.assertEqual(file_store.get(UUID), oauth("old"))
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, before)

    def test_missing_main_with_leftover_previous_is_none(self):
        file_store = self.make()
        (self.folder / (PREVIOUS + ".json")).write_text(json.dumps({"claudeAiOauth": oauth("old")}))
        self.assertIsNone(file_store.get(UUID))

    def test_remove_stops_when_previous_cannot_be_deleted(self):
        file_store = self.make()
        file_store.put(UUID, oauth())
        (self.folder / (PREVIOUS + ".json")).write_text("{}")
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith(PREVIOUS + ".json"):
                raise OSError("busy")
            return real(path, *args, **kwargs)

        with mock.patch.object(store.os, "unlink", unlink):
            with self.assertRaises(StoreError):
                file_store.remove(UUID)
        self.assertTrue((self.folder / (UUID + ".json")).exists())

    def test_file_that_is_not_utf8_is_damaged_and_leaks_nothing(self):
        file_store = self.make()
        file_store.put(UUID, oauth())
        (self.folder / (UUID + ".json")).write_bytes(b"\xff" + ACCESS.encode() + REFRESH.encode())
        for action in (lambda: file_store.get(UUID), lambda: file_store.put(UUID, oauth("new"))):
            try:
                action()
            except StoreError as error:
                text = chain_text(error)
                self.assertNotIn("SECRETVALUE", text)
                self.assertNotIn("sk-ant", text)
                self.assertIsNone(error.__context__)
        file_store.put(UUID, oauth("new"))
        self.assertEqual(file_store.get(UUID), oauth("new"))

    def test_non_utf8_file_without_previous_raises_clean_error(self):
        file_store = self.make()
        (self.folder / (UUID + ".json")).write_bytes(b"\xff" + ACCESS.encode())
        with self.assertRaises(StoreError) as caught:
            file_store.get(UUID)
        self.assertNotIn("SECRETVALUE", chain_text(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_damaged_main_without_previous_raises(self):
        file_store = self.make()
        file_store.put(UUID, oauth())
        (self.folder / (UUID + ".json")).write_text("{broken")
        with self.assertRaises(StoreError):
            file_store.get(UUID)

    def test_remove_deletes_both_and_missing_is_done(self):
        file_store = self.make()
        file_store.put(UUID, oauth())
        (self.folder / (PREVIOUS + ".json")).write_text("{}")
        file_store.remove(UUID)
        self.assertEqual(list(self.folder.iterdir()), [])
        file_store.remove(UUID)

    def test_bad_uuids_never_touch_the_disk(self):
        file_store = self.make()
        outside = self.home / "outside.json"
        for value in ("../outside", "a/b", "", "a.b", "..", UUID + "\n", None):
            for action in (
                lambda v=value: file_store.put(v, oauth()),
                lambda v=value: file_store.get(v),
                lambda v=value: file_store.remove(v),
            ):
                with self.assertRaises(StoreError, msg=repr(value)):
                    action()
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_file_store_outside_claude_folder(self):
        slot_folder = self.home / ".claude"
        slot_folder.mkdir()
        for target in (slot_folder, slot_folder / "signins", slot_folder / "a" / "b"):
            with self.assertRaises(StoreError):
                FileStore(target)
        self.assertEqual(list(slot_folder.iterdir()), [])

    def test_refuses_folder_reached_through_a_symlink(self):
        slot_folder = self.home / ".claude"
        slot_folder.mkdir()
        link = self.home / "innocent"
        link.symlink_to(slot_folder)
        with self.assertRaises(StoreError):
            FileStore(link)
        with self.assertRaises(StoreError):
            FileStore(link / "deeper")
        self.assertEqual(list(slot_folder.iterdir()), [])

    def test_refuses_when_the_slot_folder_is_itself_a_symlink(self):
        real = self.home / "elsewhere"
        real.mkdir()
        (self.home / ".claude").symlink_to(real)
        with self.assertRaises(StoreError):
            FileStore(real / "signins")
        with self.assertRaises(StoreError):
            FileStore(self.home / ".claude" / "signins")
        self.assertEqual(list(real.iterdir()), [])

    def test_refuses_dotdot_path_that_lands_inside(self):
        (self.home / ".claude").mkdir()
        with self.assertRaises(StoreError):
            FileStore(self.home / "signins" / ".." / ".claude" / "x")

    def test_sibling_with_similar_name_is_allowed(self):
        FileStore(self.home / ".claude-old" / "signins")
        FileStore(self.home / ".claudex")

    def test_folder_swapped_for_a_symlink_into_slot_folder_stops_writes(self):
        file_store = self.make()
        slot_folder = self.home / ".claude"
        slot_folder.mkdir()
        self.folder.rmdir()
        self.folder.symlink_to(slot_folder)
        with self.assertRaises(StoreError):
            file_store.put(UUID, oauth())
        self.assertEqual(list(slot_folder.iterdir()), [])

    def test_writes_stay_inside_the_folder(self):
        file_store = self.make()
        before = {p for p in self.home.rglob("*")}
        file_store.put(UUID, oauth("old"))
        file_store.put(UUID, oauth("new"))
        file_store.get(UUID)
        file_store.remove(UUID)
        created = {p for p in self.home.rglob("*")} - before
        for path in created:
            self.assertTrue(path == self.folder or self.folder in path.parents, str(path))

    def test_symlinked_sign_in_file_is_refused_for_both_names(self):
        file_store = self.make()
        slot_folder = self.home / ".claude"
        slot_folder.mkdir()
        target = slot_folder / ".credentials.json"
        target.write_text('{"claudeAiOauth":{"keep":1},"mcpOAuth":{"s":1}}')
        original = target.read_bytes()
        for name in (UUID, PREVIOUS):
            (self.folder / (name + ".json")).symlink_to(target)
        for action in (
            lambda: file_store.put(UUID, oauth("new")),
            lambda: file_store.get(UUID),
        ):
            with self.assertRaises(StoreError):
                action()
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue((self.folder / (UUID + ".json")).is_symlink())

    def test_symlinked_previous_file_is_refused_and_target_untouched(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        outside = self.home / "outside.json"
        outside.write_text("original")
        (self.folder / (PREVIOUS + ".json")).symlink_to(outside)
        with self.assertRaises(StoreError):
            file_store.put(UUID, oauth("new"))
        self.assertEqual(outside.read_text(), "original")
        self.assertEqual(file_store.get(UUID), oauth("old"))

    def test_symlinked_file_to_another_folder_is_refused(self):
        file_store = self.make()
        other = self.home / "other"
        other.mkdir()
        (other / (UUID + ".json")).write_text(json.dumps({"claudeAiOauth": oauth("planted")}))
        (self.folder / (UUID + ".json")).symlink_to(other / (UUID + ".json"))
        with self.assertRaises(StoreError):
            file_store.get(UUID)

    def test_case_variant_of_the_slot_folder_is_refused_when_the_disk_ignores_case(self):
        (self.home / ".claude").mkdir()
        if not (self.home / ".CLAUDE").exists():
            self.skipTest("this disk is case sensitive")
        with self.assertRaises(StoreError):
            FileStore(self.home / ".CLAUDE" / "signins")
        self.assertEqual(list((self.home / ".claude").iterdir()), [])

    def test_identity_check_refuses_a_link_even_when_resolution_fails(self):
        slot_folder = self.home / ".claude"
        slot_folder.mkdir()
        link = self.home / "innocent"
        link.symlink_to(slot_folder)
        with mock.patch.object(store.os.path, "realpath", lambda path, **kw: os.fspath(path)):
            with self.assertRaises(StoreError):
                FileStore(link / "deeper")
        self.assertEqual(list(slot_folder.iterdir()), [])

    def test_existing_loose_folder_is_tightened_to_700(self):
        self.folder.mkdir(mode=0o755)
        os.chmod(self.folder, 0o755)
        FileStore(self.folder)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)

    def test_no_secret_in_exception_text(self):
        file_store = self.make()
        file_store.put(UUID, oauth("old"))
        (self.folder / (UUID + ".json")).write_text("{" + ACCESS)
        with self.assertRaises(StoreError) as caught:
            file_store.get(UUID)
        self.assertNotIn("SECRETVALUE", chain_text(caught.exception))
        with self.assertRaises(StoreError) as caught:
            FileStore(self.home / ".claude" / ACCESS)
        self.assertNotIn("SECRETVALUE", chain_text(caught.exception))


DUMP = """keychain: "/private/var/test/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="com.example.lifeline"
    "acct"<blob>="a7a7a7a7-aaaa-4bbb-8ccc-0123456789ab"
    "svce"<blob>="com.example.lifeline"
keychain: "/private/var/test/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="a7a7a7a7-aaaa-4bbb-8ccc-0123456789ab.previous"
    "svce"<blob>="com.example.lifeline"
keychain: "/private/var/test/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="someone else"
    "svce"<blob>="com.example.lifeline"
keychain: "/private/var/test/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="44444444-dddd-4ddd-8ddd-000000000004"
    "svce"<blob>="Claude Code-credentials"
keychain: "/private/var/test/Library/Keychains/login.keychain-db"
version: 512
class: "inet"
attributes:
    "acct"<blob>=<NULL>
    "srvr"<blob>="example.invalid"
"""


class StoreNamesTests(unittest.TestCase):
    """Reset lists every sign-in the store holds, so a damaged account list leaves none behind."""

    def test_the_keychain_store_lists_only_its_own_sign_ins(self):
        calls = []

        def runner(args, stdin=None):
            calls.append(list(args))
            return DUMP

        self.assertEqual(MacKeychainStore(runner=runner).names(), {UUID})
        self.assertEqual(calls, [["dump-keychain", slot.login_keychain()]], "only the login keychain")
        self.assertEqual(MacKeychainStore(runner=runner, service="Claude Code-credentials").names(), {"44444444-dddd-4ddd-8ddd-000000000004"})

    def test_the_file_store_lists_its_sign_ins(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "signins"
            kept = FileStore(folder)
            kept.put(UUID, oauth())
            kept.put(UUID, oauth("two"))
            (folder / "notes.txt").write_text("x")
            (folder / "bad name!.json").write_text("{}")
            self.assertEqual(kept.names(), {UUID})


if __name__ == "__main__":
    unittest.main()
