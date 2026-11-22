import fcntl
import hashlib
import json
import os
import pwd
import shlex
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lifeline import enroll, recovery, switch, writelock
from lifeline.accounts import Account, Registry
from lifeline.anthropic import Profile, Unavailable
from lifeline.slot import FileSlot, MacKeychainSlot, SecurityError, SlotError
from lifeline.store import FileStore, StoreLeftover

USER = "tester"
SECRET_ACCESS = "sk-ant-oat01-NEWACCESSSECRET"
SECRET_REFRESH = "sk-ant-ort01-NEWREFRESHSECRET"
LIVE_ACCESS = "sk-ant-oat01-LIVEACCESSSECRET"
LIVE_REFRESH = "sk-ant-ort01-LIVEREFRESHSECRET"
OTHER_ACCESS = "sk-ant-oat01-OTHERACCESSSECRET"
MCP = {"server": {"accessToken": "MCPSECRETTOKEN"}}


class FakeKeychain:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.on_write = None
        self.fail_delete = False

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        if args[0] == "-i":
            words = shlex.split(stdin.split("\n")[0])
            return self._write(
                words[words.index("-s") + 1], words[words.index("-a") + 1], words[words.index("-X") + 1]
            )
        service = args[args.index("-s") + 1]
        account = args[args.index("-a") + 1] if "-a" in args else None
        if args[0] == "find-generic-password":
            if (service, account) not in self.items:
                raise SecurityError(args[0], 44, "not found")
            return self.items[(service, account)] + "\n"
        if args[0] == "delete-generic-password":
            if self.fail_delete:
                raise SecurityError(args[0], 1, "")
            found = [k for k in self.items if k[0] == service and (account is None or k[1] == account)]
            if not found:
                raise SecurityError(args[0], 44, "")
            del self.items[found[0]]
            return ""
        raise AssertionError(args)

    def _write(self, service, account, digits):
        if self.on_write:
            self.on_write(service)
        self.items[(service, account)] = bytes.fromhex(digits).decode("utf-8")
        return ""

    def put(self, service, value, account=USER):
        self.items[(service, account)] = json.dumps(value)

    def get(self, service):
        text = self.items.get((service, USER))
        return json.loads(text) if text is not None else None

    def temporary(self):
        return [k for k in self.items if k[0].startswith("Claude Code-credentials-")]


def hashed_for(folder):
    return "Claude Code-credentials-" + hashlib.sha256(str(folder).encode()).hexdigest()[:8]


def details_for(uuid, name="Ama Rivera", email="ama@example.com", tier="default_claude_max_5x"):
    value = {
        "accountUuid": uuid,
        "emailAddress": email,
        "organizationRateLimitTier": tier,
    }
    if name is not None:
        value["displayName"] = name
    return value


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.temp_root = self.root / "temp"
        self.temp_root.mkdir()
        self.keychain = FakeKeychain()
        self.store = FileStore(self.root / "signins")
        self.registry = Registry(self.root / "accounts.json")
        self.folders = []
        self.logins = 0
        self.live_uuid = "uuid-live"
        self.after_check = lambda token: None
        self.lock_path = self.root / "lifeline.lock"

    def login(self, uuid, details=None, signin=None, write_details=True, write_item=True):
        def run_login(folder):
            self.logins += 1
            self.folders.append(folder)
            if write_details:
                body = {"oauthAccount": details if details is not None else details_for(uuid), "other": 1}
                (folder / ".claude.json").write_text(json.dumps(body))
            if write_item:
                self.keychain.put(
                    hashed_for(folder),
                    signin
                    if signin is not None
                    else {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH}},
                )

        return run_login

    def profile(self, uuid):
        def profile_fn(token):
            if token == LIVE_ACCESS:
                answer = Profile(self.live_uuid, "live@example.com", "org", None)
                self.after_check(token)
                return answer
            if token == OTHER_ACCESS:
                answer = Profile("uuid-other", "other@example.com", "org", None)
                self.after_check(token)
                return answer
            return Profile(uuid, "ama@example.com", "org", "default_claude_max_5x")

        return profile_fn

    def call_enroll(self, uuid="uuid-new", **extra):
        arguments = dict(
            run_login=self.login(uuid),
            runner=self.keychain,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profile(uuid),
            user=USER,
            temp_root=self.temp_root,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            home=self.home,
            live_slot=MacKeychainSlot(account=USER, runner=self.keychain),
        )
        arguments.update(extra)
        return enroll.enroll(**arguments)

    def seed(self, n, uuid=None, org=None):
        account = Account(
            uuid=uuid or f"uuid-{n}",
            email=f"a{n}@example.com",
            name=f"Name {n}",
            short=f"S{n}",
            full=f"Full {n}",
            plan="Max 5x",
            key=n,
            org_uuid=org,
        )
        self.registry.upsert(account)
        return account

    def assert_no_secret_in_chain(self, error):
        secrets = (SECRET_ACCESS, SECRET_REFRESH, LIVE_ACCESS, LIVE_REFRESH, OTHER_ACCESS, "MCPSECRETTOKEN")
        seen = 0
        while error is not None and seen < 20:
            texts = [str(error), repr(error), *getattr(error, "__notes__", [])]
            for secret in secrets:
                for text in texts:
                    self.assertNotIn(secret, text)
            error = error.__context__ or error.__cause__
            seen += 1

    def assert_clean(self):
        self.assertEqual(self.keychain.temporary(), [])
        self.assertEqual(list(self.temp_root.iterdir()), [])


class EnrollTests(Harness):
    def test_enroll_moves_not_copies(self):
        account = self.call_enroll()
        self.assert_clean()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.registry.by_uuid("uuid-new"), account)
        self.assertRegex(account.signed_in_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(account.oauth_account["accountUuid"], "uuid-new")

    def test_enroll_login_runs_in_resolved_private_folder_with_hashed_item(self):
        seen = {}
        inner = self.login("uuid-new")

        def run_login(folder):
            seen["folder"] = folder
            seen["mode"] = os.stat(folder).st_mode & 0o777
            inner(folder)

        self.call_enroll(run_login=run_login)
        self.assertEqual(seen["folder"], seen["folder"].resolve())
        self.assertEqual(seen["folder"].parent, self.temp_root)
        self.assertEqual(seen["mode"], 0o700)
        self.assertTrue(any(c[:3] == ["delete-generic-password", "-s", hashed_for(seen["folder"])] for c in self.keychain.calls))

    def test_enroll_leaves_live_slot_untouched(self):
        live = {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}, "mcpOAuth": MCP}
        self.keychain.put("Claude Code-credentials", live)
        claude_json = self.home / ".claude.json"
        claude_json.write_text('{"oauthAccount": {"accountUuid": "uuid-live"}}')
        before = claude_json.read_bytes()
        self.call_enroll()
        self.assertEqual(self.keychain.get("Claude Code-credentials"), live)
        self.assertEqual(claude_json.read_bytes(), before)
        for call in self.keychain.calls:
            if "Claude Code-credentials" in call:
                self.assertEqual(call[0], "find-generic-password")

    def test_defaults_for_names_and_key(self):
        account = self.call_enroll()
        self.assertEqual((account.name, account.short, account.full, account.key), ("Ama Rivera", "Ama", "Ama Rivera", 1))
        self.assertEqual(account.plan, "Max 5x")
        self.assertEqual(account.email, "ama@example.com")

    def test_defaults_use_email_local_part_without_display_name(self):
        account = self.call_enroll(run_login=self.login("uuid-new", details_for("uuid-new", name=None)))
        self.assertEqual((account.name, account.short, account.full), ("ama", "ama", "ama"))

    def test_caller_names_and_key_win(self):
        account = self.call_enroll(name="Work Account", short="Work", key=3)
        self.assertEqual((account.name, account.short, account.full, account.key), ("Work Account", "Work", "Work Account", 3))

    def test_short_defaults_to_first_word_of_given_name(self):
        account = self.call_enroll(name="Personal Account")
        self.assertEqual(account.short, "Personal")
        self.assertEqual(account.full, "Personal Account")

    def test_there_is_no_separate_full_name_to_give(self):
        with self.assertRaises(TypeError):
            self.call_enroll(name="Work Account", full="Example Team's Account")

    def test_lowest_free_key_fills_gaps(self):
        self.seed(1)
        self.seed(3)
        self.assertEqual(self.call_enroll().key, 2)

    def test_key_must_be_free_and_in_range(self):
        self.seed(1)
        for bad in (1, 0, -3, True, "2"):
            with self.subTest(bad=bad):
                with self.assertRaises(enroll.EnrollError):
                    self.call_enroll(key=bad)
        self.assertEqual(self.logins, 0)

    def test_a_fifth_account_takes_number_5_and_numbers_go_on(self):
        for n in range(1, 5):
            self.seed(n)
        self.call_enroll()
        self.assertEqual(self.registry.by_uuid("uuid-new").key, 5)
        self.assertEqual(enroll._free_key(self.registry.load(), None), 6)
        self.assertEqual(enroll._free_key(self.registry.load(), 12), 12)
        for bad in (0, -1, True, 1.5, "7"):
            with self.subTest(bad=bad):
                with self.assertRaises(enroll.EnrollError) as caught:
                    enroll._free_key([], bad)
                self.assertEqual(str(caught.exception), "An account number must be a whole number, 1 or more.")

    def test_already_added_account_refused_and_cleaned(self):
        self.seed(1, uuid="uuid-new")
        with self.assertRaises(enroll.EnrollError):
            self.call_enroll()
        self.assert_clean()
        self.assertIsNone(self.store.get("uuid-new"))

    def test_profile_mismatch_refused_and_nothing_kept(self):
        with self.assertRaises(enroll.EnrollError):
            self.call_enroll(profile_fn=self.profile("someone-else"))
        self.assert_clean()
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(self.registry.load(), [])

    def test_cleanup_on_every_failure(self):
        def broken_store():
            class Broken(FileStore):
                def put(self, uuid, oauth):
                    raise RuntimeError("disk full")

            return Broken(self.root / "other")

        def broken_registry():
            class Broken(Registry):
                def upsert(self, account):
                    raise RuntimeError("no space")

            return Broken(self.root / "r.json")

        def boom(folder):
            raise RuntimeError("closed")

        def profile_boom(token):
            raise Unavailable()

        cases = {
            "login fails": dict(run_login=boom),
            "no item": dict(run_login=self.login("uuid-new", write_item=False)),
            "no details": dict(run_login=self.login("uuid-new", write_details=False)),
            "no uuid": dict(run_login=self.login("uuid-new", details={"emailAddress": "x"})),
            "empty signin": dict(run_login=self.login("uuid-new", signin={"mcpOAuth": {}})),
            "profile fails": dict(profile_fn=profile_boom),
            "store fails": dict(store=broken_store()),
            "registry fails": dict(registry=broken_registry()),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                with self.assertRaises(Exception):
                    self.call_enroll(**extra)
                self.assert_clean()

    def test_new_signin_without_a_renewal_key_is_refused_before_any_write(self):
        for label, oauth in (
            ("absent", {"accessToken": SECRET_ACCESS}),
            ("empty", {"accessToken": SECRET_ACCESS, "refreshToken": ""}),
        ):
            with self.subTest(label):
                with self.assertRaises(enroll.EnrollError):
                    self.call_enroll(run_login=self.login("uuid-new", signin={"claudeAiOauth": oauth}))
                self.assert_clean()
                self.assertIsNone(self.store.get("uuid-new"))
                self.assertEqual(self.registry.load(), [])

    def test_missing_temporary_item_on_cleanup_is_really_missing(self):
        def profile_fn(token):
            self.keychain.items.clear()
            return Profile("uuid-new", "ama@example.com", "org", None)

        self.call_enroll(profile_fn=profile_fn)
        self.assert_clean()
        self.assertEqual(self.registry.by_uuid("uuid-new").key, 1)

    def test_failed_temporary_delete_names_the_item_and_says_account_was_added(self):
        self.keychain.fail_delete = True
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_enroll()
        text = str(caught.exception)
        self.assertIn("The account was saved", text)
        self.assertIn("Claude Code-credentials-", text)
        self.assertIsNotNone(self.registry.by_uuid("uuid-new"))
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_failure_keeps_original_error_and_adds_leftover_note(self):
        self.keychain.fail_delete = True
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_enroll(profile_fn=self.profile("other"))
        self.assertIn("does not belong", str(caught.exception))
        notes = " ".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("Claude Code-credentials-", notes)

    def test_temporary_item_is_deleted_before_registry_upsert(self):
        order = []
        keychain_call = self.keychain.__call__

        def runner(args, stdin=None):
            if args[0] == "delete-generic-password":
                order.append("delete")
            return keychain_call(args, stdin)

        class Spy(Registry):
            def upsert(inner, account):
                order.append("upsert")
                super().upsert(account)

        self.call_enroll(runner=runner, registry=Spy(self.root / "spy.json"))
        self.assertLess(order.index("delete"), order.index("upsert"))

    def test_temporary_item_under_another_account_name_is_also_removed(self):
        inner = self.login("uuid-new")

        def both(folder):
            inner(folder)
            self.keychain.put(hashed_for(folder), {"claudeAiOauth": {"accessToken": "zzz"}}, account="someone")

        self.call_enroll(run_login=both)
        self.assertEqual(self.keychain.temporary(), [])

    def test_failure_of_chmod_still_removes_folder(self):
        original = os.chmod

        def broken(path, mode, *a, **k):
            if Path(path).name.startswith("lifeline-login-"):
                raise PermissionError("no")
            return original(path, mode, *a, **k)

        with mock.patch("lifeline.enroll.os.chmod", broken):
            with self.assertRaises(PermissionError):
                self.call_enroll()
        self.assert_clean()

    def test_leftover_login_folders_and_items_are_swept_at_start(self):
        stale = self.temp_root / "lifeline-login-old"
        stale.mkdir()
        (stale / ".claude.json").write_text("{}")
        old = 1_000_000_000
        os.utime(stale, (old, old))
        self.keychain.put(hashed_for(stale.resolve()), {"claudeAiOauth": {"accessToken": "left"}})
        fresh = self.temp_root / "lifeline-login-busy"
        fresh.mkdir()
        self.keychain.put(hashed_for(fresh.resolve()), {"claudeAiOauth": {"accessToken": "inuse"}})
        self.call_enroll()
        self.assertFalse(stale.exists())
        self.assertNotIn((hashed_for(stale.resolve()), USER), self.keychain.items)
        self.assertTrue(fresh.exists())
        self.assertIn((hashed_for(fresh.resolve()), USER), self.keychain.items)

    def test_login_root_per_platform(self):
        home = Path("/h")
        self.assertEqual(enroll.login_root(home, "darwin"), home / "Library/Application Support/com.example.lifeline/login")
        self.assertEqual(enroll.login_root(home, "linux"), home / ".local/share/com.example.lifeline/login")

    def test_held_write_lock_refuses_enroll_in_plain_words_and_cleans_up(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with writelock.held(self.lock_path):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_enroll()
        self.assertIn("busy", str(caught.exception))
        self.assert_clean()
        self.assertEqual(self.registry.load(), [])
        self.assertIsNone(self.store.get("uuid-new"))

    def test_registry_is_rechecked_under_the_lock(self):
        inner = self.login("uuid-new")

        def run_login(folder):
            inner(folder)
            self.seed(1, uuid="uuid-new", org="org")

        with self.assertRaises(enroll.AlreadyHas) as caught:
            self.call_enroll(run_login=run_login)
        self.assertEqual(str(caught.exception), "Lifeline already has Name 1. Your browser signed in as a1@example.com, so nothing changed.")
        self.assertIsInstance(caught.exception, enroll.EnrollError)
        self.assertEqual((caught.exception.name, caught.exception.email), ("Name 1", "a1@example.com"))
        self.assert_clean()
        self.assertIsNone(self.store.get("uuid-new"))

    def test_key_taken_during_login_is_refused(self):
        inner = self.login("uuid-new")

        def run_login(folder):
            inner(folder)
            self.seed(2)

        with self.assertRaises(enroll.EnrollError):
            self.call_enroll(run_login=run_login, key=2)
        self.assert_clean()
        self.assertIsNone(self.store.get("uuid-new"))

    def test_a_number_taken_during_login_moves_the_new_account_to_the_next_one(self):
        for n in range(1, 4):
            self.seed(n)
        inner = self.login("uuid-new")

        def run_login(folder):
            inner(folder)
            self.seed(4)

        self.call_enroll(run_login=run_login)
        self.assertEqual(self.registry.by_uuid("uuid-new").key, 5)
        self.assert_clean()

    def test_keychain_read_failure_is_plain_and_clean(self):
        def runner(args, stdin=None):
            if args[0] == "find-generic-password":
                raise SecurityError(args[0], 1, "boom")
            return self.keychain(args, stdin)

        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_enroll(runner=runner)
        self.assert_no_secret_in_chain(caught.exception)
        self.assertIn("could not be read", str(caught.exception))

    def test_no_secret_in_any_error(self):
        def profile_boom(token):
            raise Unavailable()

        for extra in (dict(profile_fn=profile_boom), dict(profile_fn=self.profile("other"))):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_enroll(**extra)
            self.assert_no_secret_in_chain(caught.exception)

    def test_plan_taken_from_profile_only_when_account_file_has_neither_tier(self):
        details = details_for("uuid-new")
        del details["organizationRateLimitTier"]
        account = self.call_enroll(run_login=self.login("uuid-new", details))
        self.assertEqual(account.plan, "Max 5x")
        self.registry.save([])
        both = details_for("uuid-new", tier="default_claude_max_20x")
        both["userRateLimitTier"] = "default_claude_max_5x"
        account = self.call_enroll(run_login=self.login("uuid-new", both))
        self.assertEqual(account.plan, "Max 20x")
        self.store.remove("uuid-new")

    def test_a_tier_lifeline_does_not_know_is_never_stored_as_the_plan(self):
        details = details_for("uuid-new", tier="default_raven")
        account = self.call_enroll(run_login=self.login("uuid-new", details))
        self.assertEqual(account.plan, "")
        self.assertEqual(account.oauth_account["organizationRateLimitTier"], "default_raven")

    def test_plan_falls_back_to_user_tier(self):
        details = details_for("uuid-new")
        del details["organizationRateLimitTier"]
        details["userRateLimitTier"] = "default_claude_max_20x"
        account = self.call_enroll(run_login=self.login("uuid-new", details))
        self.assertEqual(account.plan, "Max 20x")

    def test_signed_in_at_uses_given_clock(self):
        account = self.call_enroll(now=lambda: "2026-11-19T10:00:00Z")
        self.assertEqual(account.signed_in_at, "2026-11-19T10:00:00Z")




LIVE_SERVICE = "Claude Code-credentials"
OLD = {"accessToken": "old-access", "refreshToken": "old-refresh"}
OTHER_ITEM = {"claudeAiOauth": {"accessToken": OTHER_ACCESS, "refreshToken": "other-refresh"}, "mcpOAuth": MCP}


class KeychainAccountTests(Harness):
    def test_keychain_account_follows_the_login_tools_rule(self):
        for user, expected in (
            ("Sam", "Sam"),
            ("a.b-c_d9", "a.b-c_d9"),
            ("john doe", "claude-code-user"),
            ("j\u00f6hn", "claude-code-user"),
            ("a/b", "claude-code-user"),
            ("x\n", "claude-code-user"),
            ("", "claude-code-user"),
            (None, "claude-code-user"),
        ):
            with self.subTest(user=user):
                self.assertEqual(enroll.keychain_account(user), expected)

    def login_under(self, uuid, account):
        def run_login(folder):
            (folder / ".claude.json").write_text(json.dumps({"oauthAccount": details_for(uuid)}))
            self.keychain.put(
                hashed_for(folder),
                {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH}},
                account=account,
            )

        return run_login

    def test_new_signin_is_read_under_a_plain_user_name(self):
        added = self.call_enroll(run_login=self.login_under("uuid-new", "Sam.S-K_1"), user="Sam.S-K_1")
        self.assertEqual(added.uuid, "uuid-new")
        self.assertEqual(self.store.get("uuid-new")["refreshToken"], SECRET_REFRESH)
        self.assertEqual(self.keychain.temporary(), [])

    def test_new_signin_is_read_under_the_fallback_name_for_an_unusual_user(self):
        added = self.call_enroll(run_login=self.login_under("uuid-new", "claude-code-user"), user="john doe")
        self.assertEqual(added.uuid, "uuid-new")
        self.assertEqual(self.store.get("uuid-new")["refreshToken"], SECRET_REFRESH)
        self.assertEqual(self.keychain.temporary(), [])
        reads = [call for call in self.keychain.calls if call[0] == "find-generic-password" and call[2].startswith(enroll.SERVICE_PREFIX)]
        self.assertEqual({call[call.index("-a") + 1] for call in reads}, {"claude-code-user"})

    def test_an_unusual_user_name_is_never_read_as_given(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_enroll(run_login=self.login_under("uuid-new", "john doe"), user="john doe")
        self.assertEqual(str(caught.exception), "The browser sign-in left no complete sign-in behind, so nothing was added.")
        self.assertEqual(self.keychain.temporary(), [])


class LoginRuleTests(Harness):
    PLANTED = {
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
        "ANTHROPIC_PROFILE": "work",
        "ANTHROPIC_CONFIG_DIR": "/elsewhere/anthropic",
        "CLAUDE_CODE_OAUTH_TOKEN": "planted-oauth-token",
        "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://example.invalid/oauth",
        "CLAUDECODE": "1",
        "CLAUDE_CONFIG_DIR": "/elsewhere/config",
        "ANTHROPIC_API_KEY": "planted-key",
    }
    KEPT = {"HTTPS_PROXY": "http://proxy.invalid:8080", "SSL_CERT_FILE": "/etc/ssl/cert.pem", "LANG": "en_GB.UTF-8"}

    def test_login_user_takes_user_then_the_login_name_and_never_logname_first(self):
        own = pwd.getpwuid(os.getuid()).pw_name
        with mock.patch.dict(os.environ, {"USER": "Sam", "LOGNAME": "someone-else"}):
            self.assertEqual(enroll.login_user(), "Sam")
        with mock.patch.dict(os.environ, {"USER": "", "LOGNAME": "someone-else"}):
            self.assertEqual(enroll.login_user(), own)
        with mock.patch.dict(os.environ, {"LOGNAME": "someone-else"}):
            os.environ.pop("USER", None)
            self.assertEqual(enroll.login_user(), own)
            with mock.patch.object(enroll.pwd, "getpwuid", side_effect=KeyError(os.getuid())):
                self.assertIsNone(enroll.login_user())
                self.assertEqual(enroll.keychain_account(enroll.login_user()), "claude-code-user")

    def test_login_environment_drops_session_variables_and_sets_the_keychain_user(self):
        for user, expected in (("Sam", "Sam"), ("john doe", "claude-code-user"), (None, "claude-code-user")):
            with self.subTest(user=user):
                with mock.patch.dict(os.environ, {**self.PLANTED, **self.KEPT, "PATH": "/usr/bin:/bin", "USER": "other"}):
                    env = enroll.login_environment(Path("/x/lifeline-login-1"), user)
                self.assertEqual([key for key in env if key.startswith(("CLAUDE", "ANTHROPIC"))], ["CLAUDE_CONFIG_DIR"])
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/x/lifeline-login-1")
                self.assertEqual(env["USER"], expected)
                self.assertEqual(env["PATH"], "/usr/bin:/bin")
                for key, value in self.KEPT.items():
                    self.assertEqual(env[key], value)

    def fallback_login(self, uuid):
        def run_login(folder):
            (folder / ".claude.json").write_text(json.dumps({"oauthAccount": details_for(uuid)}))
            self.keychain.put(
                hashed_for(folder),
                {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH}},
                account="claude-code-user",
            )

        return run_login

    def live_reads(self):
        return [
            call[call.index("-a") + 1]
            for call in self.keychain.calls
            if call[0] == "find-generic-password" and call[2] == "Claude Code-credentials"
        ]

    def test_default_live_slot_for_enroll_uses_the_keychain_rule(self):
        self.call_enroll(live_slot=None, user="john doe", run_login=self.fallback_login("uuid-new"))
        self.assertTrue(self.live_reads())
        self.assertEqual(set(self.live_reads()), {"claude-code-user"})

    def test_default_live_slot_for_resign_uses_the_keychain_rule(self):
        self.seed(1, uuid="uuid-new")
        enroll.resign(
            "uuid-new",
            run_login=self.fallback_login("uuid-new"),
            runner=self.keychain,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profile("uuid-new"),
            user="john doe",
            temp_root=self.temp_root,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            home=self.home,
        )
        self.assertTrue(self.live_reads())
        self.assertEqual(set(self.live_reads()), {"claude-code-user"})


class SigninEndEnrollTests(Harness):
    END_MS = 1797000000000
    END = "2026-12-11T14:40:00Z"

    def test_enroll_records_the_signin_end_from_the_new_signin(self):
        signin = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH, "refreshTokenExpiresAt": self.END_MS}}
        account = self.call_enroll(run_login=self.login("uuid-new", signin=signin))
        self.assertEqual(account.signin_ends, self.END)
        self.assertEqual(self.registry.by_uuid("uuid-new").signin_ends, self.END)
        self.assertEqual(self.store.get("uuid-new")["refreshTokenExpiresAt"], self.END_MS)

    def test_enroll_without_the_field_records_no_end(self):
        account = self.call_enroll()
        self.assertIsNone(account.signin_ends)

    def test_resign_replaces_the_signin_end(self):
        self.seed(1, uuid="uuid-new")
        self.registry.upsert(Account(**dict(vars(self.registry.by_uuid("uuid-new")), signin_ends="2026-11-20T00:00:00Z")))
        signin = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH, "refreshTokenExpiresAt": self.END_MS}}
        updated = enroll.resign(
            "uuid-new",
            run_login=self.login("uuid-new", signin=signin),
            runner=self.keychain,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profile("uuid-new"),
            user=USER,
            temp_root=self.temp_root,
            home=self.home,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            live_slot=MacKeychainSlot(account=USER, runner=self.keychain),
        )
        self.assertEqual(updated.signin_ends, self.END)
        plain = enroll.resign(
            "uuid-new",
            run_login=self.login("uuid-new"),
            runner=self.keychain,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profile("uuid-new"),
            user=USER,
            temp_root=self.temp_root,
            home=self.home,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            live_slot=MacKeychainSlot(account=USER, runner=self.keychain),
        )
        self.assertIsNone(plain.signin_ends)

    def test_adopt_records_the_live_signin_end(self):
        self.live_uuid = "uuid-live"
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": details_for("uuid-live", email="alex@example.com")}))
        item = {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH, "refreshTokenExpiresAt": self.END_MS}}
        self.keychain.put(LIVE_SERVICE, item)
        slot = MacKeychainSlot(account=USER, runner=self.keychain)
        account = enroll.adopt_live(slot=slot, registry=self.registry, profile_fn=self.profile("uuid-live"), home=self.home, lock_path=self.lock_path, lock_timeout=0.3)
        self.assertEqual(account.signin_ends, self.END)
        self.keychain.put(LIVE_SERVICE, {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}})
        again = enroll.adopt_live(slot=slot, registry=self.registry, profile_fn=self.profile("uuid-live"), home=self.home, lock_path=self.lock_path, lock_timeout=0.3)
        self.assertIsNone(again.signin_ends)


class ResignTests(Harness):
    def setUp(self):
        super().setUp()
        self.seed(1, uuid="uuid-new")
        self.record = switch.record_path_for(self.lock_path)

    def call_resign(self, uuid="uuid-new", **extra):
        arguments = dict(
            run_login=self.login("uuid-new"),
            runner=self.keychain,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profile("uuid-new"),
            user=USER,
            temp_root=self.temp_root,
            home=self.home,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            live_slot=MacKeychainSlot(account=USER, runner=self.keychain),
            now=lambda: "2026-12-01T00:00:00Z",
        )
        arguments.update(extra)
        return enroll.resign(uuid, **arguments)

    def put_live(self):
        self.live_uuid = "uuid-new"
        live = {
            "claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH},
            "mcpOAuth": MCP,
            "other": {"keep": True},
        }
        self.keychain.put(LIVE_SERVICE, live)
        return live

    def put_other_live_and_park(self):
        self.keychain.put(LIVE_SERVICE, OTHER_ITEM)
        self.store.put("uuid-new", OLD)

    def slot_token(self):
        item = self.keychain.get(LIVE_SERVICE)
        return item["claudeAiOauth"]["accessToken"] if item else None

    def test_resign_replaces_in_use_signin_under_locks(self):
        self.put_live()
        observed = {}

        def on_write(service):
            if service != LIVE_SERVICE:
                return
            other = os.open(self.lock_path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed["flock"] = False
                except BlockingIOError:
                    observed["flock"] = True
            finally:
                os.close(other)
            observed["dirs"] = [
                (self.home / ".claude" / ".oauth_refresh.lock").is_dir(),
                (self.home / ".claude.lock").is_dir(),
                (self.home / ".claude.json.lock").is_dir(),
            ]

        self.keychain.on_write = on_write
        updated = self.call_resign()
        self.assertTrue(observed["flock"])
        self.assertEqual(observed["dirs"], [True, True, True])
        self.assertEqual(self.slot_token(), SECRET_ACCESS)
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(updated.signed_in_at, "2026-12-01T00:00:00Z")
        self.assertFalse((self.home / ".claude.lock").exists())
        with writelock.held(self.lock_path, timeout=0.2):
            pass
        self.assert_clean()

    def test_resign_of_the_account_in_use_moves_open_sessions_under_the_storage_lock(self):
        self.put_live()
        claude = self.home / ".claude"
        path = claude / ".credentials.json"
        path.write_bytes(b"stale copy")
        old = time.time() - 86400
        os.utime(path, (old, old))
        observed = {}

        def on_write(service):
            if service == LIVE_SERVICE:
                observed["storage"] = (claude / ".storage-write.lock").is_dir()
                observed["moved"] = time.time() - path.stat().st_mtime < 5

        self.keychain.on_write = on_write
        self.call_resign()
        self.assertEqual(observed, {"storage": True, "moved": False})
        self.assertLess(time.time() - path.stat().st_mtime, 5)
        self.assertEqual(path.read_bytes(), b"stale copy")
        self.assertFalse((claude / ".storage-write.lock").exists())

    def test_resign_waits_for_claude_codes_storage_lock_and_changes_nothing_if_it_stays(self):
        live = self.put_live()
        storage = self.home / ".claude" / ".storage-write.lock"
        storage.mkdir()
        registry_before = self.registry.path.read_bytes()
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_resign(storage_timeout=0.3)
        self.assertEqual(str(caught.exception), switch.SESSION_BUSY)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(self.registry.path.read_bytes(), registry_before)
        self.assertTrue(storage.is_dir())

    def test_a_new_sign_in_for_an_account_not_in_use_never_waits_for_claude_codes_storage_lock(self):
        self.put_other_live_and_park()
        storage = self.home / ".claude" / ".storage-write.lock"
        storage.mkdir()
        self.call_resign(storage_timeout=0.3)
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertTrue(storage.is_dir())

    def test_a_new_sign_in_for_the_account_in_use_says_when_open_sessions_may_keep_the_old_one(self):
        self.put_live()
        path = self.home / ".claude" / ".credentials.json"
        path.write_bytes(b"stale copy")
        real = os.utime

        def utime(target, *args, **kwargs):
            if Path(target).name == ".credentials.json":
                raise PermissionError()
            return real(target, *args, **kwargs)

        with mock.patch.object(enroll.os, "utime", utime):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign()
        self.assertEqual(str(caught.exception), enroll.OLD_SIGNIN_STAYS)
        self.assertEqual(self.slot_token(), SECRET_ACCESS)

    def test_resign_keeps_mcp_and_every_other_key_of_live_item(self):
        live = self.put_live()
        self.call_resign()
        after = self.keychain.get(LIVE_SERVICE)
        self.assertEqual(after["mcpOAuth"], live["mcpOAuth"])
        self.assertEqual(after["other"], live["other"])
        self.assertEqual(set(after), set(live))

    def test_resign_parked_account_replaces_store_and_leaves_live(self):
        self.put_other_live_and_park()
        updated = self.call_resign()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assertEqual((updated.key, updated.name), (1, "Name 1"))
        self.assert_clean()

    def test_resign_changes_only_email_plan_details_and_time(self):
        self.put_other_live_and_park()
        before = self.registry.by_uuid("uuid-new")
        updated = self.call_resign()
        self.assertEqual(
            (updated.uuid, updated.name, updated.short, updated.full, updated.key),
            (before.uuid, before.name, before.short, before.full, before.key),
        )

    def test_resign_identifies_expired_key_by_record_hash(self):
        live = {
            "claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH, "expiresAt": 1000},
            "mcpOAuth": MCP,
        }
        self.keychain.put(LIVE_SERVICE, live)
        (self.home / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "uuid-new"}}')
        switch.record_confirmed(self.record, "uuid-new", live, datetime.now(timezone.utc))

        received = []

        def recording(token):
            received.append(token)
            return Profile("uuid-new", "ama@example.com", "org", None)

        self.call_resign(profile_fn=recording)
        self.assertNotIn(LIVE_ACCESS, received)
        self.assertEqual(self.slot_token(), SECRET_ACCESS)
        self.assertEqual(self.keychain.get(LIVE_SERVICE)["mcpOAuth"], MCP)

    def test_resign_expired_key_with_record_for_a_different_key_is_refused(self):
        live = {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH, "expiresAt": 1000}}
        self.keychain.put(LIVE_SERVICE, live)
        (self.home / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "uuid-new"}}')
        older = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "some-other-renewal-key"}}
        switch.record_confirmed(self.record, "uuid-new", older, datetime.now(timezone.utc))
        with self.assertRaises(enroll.EnrollError):
            self.call_resign()
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assertIsNone(self.store.get("uuid-new"))
        self.assert_clean()

    def test_resign_of_live_account_records_the_new_renewal_key(self):
        self.put_live()
        self.call_resign()
        entry = switch.read_record_entry(self.record)
        self.assertEqual(entry["uuid"], "uuid-new")
        self.assertEqual(entry["refresh_sha256"], switch.fingerprint({"refreshToken": SECRET_REFRESH}))
        self.assertEqual(os.stat(self.record).st_mode & 0o777, 0o600)

    def test_resign_of_parked_account_leaves_the_record_alone(self):
        self.put_other_live_and_park()
        self.call_resign()
        self.assertFalse(self.record.exists())

    def test_resign_refuses_when_identity_cannot_be_confirmed(self):
        live = self.put_live()
        (self.home / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "uuid-new"}}')

        def flaky(token):
            if token == LIVE_ACCESS:
                raise Unavailable()
            return Profile("uuid-new", "ama@example.com", "org", None)

        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_resign(profile_fn=flaky)
        self.assertIn("could not confirm", str(caught.exception))
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assertIsNone(self.store.get("uuid-new"))
        self.assert_clean()

    def test_resign_signed_out_parks_new_signin_and_never_writes_slot(self):
        updated = self.call_resign()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertIsNone(self.keychain.get(LIVE_SERVICE))
        self.assertEqual(updated.signed_in_at, "2026-12-01T00:00:00Z")
        for call in self.keychain.calls:
            self.assertNotEqual(call[0], "add-generic-password")
            if call[0] == "-i":
                self.fail("the slot was written")
        self.assert_clean()

    def test_resign_different_account_refused_and_nothing_changed(self):
        live = self.put_live()
        with self.assertRaises(enroll.EnrollError):
            self.call_resign(profile_fn=self.profile("another-account"))
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assertIsNone(self.registry.by_uuid("uuid-new").signed_in_at)
        self.assert_clean()

    def test_resign_unknown_account_refused_before_login(self):
        with self.assertRaises(enroll.EnrollError):
            self.call_resign(uuid="nobody")
        self.assertEqual(self.logins, 0)

    def test_resign_account_removed_during_login_is_refused(self):
        self.put_other_live_and_park()
        inner = self.login("uuid-new")

        def run_login(folder):
            inner(folder)
            self.registry.save([])

        with self.assertRaises(enroll.EnrollError):
            self.call_resign(run_login=run_login)
        self.assertEqual(self.store.get("uuid-new"), OLD)
        self.assert_clean()

    def test_race_account_moves_out_after_the_decision(self):
        self.put_live()
        fired = []

        def switch_away(token):
            if token == LIVE_ACCESS and not fired:
                fired.append(1)
                self.keychain.put(LIVE_SERVICE, OTHER_ITEM)
                self.store.put("uuid-new", OLD)

        self.after_check = switch_away
        self.call_resign()
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assert_clean()

    def test_race_account_moves_in_after_the_decision(self):
        self.put_other_live_and_park()
        fired = []

        def switch_in(token):
            if token == OTHER_ACCESS and not fired:
                fired.append(1)
                self.live_uuid = "uuid-new"
                self.keychain.put(
                    LIVE_SERVICE,
                    {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}, "mcpOAuth": MCP},
                )
                self.store.remove("uuid-new")
                self.store.put("uuid-other", {"accessToken": OTHER_ACCESS})

        self.after_check = switch_in
        self.call_resign()
        self.assertEqual(self.slot_token(), SECRET_ACCESS)
        self.assertEqual(self.keychain.get(LIVE_SERVICE)["mcpOAuth"], MCP)
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(self.store.get("uuid-other")["accessToken"], OTHER_ACCESS)
        self.assert_clean()

    def test_race_slot_keeps_changing_is_refused_and_changes_nothing(self):
        self.put_live()
        states = [
            {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}},
            OTHER_ITEM,
        ]
        turn = [0]

        def toggle(token):
            turn[0] += 1
            self.keychain.put(LIVE_SERVICE, states[turn[0] % 2])

        self.after_check = toggle
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_resign()
        self.assertIn("kept changing", str(caught.exception))
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertIsNone(self.registry.by_uuid("uuid-new").signed_in_at)
        self.assertNotEqual(self.slot_token(), SECRET_ACCESS)
        self.assert_clean()

    def test_race_slot_emptied_after_the_decision_goes_to_the_store(self):
        self.put_live()

        def sign_out(token):
            self.keychain.items.pop((LIVE_SERVICE, USER), None)

        self.after_check = sign_out
        self.call_resign()
        self.assertIsNone(self.keychain.get(LIVE_SERVICE))
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assert_clean()

    def test_mcp_renewal_during_the_check_is_kept(self):
        self.put_live()
        renewed = {"server": {"accessToken": "RENEWEDMCP"}}
        calls = []

        def renew(token):
            calls.append(token)
            if len(calls) == 2:
                item = self.keychain.get(LIVE_SERVICE)
                item["mcpOAuth"] = renewed
                self.keychain.put(LIVE_SERVICE, item)

        self.after_check = renew
        self.call_resign()
        after = self.keychain.get(LIVE_SERVICE)
        self.assertEqual(after["mcpOAuth"], renewed)
        self.assertEqual(after["claudeAiOauth"]["accessToken"], SECRET_ACCESS)

    def test_foreign_signin_arriving_during_the_check_is_not_overwritten(self):
        self.put_live()
        calls = []

        def foreign(token):
            calls.append(token)
            if len(calls) == 2:
                self.keychain.put(LIVE_SERVICE, OTHER_ITEM)

        self.after_check = foreign
        self.call_resign()
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assert_clean()

    def test_write_that_landed_but_could_not_be_confirmed_updates_the_registry(self):
        self.put_live()
        original = MacKeychainSlot.write

        def landed(slot_self, signin):
            original(slot_self, signin)
            raise SlotError("The Keychain did not hold what was written.")

        with mock.patch.object(MacKeychainSlot, "write", landed):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign()
        self.assertIn("could not confirm it", str(caught.exception))
        self.assertIn("Claude Code's saved sign-in is now the new one.", str(caught.exception))
        self.assertNotIn("could not be replaced", str(caught.exception))
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assertEqual(self.slot_token(), SECRET_ACCESS)

    def test_live_write_failure_reports_what_the_slot_holds(self):
        live = self.put_live()

        def on_write(service):
            if service == LIVE_SERVICE:
                raise SecurityError("add-generic-password", 1, "")

        self.keychain.on_write = on_write
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_resign()
        self.assertIn("holds the sign-in it held before", str(caught.exception))
        self.assert_no_secret_in_chain(caught.exception)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assert_clean()

    def overwrite_after_write(self, replacement):
        original = MacKeychainSlot.write

        def flawed(slot_self, signin):
            original(slot_self, signin)
            if replacement is None:
                self.keychain.items.pop((LIVE_SERVICE, USER), None)
            else:
                self.keychain.put(LIVE_SERVICE, replacement)
            raise SlotError("The Keychain did not hold what was written.")

        return mock.patch.object(MacKeychainSlot, "write", flawed)

    def test_signin_replaced_by_another_app_after_our_write_is_parked(self):
        self.put_live()
        with self.overwrite_after_write(OTHER_ITEM):
            self.assert_raises_and_get()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assertFalse(self.record.exists())
        self.assert_clean()

    def assert_raises_and_get(self, **extra):
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_resign(**extra)
        self.assertIn("another app changed Claude Code's account", str(caught.exception))
        self.assertIn("kept the new sign-in", str(caught.exception))
        self.assert_no_secret_in_chain(caught.exception)
        return caught.exception

    def test_signin_signed_out_by_another_app_after_our_write_is_parked(self):
        self.put_live()
        with self.overwrite_after_write(None):
            self.assert_raises_and_get()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)

    def test_parking_respects_a_newer_parked_signin(self):
        self.put_live()
        newer = {"accessToken": "NEWERPARKED", "refreshToken": "r", "expiresAt": 9_999_999_999_999}
        self.store.put("uuid-new", newer)
        original = MacKeychainSlot.write

        def flawed(slot_self, signin):
            signin = json.loads(json.dumps(signin))
            signin["claudeAiOauth"]["expiresAt"] = 5000
            original(slot_self, signin)
            self.keychain.put(LIVE_SERVICE, OTHER_ITEM)
            raise SlotError("The Keychain did not hold what was written.")

        fresh = {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH, "expiresAt": 5000}
        inner = self.login("uuid-new", signin={"claudeAiOauth": dict(fresh)})
        with mock.patch.object(MacKeychainSlot, "write", flawed):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign(run_login=inner)
        self.assertEqual(self.store.get("uuid-new"), newer)
        self.assertTrue(recovery.holds(recovery.folder_for(self.lock_path), "uuid-new", fresh))
        self.assertEqual(
            str(caught.exception),
            "The new sign-in was saved, but another app changed Claude Code's account right after it was written. "
            "Lifeline kept its newer saved sign-in for this account and put the new one in its recovery folder.",
        )
        self.assertNotIn("kept the new sign-in for this account", str(caught.exception))
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assert_no_secret_in_chain(caught.exception)

    def lands_then_replaced(self, replacement):
        original = MacKeychainSlot.write

        def write(slot_self, signin):
            original(slot_self, signin)
            if replacement is None:
                self.keychain.items.pop((LIVE_SERVICE, USER), None)
            else:
                self.keychain.put(LIVE_SERVICE, replacement)

        return mock.patch.object(MacKeychainSlot, "write", write)

    def test_a_write_that_lands_then_another_app_replaces_the_slot_parks_the_new_signin(self):
        self.put_live()
        with self.lands_then_replaced(OTHER_ITEM):
            self.assert_raises_and_get()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assertFalse(self.record.exists())
        self.assert_clean()

    def test_a_write_that_lands_then_the_slot_is_signed_out_parks_the_new_signin(self):
        self.put_live()
        with self.lands_then_replaced(None):
            self.assert_raises_and_get()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertIsNone(self.keychain.get(LIVE_SERVICE))
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assertFalse(self.record.exists())
        self.assert_clean()

    def test_a_park_that_saves_but_leaves_an_old_copy_counts_as_kept(self):
        self.put_live()

        class Leftover(FileStore):
            def put(inner, uuid, oauth):
                FileStore.put(inner, uuid, oauth)
                raise StoreLeftover("The new sign-in is saved, but an old copy remains.")

        store = Leftover(self.root / "leftover")
        with self.overwrite_after_write(OTHER_ITEM):
            self.assert_raises_and_get(store=store)
        self.assertEqual(store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assert_clean()

    def test_a_store_branch_save_that_leaves_an_old_copy_still_updates_the_list(self):
        self.put_other_live_and_park()

        class Leftover(FileStore):
            def put(inner, uuid, oauth):
                FileStore.put(inner, uuid, oauth)
                raise StoreLeftover("The new sign-in is saved, but an old copy remains.")

        store = Leftover(self.root / "leftover")
        updated = self.call_resign(store=store)
        self.assertEqual(store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(updated.signed_in_at, "2026-12-01T00:00:00Z")
        self.assertEqual(self.registry.by_uuid("uuid-new").signed_in_at, "2026-12-01T00:00:00Z")
        self.assert_clean()

    def test_disturbed_locks_never_hide_the_other_app_report(self):
        self.put_live()

        class Disturbed(enroll.SessionLocks):
            @property
            def compromised(self):
                return True

        with mock.patch("lifeline.enroll.SessionLocks", Disturbed), self.overwrite_after_write(OTHER_ITEM):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign()
        text = str(caught.exception)
        self.assertIn("another app changed Claude Code's account", text)
        self.assertIn("kept the new sign-in", text)
        self.assertIn("Claude Code changed its files while Lifeline saved the sign-in.", text)
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)

    def test_parking_failure_is_reported_plainly(self):
        self.put_live()

        class Broken(FileStore):
            def put(inner, uuid, oauth):
                raise RuntimeError("disk full")

        with self.overwrite_after_write(OTHER_ITEM):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign(store=Broken(self.root / "broken"))
        self.assertIn("could not keep the new sign-in", str(caught.exception))
        self.assertIsNone(self.registry.by_uuid("uuid-new").signed_in_at)

    def test_held_write_lock_refuses_resign_in_plain_words_and_cleans_up(self):
        live = self.put_live()
        with writelock.held(self.lock_path):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign()
        self.assertIn("busy", str(caught.exception))
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assert_clean()

    def test_missing_settings_folder_is_plain(self):
        self.put_live()
        (self.home / ".claude").rmdir()
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_resign()
        self.assertIn("settings folder", str(caught.exception))
        self.assert_clean()

    def test_disturbed_locks_on_the_store_branch_do_not_mention_the_sign_in_slot(self):
        self.put_other_live_and_park()

        class Disturbed(enroll.SessionLocks):
            @property
            def compromised(self):
                return True

        with mock.patch("lifeline.enroll.SessionLocks", Disturbed):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign()
        self.assertNotIn("still signed in", str(caught.exception))
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)

    def test_disturbed_locks_are_reported_after_the_write(self):
        self.put_live()

        class Disturbed(enroll.SessionLocks):
            @property
            def compromised(self):
                return True

        with mock.patch("lifeline.enroll.SessionLocks", Disturbed):
            with self.assertRaises(enroll.EnrollError) as caught:
                self.call_resign()
        self.assertIn("Claude Code changed its files while Lifeline saved the sign-in.", str(caught.exception))
        self.assertEqual(self.slot_token(), SECRET_ACCESS)
        self.assert_clean()


class CertificateTests(Harness):
    def test_adopt_says_the_certificate_failed(self):
        from lifeline.anthropic import CertificateFailed

        self.keychain.put(LIVE_SERVICE, {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}})
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": details_for("uuid-live")}))

        def failing(token):
            raise CertificateFailed()

        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.adopt_live(
                slot=MacKeychainSlot(account=USER, runner=self.keychain),
                registry=self.registry,
                profile_fn=failing,
                home=self.home,
                lock_path=self.lock_path,
                lock_timeout=0.3,
            )
        self.assertEqual(str(caught.exception), switch.CERTIFICATE_UNCHECKED)
        self.assertNotIn("internet", str(caught.exception))
        self.assertEqual(self.registry.load(), [])

    def test_a_new_signin_whose_check_fails_on_the_certificate_says_so(self):
        from lifeline.anthropic import CertificateFailed

        def failing(token):
            raise CertificateFailed()

        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_enroll(profile_fn=failing)
        self.assertEqual(str(caught.exception), enroll.NEW_CERTIFICATE)
        self.assertNotIn("internet", str(caught.exception))
        self.assert_clean()
        self.assertEqual(self.registry.load(), [])


class LiveEnrollTests(Harness):
    def live_item(self):
        return {
            "claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH},
            "mcpOAuth": MCP,
            "other": {"keep": True},
        }

    def test_enroll_of_the_live_account_replaces_only_the_live_signin(self):
        self.live_uuid = "uuid-new"
        self.keychain.put(LIVE_SERVICE, self.live_item())
        account = self.call_enroll()
        after = self.keychain.get(LIVE_SERVICE)
        self.assertEqual(after["claudeAiOauth"]["accessToken"], SECRET_ACCESS)
        self.assertEqual(after["mcpOAuth"], MCP)
        self.assertEqual(after["other"], {"keep": True})
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(self.registry.by_uuid("uuid-new"), account)
        self.assert_clean()

    def test_enroll_of_the_live_account_records_the_new_renewal_key(self):
        self.live_uuid = "uuid-new"
        self.keychain.put(LIVE_SERVICE, self.live_item())
        self.call_enroll()
        entry = switch.read_record_entry(switch.record_path_for(self.lock_path))
        self.assertEqual(entry["uuid"], "uuid-new")
        self.assertEqual(entry["refresh_sha256"], switch.fingerprint({"refreshToken": SECRET_REFRESH}))

    def test_enroll_with_unidentifiable_live_slot_is_refused(self):
        self.keychain.put(LIVE_SERVICE, self.live_item())

        def flaky(token):
            if token == LIVE_ACCESS:
                raise Unavailable()
            return Profile("uuid-new", "ama@example.com", "org", None)

        with self.assertRaises(enroll.EnrollError):
            self.call_enroll(profile_fn=flaky)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), self.live_item())
        self.assertIsNone(self.store.get("uuid-new"))
        self.assertEqual(self.registry.load(), [])
        self.assert_clean()

    def test_enroll_race_account_moves_into_slot_after_the_decision(self):
        self.keychain.put(LIVE_SERVICE, OTHER_ITEM)
        fired = []

        def switch_in(token):
            if token == OTHER_ACCESS and not fired:
                fired.append(1)
                self.live_uuid = "uuid-new"
                self.keychain.put(LIVE_SERVICE, self.live_item())

        self.after_check = switch_in
        self.call_enroll()
        self.assertEqual(self.keychain.get(LIVE_SERVICE)["claudeAiOauth"]["accessToken"], SECRET_ACCESS)
        self.assertIsNone(self.store.get("uuid-new"))

    def test_enroll_race_account_moves_out_of_slot_after_the_decision(self):
        self.live_uuid = "uuid-new"
        self.keychain.put(LIVE_SERVICE, self.live_item())
        fired = []

        def switch_away(token):
            if token == LIVE_ACCESS and not fired:
                fired.append(1)
                self.keychain.put(LIVE_SERVICE, OTHER_ITEM)

        self.after_check = switch_away
        self.call_enroll()
        self.assertEqual(self.keychain.get(LIVE_SERVICE), OTHER_ITEM)
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assert_clean()

    def test_enroll_into_empty_slot_goes_to_the_store(self):
        self.call_enroll()
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertIsNone(self.keychain.get(LIVE_SERVICE))


class AdoptTests(Harness):
    def setUp(self):
        super().setUp()
        self.live_uuid = "uuid-live"
        self.record = switch.record_path_for(self.lock_path)
        self.slot = MacKeychainSlot(account=USER, runner=self.keychain)
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": details_for("uuid-live", email="alex@example.com", name="Alex Rivera")})
        )

    def put_live(self):
        item = {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}, "mcpOAuth": MCP}
        self.keychain.put(LIVE_SERVICE, item)
        return item

    def call_adopt(self, **extra):
        arguments = dict(
            slot=self.slot,
            registry=self.registry,
            profile_fn=self.profile("uuid-live"),
            home=self.home,
            lock_path=self.lock_path,
            lock_timeout=0.3,
        )
        arguments.update(extra)
        return enroll.adopt_live(**arguments)

    def test_adopt_registers_without_touching_slot_or_store_and_writes_record(self):
        item = self.put_live()
        before_calls = len(self.keychain.calls)
        account = self.call_adopt(name="Alex's Account", short="Alex", key=1)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), item)
        for call in self.keychain.calls[before_calls:]:
            self.assertEqual(call[0], "find-generic-password")
        self.assertEqual(list(self.root.joinpath("signins").iterdir()), [])
        self.assertEqual(switch.read_record(self.record), "uuid-live")
        self.assertEqual(
            switch.read_record_entry(self.record)["refresh_sha256"],
            switch.fingerprint({"refreshToken": LIVE_REFRESH}),
        )
        self.assertEqual(self.registry.by_uuid("uuid-live"), account)
        self.assertEqual((account.name, account.short, account.key, account.email), ("Alex's Account", "Alex", 1, "alex@example.com"))
        self.assertEqual(account.plan, "Max 5x")
        self.assertIsNone(account.signed_in_at)

    def test_adopt_defaults_and_given_sign_in_time(self):
        self.put_live()
        account = self.call_adopt(signed_in_at="2026-11-01T00:00:00Z")
        self.assertEqual((account.name, account.short, account.key), ("Alex Rivera", "Alex", 1))
        self.assertEqual(account.signed_in_at, "2026-11-01T00:00:00Z")

    def test_adopt_refuses_empty_slot(self):
        with self.assertRaises(enroll.EnrollError):
            self.call_adopt()
        self.assertEqual(self.registry.load(), [])
        self.assertFalse(self.record.exists())

    def test_adopt_refuses_unknown_identity(self):
        self.put_live()

        def down(token):
            raise Unavailable()

        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_adopt(profile_fn=down)
        self.assertIn("could not confirm", str(caught.exception))
        self.assertEqual(self.registry.load(), [])
        self.assertFalse(self.record.exists())

    def test_adopt_refuses_when_account_details_disagree(self):
        self.put_live()
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": details_for("someone-else")}))
        with self.assertRaises(enroll.EnrollError):
            self.call_adopt()
        self.assertEqual(self.registry.load(), [])

    def test_adopt_of_registered_account_keeps_its_names(self):
        self.put_live()
        self.seed(2, uuid="uuid-live")
        account = self.call_adopt(name="Other")
        self.assertEqual((account.name, account.key), ("Name 2", 2))
        self.assertEqual(len(self.registry.load()), 1)

    def test_adopt_refuses_slot_without_a_renewal_key(self):
        self.keychain.put(LIVE_SERVICE, {"claudeAiOauth": {"accessToken": LIVE_ACCESS}})
        with self.assertRaises(enroll.EnrollError):
            self.call_adopt()
        self.assertEqual(self.registry.load(), [])
        self.assertFalse(self.record.exists())

    def test_adopt_keeps_nothing_when_another_account_is_live(self):
        self.put_live()
        before = self.registry.load()
        with self.assertRaises(enroll.EnrollError) as caught:
            self.call_adopt(expected="uuid-other")
        self.assertEqual(str(caught.exception), enroll.LIVE_NOT_ASKED)
        self.assertEqual(self.registry.load(), before)
        self.assertFalse(self.record.exists())

    def test_adopt_adds_a_fifth_account_as_number_5(self):
        self.put_live()
        for n in range(1, 5):
            self.seed(n)
        adopted = self.call_adopt()
        self.assertEqual(adopted.key, 5)

    def test_adopt_refuses_when_write_lock_held(self):
        self.put_live()
        with writelock.held(self.lock_path):
            with self.assertRaises(enroll.EnrollError):
                self.call_adopt()
        self.assertEqual(self.registry.load(), [])


class FileStorageTests(Harness):
    def setUp(self):
        super().setUp()
        self.live_path = self.home / ".claude" / ".credentials.json"
        self.live_item = {
            "claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH},
            "mcpOAuth": MCP,
            "other": {"keep": True},
        }
        self.live_path.write_text(json.dumps(self.live_item))
        self.forbidden = mock.Mock(side_effect=AssertionError("security was run"))

    def file_login(self, uuid, signin=None, write_item=True, raw=None):
        def run_login(folder):
            self.logins += 1
            self.folders.append(folder)
            (folder / ".claude.json").write_text(json.dumps({"oauthAccount": details_for(uuid)}))
            if raw is not None:
                (folder / ".credentials.json").write_text(raw)
            elif write_item:
                item = signin if signin is not None else {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH}}
                (folder / ".credentials.json").write_text(json.dumps(item))

        return run_login

    def file_arguments(self, uuid, **extra):
        arguments = dict(
            run_login=self.file_login(uuid),
            runner=self.forbidden,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profile(uuid),
            user=USER,
            temp_root=self.temp_root,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            home=self.home,
            live_slot=FileSlot(self.live_path),
            saved_in=enroll.SAVED_IN_FILE,
        )
        arguments.update(extra)
        return arguments

    def test_enroll_reads_the_new_signin_from_the_login_folder_and_never_runs_security(self):
        account = enroll.enroll(**self.file_arguments("uuid-new"))
        self.assertEqual(self.store.get("uuid-new"), {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH})
        self.assertEqual(account.uuid, "uuid-new")
        self.assertEqual(json.loads(self.live_path.read_text()), self.live_item)
        self.forbidden.assert_not_called()
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_the_login_folder_file_is_gone_before_the_registry_is_written(self):
        seen = {}

        class Watching(Registry):
            def upsert(inner, account):
                seen["left"] = [path.name for folder in self.folders for path in folder.iterdir()]
                return super().upsert(account)

        enroll.enroll(**self.file_arguments("uuid-new", registry=Watching(self.root / "accounts.json")))
        self.assertEqual(seen["left"], [".claude.json"])

    def test_a_missing_or_damaged_signin_file_adds_nothing_and_cleans_up(self):
        cases = {
            "missing": (dict(write_item=False), "The browser sign-in left no complete sign-in behind, so nothing was added."),
            "damaged": (dict(raw="{not json"), "The new sign-in could not be read from the login folder."),
            "no renewal key": (
                dict(signin={"claudeAiOauth": {"accessToken": SECRET_ACCESS}}),
                "The browser sign-in left no complete sign-in behind, so nothing was added.",
            ),
        }
        for name, (options, sentence) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(enroll.EnrollError) as caught:
                    enroll.enroll(**self.file_arguments("uuid-new", run_login=self.file_login("uuid-new", **options)))
                self.assertEqual(str(caught.exception), sentence)
                self.assert_no_secret_in_chain(caught.exception)
                self.assertIsNone(self.store.get("uuid-new"))
                self.assertEqual(list(self.temp_root.iterdir()), [])
        self.forbidden.assert_not_called()

    def test_resign_of_the_live_account_writes_the_file_slot_and_keeps_every_other_key(self):
        self.seed(1, uuid="uuid-live")
        updated = enroll.resign("uuid-live", **self.file_arguments("uuid-live", now=lambda: "2026-12-01T00:00:00Z"))
        after = json.loads(self.live_path.read_text())
        self.assertEqual(after["claudeAiOauth"], {"accessToken": SECRET_ACCESS, "refreshToken": SECRET_REFRESH})
        self.assertEqual(after["mcpOAuth"], MCP)
        self.assertEqual(after["other"], {"keep": True})
        self.assertIsNone(self.store.get("uuid-live"))
        self.assertEqual(updated.signed_in_at, "2026-12-01T00:00:00Z")
        self.forbidden.assert_not_called()
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_resign_of_a_parked_account_puts_it_in_the_store_and_leaves_the_live_file(self):
        self.seed(2, uuid="uuid-new")
        before = self.live_path.read_bytes()
        enroll.resign("uuid-new", **self.file_arguments("uuid-new"))
        self.assertEqual(self.store.get("uuid-new")["accessToken"], SECRET_ACCESS)
        self.assertEqual(self.live_path.read_bytes(), before)
        self.forbidden.assert_not_called()

    def test_stale_login_folders_are_swept_without_security(self):
        stale = self.temp_root / (enroll.FOLDER_PREFIX + "old")
        stale.mkdir()
        (stale / ".credentials.json").write_text("{}")
        old = os.stat(stale).st_mtime - enroll.STALE_LOGIN_SECONDS - 10
        os.utime(stale, (old, old))
        enroll.enroll(**self.file_arguments("uuid-new"))
        self.assertFalse(stale.exists())
        self.forbidden.assert_not_called()

    def test_an_unknown_place_for_the_new_signin_is_refused_before_any_login(self):
        with self.assertRaises(ValueError):
            enroll.enroll(**self.file_arguments("uuid-new", saved_in="elsewhere"))
        self.assertEqual(self.logins, 0)
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_the_keychain_stays_the_default(self):
        account = self.call_enroll()
        self.assertEqual(account.uuid, "uuid-new")
        self.assertTrue(any(call[0] == "find-generic-password" for call in self.keychain.calls))


SAM_LOGIN = "a1a1a1a1-0000-4000-8000-00000000000a"
PERSONAL_ORG = "b2b2b2b2-0000-4000-8000-00000000000b"
TEAM_ORG = "f6f6f6f6-0000-4000-8000-00000000000c"
TEAM_ID = f"{SAM_LOGIN}-{TEAM_ORG}"
PERSONAL_SIGNIN = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": "sk-ant-ort01-PERSONALSECRET"}}
TEAM_SIGNIN = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": "sk-ant-ort01-TEAMSECRET"}}


def org_details(org, name="Sam Taylor"):
    value = details_for(SAM_LOGIN, name=name, email="sam@example.com")
    value["organizationUuid"] = org
    return value


class OrganizationTests(Harness):
    def pair_profile(self, org, live_org=PERSONAL_ORG):
        def profile_fn(token):
            if token == LIVE_ACCESS:
                return Profile(SAM_LOGIN, "sam@example.com", live_org, None)
            return Profile(SAM_LOGIN, "sam@example.com", org, "default_claude_max_5x")

        return profile_fn

    def add(self, org, signin, **extra):
        return self.call_enroll(
            run_login=self.login(SAM_LOGIN, org_details(org), signin=signin),
            profile_fn=self.pair_profile(org),
            **extra,
        )

    def test_two_organizations_of_one_login_are_two_accounts_and_a_third_add_names_the_one_it_found(self):
        personal = self.add(PERSONAL_ORG, PERSONAL_SIGNIN, name="Personal Account", short="Personal")
        team = self.add(TEAM_ORG, TEAM_SIGNIN, name="Team Account", short="Team")
        self.assertEqual((personal.uuid, personal.login, personal.org_uuid), (SAM_LOGIN, SAM_LOGIN, PERSONAL_ORG))
        self.assertEqual((team.uuid, team.login, team.org_uuid), (TEAM_ID, SAM_LOGIN, TEAM_ORG))
        self.assertEqual(self.store.get(SAM_LOGIN), PERSONAL_SIGNIN["claudeAiOauth"])
        self.assertEqual(self.store.get(TEAM_ID), TEAM_SIGNIN["claudeAiOauth"])
        self.assertEqual([a.uuid for a in self.registry.load()], [SAM_LOGIN, TEAM_ID])
        registry_before = self.registry.path.read_bytes()
        for org, name in ((PERSONAL_ORG, "Personal Account"), (TEAM_ORG, "Team Account")):
            with self.subTest(org=org):
                again = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": "sk-ant-ort01-AGAINSECRET"}}
                with self.assertRaises(enroll.EnrollError) as caught:
                    self.add(org, again)
                self.assertIn(name, str(caught.exception))
                self.assertIn("already has", str(caught.exception))
                self.assert_no_secret_in_chain(caught.exception)
                self.assert_clean()
        self.assertEqual(self.registry.path.read_bytes(), registry_before)
        self.assertEqual(self.store.get(SAM_LOGIN), PERSONAL_SIGNIN["claudeAiOauth"])
        self.assertEqual(self.store.get(TEAM_ID), TEAM_SIGNIN["claudeAiOauth"])

    def test_a_match_through_an_account_with_no_known_organization_says_so(self):
        self.seed(1, uuid=SAM_LOGIN)
        with self.assertRaises(enroll.EnrollError) as caught:
            self.add(TEAM_ORG, TEAM_SIGNIN)
        self.assertEqual(
            str(caught.exception),
            "Your browser signed in as sam@example.com. Lifeline has an account for that login whose organization "
            "it does not know yet, so nothing changed. Sign in again to Name 1 first, so Lifeline learns its organization.",
        )
        self.assert_clean()

    def test_adding_the_other_organization_parks_it_and_leaves_the_live_one_alone(self):
        self.add(PERSONAL_ORG, PERSONAL_SIGNIN, name="Personal Account")
        self.store.remove(SAM_LOGIN)
        live = {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}, "mcpOAuth": MCP}
        self.keychain.put(LIVE_SERVICE, live)
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": org_details(PERSONAL_ORG)}))
        team = self.add(TEAM_ORG, TEAM_SIGNIN, name="Team Account")
        self.assertEqual(team.uuid, TEAM_ID)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)
        self.assertEqual(self.store.get(TEAM_ID), TEAM_SIGNIN["claudeAiOauth"])
        self.assertIsNone(self.store.get(SAM_LOGIN))

    def test_a_sign_in_that_names_another_organization_than_its_details_is_refused(self):
        with self.assertRaises(enroll.EnrollError):
            self.call_enroll(
                run_login=self.login(SAM_LOGIN, org_details(PERSONAL_ORG), signin=TEAM_SIGNIN),
                profile_fn=self.pair_profile(TEAM_ORG),
            )
        self.assert_clean()
        self.assertEqual(self.registry.load(), [])

    def test_signing_in_again_checks_the_organization_too(self):
        self.add(PERSONAL_ORG, PERSONAL_SIGNIN, name="Personal Account")
        self.add(TEAM_ORG, TEAM_SIGNIN, name="Team Account")
        common = dict(
            runner=self.keychain,
            store=self.store,
            registry=self.registry,
            user=USER,
            temp_root=self.temp_root,
            home=self.home,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            live_slot=MacKeychainSlot(account=USER, runner=self.keychain),
        )
        wrong = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": "sk-ant-ort01-WRONGSECRET"}}
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.resign(
                TEAM_ID, run_login=self.login(SAM_LOGIN, org_details(PERSONAL_ORG), signin=wrong),
                profile_fn=self.pair_profile(PERSONAL_ORG), **common,
            )
        self.assertIn("different account", str(caught.exception))
        self.assertEqual(self.store.get(TEAM_ID), TEAM_SIGNIN["claudeAiOauth"])
        self.assertEqual(self.store.get(SAM_LOGIN), PERSONAL_SIGNIN["claudeAiOauth"])
        fresh = {"claudeAiOauth": {"accessToken": SECRET_ACCESS, "refreshToken": "sk-ant-ort01-FRESHTEAMSECRET"}}
        updated = enroll.resign(
            TEAM_ID, run_login=self.login(SAM_LOGIN, org_details(TEAM_ORG), signin=fresh),
            profile_fn=self.pair_profile(TEAM_ORG), **common,
        )
        self.assertEqual((updated.uuid, updated.org_uuid), (TEAM_ID, TEAM_ORG))
        self.assertEqual(self.store.get(TEAM_ID), fresh["claudeAiOauth"])
        self.assertEqual(self.store.get(SAM_LOGIN), PERSONAL_SIGNIN["claudeAiOauth"])
        self.assert_clean()

    def test_adopting_the_live_organization_never_overwrites_the_other_one(self):
        personal = self.add(PERSONAL_ORG, PERSONAL_SIGNIN, name="Personal Account")
        live = {"claudeAiOauth": {"accessToken": LIVE_ACCESS, "refreshToken": LIVE_REFRESH}, "mcpOAuth": MCP}
        self.keychain.put(LIVE_SERVICE, live)
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": org_details(TEAM_ORG)}))
        adopted = enroll.adopt_live(
            slot=MacKeychainSlot(account=USER, runner=self.keychain),
            registry=self.registry,
            profile_fn=self.pair_profile(TEAM_ORG, live_org=TEAM_ORG),
            home=self.home,
            lock_path=self.lock_path,
            lock_timeout=0.3,
            name="Team Account",
        )
        self.assertEqual((adopted.uuid, adopted.login, adopted.org_uuid), (TEAM_ID, SAM_LOGIN, TEAM_ORG))
        self.assertEqual(self.registry.by_uuid(SAM_LOGIN), personal)
        self.assertEqual(switch.read_record(switch.record_path_for(self.lock_path)), TEAM_ID)
        self.assertEqual(self.keychain.get(LIVE_SERVICE), live)


if __name__ == "__main__":
    unittest.main()
