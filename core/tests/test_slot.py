import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lifeline import slot, store
from lifeline.slot import (
    FileSlot,
    MacKeychainSlot,
    SecurityError,
    SlotError,
    access_token,
    expires_at,
    refresh_token,
    run_security,
    scopes,
)

ACCESS = "sk-ant-oat01-ACCESSSECRETVALUE"
REFRESH = "sk-ant-ort01-REFRESHSECRETVALUE"


def signin(extra=None):
    data = {
        "claudeAiOauth": {
            "accessToken": ACCESS,
            "refreshToken": REFRESH,
            "expiresAt": 1789000000000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        },
        "mcpOAuth": {"server": {"token": "keep-me"}},
    }
    if extra:
        data.update(extra)
    return data


class FakeSecurity:
    def __init__(self, stored=None, fail_on=None, drop_writes=False):
        self.stored = stored
        self.calls = []
        self.fail_on = fail_on
        self.drop_writes = drop_writes

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        if self.fail_on and args[0] == self.fail_on[0]:
            raise SecurityError(args[0], self.fail_on[1], "failed")
        if args[0] == "find-generic-password":
            if self.stored is None:
                raise SecurityError(args[0], 44, "The specified item could not be found in the keychain.")
            return self.stored + "\n"
        if args[0] == "-i":
            if not self.drop_writes:
                line = stdin.split("\n")[0][:4095]
                words = line.split()
                if "-X" not in words:
                    found = shlex.split(line)
                    self.stored = found[found.index("-w") + 1]
                    return ""
                digits = words[words.index("-X") + 1]
                self.stored = bytes.fromhex(digits[: len(digits) // 2 * 2]).decode("utf-8", "replace")
            return ""
        if args[0] == "delete-generic-password":
            if self.stored is None:
                raise SecurityError(args[0], 44, "")
            self.stored = None
            return ""
        raise AssertionError(args)

    def update(self, service, account, data, keychain=None):
        self.updated = (service, account, data)
        if self.fail_on and self.fail_on[0] == "update":
            return self.fail_on[1]
        if self.stored is None:
            return slot.NOT_FOUND
        if not self.drop_writes:
            self.stored = data.decode("utf-8")
        return 0


class MacKeychainSlotTests(unittest.TestCase):
    def test_read_parses_claude_json(self):
        fake = FakeSecurity(json.dumps(signin()))
        result = MacKeychainSlot(runner=fake, account="Sam").read()
        self.assertEqual(result, signin())
        argv, stdin = fake.calls[0]
        self.assertEqual(
            argv,
            ["find-generic-password", "-s", "Claude Code-credentials", "-a", "Sam", "-w", slot.login_keychain()],
        )
        self.assertIsNone(stdin)

    def test_read_missing_item_is_none(self):
        self.assertIsNone(MacKeychainSlot(runner=FakeSecurity(None), account="Sam").read())

    def test_read_other_failure_raises(self):
        fake = FakeSecurity(None, fail_on=("find-generic-password", 51))
        with self.assertRaises(SecurityError):
            MacKeychainSlot(runner=fake, account="Sam").read()

    def test_read_empty_item_is_none(self):
        self.assertIsNone(MacKeychainSlot(runner=FakeSecurity(""), account="Sam").read())

    def test_read_garbage_raises_without_leaking(self):
        fake = FakeSecurity("not json " + ACCESS)
        with self.assertRaises(SlotError) as ctx:
            MacKeychainSlot(runner=fake, account="Sam").read()
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_read_non_object_raises(self):
        with self.assertRaises(SlotError):
            MacKeychainSlot(runner=FakeSecurity("[1,2]"), account="Sam").read()

    def test_read_hex_encoded_output(self):
        encoded = json.dumps(signin()).encode().hex()
        self.assertEqual(MacKeychainSlot(runner=FakeSecurity(encoded), account="Sam").read(), signin())

    def test_write_never_passes_secret_in_argv(self):
        fake = FakeSecurity()
        MacKeychainSlot(runner=fake, account="Sam").write(signin())
        write_calls = [c for c in fake.calls if c[0][0] == "-i"]
        self.assertEqual(len(write_calls), 1)
        for argv, _ in fake.calls:
            blob = " ".join(argv)
            self.assertNotIn(ACCESS, blob)
            self.assertNotIn(REFRESH, blob)
            self.assertNotIn(ACCESS.encode().hex(), blob)
        self.assertEqual(write_calls[0][0], ["-i"])

    def test_write_command_on_stdin_with_hex_and_quoting(self):
        fake = FakeSecurity()
        MacKeychainSlot(runner=fake, account="Sam").write(signin())
        stdin = [c for c in fake.calls if c[0][0] == "-i"][0][1]
        compact = json.dumps(signin(), separators=(",", ":"))
        self.assertEqual(
            stdin,
            'add-generic-password -U -a "Sam" -s "Claude Code-credentials" -X '
            + compact.encode().hex()
            + " "
            + slot._quote(slot.login_keychain())
            + "\n",
        )
        self.assertEqual(stdin.count("\n"), 1)

    def test_write_roundtrip_keeps_every_field(self):
        fake = FakeSecurity()
        slot_ = MacKeychainSlot(runner=fake, account="Sam")
        original = signin({"extra": {"unicode": "café ✓", "quote": 'a"b\\c'}})
        slot_.write(original)
        self.assertEqual(slot_.read(), original)

    def test_write_stored_form_is_compact(self):
        fake = FakeSecurity()
        MacKeychainSlot(runner=fake, account="Sam").write(signin())
        self.assertEqual(fake.stored, json.dumps(signin(), separators=(",", ":")))

    def test_write_failure_message_has_no_secret(self):
        fake = FakeSecurity(fail_on=("-i", 1))
        with self.assertRaises(SecurityError) as ctx:
            MacKeychainSlot(runner=fake, account="Sam").write(signin())
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(ACCESS, text)
        self.assertNotIn(REFRESH, text)
        self.assertNotIn(ACCESS.encode().hex()[:40], text)

    def test_write_that_did_not_land_is_detected(self):
        fake = FakeSecurity(stored=json.dumps({"old": 1}), drop_writes=True)
        with self.assertRaises(SlotError) as ctx:
            MacKeychainSlot(runner=fake, account="Sam").write(signin())
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_write_rejects_non_dict(self):
        with self.assertRaises(SlotError):
            MacKeychainSlot(runner=FakeSecurity(), account="Sam").write(["x"])

    def test_names_with_quotes_are_escaped(self):
        fake = FakeSecurity()
        MacKeychainSlot(runner=fake, service='Odd "svc"\\', account='Acc "x"').write(signin())
        stdin = [c for c in fake.calls if c[0][0] == "-i"][0][1]
        self.assertIn('-a "Acc \\"x\\"" -s "Odd \\"svc\\"\\\\" -X', stdin)

    def test_names_with_newline_rejected(self):
        with self.assertRaises(SlotError):
            MacKeychainSlot(runner=FakeSecurity(), account="a\nb").write(signin())

    def test_default_account_is_user_name(self):
        with mock.patch.dict(os.environ, {"USER": "Someone"}):
            self.assertEqual(MacKeychainSlot(runner=FakeSecurity()).account, "Someone")

    def test_default_service(self):
        self.assertEqual(MacKeychainSlot(runner=FakeSecurity(), account="a").service, "Claude Code-credentials")

    def test_delete_removes_the_item_and_a_missing_item_is_fine(self):
        fake = FakeSecurity(json.dumps(signin()))
        item = MacKeychainSlot(runner=fake, account="Sam")
        item.delete()
        self.assertIsNone(item.read())
        item.delete()
        self.assertEqual(fake.calls[0][0], ["delete-generic-password", "-s", "Claude Code-credentials", "-a", "Sam", slot.login_keychain()])
        with self.assertRaises(SecurityError):
            MacKeychainSlot(runner=FakeSecurity(fail_on=("delete-generic-password", 51)), account="Sam").delete()


PREFIX = 'add-generic-password -U -a "Sam" -s "Lifeline test" -X '
KC = "/k.keychain"
SUFFIX = ' "/k.keychain"'


def padded(command_length):
    for n in range(8000):
        data = {"p": "x" * n}
        compact = json.dumps(data, separators=(",", ":"))
        if len(PREFIX) + 2 * len(compact) + len(SUFFIX) + 1 == command_length:
            return data
    raise AssertionError(command_length)


class LargeItemTests(unittest.TestCase):
    def test_item_at_limit_goes_through_stdin(self):
        fake = FakeSecurity()
        data = padded(4032)
        MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write(data)
        writes = [c for c in fake.calls if c[0][0] == "-i"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0][1].encode()), 4032)
        self.assertFalse(hasattr(fake, "direct"))
        self.assertEqual(json.loads(fake.stored), data)

    def test_item_over_limit_goes_through_the_framework_never_a_command_line(self):
        fake = FakeSecurity(stored="{}")
        data = padded(4034)
        with mock.patch.object(slot, "update_item", fake.update):
            MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write(data)
        self.assertEqual(fake.updated, ("Lifeline test", "Sam", json.dumps(data, separators=(",", ":")).encode()))
        self.assertEqual([argv for argv, _ in fake.calls], [["find-generic-password", "-s", "Lifeline test", "-a", "Sam", "-w", KC]])
        self.assertEqual(json.loads(fake.stored), data)

    def test_a_missing_big_item_is_made_by_security_first_so_claude_code_can_still_read_it(self):
        fake = FakeSecurity()
        data = padded(4034)
        with mock.patch.object(slot, "update_item", fake.update):
            MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write(data)
        made = [stdin for argv, stdin in fake.calls if argv == ["-i"]]
        self.assertEqual(made, ['add-generic-password -a "Sam" -s "Lifeline test" -w "{}" "/k.keychain"\n'])
        self.assertEqual(json.loads(fake.stored), data)

    def test_big_item_with_mcp_sign_ins_round_trips(self):
        fake = FakeSecurity()
        data = signin({"mcpOAuth": {f"s{n}": {"token": "t" * 200} for n in range(30)}})
        slot_ = MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC)
        with mock.patch.object(slot, "update_item", fake.update):
            slot_.write(data)
        self.assertEqual(slot_.read(), data)

    def test_model_corrupts_if_size_branch_is_removed(self):
        fake = FakeSecurity()
        data = padded(5000)
        command = PREFIX + json.dumps(data, separators=(",", ":")).encode().hex() + "\n"
        fake(["-i"], stdin=command)
        self.assertNotEqual(fake.stored, json.dumps(data, separators=(",", ":")))

    def test_over_limit_failure_has_no_secret(self):
        fake = FakeSecurity(stored="{}", fail_on=("update", -25308))
        data = {"claudeAiOauth": {"accessToken": ACCESS}, "pad": "x" * 3000}
        with mock.patch.object(slot, "update_item", fake.update), self.assertRaises(SlotError) as ctx:
            MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write(data)
        self.assertEqual(str(ctx.exception), slot.KEYCHAIN_ASKS)
        self.assertEqual(fake.stored, "{}", "an item that was there is left as it was")
        fake = FakeSecurity(stored="{}", fail_on=("update", -50))
        with mock.patch.object(slot, "update_item", fake.update), self.assertRaises(SlotError) as ctx:
            MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write(data)
        self.assertEqual(str(ctx.exception), slot.KEYCHAIN_UNSAVED)
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(ACCESS, text)
        self.assertNotIn(ACCESS.encode().hex(), text)

    def test_non_json_values_do_not_cause_false_mismatch(self):
        fake = FakeSecurity()
        MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write({"scopes": ("a", "b")})

    def test_mismatch_message(self):
        fake = FakeSecurity(stored="{}", drop_writes=True)
        with self.assertRaises(SlotError) as ctx:
            MacKeychainSlot("Lifeline test", "Sam", runner=fake, keychain=KC).write(signin())
        self.assertIn("did not hold what was written", str(ctx.exception))


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("LIFELINE_LIVE_KEYCHAIN") == "1",
    "The suite makes no Keychain call; set LIFELINE_LIVE_KEYCHAIN=1 to run these on a throwaway keychain file",
)
class ThrowawayKeychainTests(unittest.TestCase):
    """The real Security.framework write, with prompts off, in a throwaway keychain file made for this test, never the
    login keychain, Claude Code's item or Lifeline's. Each item is made the way Claude Code makes its own (security, on
    stdin), and the keychain is deleted afterwards. The search list is checked unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.guard = mock.patch.dict(os.environ, {slot.TEST_GUARD: ""})
        cls.guard.start()
        cls.addClassCleanup(cls.guard.stop)
        cls.folder = tempfile.mkdtemp(prefix="lifeline-keychain-", dir="/private/tmp")
        cls.path = os.path.join(cls.folder, "throwaway.keychain")
        cls.listed = run_security(["list-keychains", "-d", "user"])
        run_security(["create-keychain", "-p", "throwaway", cls.path])
        run_security(["unlock-keychain", "-p", "throwaway", cls.path])
        run_security(["set-keychain-settings", cls.path])

    @classmethod
    def tearDownClass(cls):
        run_security(["delete-keychain", cls.path])
        shutil.rmtree(cls.folder, ignore_errors=True)
        if run_security(["list-keychains", "-d", "user"]) != cls.listed:
            raise AssertionError("the keychain search list changed")

    def setUp(self):
        self.service = f"Lifeline test {os.urandom(6).hex()}"
        self.item = MacKeychainSlot(self.service, "lifeline-test", keychain=self.path)

    def held(self):
        try:
            return run_security(["find-generic-password", "-s", self.service, "-a", "lifeline-test", self.path])
        except SecurityError as error:
            self.assertEqual(error.returncode, slot.ITEM_NOT_FOUND)
            return None

    def big(self):
        value = signin({"mcpOAuth": {f"server-{n}|{n:04x}": {"accessToken": "a" * 160, "refreshToken": "r" * 160} for n in range(24)}})
        self.assertGreater(len(slot._compact(value)), 8000)
        return value

    def test_a_sign_in_as_big_as_claude_codes_is_written_with_prompts_off(self):
        self.item.write(signin())
        self.assertIsNotNone(self.held())
        value = self.big()
        self.item.write(value)
        self.assertEqual(self.item.read(), value)

    def test_a_missing_item_is_made_by_security_then_written(self):
        self.assertIsNone(self.held())
        value = self.big()
        self.item.write(value)
        self.assertEqual(self.item.read(), value)

    def test_the_framework_call_says_when_there_is_no_item(self):
        self.assertEqual(slot.update_item(self.service, "lifeline-test", b"{}", self.path), slot.NOT_FOUND)

    def test_a_refused_write_leaves_no_placeholder_behind(self):
        with mock.patch.object(slot, "update_item", side_effect=[slot.NOT_FOUND, slot.ASKS]), self.assertRaises(SlotError) as ctx:
            self.item.write(self.big())
        self.assertEqual(str(ctx.exception), slot.KEYCHAIN_ASKS)
        self.assertIsNone(self.held())


class KeychainGuardTests(unittest.TestCase):
    """Under the tests, a call that would reach a real Keychain item without a fake raises at once.
    The stand-ins below record any call that gets past the guard; none does, and nothing reaches the Keychain."""

    def test_the_guard_is_on_under_the_tests(self):
        self.assertEqual(os.environ.get(slot.TEST_GUARD), "1")

    def test_a_real_item_without_a_fake_raises_before_any_keychain_access(self):
        reached = []

        def stand_in(*args, **options):
            reached.append(args)
            raise AssertionError("the guard let a call through")

        uuid = "3f2b8c1e-aaaa-4bbb-8ccc-0123456789ab"
        calls = (
            lambda: MacKeychainSlot(account="Sam").read(),
            lambda: MacKeychainSlot(account="Sam").write(signin()),
            lambda: store.MacKeychainStore().get(uuid),
            lambda: store.MacKeychainStore().names(),
            lambda: run_security(["delete-generic-password", "-s", "Claude Code-credentials-0123abcd", slot.login_keychain()]),
            lambda: slot.update_item("Claude Code-credentials", "Sam", b"{}", slot.login_keychain()),
            lambda: slot.update_item(store.STORE_SERVICE, uuid, b"{}", slot.login_keychain()),
            lambda: MacKeychainSlot(account="Sam", runner=FakeSecurity()).write(signin({"pad": "x" * 5000})),
        )
        with mock.patch.object(slot.subprocess, "run", stand_in), mock.patch.object(slot, "_RUN", stand_in):
            with mock.patch.object(slot.ctypes, "CDLL", stand_in), mock.patch.object(slot, "_CDLL", stand_in):
                for call in calls:
                    with self.assertRaises(slot.KeychainGuard):
                        call()
        self.assertEqual(reached, [])

    def test_a_faked_call_passes_and_a_throwaway_name_without_a_fake_raises_too(self):
        done = mock.Mock(returncode=44, stdout="", stderr="")
        with mock.patch.object(slot.subprocess, "run", return_value=done):
            self.assertIsNone(MacKeychainSlot(account="Sam").read())
        reached = []

        def stand_in(*args, **options):
            reached.append(args)

        with mock.patch.object(slot.subprocess, "run", stand_in), mock.patch.object(slot, "_RUN", stand_in):
            with self.assertRaises(slot.KeychainGuard):
                MacKeychainSlot("Lifeline test", "nobody").read()
        with mock.patch.object(slot.ctypes, "CDLL", stand_in), mock.patch.object(slot, "_CDLL", stand_in):
            with self.assertRaises(slot.KeychainGuard):
                slot.update_item("Lifeline test", "nobody", b"{}", "/k.keychain")
        self.assertEqual(reached, [])


class LoginKeychainOnlyTests(unittest.TestCase):
    """Lifeline names the login keychain in every Keychain call, so a lookup for a missing item never
    reaches another keychain, such as a locked one that would open a password dialog. Nothing here calls the real
    Keychain: security is replaced at subprocess, and the framework call is caught before it runs."""

    def test_every_security_call_names_the_login_keychain(self):
        from lifeline import enroll

        login, keychain, seen = slot.login_keychain(), {}, []

        def run(argv, input=None, **options):
            seen.append((list(argv), input))
            args = argv[1:]
            if args[0] == "-i":
                words = shlex.split(input.split("\n")[0])
                value = bytes.fromhex(words[words.index("-X") + 1]).decode() if "-X" in words else words[words.index("-w") + 1]
                keychain[(words[words.index("-s") + 1], words[words.index("-a") + 1])] = value
                return subprocess.CompletedProcess(argv, 0, "", "")
            if args[0] == "dump-keychain":
                return subprocess.CompletedProcess(argv, 0, "", "")
            found = (args[args.index("-s") + 1], args[args.index("-a") + 1] if "-a" in args else None)
            if found not in keychain:
                return subprocess.CompletedProcess(argv, 44, "", "not found")
            if args[0] == "delete-generic-password":
                del keychain[found]
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, keychain[found] + "\n", "")

        places = []

        def update(service, account, data, where):
            places.append(where)
            if (service, account) not in keychain:
                return slot.NOT_FOUND
            keychain[(service, account)] = data.decode()
            return 0

        uuid = "3f2b8c1e-aaaa-4bbb-8ccc-0123456789ab"
        with mock.patch.object(slot.subprocess, "run", run), mock.patch.object(slot, "update_item", update):
            item = MacKeychainSlot(account="Sam")
            item.read()
            item.write(signin())
            item.write(signin({"pad": "x" * 5000}))
            item.read()
            with mock.patch.object(slot, "update_item", side_effect=[slot.NOT_FOUND, slot.ASKS]) as refused, self.assertRaises(SlotError):
                MacKeychainSlot(service="Lifeline other", account="Sam").write(signin({"pad": "x" * 5000}))
            places += [call.args[3] for call in refused.call_args_list]
            parked = store.MacKeychainStore()
            parked.get(uuid)
            parked.put(uuid, signin()["claudeAiOauth"])
            parked.put(uuid, signin()["claudeAiOauth"])
            parked.names()
            parked.remove(uuid)
            enroll._delete_item(run_security, "Claude Code-credentials-0123abcd")
        self.assertTrue(len(seen) > 10)
        for argv, stdin in seen:
            if argv[1:] == ["-i"]:
                self.assertTrue(stdin.rstrip("\n").endswith(slot._quote(login)), stdin[:60])
            else:
                self.assertEqual(argv[-1], login, argv)
        self.assertEqual(places, [login, login, login])

    @unittest.skipUnless(sys.platform == "darwin", "Security.framework is on the Mac only")
    def test_the_framework_call_searches_only_the_login_keychain(self):
        import ctypes

        real = ctypes.CDLL
        cf = real("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security = real("/System/Library/Frameworks/Security.framework/Security")
        pointer = ctypes.c_void_p
        cf.CFDictionaryGetValue.restype = cf.CFArrayGetValueAtIndex.restype = pointer
        cf.CFDictionaryGetValue.argtypes = [pointer, pointer]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [pointer]
        cf.CFArrayGetValueAtIndex.argtypes = [pointer, ctypes.c_long]
        seen = {}

        class Caught:
            _handle = security._handle

            def __getattr__(self, name):
                return getattr(security, name)

        def update(query, change):
            listed = cf.CFDictionaryGetValue(query, pointer.in_dll(security, "kSecMatchSearchList").value)
            paths = []
            for index in range(cf.CFArrayGetCount(listed) if listed else 0):
                text = ctypes.create_string_buffer(1024)
                cf.CFStringGetCString(cf.CFArrayGetValueAtIndex(listed, index), text, 1024, 0x08000100)
                paths.append(text.value.decode())
            seen["paths"] = paths
            return slot.NOT_FOUND

        def opened(path, out):
            seen["opened"] = path
            out._obj.value = cf.CFStringCreateWithCString(None, path, 0x08000100)
            return 0

        cf.CFStringCreateWithCString.restype = pointer
        cf.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        caught = Caught()
        caught.SecKeychainOpen = opened
        caught.SecItemUpdate = update
        caught.SecKeychainSetUserInteractionAllowed = lambda allowed: seen.setdefault("interaction", allowed)
        with mock.patch.object(ctypes, "CDLL", lambda path: caught if "Security.framework" in path else real(path)):
            self.assertEqual(slot.update_item("Lifeline test", "nobody", b"{}", slot.login_keychain()), slot.NOT_FOUND)
        login = slot.login_keychain()
        self.assertEqual(seen, {"opened": login.encode(), "interaction": False, "paths": [login]})


class NoTokenOnACommandLineTests(unittest.TestCase):
    """Any local user can read a command line with ps, so no Keychain write ever puts a sign-in on one."""

    def test_no_keychain_write_puts_a_sign_in_on_a_command_line(self):
        keychain, seen = {}, []

        def run(argv, input=None, **options):
            seen.append(list(argv))
            args = argv[1:]
            if args[0] == "-i":
                words = shlex.split(input.split("\n")[0])
                found = (words[words.index("-s") + 1], words[words.index("-a") + 1])
                keychain[found] = bytes.fromhex(words[words.index("-X") + 1]).decode() if "-X" in words else words[-1]
                return subprocess.CompletedProcess(argv, 0, "", "")
            found = (args[args.index("-s") + 1], args[args.index("-a") + 1])
            if found not in keychain:
                return subprocess.CompletedProcess(argv, 44, "", "not found")
            if args[0] == "delete-generic-password":
                del keychain[found]
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, keychain[found] + "\n" if "-w" in args else "attributes\n", "")

        def update(service, account, data, place=None):
            if (service, account) not in keychain:
                return slot.NOT_FOUND
            keychain[(service, account)] = data.decode()
            return 0

        uuid = "3f2b8c1e-aaaa-4bbb-8ccc-0123456789ab"
        with mock.patch.object(slot.subprocess, "run", run), mock.patch.object(slot, "update_item", update):
            for size in (10, 3000, 30000):
                item = signin({"pad": "x" * size})
                MacKeychainSlot(account="Sam").write(item)
                self.assertEqual(MacKeychainSlot(account="Sam").read(), item)
                parked = store.MacKeychainStore()
                try:
                    parked.put(uuid, dict(item["claudeAiOauth"], pad="x" * size))
                    parked.put(uuid, dict(item["claudeAiOauth"], pad="y" * size))
                except store.StoreError:
                    pass
                parked.get(uuid)
                parked.remove(uuid)
        self.assertTrue(seen)
        for argv in seen:
            for word in argv:
                for token in (ACCESS, REFRESH):
                    self.assertNotIn(token, word)
                    self.assertNotIn(token.encode().hex(), word)

    def test_no_source_builds_a_keychain_write_as_a_command_line(self):
        found = [
            f"{path.name}:{number}"
            for path in sorted(Path(slot.__file__).parent.glob("*.py"))
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if '"add-generic-password",' in line or "'add-generic-password'," in line
        ]
        self.assertEqual(found, [])


class RunSecurityTests(unittest.TestCase):
    def test_timeout_is_passed_and_no_secret_in_error_or_context(self):
        boom = slot.subprocess.TimeoutExpired(["security"], 5, output=ACCESS.encode(), stderr=ACCESS.encode())
        with mock.patch.object(slot.subprocess, "run", side_effect=boom) as run:
            with self.assertRaises(SecurityError) as ctx:
                run_security(["find-generic-password"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn(ACCESS, str(ctx.exception) + repr(ctx.exception))

    def test_write_failure_by_argv_shows_no_stderr(self):
        done = mock.Mock(returncode=1, stdout="", stderr="bad " + ACCESS)
        with mock.patch.object(slot.subprocess, "run", return_value=done):
            with self.assertRaises(SecurityError) as ctx:
                run_security(["add-generic-password", "-X", ACCESS.encode().hex()])
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_uses_absolute_path_and_passes_stdin(self):
        done = mock.Mock(returncode=0, stdout="out", stderr="")
        with mock.patch.object(slot.subprocess, "run", return_value=done) as run:
            self.assertEqual(run_security(["-i"], stdin="work\n"), "out")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "-i"])
        self.assertEqual(run.call_args.kwargs["input"], "work\n")

    def test_non_zero_raises_without_stdin_or_argv_secret(self):
        done = mock.Mock(returncode=1, stdout="", stderr="boom " + ACCESS)
        with mock.patch.object(slot.subprocess, "run", return_value=done):
            with self.assertRaises(SecurityError) as ctx:
                run_security(["-i"], stdin="add-generic-password -X " + ACCESS.encode().hex())
        text = str(ctx.exception) + repr(ctx.exception)
        self.assertNotIn(ACCESS, text)
        self.assertNotIn(ACCESS.encode().hex(), text)
        self.assertEqual(ctx.exception.returncode, 1)

    def test_missing_binary_context_clean(self):
        with mock.patch.object(slot.subprocess, "run", side_effect=FileNotFoundError(ACCESS)):
            with self.assertRaises(SecurityError) as ctx:
                run_security(["-i"], stdin="x")
        self.assertIsNone(ctx.exception.__context__)
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_security_runs_in_its_own_process_group_with_no_terminal_input(self):
        code = "import os, sys; print(os.getpgrp(), os.getpid(), repr(sys.stdin.read()))"
        with mock.patch.object(slot, "SECURITY_BINARY", sys.executable):
            group, pid, given = run_security(["-I", "-c", code]).split()
        self.assertEqual(int(group), int(pid))
        self.assertNotEqual(int(group), os.getpgrp())
        self.assertEqual(given, "''")

    def test_security_input_still_reaches_it_in_its_own_group(self):
        code = "import os, sys; print(os.getpgrp() == os.getpid(), sys.stdin.read())"
        with mock.patch.object(slot, "SECURITY_BINARY", sys.executable):
            self.assertEqual(run_security(["-I", "-c", code], stdin="add-generic-password\n"), "True add-generic-password\n\n")

    def test_process_group_and_input_settings_are_passed(self):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(slot.subprocess, "run", return_value=done) as run:
            run_security(["find-generic-password"])
            run_security(["-i"], stdin="work\n")
        first, second = run.call_args_list
        self.assertEqual(first.kwargs["process_group"], 0)
        self.assertIs(first.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("input", first.kwargs)
        self.assertEqual(second.kwargs["process_group"], 0)
        self.assertEqual(second.kwargs["input"], "work\n")
        self.assertNotIn("stdin", second.kwargs)

    def test_missing_binary_raises_security_error(self):
        with mock.patch.object(slot.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(SecurityError):
                run_security(["-i"], stdin="x")


class ParseContextTests(unittest.TestCase):
    def test_garbage_has_no_context(self):
        for text in ("{bad " + ACCESS, "zz" + ACCESS, "ff"):
            with self.assertRaises(SlotError) as ctx:
                MacKeychainSlot(runner=FakeSecurity(text), account="a").read()
            self.assertIsNone(ctx.exception.__context__)
            self.assertIsNone(ctx.exception.__cause__)


class FileSlotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dot" / ".credentials.json"

    def test_read_missing_is_none(self):
        self.assertIsNone(FileSlot(self.path).read())

    def test_write_creates_parent_and_roundtrips(self):
        FileSlot(self.path).write(signin())
        self.assertEqual(FileSlot(self.path).read(), signin())
        self.assertEqual(self.path.read_text(), json.dumps(signin(), separators=(",", ":")))

    def test_file_slot_mode_600(self):
        FileSlot(self.path).write(signin())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_mode_600_after_overwrite_of_loose_file(self):
        self.path.parent.mkdir()
        self.path.write_text("{}")
        os.chmod(self.path, 0o644)
        FileSlot(self.path).write(signin())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_file_slot_atomic_when_replace_fails(self):
        FileSlot(self.path).write(signin())
        before = self.path.read_bytes()
        with mock.patch.object(slot.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                FileSlot(self.path).write({"claudeAiOauth": {"accessToken": "new"}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.path.parent), [".credentials.json"])

    def test_file_slot_atomic_when_fsync_fails(self):
        FileSlot(self.path).write(signin())
        before = self.path.read_bytes()
        with mock.patch.object(slot.os, "fsync", side_effect=OSError("io")):
            with self.assertRaises(OSError):
                FileSlot(self.path).write({"a": 1})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.path.parent), [".credentials.json"])

    def test_failed_first_write_leaves_nothing(self):
        with mock.patch.object(slot.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                FileSlot(self.path).write(signin())
        self.assertFalse(self.path.exists())
        self.assertEqual(os.listdir(self.path.parent), [])

    def test_temp_file_is_in_same_folder(self):
        seen = []
        real = os.replace

        def spy(src, dst):
            seen.append((os.path.dirname(src), os.path.dirname(dst)))
            real(src, dst)

        with mock.patch.object(slot.os, "replace", side_effect=spy):
            FileSlot(self.path).write(signin())
        self.assertEqual(seen[0][0], seen[0][1])

    def test_read_garbage_raises_without_leaking(self):
        self.path.parent.mkdir()
        self.path.write_text("garbage " + ACCESS)
        with self.assertRaises(SlotError) as ctx:
            FileSlot(self.path).read()
        self.assertNotIn(ACCESS, str(ctx.exception))

    def test_read_non_object_raises(self):
        self.path.parent.mkdir()
        self.path.write_text("[1]")
        with self.assertRaises(SlotError):
            FileSlot(self.path).read()

    def test_write_rejects_non_dict(self):
        with self.assertRaises(SlotError):
            FileSlot(self.path).write("x")
        self.assertFalse(self.path.exists())

    def test_mode_600_without_fchmod(self):
        with mock.patch.object(slot.os, "fchmod", lambda *a: None):
            FileSlot(self.path).write(signin())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_new_folder_is_700(self):
        FileSlot(self.path).write(signin())
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)

    def test_symlink_stays_and_target_gets_sign_in(self):
        real = Path(self.tmp.name) / "real" / "store.json"
        real.parent.mkdir()
        real.write_text("{}")
        self.path.parent.mkdir()
        self.path.symlink_to(real)
        FileSlot(self.path).write(signin())
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(real.read_text()), signin())
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(real.parent)), ["store.json"])

    def test_folder_fsync_failure_does_not_fail_write(self):
        real_fsync = os.fsync
        calls = []

        def flaky(fd):
            calls.append(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("dir")
            real_fsync(fd)

        with mock.patch.object(slot.os, "fsync", side_effect=flaky):
            FileSlot(self.path).write(signin())
        self.assertEqual(FileSlot(self.path).read(), signin())

    def test_repeated_writes_keep_one_file(self):
        slot_ = FileSlot(self.path)
        for n in range(20):
            slot_.write({"n": n})
        self.assertEqual(slot_.read(), {"n": 19})
        self.assertEqual(os.listdir(self.path.parent), [".credentials.json"])


class AccessorTests(unittest.TestCase):
    def test_fields(self):
        s = signin()
        self.assertEqual(access_token(s), ACCESS)
        self.assertEqual(refresh_token(s), REFRESH)
        self.assertEqual(expires_at(s), 1789000000000)
        self.assertEqual(scopes(s), ["user:inference", "user:profile"])

    def test_missing_fields(self):
        for s in ({}, {"claudeAiOauth": {}}, {"claudeAiOauth": None}, None):
            self.assertIsNone(access_token(s))
            self.assertIsNone(refresh_token(s))
            self.assertIsNone(expires_at(s))
            self.assertEqual(scopes(s), [])

    def test_scopes_wrong_type(self):
        self.assertEqual(scopes({"claudeAiOauth": {"scopes": "x"}}), [])


if __name__ == "__main__":
    unittest.main()
