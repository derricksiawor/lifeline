import contextlib
from dataclasses import replace
import copy
import http.client
import http.server
import io
import json
import os
import shlex
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lifeline import cli, devbox, enroll, server, writelock
from lifeline import switch as switching
from lifeline.accounts import Account, Registry
from lifeline.anthropic import Profile, SignInDead, Unavailable
from lifeline.history import History
from lifeline.slot import SecurityError, Slot
from lifeline.store import FileStore, StoreError
from lifeline.switch import IdentityUnknown, LifelineBusy, SwitchFailed, SwitchRefused
from tests import test_server as served

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "bin" / "lifeline"

ALEX = "11111111-aaaa-4aaa-8aaa-000000000001"
WORK = "22222222-bbbb-4bbb-8bbb-000000000002"
PERSONAL = "33333333-cccc-4ccc-8ccc-000000000003"
ORG = "44444444-dddd-4ddd-8ddd-000000000004"
STRANGER = "55555555-eeee-4eee-8eee-000000000005"
TEAM_ORG = "66666666-ffff-4fff-8fff-000000000006"
TEAM = f"{PERSONAL}-{TEAM_ORG}"
PAIRS = {TEAM: (PERSONAL, TEAM_ORG)}
TAGS = {TEAM: "teamorg0"}


def pair_of(uuid):
    return PAIRS.get(uuid, (uuid, f"org-{uuid[:8]}"))


ACCOUNTS = {
    ALEX: ("Alex's Account", "Alex", "Alex's Account", 1, "Max 20x"),
    WORK: ("Work Account", "Work", "Example Team's Account", 2, "default_claude_max_5x"),
    PERSONAL: ("Personal Account", "Personal", "Example Org Personal Account", 3, "Max 5x"),
    ORG: ("Team Account", "Team", "Example Org Team Account", 4, ""),
}
USER = "tester"
FUTURE_MS = 4102444800000
NOW = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
MCP = {"github|1a2b": {"serverName": "github", "accessToken": "mcp-access-SECRET", "refreshToken": "mcp-refresh-SECRET"}}
KEY_MARKER = "KEY-FILE-MARKER-SECRET-0123456789"


def details(uuid):
    login, org = pair_of(uuid)
    return {
        "accountUuid": login,
        "emailAddress": f"{login[:8]}@example.invalid",
        "organizationUuid": org,
        "organizationRateLimitTier": "default_claude_max_5x",
        "displayName": f"Person {login[:8]}",
    }


def config_data(oauth_account):
    data = {
        "numStartups": 412,
        "installMethod": "native",
        "mcpServers": {"one": {"type": "http", "url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer header-SECRET"}}},
        "projects": {f"/home/test/project-{i}": {"allowedTools": [], "lastCost": i / 7} for i in range(20)},
    }
    if oauth_account is not None:
        data["oauthAccount"] = oauth_account
    return data


class FakeProfiles:
    def __init__(self):
        self.owners = {}
        self.calls = []
        self.offline = False

    def issue(self, uuid, n):
        tag = TAGS.get(uuid, uuid[:8])
        oauth = {
            "accessToken": f"access-{tag}-{n}-SECRET",
            "refreshToken": f"refresh-{tag}-{n}-SECRET",
            "expiresAt": FUTURE_MS,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
        self.owners[oauth["accessToken"]] = uuid
        self.owners[oauth["refreshToken"]] = uuid
        return oauth

    def secrets(self):
        return list(self.owners)

    def __call__(self, token):
        self.calls.append(token)
        if self.offline:
            raise Unavailable("Could not reach the server")
        if token not in self.owners:
            raise SignInDead()
        login, org = pair_of(self.owners[token])
        return Profile(login, f"{login[:8]}@example.invalid", org, "default_claude_max_5x")


class FakeSlot(Slot):
    def __init__(self, item):
        self.item = copy.deepcopy(item)
        self.reads = 0
        self.writes = 0
        self.read_hooks = {}
        self.write_hooks = []

    def read(self):
        self.reads += 1
        hook = self.read_hooks.pop(self.reads, None)
        if hook is not None:
            hook(self)
        return copy.deepcopy(self.item)

    def write(self, item):
        self.writes += 1
        if self.write_hooks:
            self.write_hooks.pop(0)(self)
        self.item = copy.deepcopy(item)


class CountingStore:
    def __init__(self, inner):
        self.inner = inner
        self.calls = []
        self.fail_puts = 0

    def get(self, uuid):
        self.calls.append(("get", uuid))
        return self.inner.get(uuid)

    def put(self, uuid, oauth):
        self.calls.append(("put", uuid))
        if self.fail_puts > 0:
            self.fail_puts -= 1
            raise StoreError("The new parked sign-in could not be saved.")
        self.inner.put(uuid, oauth)

    def remove(self, uuid):
        self.calls.append(("remove", uuid))
        self.inner.remove(uuid)


class FakeKeychain:
    def __init__(self):
        self.items = {}
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        if args[0] == "-i":
            words = shlex.split(stdin.split("\n")[0])
            service = words[words.index("-s") + 1]
            account = words[words.index("-a") + 1]
            self.items[(service, account)] = bytes.fromhex(words[words.index("-X") + 1]).decode("utf-8")
            return ""
        service = args[args.index("-s") + 1]
        account = args[args.index("-a") + 1] if "-a" in args else None
        if args[0] == "find-generic-password":
            if (service, account) not in self.items:
                raise SecurityError(args[0], 44, "not found")
            return self.items[(service, account)] + "\n"
        if args[0] == "delete-generic-password":
            found = [key for key in self.items if key[0] == service and (account is None or key[1] == account)]
            if not found:
                raise SecurityError(args[0], 44, "")
            del self.items[found[0]]
            return ""
        raise AssertionError(args)

    def put(self, service, value, account=USER):
        self.items[(service, account)] = json.dumps(value)

    def temporary(self):
        return [key for key in self.items if key[0].startswith(enroll.SERVICE_PREFIX)]


class FakeProcess:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.account = None
        self.returncode = 0
        self.error = None
        self.issued = []
        self.on_call = None

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        folder = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        if self.returncode == 0 and self.account is not None:
            signin = self.world.profiles.issue(self.account, 10 + len(self.issued))
            self.issued.append(signin)
            (folder / ".claude.json").write_text(json.dumps({"oauthAccount": details(self.account), "numStartups": 1}))
            self.world.keychain.put(
                enroll.hashed_service(folder), {"claudeAiOauth": signin}, account=enroll.keychain_account(kwargs["env"].get("USER"))
            )
        return subprocess.CompletedProcess(args, self.returncode)


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.hooks = {}

    def monotonic(self):
        return self.now

    def wall(self):
        return 1_800_000_000.0 + self.now

    def sleep(self, seconds):
        if len(self.sleeps) >= 400:
            raise AssertionError("The watch never ended.")
        self.sleeps.append(seconds)
        hook = self.hooks.pop(len(self.sleeps), None)
        self.now += seconds
        if hook is not None:
            hook()


class World:
    def __init__(self, case, live=ALEX, parked=(WORK, PERSONAL, ORG), registered=tuple(ACCOUNTS), record=True):
        self.case = case
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        case.addCleanup(patcher.stop)
        self.folder = self.home / "Library" / "Application Support" / "com.example.lifeline"
        self.lock_path = self.folder / "lifeline.lock"
        self.record_path = self.folder / "last_loaded.json"
        self.note_path = self.folder / "last_switch.json"
        self.history_path = self.folder / "history.db"
        self.registry = Registry(self.folder / "accounts.json")
        self.backups = self.home / ".lifeline-backups"
        self.temp_root = self.folder / "login"
        self.account_file = self.home / ".claude.json"
        self.sessions = self.home / ".claude" / "sessions"
        self.profiles = FakeProfiles()
        self.keychain = FakeKeychain()
        self.process = FakeProcess(self)
        self.time = FakeTime()
        self.alive = {}
        self.store = CountingStore(FileStore(self.root / "signins"))
        accounts = []
        for uuid in registered:
            name, short, full, key, plan = ACCOUNTS[uuid]
            accounts.append(
                Account(uuid=uuid, email=f"{uuid[:8]}@example.invalid", name=name, short=short, full=full, plan=plan, key=key, oauth_account=details(uuid))
            )
        self.registry.save(accounts)
        self.signins = {}
        for uuid in parked:
            self.signins[uuid] = self.profiles.issue(uuid, 1)
            self.store.inner.put(uuid, self.signins[uuid])
        item = {"mcpOAuth": copy.deepcopy(MCP), "otherKey": {"kept": True}}
        if live is not None:
            self.signins[live] = self.profiles.issue(live, 1)
            item["claudeAiOauth"] = self.signins[live]
        self.slot = FakeSlot(item)
        self.write_config(config_data(details(live) if live is not None else None))
        if record and live is not None:
            switching.record_confirmed(self.record_path, live, {"claudeAiOauth": self.signins[live]}, datetime.now(timezone.utc))
        self.out = io.StringIO()
        self.err = io.StringIO()

    def write_config(self, data):
        self.account_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def config(self):
        return json.loads(self.account_file.read_text(encoding="utf-8"))

    def wiring(self, **changes):
        options = dict(
            home=self.home,
            user=USER,
            slot=self.slot,
            store=self.store,
            registry=self.registry,
            profile_fn=self.profiles,
            runner=self.keychain,
            run_process=self.process,
            lock_path=self.lock_path,
            backups=self.backups,
            history_path=self.history_path,
            temp_root=self.temp_root,
            out=self.out,
            err=self.err,
            now=lambda: NOW,
            tz=timezone.utc,
            monotonic=self.time.monotonic,
            wall=self.time.wall,
            sleep=self.time.sleep,
            pid_alive=lambda pid: self.alive.get(pid),
            lock_timeout=2.0,
            session_timeout=2.0,
            settle=0,
            watch_lock_timeout=2.0,
            watch_session_timeout=2.0,
            open_browser=self.no_browser,
            browser_fn=lambda: "Google Chrome",
        )
        options.update(changes)
        return cli.Wiring(**options)

    @staticmethod
    def no_browser(url):
        raise AssertionError("A test tried to open a browser.")

    def run(self, *argv, **changes):
        self.out.seek(0)
        self.out.truncate()
        self.err.seek(0)
        self.err.truncate()
        code = cli.main(list(argv), wiring=self.wiring(**changes))
        out, err = self.out.getvalue(), self.err.getvalue()
        self.assert_secret_free(out, err)
        self.assert_sentences(out)
        self.assert_sentences(err)
        self.case.assertNotIn("Traceback", out + err)
        return code, out, err

    def live(self):
        return self.slot.item.get("claudeAiOauth")

    def assert_secret_free(self, *texts):
        secrets = self.profiles.secrets() + ["mcp-access-SECRET", "mcp-refresh-SECRET", "header-SECRET", KEY_MARKER]
        for text in texts:
            for secret in secrets:
                self.case.assertNotIn(secret, text)

    def assert_sentences(self, text):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            self.case.assertTrue(stripped.endswith("."), f"not a full sentence: {line!r}")
            self.case.assertTrue(stripped[0].isupper() or stripped[0].isdigit(), f"not a full sentence: {line!r}")

    def assert_others_kept(self):
        self.case.assertEqual(self.slot.item["mcpOAuth"], MCP)
        self.case.assertEqual(self.slot.item["otherKey"], {"kept": True})

    def add_reading(self, uuid, at, five=None, five_reset=None, week=None, week_reset=None):
        History(self.history_path).add_reading(uuid, "usage", five, five_reset, week, week_reset, at)

    def note(self):
        return json.loads(self.note_path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def interrupt_net():
    calls = []

    def guard(signum, frame):
        calls.append(signum)

    previous = {number: signal.signal(number, guard) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield calls
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def send_interrupt():
    os.kill(os.getpid(), signal.SIGINT)


def send_signal(number):
    def send():
        os.kill(os.getpid(), number)

    return send


SAVED_WORK = "Lifeline saved the sign-in for Work Account in its recovery folder, so it is not lost.\n"
SAVING_WORK = (
    "Lifeline is still saving the sign-in for Work Account. "
    "If you press Control-C again, Lifeline will put it in its recovery folder and stop.\n"
)


def rescued(world, uuid):
    folder = world.folder / "recovery"
    if not folder.is_dir():
        return False
    files = [path for path in folder.iterdir() if path.name.startswith(uuid + ".rescue.") and path.name.endswith(".json")]
    for path in files:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False
    secret = world.signins[uuid]["refreshToken"]
    return any(secret.encode() in path.read_bytes() for path in files)


ADD_HINT = (
    "Lifeline opens Claude's sign-in in Google Chrome, your default browser. Sign in there as the account you are adding. "
    "If Claude asks you to choose an organization, choose the one you are adding, then press Authorize."
)
ALREADY_NOTE = "If Google Chrome is signed in to claude.ai as that account, sign out there or choose the other account, then try again."
ADD_AGAIN_NOTE = (
    "If Google Chrome is signed in to claude.ai as that account, sign out there or choose the other account, "
    "then run lifeline add again."
)
DEVBOX_SENTENCE = "Lifeline switches only this Mac, so nothing was changed.\n"


def on_disk(root, secret):
    hits = []
    for folder, _, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            try:
                if secret.encode() in path.read_bytes():
                    hits.append(str(path.relative_to(root)))
            except OSError:
                pass
    return hits


class SwitchCommandTests(unittest.TestCase):
    def test_switch_by_number(self):
        world = World(self)
        code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(
            out,
            "Claude Code now uses Work Account. It was on Alex's Account.\n"
            "Anthropic confirmed that the new sign-in belongs to Work Account.\n",
        )
        self.assertEqual(world.live(), world.signins[WORK])
        world.assert_others_kept()
        self.assertEqual(world.store.inner.get(ALEX), world.signins[ALEX])
        self.assertIsNone(world.store.inner.get(WORK))
        self.assertEqual(world.config()["oauthAccount"], details(WORK))
        self.assertEqual(world.time.sleeps, [])
        note = world.note()
        self.assertEqual(set(note), {"from_uuid", "to_uuid", "at"})
        self.assertEqual((note["from_uuid"], note["to_uuid"]), (ALEX, WORK))
        self.assertEqual(note["at"], "2026-11-19T14:00:00+00:00")
        self.assertEqual(stat.S_IMODE(world.note_path.stat().st_mode), 0o600)
        world.assert_secret_free(world.note_path.read_text())

    def test_switch_through_every_number_and_back(self):
        world = World(self)
        for number, uuid in (("2", WORK), ("3", PERSONAL), ("4", ORG), ("1", ALEX), ("3", PERSONAL)):
            code, out, err = world.run("switch", number, "--no-watch")
            self.assertEqual(code, 0, err)
            self.assertEqual(world.live(), world.signins[uuid])
            self.assertEqual(world.config()["oauthAccount"], details(uuid))
            self.assertEqual(world.note()["to_uuid"], uuid)
            world.assert_others_kept()
            for other in ACCOUNTS:
                if other != uuid:
                    self.assertEqual(world.store.inner.get(other), world.signins[other])
            self.assertIsNone(world.store.inner.get(uuid))

    def test_bad_numbers_are_refused_before_anything_is_read(self):
        world = World(self)
        for word in ("0", "x", "1.5", "-1", "01", " 2", "２", ""):
            with self.subTest(word=word):
                code, out, err = world.run("switch", word, "--no-watch")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err, "An account number must be a whole number, 1 or more.\n")
        self.assertEqual(world.slot.reads, 0)
        self.assertEqual(world.profiles.calls, [])

    def test_missing_or_extra_words_are_refused(self):
        world = World(self)
        cases = (
            (("switch",), "Give the number of the account to switch to, as in lifeline switch 2.\n"),
            (("switch", "2", "3"), "Give the number of the account to switch to, as in lifeline switch 2.\n"),
            (("switch", "2", "--watch"), "The switch command has no option called --watch. Run lifeline help to see its options.\n"),
            (("switch", "2", "--no-watch", "--no-watch"), "The option --no-watch was given twice.\n"),
            (("switch", "2", "--no-watch=yes"), "The option --no-watch takes no value.\n"),
            (("undo", "2"), "Lifeline did not expect 2 after undo. Run lifeline help to see its commands.\n"),
            (("jump",), "Lifeline has no command called jump. Run lifeline help to see its commands.\n"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                code, out, err = world.run(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(err, message)
        self.assertEqual(world.slot.reads, 0)

    def test_number_not_in_lifeline_is_refused(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        code, out, err = world.run("switch", "4", "--no-watch")
        self.assertEqual(code, 1)
        self.assertEqual(err, "There is no account number 4 in Lifeline, so nothing was changed.\n")
        self.assertEqual(world.live(), world.signins[ALEX])
        self.assertFalse(world.note_path.exists())

    def test_switch_to_the_account_in_use_is_refused(self):
        world = World(self)
        code, out, err = world.run("switch", "1", "--no-watch")
        self.assertEqual(code, 1)
        self.assertEqual(err, "Claude Code is already using Alex's Account, so nothing was changed.\n")
        self.assertFalse(world.note_path.exists())

    def test_each_switch_error_is_one_plain_sentence(self):
        world = World(self)
        for kind in (SwitchRefused, SwitchFailed, LifelineBusy, IdentityUnknown):
            message = f"A {kind.__name__} sentence for the test, so nothing was changed."
            with self.subTest(kind=kind.__name__):
                with mock.patch.object(switching, "switch", side_effect=kind(message)):
                    code, out, err = world.run("switch", "2")
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err, message + "\n")
        self.assertEqual(world.time.sleeps, [])
        self.assertFalse(world.note_path.exists())

    def test_error_notes_are_printed_as_their_own_sentences(self):
        world = World(self)
        error = SwitchFailed("Lifeline could not finish the switch.")
        error.add_note("Lifeline could not remove the temporary folder. Remove it before going on.")
        with mock.patch.object(switching, "switch", side_effect=error):
            code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            "Lifeline could not finish the switch.\nLifeline could not remove the temporary folder. Remove it before going on.\n",
        )

    def test_unknown_identity_from_the_real_switch(self):
        world = World(self)
        world.profiles.offline = True
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, switching.UNCONFIRMED + "\n")
        self.assertEqual(world.live(), world.signins[ALEX])
        self.assertEqual(world.time.sleeps, [])

    def test_busy_lifeline_from_the_real_switch(self):
        world = World(self)
        with writelock.held(world.lock_path):
            code, out, err = world.run("switch", "2", lock_timeout=0.2)
        self.assertEqual(code, 1)
        self.assertEqual(err, switching.LIFELINE_BUSY + "\n")
        self.assertEqual(world.live(), world.signins[ALEX])

    def test_failed_switch_from_the_real_switch(self):
        world = World(self)
        world.store.fail_puts = 1
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            "Lifeline could not save the sign-in for Alex's Account. "
            "Lifeline stopped before changing Claude Code, which is still on Alex's Account.\n",
        )
        self.assertEqual(world.live(), world.signins[ALEX])
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])

    def test_signed_out_slot_from_the_real_switch(self):
        world = World(self)
        del world.slot.item["claudeAiOauth"]
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, switching.NOT_SIGNED_IN + "\n")

    def test_unexpected_error_shows_no_detail(self):
        world = World(self)
        with mock.patch.object(switching, "switch", side_effect=RuntimeError("refresh-11111111-1-SECRET in a message")):
            code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, cli.UNEXPECTED + "\n")

    def test_unconfirmed_and_wrong_account_results_are_reported(self):
        world = World(self)
        real = switching.switch

        def unconfirmed(*args, **kwargs):
            result = real(*args, **kwargs)
            return switching.SwitchResult(result.from_uuid, result.to_uuid, result.backup, True, None, result.signin)

        with mock.patch.object(switching, "switch", side_effect=unconfirmed):
            code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code now uses Work Account. It was on Alex's Account.\n"
            "Lifeline could not confirm the new sign-in with Anthropic.\n"
            "A spare copy of the sign-in for Work Account may be left in Lifeline.\n",
        )
        wrong = switching.SwitchResult(WORK, PERSONAL, None, False, False, None)
        with mock.patch.object(switching, "switch", return_value=wrong):
            code, out, err = world.run("switch", "3", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code now uses Personal Account. It was on Work Account.\n"
            "Anthropic says the new sign-in does not belong to Personal Account. "
            "Sign in to Personal Account again with lifeline resign 3.\n",
        )

    def test_note_that_cannot_be_saved_is_reported_and_the_switch_stands(self):
        world = World(self)
        world.folder.mkdir(parents=True, exist_ok=True)
        world.note_path.mkdir()
        code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0)
        self.assertIn("Lifeline could not save a note of this switch, so lifeline undo cannot switch back from it.\n", out)
        self.assertEqual(world.live(), world.signins[WORK])

    def test_interrupt_during_the_switch_finishes_it_and_skips_the_watch(self):
        world = World(self)
        world.slot.write_hooks.append(lambda slot: send_interrupt())
        with interrupt_net() as caught:
            net = signal.getsignal(signal.SIGINT)
            code, out, err = world.run("switch", "2")
            self.assertIs(signal.getsignal(signal.SIGINT), net)
        self.assertEqual(caught, [])
        self.assertEqual(code, 0, err)
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertEqual(world.store.inner.get(ALEX), world.signins[ALEX])
        self.assertTrue(out.endswith("You pressed Control-C, so Lifeline will not watch this switch.\n"))
        self.assertEqual(world.time.sleeps, [])


class WatchTests(unittest.TestCase):
    def test_watch_ends_at_its_lifetime(self):
        world = World(self)
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code now uses Work Account. It was on Alex's Account.\n"
            "Anthropic confirmed that the new sign-in belongs to Work Account.\n"
            "Lifeline will watch Claude Code for 10 minutes so the switch holds.\n"
            "Press Control-C to stop watching. Stopping the watch does not undo the switch.\n"
            "Lifeline has stopped watching Claude Code.\n",
        )
        self.assertEqual(set(world.time.sleeps), {5})
        self.assertEqual(sum(world.time.sleeps), 600)
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertIsNone(world.store.inner.get(WORK))

    def test_watch_prints_each_report(self):
        world = World(self)
        foreign = world.profiles.issue(PERSONAL, 2)

        def other_app():
            world.slot.item["claudeAiOauth"] = copy.deepcopy(foreign)

        world.time.hooks[2] = other_app
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertIn(
            "Another app switched Claude Code to Personal Account. "
            "Lifeline kept the newest sign-in for Personal Account and switched back to Work Account.\n",
            out,
        )
        self.assertTrue(out.endswith("Lifeline has stopped watching Claude Code.\n"))
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertEqual(world.store.inner.get(PERSONAL), foreign)

    def test_keyboard_interrupt_between_polls_ends_the_watch_and_the_switch_stands(self):
        world = World(self)

        def press():
            raise KeyboardInterrupt

        world.time.hooks[3] = press
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("You stopped the watch. The switch to Work Account stands.\n"), out)
        self.assertEqual(len(world.time.sleeps), 3)
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertIsNone(world.store.inner.get(WORK))

    def test_control_c_signal_during_the_wait_ends_the_watch(self):
        world = World(self)
        world.time.hooks[2] = send_interrupt
        with interrupt_net() as caught:
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("You stopped the watch. The switch to Work Account stands.\n"), out)
        self.assertEqual(len(world.time.sleeps), 2)
        self.assertEqual(world.live(), world.signins[WORK])

    def test_control_c_during_a_check_waits_for_the_check_to_finish(self):
        world = World(self)
        world.time.hooks[1] = lambda: world.slot.read_hooks.update({world.slot.reads + 1: lambda slot: send_interrupt()})
        with interrupt_net() as caught:
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("You stopped the watch. The switch to Work Account stands.\n"), out)
        self.assertEqual(len(world.time.sleeps), 1)

    def test_stopping_while_another_app_holds_the_slot_keeps_the_chosen_signin(self):
        world = World(self)
        stranger = world.profiles.issue(STRANGER, 1)

        def other_app_then_press():
            world.slot.item["claudeAiOauth"] = copy.deepcopy(stranger)
            raise KeyboardInterrupt

        world.time.hooks[2] = other_app_then_press
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])
        self.assertEqual(world.live(), stranger)
        self.assertIn(
            "Claude Code is on an account that is not in Lifeline at the end of the watch. "
            "Work Account is saved in Lifeline, ready to switch back to.\n",
            out,
        )
        self.assertTrue(
            out.endswith("You stopped the watch.\nLifeline's record names no account, and Claude Code's settings name Work Account.\n"),
            out,
        )
        self.assertNotIn("stands", out)

    def test_second_control_c_during_the_last_check_stops_at_once_and_rescues(self):
        world = World(self)

        def press():
            raise KeyboardInterrupt

        world.time.hooks[1] = press
        real_poll = switching.Watcher.poll
        polls = []

        def poll(watcher):
            polls.append(1)
            if len(polls) == 2:
                send_interrupt()
            return real_poll(watcher)

        with interrupt_net() as caught, mock.patch.object(switching.Watcher, "poll", poll):
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 130)
        self.assertTrue(out.endswith("You stopped the watch.\n" + SAVED_WORK), out)
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertTrue(rescued(world, WORK))

    def test_last_check_keeps_trying_until_it_can_save_the_chosen_signin(self):
        world = World(self)
        stranger = world.profiles.issue(STRANGER, 1)
        holder = contextlib.ExitStack()
        self.addCleanup(holder.close)

        def other_app_holds_the_lock_then_press():
            world.slot.item["claudeAiOauth"] = copy.deepcopy(stranger)
            holder.enter_context(writelock.held(world.lock_path))
            raise KeyboardInterrupt

        world.time.hooks[2] = other_app_holds_the_lock_then_press
        world.time.hooks[4] = holder.close
        code, out, err = world.run("switch", "2", watch_lock_timeout=0.1)
        self.assertEqual(code, 0, err)
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])
        self.assertTrue(
            out.endswith(
                SAVING_WORK
                + "Claude Code is on an account that is not in Lifeline at the end of the watch. "
                "Work Account is saved in Lifeline, ready to switch back to.\n"
                "You stopped the watch.\n"
                "Lifeline's record names no account, and Claude Code's settings name Work Account.\n"
            ),
            out,
        )
        self.assertEqual(world.time.sleeps, [5, 5, 5, 5])

    def test_second_control_c_while_saving_rescues_the_signin_and_a_later_switch_uses_it(self):
        world = World(self)
        stranger = world.profiles.issue(STRANGER, 1)
        holder = contextlib.ExitStack()
        self.addCleanup(holder.close)

        def other_app_holds_the_lock_then_press():
            world.slot.item["claudeAiOauth"] = copy.deepcopy(stranger)
            holder.enter_context(writelock.held(world.lock_path))
            send_interrupt()

        world.time.hooks[1] = other_app_holds_the_lock_then_press
        world.time.hooks[2] = send_interrupt
        with interrupt_net() as caught:
            code, out, err = world.run("switch", "2", watch_lock_timeout=0.1)
        self.assertEqual(caught, [])
        self.assertEqual(code, 130)
        self.assertTrue(out.endswith(SAVING_WORK + "You stopped the watch.\n" + SAVED_WORK), out)
        self.assertEqual(world.live(), stranger)
        self.assertIsNone(world.store.inner.get(WORK))
        self.assertTrue(rescued(world, WORK))
        holder.close()
        world.slot.item["claudeAiOauth"] = copy.deepcopy(world.signins[ALEX])
        world.write_config(config_data(details(ALEX)))
        code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.live(), world.signins[WORK])

    def test_control_c_during_the_check_that_ends_the_watch_does_not_claim_the_switch_stands(self):
        world = World(self)
        world.slot.read_hooks[1] = None
        world.time.hooks[1] = lambda: world.slot.read_hooks.update(
            {world.slot.reads + 1: lambda slot: (slot.item.pop("claudeAiOauth"), send_interrupt())}
        )
        with interrupt_net() as caught:
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 0, err)
        self.assertIn("Claude Code was signed out. Work Account is saved in Lifeline, ready to switch back to.\n", out)
        self.assertTrue(out.endswith("Lifeline has stopped watching Claude Code.\n"), out)
        self.assertNotIn("stands", out)
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])

    def test_unexpected_error_in_every_check_still_rescues(self):
        world = World(self)
        with mock.patch.object(switching.Watcher, "poll", side_effect=RuntimeError("access-SECRET leaked")) as poll:
            code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(err, "Lifeline hit an unexpected problem while watching, so it stopped watching.\n" + SAVED_WORK)
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertTrue(rescued(world, WORK))

    def test_unexpected_error_runs_one_last_check_then_rescues(self):
        world = World(self)
        stranger = world.profiles.issue(STRANGER, 1)
        real_poll = switching.Watcher.poll
        polls = []

        def poll(watcher):
            polls.append(1)
            if len(polls) == 2:
                world.slot.item["claudeAiOauth"] = copy.deepcopy(stranger)
                raise RuntimeError("access-SECRET leaked")
            return real_poll(watcher)

        with mock.patch.object(switching.Watcher, "poll", poll):
            code, out, err = world.run("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, "Lifeline hit an unexpected problem while watching, so it stopped watching.\n" + SAVED_WORK)
        self.assertEqual(len(polls), 3)
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])
        self.assertTrue(out.endswith("Work Account is saved in Lifeline, ready to switch back to.\n"), out)

    def test_watcher_gets_the_seed_and_ten_minutes(self):
        world = World(self)
        made = []
        real = switching.Watcher

        def spy(*args, **kwargs):
            made.append((args, kwargs))
            return real(*args, **kwargs)

        with mock.patch.object(switching, "Watcher", side_effect=spy):
            world.run("switch", "2")
        args, kwargs = made[0]
        self.assertEqual(args[3], WORK)
        self.assertEqual(kwargs["lifetime"], 10)
        self.assertEqual(kwargs["seed"].to_uuid, WORK)
        self.assertEqual(kwargs["home"], world.home)
        self.assertEqual(kwargs["lock_path"], world.lock_path)



def stale_signin_file(folder):
    path = folder / ".credentials.json"
    path.write_bytes(b"stale copy")
    os.chmod(path, 0o600)
    old = time.time() - 86400
    os.utime(path, (old, old))
    return path


def sessions_moved(path):
    return time.time() - path.stat().st_mtime < 5


class SessionMoveCommandTests(unittest.TestCase):
    def test_the_command_line_switch_and_undo_move_open_sessions(self):
        world = World(self)
        path = stale_signin_file(world.home / ".claude")
        code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertTrue(sessions_moved(path))
        old = time.time() - 86400
        os.utime(path, (old, old))
        code, out, err = world.run("undo", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertTrue(sessions_moved(path))
        self.assertEqual(path.read_bytes(), b"stale copy")

    def test_the_command_line_moves_the_plain_items_sessions_whatever_the_environment(self):
        world = World(self)
        other = world.root / "config"
        other.mkdir()
        elsewhere = stale_signin_file(other)
        usual = stale_signin_file(world.home / ".claude")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other), "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(other)}):
            code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertTrue(sessions_moved(usual))
        self.assertFalse(sessions_moved(elsewhere))

    def test_the_command_line_adopt_moves_open_sessions(self):
        world = World(self, registered=(WORK, PERSONAL, ORG))
        path = stale_signin_file(world.home / ".claude")
        code, out, err = world.run("adopt", "--name", "Alex's Account", "--short", "Alex")
        self.assertEqual(code, 0, err)
        self.assertTrue(sessions_moved(path))

    def test_the_mac_command_line_takes_no_folder_from_the_environment(self):
        world = World(self)
        other = str(world.root / "config")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": other, "CLAUDE_SECURESTORAGE_CONFIG_DIR": other}):
            wiring = cli.production(system="darwin")
        self.assertEqual(wiring.slot.service, "Claude Code-credentials")
        self.assertIsNone(getattr(wiring, "claude_folder", None))



class DevboxUnsureTests(unittest.TestCase):
    def silent_link(self, calls):
        from lifeline import devbox

        class Silent:
            def request(self, method, route, body=None):
                return 200, {"accounts": 0}

            def start_signin(self, route, body):
                calls["token"] = body["request"]
                raise devbox.LinkError()

            def find_request(self, token):
                raise devbox.LinkError()

        return Silent()

    def run_add(self, world, calls, **extra):
        options = dict(devbox_link=lambda: self.silent_link(calls), system="darwin", relay=None, open_page=None)
        options.update(extra)
        return world.run("computer", "add", "3", **options)

    def handed(self, fake):
        return [body for method, route, body, _ in fake.calls if (method, route) == ("POST", "/devbox/unsure")]

    def test_an_unsure_start_hands_its_request_to_the_mac_service(self):
        from lifeline import devbox

        world = World(self)
        calls = {}
        fake = FakeService({("POST", "/devbox/unsure"): (202, {"kept": True})})
        code, out, err = self.run_add(world, calls, service=lambda: fake)
        self.assertEqual(code, 1)
        self.assertEqual(self.handed(fake), [{"request": calls["token"]}])
        self.assertIn(devbox.DEVBOX_UNSURE, err)

    def test_with_no_mac_service_the_line_promises_only_what_the_devbox_does(self):
        world = World(self)
        calls = {}
        code, out, err = self.run_add(world, calls, service=lambda: None)
        self.assertEqual(code, 1)
        self.assertIn(cli.DEVBOX_UNSURE_ALONE, err)
        self.assertEqual(
            cli.DEVBOX_UNSURE_ALONE,
            "The other computer did not answer in time. If the sign-in started there, the other computer ends it on its own within 10 minutes.",
        )
        self.assertNotIn("Lifeline stops it", err)

    def test_a_stop_during_the_lookup_hands_the_request_on_and_says_what_happens(self):
        world = World(self)
        calls = {}
        fake = FakeService({("POST", "/devbox/unsure"): (202, {"kept": True})})

        def stop(seconds):
            raise KeyboardInterrupt()

        code, out, err = self.run_add(world, calls, service=lambda: fake, sleep=stop)
        self.assertEqual(code, 130)
        self.assertEqual(self.handed(fake), [{"request": calls["token"]}])
        self.assertIn(cli.STOPPED_KEPT, err)
        code, out, err = self.run_add(world, calls, service=lambda: None, sleep=stop)
        self.assertIn(cli.STOPPED_ALONE, err)


class UndoTests(unittest.TestCase):
    def test_undo_without_a_last_switch_is_refused(self):
        world = World(self)
        code, out, err = world.run("undo")
        self.assertEqual(code, 1)
        self.assertEqual(err, "There is no switch to undo, so nothing was changed.\n")
        self.assertEqual(world.slot.reads, 0)
        self.assertEqual(world.live(), world.signins[ALEX])

    def test_undo_switches_back_and_can_be_undone_again(self):
        world = World(self)
        world.run("switch", "2", "--no-watch")
        code, out, err = world.run("undo", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline undid the last switch.\n"
            "Claude Code now uses Alex's Account. It was on Work Account.\n"
            "Anthropic confirmed that the new sign-in belongs to Alex's Account.\n",
        )
        self.assertEqual(world.live(), world.signins[ALEX])
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])
        self.assertEqual((world.note()["from_uuid"], world.note()["to_uuid"]), (WORK, ALEX))
        code, out, err = world.run("undo", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.live(), world.signins[WORK])
        world.assert_others_kept()

    def test_undo_watches_by_default(self):
        world = World(self)
        world.run("switch", "2", "--no-watch")
        code, out, err = world.run("undo")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("Lifeline has stopped watching Claude Code.\n"))
        self.assertEqual(sum(world.time.sleeps), 600)

    def test_damaged_note_is_refused(self):
        for text in ("{not json", "[]", '{"from_uuid": "", "to_uuid": "x", "at": "y"}', '{"from_uuid": 5, "to_uuid": "x"}'):
            with self.subTest(text=text):
                world = World(self)
                world.folder.mkdir(parents=True, exist_ok=True)
                world.note_path.write_text(text)
                code, out, err = world.run("undo")
                self.assertEqual(code, 1)
                self.assertEqual(err, "Lifeline could not read its note of the last switch, so nothing was changed.\n")
                self.assertEqual(world.live(), world.signins[ALEX])

    def test_undo_errors_are_plain(self):
        world = World(self)
        world.run("switch", "2", "--no-watch")
        world.profiles.offline = True
        code, out, err = world.run("undo")
        self.assertEqual(code, 1)
        self.assertEqual(err, switching.UNCONFIRMED + "\n")
        self.assertEqual(world.live(), world.signins[WORK])


class AddTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        self.world.process.account = PERSONAL

    def login_call(self):
        self.assertEqual(len(self.world.process.calls), 1)
        args, kwargs = self.world.process.calls[0]
        folder = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        self.assertEqual(folder.parent, self.world.temp_root)
        self.assertTrue(folder.name.startswith("lifeline-login-"))
        self.assertFalse(folder.exists())
        self.assertEqual(kwargs["env"]["PATH"], os.pathsep.join([str(folder / server.OPEN_SHIM), os.environ["PATH"]]))
        self.assertEqual(kwargs["env"]["BROWSER"], str(devbox.hook_path()))
        self.assertNotEqual(kwargs.get("shell"), True)
        self.assertEqual(kwargs.get("timeout"), cli.LOGIN_TIMEOUT)
        self.assertGreater(cli.LOGIN_TIMEOUT, 0)
        return args

    def test_an_add_of_a_known_account_stores_its_names(self):
        known = [{"name": "Mission Personal", "short": "MP", "full": "Mission Personal Full",
                  "email": "33333333@example.invalid", "organization": "personal"}]
        (self.world.folder / "known_accounts.json").write_text(json.dumps(known))
        code, out, err = self.world.run("add")
        self.assertEqual(code, 0, err)
        account = self.world.registry.by_uuid(PERSONAL)
        self.assertEqual((account.name, account.short, account.full), ("Mission Personal", "MP", "Mission Personal Full"))
        self.assertIn("Lifeline added Mission Personal as number 3.", out)

    def test_add_without_email(self):
        code, out, err = self.world.run("add")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.login_call(), ["claude", "auth", "login", "--claudeai"])
        account = self.world.registry.by_uuid(PERSONAL)
        self.assertEqual(account.key, 3)
        self.assertEqual(self.world.store.inner.get(PERSONAL), self.world.process.issued[0])
        self.assertEqual(self.world.live(), self.world.signins[ALEX])
        self.assertEqual(self.world.keychain.temporary(), [])
        self.assertEqual(
            out,
            ADD_HINT + "\n"
            "Lifeline added Person 33333333 as number 3. It is on the Max 5x plan.\n",
        )

    def test_add_with_email(self):
        code, out, err = self.world.run("add", "--email", "sam@example.org")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.login_call(), ["claude", "auth", "login", "--claudeai", "--email", "sam@example.org"])
        self.assertTrue(out.startswith(
            "Lifeline opens Claude's sign-in for sam@example.org in Google Chrome, your default browser. "
            "If Claude asks you to choose an organization, choose the one you are adding, then press Authorize.\n"
        ), out)

    def test_on_a_mac_the_add_opens_its_page_in_the_default_browser(self):
        world = self.world
        opened, seen = [], {}

        def open_browser(url):
            opened.append(url)
            return "Google Chrome", True

        def run_process(args, **kwargs):
            seen.update(kwargs["env"])
            Path(kwargs["env"][devbox.PAGE_VARIABLE]).write_text(served.PAGE + "\n")
            deadline = time.monotonic() + 5
            while not opened and time.monotonic() < deadline:
                time.sleep(0.01)
            return world.process(args, **kwargs)

        code, out, err = world.run("add", system="darwin", run_process=run_process, open_browser=open_browser)
        self.assertEqual(code, 0, err)
        self.assertEqual(opened, [served.PAGE])
        self.assertEqual(seen["BROWSER"], str(devbox.hook_path()))
        self.assertTrue(seen["PATH"].startswith(seen["CLAUDE_CONFIG_DIR"]))
        self.assertIn("Lifeline opened the sign-in page in Google Chrome.\n", out)
        self.assertNotIn("private", out)
        self.assertIsNotNone(world.registry.by_uuid(PERSONAL))

    def page_run(self, world, page, browser, **changes):
        opened = []

        def open_browser(url):
            opened.append(url)
            return browser, True

        def run_process(args, **kwargs):
            Path(kwargs["env"][devbox.PAGE_VARIABLE]).write_text(page + "\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not world.said_page:
                time.sleep(0.01)
            return world.process(args, **kwargs)

        world.said_page = False
        return world.run("add", system="darwin", run_process=run_process, open_browser=open_browser, **changes), opened

    def test_safari_as_the_default_browser_is_named_plainly(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = PERSONAL
        real_say = cli._say

        def watch(wiring, lines):
            if any("sign-in page" in line and "opened" in line for line in lines):
                world.said_page = True
            return real_say(wiring, lines)

        with mock.patch.object(cli, "_say", watch):
            (code, out, err), opened = self.page_run(world, served.PAGE, "Safari", browser_fn=lambda: "Safari")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(ADD_HINT.replace("Google Chrome", "Safari") + "\n"), out)
        self.assertIn("Lifeline opened the sign-in page in Safari.\n", out)
        self.assertNotIn("private", out)

    def test_an_untrusted_page_says_how_to_stop_the_sign_in(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = PERSONAL
        real_say = cli._say

        def watch(wiring, lines):
            if any("does not trust" in line for line in lines):
                world.said_page = True
            return real_say(wiring, lines)

        with mock.patch.object(cli, "_say", watch):
            (code, out, err), opened = self.page_run(world, "https://example.invalid/oauth/authorize", "Google Chrome")
        self.assertEqual(opened, [])
        self.assertIn(
            "Claude Code gave a sign-in page Lifeline does not trust, so Lifeline did not open it. "
            "Press Control-C to stop the sign-in.\n",
            out,
        )

    def test_a_sign_in_again_names_the_organization_to_choose(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = WORK
        saved = world.registry.by_uuid(WORK)
        organization = "Example Team"
        world.registry.upsert(replace(saved, oauth_account=dict(saved.oauth_account or {}, organizationName=organization)))
        code, out, err = world.run("resign", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out.splitlines()[0],
            "Lifeline opens Claude's sign-in in Google Chrome, your default browser. Sign in there as Work Account. "
            f"If Claude asks you to choose an organization, choose {organization}, then press Authorize.",
        )

    def test_a_sign_in_again_to_a_personal_account_says_to_choose_personal(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = WORK
        saved = world.registry.by_uuid(WORK)
        world.registry.upsert(replace(saved, oauth_account=dict(saved.oauth_account or {}, organizationName="c@example.invalid's Organization")))
        code, out, err = world.run("resign", "2")
        self.assertEqual(code, 0, err)
        self.assertTrue(
            out.splitlines()[0].endswith("If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize."),
            out.splitlines()[0],
        )

    def test_add_with_email_given_with_equals_sign(self):
        code, out, err = self.world.run("add", "--email=a.b@example.com")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.login_call()[-2:], ["--email", "a.b@example.com"])

    def test_add_with_names_and_number(self):
        code, out, err = self.world.run("add", "--name", "Team Account", "--short", "Team", "--number", "4")
        self.assertEqual(code, 0, err)
        account = self.world.registry.by_uuid(PERSONAL)
        self.assertEqual((account.name, account.short, account.full, account.key), ("Team Account", "Team", "Team Account", 4))
        self.assertIn("Lifeline added Team Account as number 4.", out)

    def test_bad_email_and_names_are_refused_before_login(self):
        cases = (
            (("add", "--email", "no-at-sign"), "An email must look like name@example.com.\n"),
            (("add", "--email=--console"), "An email must look like name@example.com.\n"),
            (("add", "--email", "a b@example.com"), "An email must look like name@example.com.\n"),
            (("add", "--email"), "The option --email needs a value.\n"),
            (("add", "--email", "--name", "X"), "The option --email needs a value.\n"),
            (("add", "--name", "   "), "A name cannot be empty.\n"),
            (("add", "--short", "two\nlines"), "A name must be plain text on one line.\n"),
            (("add", "--number", "0"), "An account number must be a whole number, 1 or more.\n"),
            (("add", "--name", "A", "--name", "B"), "The option --name was given twice.\n"),
            (("add", "--no-watch"), "The add command has no option called --no-watch. Run lifeline help to see its options.\n"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                code, out, err = self.world.run(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(err, message)
        self.assertEqual(self.world.process.calls, [])

    def test_failed_login_says_why_and_adds_nothing(self):
        cases = (
            ("returncode", 1, "Claude Code's sign-in command stopped with code 1, so nothing was added."),
            ("error", FileNotFoundError("claude"), "Lifeline could not find Claude Code's command on this computer, so nothing was added."),
            ("error", subprocess.TimeoutExpired(["claude"], cli.LOGIN_TIMEOUT), "Lifeline stopped waiting for the sign-in after 10 minutes, so nothing was added."),
            ("error", OSError("boom"), "Lifeline could not start Claude Code's sign-in command, so nothing was added."),
        )
        for field, value, reason in cases:
            with self.subTest(reason=reason):
                world = World(self, registered=(ALEX, WORK), parked=(WORK,))
                world.process.account = PERSONAL
                setattr(world.process, field, value)
                code, out, err = world.run("add")
                self.assertEqual(code, 1)
                self.assertEqual(err, reason + "\n")
                self.assertIsNone(world.registry.by_uuid(PERSONAL))
                self.assertEqual(list(world.temp_root.iterdir()), [])

    def test_a_fifth_account_is_added_as_number_5(self):
        world = World(self)
        world.process.account = STRANGER
        code, out, err = world.run("add")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(world.registry.by_uuid(STRANGER).key, 5)
        code, out, err = world.run("switch", "5", "--no-watch")
        self.assertEqual(code, 0, err)
        code, out, err = world.run("switch", "12", "--no-watch")
        self.assertEqual((code, err), (1, "There is no account number 12 in Lifeline, so nothing was changed.\n"))

    def test_already_added_account_is_refused(self):
        self.world.process.account = WORK
        code, out, err = self.world.run("add")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            "Lifeline already has Work Account. Your browser signed in as 22222222@example.invalid, so nothing changed.\n"
            + ADD_AGAIN_NOTE + "\n",
        )
        self.assertEqual(self.world.store.inner.get(WORK), self.world.signins[WORK])

    def test_store_failure_is_plain(self):
        self.world.store.fail_puts = 1
        code, out, err = self.world.run("add")
        self.assertEqual(code, 1)
        self.assertEqual(err, "Lifeline could not save the new sign-in in the Keychain, so nothing was added.\n")
        self.assertIsNone(self.world.registry.by_uuid(PERSONAL))
        self.assertEqual(self.world.keychain.temporary(), [])

    def test_damaged_registry_is_plain(self):
        self.world.registry.path.write_text("{broken")
        code, out, err = self.world.run("add")
        self.assertEqual(code, 1)
        self.assertEqual(err, cli.REGISTRY_PROBLEM + "\n")


class AdoptTests(unittest.TestCase):
    def test_adopt_adds_the_account_in_use(self):
        world = World(self, registered=(WORK, PERSONAL, ORG))
        before = (copy.deepcopy(world.slot.item), world.slot.writes)
        code, out, err = world.run("adopt", "--name", "Alex's Account", "--short", "Alex")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline added Alex's Account as number 1. It is on the Max 5x plan. Claude Code uses it now.\n",
        )
        account = world.registry.by_uuid(ALEX)
        self.assertEqual((account.name, account.short, account.key), ("Alex's Account", "Alex", 1))
        self.assertEqual((world.slot.item, world.slot.writes), before)
        self.assertNotIn(("put", ALEX), world.store.calls)
        self.assertEqual(switching.read_record(world.record_path), ALEX)

    def test_adopt_updates_a_registered_account(self):
        world = World(self)
        code, out, err = world.run("adopt")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Lifeline updated Alex's Account, number 1, with the account details Claude Code holds now.\n")
        self.assertEqual(world.registry.by_uuid(ALEX).name, "Alex's Account")

    def test_adopt_refuses_when_signed_out(self):
        world = World(self, live=None, registered=(WORK,), parked=(WORK,))
        code, out, err = world.run("adopt")
        self.assertEqual(code, 1)
        self.assertEqual(err, switching.NOT_SIGNED_IN + "\n")
        self.assertEqual(len(world.registry.load()), 1)

    def test_adopt_refuses_a_taken_number(self):
        world = World(self, registered=(WORK, PERSONAL, ORG))
        code, out, err = world.run("adopt", "--number", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, "Account number 2 is already in use.\n")
        self.assertIsNone(world.registry.by_uuid(ALEX))


class ResignTests(unittest.TestCase):
    def test_resign_parked_account(self):
        world = World(self)
        world.process.account = WORK
        code, out, err = world.run("resign", "2")
        self.assertEqual(code, 0, err)
        args, kwargs = world.process.calls[0]
        self.assertEqual(args, ["claude", "auth", "login", "--claudeai"])
        self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"].startswith(str(world.temp_root)), True)
        self.assertEqual(world.store.inner.get(WORK), world.process.issued[0])
        self.assertEqual(world.live(), world.signins[ALEX])
        self.assertTrue(out.endswith("Lifeline has a new sign-in for Work Account.\n"))

    def test_resign_with_email(self):
        world = World(self)
        world.process.account = WORK
        code, out, err = world.run("resign", "2", "--email", "hello@example.net")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.process.calls[0][0][-2:], ["--email", "hello@example.net"])

    def test_resign_live_account_keeps_every_other_key(self):
        world = World(self)
        world.process.account = ALEX
        code, out, err = world.run("resign", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.live(), world.process.issued[0])
        world.assert_others_kept()
        self.assertIsNone(world.store.inner.get(ALEX))

    def test_resign_with_another_account_is_refused(self):
        world = World(self)
        world.process.account = PERSONAL
        code, out, err = world.run("resign", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, "This sign-in belongs to a different account, so nothing was changed.\n")
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])

    def test_resign_unknown_number_is_refused_before_login(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        code, out, err = world.run("resign", "4")
        self.assertEqual(code, 1)
        self.assertEqual(err, "There is no account number 4 in Lifeline, so nothing was changed.\n")
        self.assertEqual(world.process.calls, [])

    def test_resign_needs_a_number(self):
        world = World(self)
        code, out, err = world.run("resign")
        self.assertEqual(code, 2)
        self.assertEqual(err, "Give the number of the account to sign in to again, as in lifeline resign 2.\n")


class RenameTests(unittest.TestCase):
    def test_rename_changes_only_the_names(self):
        world = World(self)
        before = world.registry.by_key(2)
        others = [account for account in world.registry.load() if account.key != 2]
        code, out, err = world.run("rename", "2", "Example Team", "Command")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Account 2 is now called Example Team, or Command for short.\n")
        after = world.registry.by_key(2)
        self.assertEqual((after.name, after.short, after.full), ("Example Team", "Command", "Example Team"))
        self.assertEqual((after.uuid, after.email, after.plan, after.key, after.oauth_account), (before.uuid, before.email, before.plan, before.key, before.oauth_account))
        self.assertEqual([account for account in world.registry.load() if account.key != 2], others)
        self.assertEqual(world.slot.reads, 0)
        self.assertEqual(world.profiles.calls, [])

    def test_the_data_fix_for_account_1(self):
        world = World(self)
        world.registry.upsert(replace(world.registry.by_key(1), name="Alexandra", short="Alex's Account"))
        code, out, err = world.run("rename", "1", "Alex's Account", "Alex")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Account 1 is now called Alex's Account, or Alex for short.\n")
        after = world.registry.by_key(1)
        self.assertEqual((after.name, after.short), ("Alex's Account", "Alex"))

    def test_there_is_one_name_and_a_short_name(self):
        world = World(self)
        code, out, err = world.run("rename", "3", "Personal", "Personal")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.registry.by_key(3).full, "Personal")
        for argv in (("rename", "3", "A", "B", "--full", "C"), ("add", "--full", "C"), ("adopt", "--full", "C")):
            with self.subTest(argv=argv):
                code, out, err = world.run(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(err, f"The {argv[0]} command has no option called --full. Run lifeline help to see its options.\n")
        self.assertEqual(world.registry.by_key(3).name, "Personal")

    def test_rename_refusals(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        cases = (
            (("rename", "2", "", "C"), 2, "A name cannot be empty.\n"),
            (("rename", "2", "Name"), 2, 'Give the account number, the new name and the new short name, as in lifeline rename 2 "Work Account" Work.\n'),
            (("rename", "4", "Name", "N"), 1, "There is no account number 4 in Lifeline, so nothing was changed.\n"),
        )
        for argv, expected_code, message in cases:
            with self.subTest(argv=argv):
                code, out, err = world.run(*argv)
                self.assertEqual(code, expected_code)
                self.assertEqual(err, message)
        self.assertEqual(world.registry.by_key(2).name, "Work Account")

    def test_rename_waits_for_the_write_lock(self):
        world = World(self)
        with writelock.held(world.lock_path):
            code, out, err = world.run("rename", "2", "Other", "O", lock_timeout=0.2)
        self.assertEqual(code, 1)
        self.assertEqual(err, switching.LIFELINE_BUSY + "\n")
        self.assertEqual(world.registry.by_key(2).name, "Work Account")


class StatusTests(unittest.TestCase):
    def test_status_prints_names_and_plans(self):
        world = World(self)
        world.add_reading(ALEX, NOW - timedelta(minutes=5), five=42.7, five_reset=NOW + timedelta(hours=2), week=17.2, week_reset=NOW + timedelta(days=3))
        world.add_reading(PERSONAL, datetime(2026, 11, 18, 9, 30, tzinfo=timezone.utc), week=64.0, week_reset=datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc))
        world.add_reading(ORG, NOW - timedelta(minutes=1), five=100.0, five_reset=NOW + timedelta(minutes=30))
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(
            out,
            "Claude Code is using Alex's Account.\n"
            "1. Alex's Account is on the Max 20x plan. It is in use now.\n"
            "   At 13:55 on 19 November it had used 42% of its 5 hours and 17% of its week.\n"
            "   Its 5 hours reset today at 16:00. Its week resets on Sunday at 14:00.\n"
            "2. Work Account is on the Max 5x plan.\n"
            "   Lifeline has no reading for it yet.\n"
            "3. Personal Account is on the Max 5x plan.\n"
            "   At 09:30 on 18 November it had used 64% of its week.\n"
            "   Its week has reset since then.\n"
            "4. Team Account is on an unknown plan.\n"
            "   At 13:59 on 19 November it had used 100% of its 5 hours.\n"
            "   Its 5 hours reset today at 14:30.\n",
        )

    def test_status_makes_no_network_call_and_reads_no_signin(self):
        world = World(self)
        world.add_reading(ALEX, NOW, five=10.0)
        refuse = mock.Mock(side_effect=AssertionError("network"))
        with mock.patch.object(socket.socket, "connect", refuse), mock.patch.object(
            socket, "create_connection", refuse
        ), mock.patch.object(urllib.request, "urlopen", refuse), mock.patch.object(subprocess, "run", refuse), mock.patch.object(
            subprocess, "Popen", refuse
        ):
            code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        refuse.assert_not_called()
        self.assertEqual(world.profiles.calls, [])
        self.assertEqual(world.slot.reads, 0)
        self.assertEqual(world.store.calls, [])
        self.assertEqual(world.keychain.calls, [])
        self.assertEqual(world.process.calls, [])

    def test_status_creates_no_history_file(self):
        world = World(self)
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertFalse(world.history_path.exists())
        self.assertEqual(out.count("Lifeline has no reading for it yet."), 4)

    def test_status_after_a_switch_names_the_last_switch(self):
        world = World(self)
        world.run("switch", "2", "--no-watch")
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Claude Code is using Work Account.\n"))
        self.assertIn("2. Work Account is on the Max 5x plan. It is in use now.\n", out)
        self.assertTrue(out.endswith("The last switch, at 14:00 on 19 November, moved Claude Code from Alex's Account to Work Account.\n"))

    def test_status_when_the_record_and_settings_disagree(self):
        world = World(self)
        switching.write_record(world.record_path, WORK)
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(
            out.startswith(
                "Claude Code's settings name Alex's Account, but Lifeline's record names Work Account, "
                "so Lifeline cannot tell which one is in use.\n"
            )
        )
        self.assertNotIn("It is in use now.", out)

    def test_status_for_an_account_not_in_lifeline(self):
        world = World(self, record=False)
        world.write_config(config_data(details(STRANGER)))
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Claude Code is using 55555555@example.invalid, which is not in Lifeline. Add it with lifeline adopt.\n"))
        self.assertNotIn("It is in use now.", out)

    def test_status_when_settings_name_no_account(self):
        world = World(self)
        world.write_config(config_data(None))
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Claude Code's settings name no account, so it may be signed out. Lifeline's record names Alex's Account.\n"))
        self.assertNotIn("It is in use now.", out)

    def test_status_when_settings_are_missing_or_damaged(self):
        world = World(self)
        world.account_file.write_text("{half written")
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Lifeline could not read Claude Code's settings file, so it cannot tell which account is in use.\n"))
        world.account_file.unlink()
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Claude Code has no settings file, so no account is in use.\n"))

    def test_status_with_no_accounts(self):
        world = World(self, registered=(), parked=())
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code is using 11111111@example.invalid, which is not in Lifeline. Add it with lifeline adopt.\n"
            "Lifeline has no accounts yet. Add one with lifeline add, or add the account Claude Code uses now with lifeline adopt.\n",
        )

    def test_status_with_a_damaged_registry(self):
        world = World(self)
        world.registry.path.write_text("[1, 2")
        code, out, err = world.run("status")
        self.assertEqual(code, 1)
        self.assertEqual(err, cli.REGISTRY_PROBLEM + "\n")

    def test_status_with_unreadable_history(self):
        world = World(self)
        world.folder.mkdir(parents=True, exist_ok=True)
        world.history_path.write_bytes(b"this is not a database at all" * 100)
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("Lifeline could not read its usage history, so no readings are shown.\n"))

    def test_status_dates_resets_on_another_day(self):
        world = World(self)
        world.add_reading(ALEX, NOW, five=5.0, five_reset=datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc))
        code, out, err = world.run("status")
        self.assertIn("   Its 5 hours reset tomorrow at 01:00.\n", out)

    def test_status_names_the_day_of_every_future_time(self):
        world = World(self)
        live_reset = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=timezone.utc)
        world.add_reading(ALEX, NOW, five=5.0, five_reset=datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc), week=61.0, week_reset=live_reset)
        world.add_reading(PERSONAL, NOW, five=5.0, five_reset=NOW + timedelta(hours=2), week=10.0, week_reset=datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc))
        code, out, err = world.run("status")
        self.assertIn("   Its 5 hours reset tomorrow at 01:00. Its week resets next Thursday at 02:00.\n", out)
        self.assertIn("   Its 5 hours reset today at 16:00. Its week resets on 27 November at 02:00.\n", out)
        for words in ("on today", "on tomorrow", "on next", "until on"):
            self.assertNotIn(words, out)

    def test_status_reset_in_the_past(self):
        world = World(self)
        world.add_reading(ALEX, NOW - timedelta(hours=6), five=88.0, five_reset=NOW - timedelta(hours=1), week=40.0, week_reset=NOW + timedelta(days=1))
        code, out, err = world.run("status")
        self.assertIn(
            "   At 08:00 on 19 November it had used 88% of its 5 hours and 40% of its week.\n"
            "   Its 5 hours have reset since then. Its week resets tomorrow at 14:00.\n",
            out,
        )

    def test_status_reading_with_no_figures(self):
        world = World(self)
        world.add_reading(ALEX, NOW, five=None, week=None)
        code, out, err = world.run("status")
        self.assertIn("   The reading at 14:00 on 19 November held no usage figures.\n", out)


class SessionsTests(unittest.TestCase):
    def write_session(self, world, pid, **fields):
        world.sessions.mkdir(parents=True, exist_ok=True)
        body = {"pid": pid, "procStart": "x", "version": "2.1.328", **fields}
        (world.sessions / f"{pid}.json").write_text(json.dumps(body))
        return world.sessions / f"{pid}.json"

    def test_sessions_never_opens_key_files(self):
        world = World(self)
        self.write_session(
            world, 12345, sessionId="s-one", cwd="/work/one", name="lifeline-build", kind="interactive", status="busy",
            startedAt=int((NOW - timedelta(hours=1)).timestamp() * 1000),
        )
        key = world.sessions / "12345.0000000000000000000000000000000000000000000000000000000000000001.key"
        key.write_text(KEY_MARKER)
        os.chmod(key, 0o600)
        (world.sessions / "evil.json").symlink_to(key)
        world.alive[12345] = True
        opened = []
        real_os_open, real_io_open = os.open, io.open

        def spy_os_open(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_os_open(path, *args, **kwargs)

        def spy_io_open(path, *args, **kwargs):
            opened.append(os.fspath(path) if not isinstance(path, int) else str(path))
            return real_io_open(path, *args, **kwargs)

        with mock.patch("os.open", spy_os_open), mock.patch("io.open", spy_io_open), mock.patch("builtins.open", spy_io_open):
            code, out, err = world.run("sessions")
        self.assertEqual(code, 0, err)
        self.assertEqual(opened, [str(world.sessions / "12345.json")])
        self.assertNotIn(KEY_MARKER, out + err)
        self.assertEqual(
            out,
            "Lifeline found 1 Claude Code session.\n"
            "1. lifeline-build, in /work/one, started at 13:00 on 19 November.\n"
            "   A process with number 12345 is running. Its kind is interactive, and its status is busy.\n"
            "   Claude Code calls it s-one.\n"
            "Lifeline could not read 1 session file.\n"
            "Which account each session is on is not known yet.\n",
        )

    def test_sessions_lists_each_session_in_start_order(self):
        world = World(self)
        self.write_session(world, 300, sessionId="s-3", cwd="/c", name="third", kind="interactive", status="idle", startedAt=int(NOW.timestamp() * 1000))
        self.write_session(world, 100, sessionId="s-1", cwd="/a", kind="background", status="waiting", startedAt=int((NOW - timedelta(days=1)).timestamp() * 1000))
        self.write_session(world, 200, name="no times")
        (world.sessions / "broken.json").write_text("{nope")
        (world.sessions / "notes.txt").write_text("ignored")
        world.alive.update({300: True, 100: False})
        code, out, err = world.run("sessions")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline found 3 Claude Code sessions.\n"
            "1. A session with no name, in /a, started at 14:00 on 18 November.\n"
            "   No process with number 100 is running. Its kind is background, and its status is waiting.\n"
            "   Claude Code calls it s-1.\n"
            "2. third, in /c, started at 14:00 on 19 November.\n"
            "   A process with number 300 is running. Its kind is interactive, and its status is idle.\n"
            "   Claude Code calls it s-3.\n"
            "3. no times, in an unknown folder, started at an unknown time.\n"
            "   Lifeline could not check for a process with number 200. Its kind is unknown, and its status is unknown.\n"
            "   Claude Code gives it no name.\n"
            "Lifeline could not read 1 session file.\n"
            "Which account each session is on is not known yet.\n",
        )

    def test_sessions_without_a_folder(self):
        world = World(self)
        code, out, err = world.run("sessions")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Lifeline found no Claude Code sessions.\n")

    def test_sessions_hides_control_characters_and_bad_pids(self):
        world = World(self)
        self.write_session(world, 7, name="red\x1b[31mtext", cwd="/x\ny", kind="a\x07b", status="ok", startedAt=True, sessionId="id")
        bad = world.sessions / "8.json"
        bad.write_text(json.dumps({"pid": -1, "name": "group", "startedAt": 10**30}))
        checked = []
        code, out, err = world.run("sessions", pid_alive=lambda pid: checked.append(pid) or True)
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("red?[31mtext, in /x?y, started at an unknown time.", out)
        self.assertIn("group, in an unknown folder, started at an unknown time.\n   It names no valid process.", out)
        self.assertEqual(checked, [7])

    def test_pid_check_never_signals_a_group(self):
        with mock.patch.object(os, "kill") as kill:
            for pid in (0, -1, True, 2**40, "12", None):
                self.assertIsNone(cli.pid_alive(pid))
            kill.assert_not_called()
        self.assertTrue(cli.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(cli.pid_alive(child.pid))

    def test_huge_or_special_session_files_are_skipped(self):
        world = World(self)
        world.sessions.mkdir(parents=True)
        (world.sessions / "big.json").write_text(json.dumps({"pid": 1, "name": "x" * (2 * 1024 * 1024)}))
        (world.sessions / "dir.json").mkdir()
        os.mkfifo(world.sessions / "pipe.json")
        code, out, err = world.run("sessions")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Lifeline found no Claude Code sessions.\nLifeline could not read 3 session files.\n")


class HelpAndLauncherTests(unittest.TestCase):
    def test_help_is_plain_sentences(self):
        for argv in ([], ["help"], ["--help"], ["-h"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
            self.assertEqual(code, 0)
            self.assertEqual(err.getvalue(), "")
            text = out.getvalue()
            self.assertIn("Run lifeline switch 2 to move Claude Code to account 2.", text)
            for line in text.splitlines():
                self.assertTrue(line.endswith(".") and line[0].isupper(), line)

    def test_production_wiring_uses_real_parts_without_touching_them(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()
        refuse = mock.Mock(side_effect=AssertionError("touched"))
        connects = []

        def local_only(sock, address):
            connects.append(address)
            if sock.family != socket.AF_UNIX:
                raise AssertionError("network")
            raise FileNotFoundError(address)

        with mock.patch.dict(os.environ, {"HOME": str(home), "USER": USER}):
            os.environ.pop("LIFELINE_SOCKET", None)
            wiring = cli.production()
            self.assertIsNone(wiring.service())
            with mock.patch.object(subprocess, "run", refuse), mock.patch.object(subprocess, "Popen", refuse), mock.patch.object(
                socket.socket, "connect", local_only
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(["status"], wiring=cli.production())
        refuse.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "Claude Code has no settings file, so no account is in use.\n" + cli.NO_ACCOUNTS + "\n")
        socket_path = str(home / "Library" / "Application Support" / "com.example.lifeline" / "lifeline.sock")
        self.assertEqual(connects, [socket_path])
        self.assertEqual(wiring.slot.service, "Claude Code-credentials")
        self.assertEqual(wiring.slot.account, USER)
        self.assertEqual(type(wiring.store).__name__, "MacKeychainStore")
        self.assertEqual(wiring.registry.path, home / "Library" / "Application Support" / "com.example.lifeline" / "accounts.json")
        self.assertEqual(wiring.lock_path, home / "Library" / "Application Support" / "com.example.lifeline" / "lifeline.lock")
        self.assertEqual(wiring.history_path, home / "Library" / "Application Support" / "com.example.lifeline" / "history.db")
        self.assertIsNone(wiring.temp_root)
        self.assertEqual((wiring.lock_timeout, wiring.session_timeout, wiring.settle), (None, None, None))
        self.assertEqual(wiring.backups, home / "Library" / "Application Support" / "com.example.lifeline" / "backups")
        self.assertIs(wiring.run_process, subprocess.run)
        self.assertEqual(wiring.profile_fn.__name__, "profile")
        self.assertEqual(list(home.iterdir()), [])

    def test_launcher_runs_through_a_link(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        home = root / "home"
        home.mkdir()
        link = root / "bin" / "lifeline"
        link.parent.mkdir()
        link.symlink_to(LAUNCHER)
        env = dict(os.environ, HOME=str(home))
        env.pop("LIFELINE_SOCKET", None)
        done = subprocess.run([str(link), "status"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(
            done.stdout,
            "Claude Code has no settings file, so no account is in use.\n"
            "Lifeline has no accounts yet. Add one with lifeline add, or add the account Claude Code uses now with lifeline adopt.\n",
        )
        self.assertEqual(list(home.iterdir()), [])
        done = subprocess.run([str(link), "jump"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stderr, "Lifeline has no command called jump. Run lifeline help to see its commands.\n")

    def test_launcher_without_its_files_fails_plainly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "bin").mkdir()
        copy_path = root / "bin" / "lifeline"
        shutil.copy2(LAUNCHER, copy_path)
        done = subprocess.run([str(copy_path), "status"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, "Lifeline could not start because some of its files are missing or damaged.\n")


class SecretTests(unittest.TestCase):
    def test_a_full_day_prints_no_secret(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL), parked=(WORK, PERSONAL))
        foreign = world.profiles.issue(PERSONAL, 3)
        world.time.hooks[1] = lambda: world.slot.item.update({"claudeAiOauth": copy.deepcopy(foreign)})
        outputs = []
        steps = (
            (ORG, ("add", "--email", "o@example.invalid")),
            (None, ("status",)),
            (None, ("switch", "4")),
            (None, ("undo",)),
            (WORK, ("resign", "2")),
            (None, ("sessions",)),
            (None, ("rename", "4", "Org", "O")),
            (None, ("status",)),
        )
        for account, argv in steps:
            world.process.account = account
            code, out, err = world.run(*argv)
            self.assertEqual(code, 0, (argv, err))
            outputs.append(out + err)
        self.assertIn("Another app switched Claude Code to Personal Account.", outputs[2])
        self.assertEqual(world.live(), world.signins[ALEX])
        world.assert_secret_free(*outputs, world.note_path.read_text(), world.registry.path.read_text())


class LoginEnvironmentTests(unittest.TestCase):
    PLANTED = {
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
        "ANTHROPIC_PROFILE": "work",
        "ANTHROPIC_CONFIG_DIR": "/elsewhere/anthropic",
        "CLAUDE_CODE_OAUTH_TOKEN": "planted-oauth-token",
        "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://example.invalid/oauth",
        "CLAUDE_CODE_FEDERATION_CACHE_DIR": "/elsewhere/federation",
        "CLAUDECODE": "1",
        "CLAUDE_CONFIG_DIR": "/elsewhere/config",
        "CLAUDE_CODE_SESSION_ID": "planted-session",
        "ANTHROPIC_API_KEY": "planted-key",
    }
    KEPT = {
        "HTTPS_PROXY": "http://proxy.invalid:8080",
        "https_proxy": "http://proxy.invalid:8080",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/extra.pem",
        "LANG": "en_GB.UTF-8",
    }

    def test_login_never_sees_session_variables_and_gets_the_keychain_user(self):
        for argv, account in ((("add",), PERSONAL), (("resign", "2"), WORK)):
            with self.subTest(argv=argv):
                world = World(self, registered=(ALEX, WORK), parked=(WORK,))
                world.process.account = account
                with mock.patch.dict(os.environ, {**self.PLANTED, **self.KEPT, "USER": "someone-else"}):
                    code, out, err = world.run(*argv)
                    path = os.environ["PATH"]
                self.assertEqual(code, 0, err)
                env = world.process.calls[0][1]["env"]
                for key in self.PLANTED:
                    if key != "CLAUDE_CONFIG_DIR":
                        self.assertNotIn(key, env)
                self.assertEqual([key for key in env if key.startswith(("CLAUDE", "ANTHROPIC"))], ["CLAUDE_CONFIG_DIR"])
                self.assertEqual(Path(env["CLAUDE_CONFIG_DIR"]).parent, world.temp_root)
                self.assertEqual(env["USER"], USER)
                self.assertEqual(env["HOME"], str(world.home))
                self.assertEqual(env["PATH"], os.pathsep.join([env["CLAUDE_CONFIG_DIR"] + "/" + server.OPEN_SHIM, path]))
                for key, value in self.KEPT.items():
                    self.assertEqual(env[key], value)
                self.assertEqual(world.keychain.temporary(), [])

    def test_login_environment_alone(self):
        with mock.patch.dict(os.environ, {**self.PLANTED, "PATH": "/usr/bin:/bin"}):
            env = cli.login_environment(Path("/x/lifeline-login-1"), "Sam")
            self.assertIsNot(env, os.environ)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/x/lifeline-login-1")
        self.assertEqual(env["USER"], "Sam")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertFalse(set(self.PLANTED) - {"CLAUDE_CONFIG_DIR"} & set(env))


class StopSignalTests(unittest.TestCase):
    def test_sigterm_during_the_wait_makes_one_blocked_try_then_rescues(self):
        world = World(self)
        world.time.hooks[2] = send_signal(signal.SIGTERM)
        real_poll = switching.Watcher.poll
        polls = []

        def poll(watcher):
            polls.append(set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
            return real_poll(watcher)

        with interrupt_net() as caught, mock.patch.object(switching.Watcher, "poll", poll):
            nets = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            code, out, err = world.run("switch", "2")
            self.assertEqual({number: signal.getsignal(number) for number in nets}, nets)
        self.assertEqual(caught, [])
        self.assertEqual(code, 143)
        self.assertTrue(out.endswith("Lifeline got a request to stop, so it stopped the watch.\n" + SAVED_WORK), out)
        self.assertNotIn("You stopped", out)
        self.assertNotIn("Control-C again", out)
        self.assertEqual(len(world.time.sleeps), 2)
        self.assertEqual(len(polls), 3)
        self.assertTrue({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= polls[-1])
        self.assertFalse({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} & polls[0])
        self.assertFalse({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} & set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
        self.assertTrue(rescued(world, WORK))

    def test_sighup_during_the_wait_runs_the_last_check_and_keeps_the_signin(self):
        world = World(self)
        stranger = world.profiles.issue(STRANGER, 1)

        def other_app_then_hangup():
            world.slot.item["claudeAiOauth"] = copy.deepcopy(stranger)
            os.kill(os.getpid(), signal.SIGHUP)

        world.time.hooks[2] = other_app_then_hangup
        with interrupt_net() as caught:
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 129)
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])
        self.assertTrue(rescued(world, WORK))
        self.assertEqual(world.live(), stranger)
        self.assertTrue(out.endswith("The terminal closed, so Lifeline stopped the watch.\n" + SAVED_WORK), out)

    def test_signal_during_the_switch_finishes_it_and_skips_the_watch(self):
        for number, sentence in (
            (signal.SIGHUP, "The terminal closed, so Lifeline will not watch this switch.\n"),
            (signal.SIGTERM, "Lifeline got a request to stop, so it will not watch this switch.\n"),
        ):
            with self.subTest(number=number):
                world = World(self)
                world.slot.write_hooks.append(lambda slot, number=number: os.kill(os.getpid(), number))
                with interrupt_net() as caught:
                    code, out, err = world.run("switch", "2")
                self.assertEqual(caught, [])
                self.assertEqual(code, 0, err)
                self.assertEqual(world.live(), world.signins[WORK])
                self.assertTrue(out.endswith(sentence), out)
                self.assertEqual(world.time.sleeps, [])

    def test_hangup_with_a_busy_lock_rescues_after_one_try(self):
        world = World(self)
        stranger = world.profiles.issue(STRANGER, 1)
        holder = contextlib.ExitStack()
        self.addCleanup(holder.close)

        def other_app_holds_the_lock_then_hangup():
            world.slot.item["claudeAiOauth"] = copy.deepcopy(stranger)
            holder.enter_context(writelock.held(world.lock_path))
            os.kill(os.getpid(), signal.SIGHUP)

        world.time.hooks[1] = other_app_holds_the_lock_then_hangup
        with interrupt_net() as caught:
            code, out, err = world.run("switch", "2", watch_lock_timeout=0.1)
        self.assertEqual(caught, [])
        self.assertEqual(code, 129)
        self.assertTrue(out.endswith("The terminal closed, so Lifeline stopped the watch.\n" + SAVED_WORK), out)
        self.assertEqual(len(world.time.sleeps), 1)
        self.assertIsNone(world.store.inner.get(WORK))
        self.assertTrue(rescued(world, WORK))

    def test_two_presses_during_one_check_stop_without_the_last_check(self):
        world = World(self)
        world.time.hooks[1] = lambda: world.slot.read_hooks.update(
            {world.slot.reads + 1: lambda slot: (send_interrupt(), send_interrupt())}
        )
        real_poll = switching.Watcher.poll
        polls = []

        def poll(watcher):
            polls.append(1)
            return real_poll(watcher)

        with interrupt_net() as caught, mock.patch.object(switching.Watcher, "poll", poll):
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 130)
        self.assertEqual(len(polls), 2)
        self.assertTrue(out.endswith("You stopped the watch.\n" + SAVED_WORK), out)
        self.assertEqual(world.live(), world.signins[WORK])

    def test_control_c_while_the_result_is_saved_keeps_the_note_and_the_result(self):
        world = World(self)
        real = cli._save_note

        def save(*args):
            send_interrupt()
            return real(*args)

        with interrupt_net() as caught, mock.patch.object(cli, "_save_note", save):
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code now uses Work Account. It was on Alex's Account.\n"
            "Anthropic confirmed that the new sign-in belongs to Work Account.\n"
            "You pressed Control-C, so Lifeline will not watch this switch.\n",
        )
        self.assertEqual(world.note()["to_uuid"], WORK)

    def test_a_second_switch_during_the_watch_is_never_claimed_to_stand(self):
        world = World(self)

        def second_switch_then_press():
            switching.switch(
                PERSONAL, slot=world.slot, store=world.store, registry=world.registry, home=world.home,
                profile_fn=world.profiles, lock_path=world.lock_path, backups=world.backups,
                lock_timeout=2.0, session_timeout=2.0, settle=0,
            )
            raise KeyboardInterrupt

        world.time.hooks[2] = second_switch_then_press
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertNotIn("stands", out)
        self.assertTrue(
            out.endswith("You stopped the watch.\nLifeline's record and Claude Code's settings both name Personal Account.\n"),
            out,
        )
        self.assertEqual(world.live(), world.signins[PERSONAL])
        self.assertEqual(world.store.inner.get(WORK), world.signins[WORK])

    def test_record_and_settings_that_disagree_are_both_named(self):
        world = World(self)

        def relabel_then_press():
            switching.write_record(world.record_path, ORG)
            raise KeyboardInterrupt

        world.time.hooks[1] = relabel_then_press
        code, out, err = world.run("switch", "2")
        self.assertTrue(
            out.endswith("You stopped the watch.\nLifeline's record names Team Account, and Claude Code's settings name Work Account.\n"),
            out,
        )


    def test_settings_relabelled_by_another_app_are_never_claimed_to_stand(self):
        world = World(self)

        def other_app_relabels_then_press():
            world.write_config(config_data(details(ORG)))
            raise KeyboardInterrupt

        world.time.hooks[1] = other_app_relabels_then_press
        code, out, err = world.run("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertNotIn("stands", out)
        self.assertEqual(switching.read_record(world.record_path), WORK)
        self.assertTrue(
            out.endswith("You stopped the watch.\nLifeline's record names Work Account, and Claude Code's settings name Team Account.\n"),
            out,
        )


class AdoptNamingTests(unittest.TestCase):
    def test_names_and_number_are_refused_for_an_account_already_added(self):
        world = World(self)
        before = (world.registry.path.read_bytes(), world.record_path.read_bytes())
        for option in (("--name", "X"), ("--short", "X"), ("--number", "3")):
            with self.subTest(option=option):
                code, out, err = world.run("adopt", *option)
                self.assertEqual(code, 1)
                self.assertEqual(
                    err,
                    "Alex's Account is already in Lifeline as number 1, so nothing was changed. "
                    "Run lifeline rename 1 to change its names.\n",
                )
        self.assertEqual((world.registry.path.read_bytes(), world.record_path.read_bytes()), before)

    def test_names_dropped_in_a_race_are_reported(self):
        world = World(self)
        with mock.patch.object(cli, "_already_added", return_value=None):
            code, out, err = world.run("adopt", "--name", "Other")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline updated Alex's Account, number 1, with the account details Claude Code holds now.\n"
            "It did not use the names or number you gave. Run lifeline rename 1 to change its names.\n",
        )
        self.assertEqual(world.registry.by_key(1).name, "Alex's Account")

    def test_adopt_without_names_skips_the_extra_check(self):
        world = World(self)
        with mock.patch.object(cli, "_already_added", side_effect=AssertionError("checked")):
            code, out, err = world.run("adopt")
        self.assertEqual(code, 0, err)


class SignInStopTests(unittest.TestCase):
    def test_control_c_during_the_sign_in_says_so(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = PERSONAL
        world.process.on_call = send_interrupt
        world.process.returncode = -2
        with interrupt_net() as caught:
            code, out, err = world.run("add")
        self.assertEqual(caught, [])
        self.assertEqual(code, 1)
        self.assertEqual(err, "You stopped Claude Code's sign-in, so nothing was added.\n")
        self.assertEqual(list(world.temp_root.iterdir()), [])
        self.assertIsNone(world.registry.by_uuid(PERSONAL))


class DevboxTests(unittest.TestCase):
    def test_switch_and_undo_are_refused_before_anything_is_read(self):
        world = World(self)
        world.process.account = WORK
        for argv in (("switch", "2"), ("switch", "2", "--no-watch"), ("undo",), ("undo", "--no-watch")):
            with self.subTest(argv=argv):
                code, out, err = world.run(*argv, changes=False)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err, DEVBOX_SENTENCE)
        self.assertEqual(world.slot.reads, 0)
        self.assertEqual(world.profiles.calls, [])
        self.assertEqual(world.process.calls, [])
        self.assertEqual(world.store.calls, [])
        self.assertEqual(world.keychain.calls, [])
        self.assertFalse(world.note_path.exists())

    def test_status_sessions_and_rename_still_work(self):
        world = World(self)
        for argv in (("status",), ("sessions",), ("rename", "2", "Command", "C")):
            with self.subTest(argv=argv):
                code, out, err = world.run(*argv, changes=False)
                self.assertEqual(code, 0, err)
        self.assertEqual(world.registry.by_key(2).name, "Command")

    def test_devbox_factory_builds_file_parts_and_writes_nothing_until_needed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()
        refuse = mock.Mock(side_effect=AssertionError("touched"))
        with mock.patch.dict(os.environ, {"HOME": str(home), "USER": USER}):
            os.environ.pop("LIFELINE_SOCKET", None)
            wiring = cli.production(system="linux")
            self.assertIsInstance(wiring.slot, cli.FileSlot)
            self.assertEqual(wiring.slot.path, home / ".claude" / ".credentials.json")
            self.assertFalse(wiring.changes)
            self.assertTrue(cli.production(system="darwin").changes)
            self.assertEqual(wiring.saved_in, enroll.SAVED_IN_FILE)
            self.assertEqual(wiring.routed, cli.ROUTED - {"add", "adopt", "resign"})
            self.assertEqual(wiring.system, "linux")
            mac = cli.production(system="darwin")
            self.assertEqual((mac.saved_in, mac.routed, mac.system), (enroll.SAVED_IN_KEYCHAIN, cli.ROUTED, "darwin"))
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(subprocess, "run", refuse), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["status"], wiring=cli.production(system="linux")), 0)
                self.assertEqual(cli.main(["sessions"], wiring=cli.production(system="linux")), 0)
                for argv in (["switch", "2"], ["undo"]):
                    self.assertEqual(cli.main(argv, wiring=cli.production(system="linux")), 1)
            refuse.assert_not_called()
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual(err.getvalue(), DEVBOX_SENTENCE * 2)
            self.assertIsNone(wiring.store.get("abc"))
            folder = home / ".config" / "com.example.lifeline" / "signins"
            self.assertTrue(folder.is_dir())
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)


def old_python_runner(launcher, *args, stdin=None):
    code = (
        "import sys; sys.version_info = (3, 9, 6, 'final', 0); path = sys.argv[1]; sys.argv = sys.argv[1:]; "
        "exec(compile(open(path).read(), path, 'exec'), {'__name__': '__main__', '__file__': path})"
    )
    return subprocess.run([sys.executable, "-I", "-c", code, str(launcher), *args], input=stdin, capture_output=True, text=True, timeout=60)


def apple_python(home):
    path = "/usr/bin/python3"
    if not os.path.exists(path):
        return None
    try:
        done = subprocess.run(
            [path, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=60, env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = done.stdout.strip()
    if done.returncode != 0 or not version or tuple(int(part) for part in version.split(".")) >= (3, 13):
        return None
    return version


class PythonVersionTests(unittest.TestCase):
    def test_launcher_says_when_python_is_too_old(self):
        done = old_python_runner(LAUNCHER, "status")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "Lifeline needs Python 3.13 or newer, but python3 here is 3.9.\n"))

    def test_launcher_under_the_system_python(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()
        version = apple_python(home)
        if version is None:
            self.skipTest("No older system python3 on this computer.")
        done = subprocess.run([str(LAUNCHER), "status"], capture_output=True, text=True, timeout=60, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        self.assertEqual(
            (done.returncode, done.stdout, done.stderr),
            (1, "", f"Lifeline needs Python 3.13 or newer, but python3 here is {version}.\n"),
        )


class RescueTests(unittest.TestCase):
    def test_rescue_runs_with_stop_signals_blocked_and_a_press_during_it_changes_nothing(self):
        world = World(self)
        world.time.hooks[2] = send_signal(signal.SIGTERM)
        real = switching.Watcher.rescue
        seen = []

        def rescue(watcher):
            seen.append(set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
            send_interrupt()
            return real(watcher)

        with interrupt_net() as caught, mock.patch.object(switching.Watcher, "rescue", rescue):
            code, out, err = world.run("switch", "2")
        self.assertEqual(caught, [])
        self.assertEqual(code, 143)
        self.assertEqual(len(seen), 1)
        self.assertTrue({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= seen[0])
        self.assertFalse({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} & set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
        self.assertTrue(out.endswith("Lifeline got a request to stop, so it stopped the watch.\n" + SAVED_WORK), out)

    def test_a_rescue_that_fails_or_raises_names_resign(self):
        for outcome in ({"return_value": False}, {"side_effect": RuntimeError("refresh-SECRET")}):
            with self.subTest(outcome=outcome):
                world = World(self)
                world.time.hooks[2] = send_signal(signal.SIGTERM)
                with interrupt_net() as caught, mock.patch.object(switching.Watcher, "rescue", **outcome) as rescue:
                    code, out, err = world.run("switch", "2")
                self.assertEqual(caught, [])
                self.assertEqual(code, 143)
                self.assertEqual(rescue.call_count, 1)
                self.assertTrue(
                    out.endswith(
                        "Lifeline got a request to stop, so it stopped the watch.\n"
                        "Lifeline could not save the sign-in for Work Account. "
                        "If Claude Code is not on Work Account, sign in to it again with lifeline resign 2.\n"
                    ),
                    out,
                )

    def test_a_first_control_c_never_rescues_and_the_end_of_the_watch_never_rescues(self):
        for hook in (None, "press"):
            with self.subTest(hook=hook):
                world = World(self)
                if hook:
                    world.time.hooks[2] = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
                with mock.patch.object(switching.Watcher, "rescue", return_value=True) as rescue:
                    code, out, err = world.run("switch", "2")
                self.assertEqual(code, 0, err)
                rescue.assert_not_called()


class NoteTests(unittest.TestCase):
    def test_a_note_from_the_switch_is_shown(self):
        world = World(self)
        real = switching.switch
        note = (
            "A copy of the sign-in for Work Account in Lifeline's recovery folder is damaged, so the sign-in Lifeline "
            "loaded may no longer work. If Claude Code asks you to sign in, sign in to Work Account again in Lifeline."
        )

        def noted(*args, **kwargs):
            result = real(*args, **kwargs)
            return switching.SwitchResult(
                result.from_uuid, result.to_uuid, result.backup, result.leftover, result.confirmed, result.signin, note=note
            )

        with mock.patch.object(switching, "switch", side_effect=noted):
            code, out, err = world.run("switch", "2", "--no-watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code now uses Work Account. It was on Alex's Account.\n"
            "Anthropic confirmed that the new sign-in belongs to Work Account.\n" + note + "\n",
        )


class ServeTests(unittest.TestCase):
    def test_serve_runs_the_service_without_building_the_command_line_parts(self):
        from lifeline import server

        with mock.patch.object(server, "main", return_value=0) as run, mock.patch.object(cli, "production", side_effect=AssertionError("built")):
            self.assertEqual(cli.main(["serve"]), 0)
        run.assert_called_once_with(remote=False)

    def test_serve_that_cannot_start_says_so_plainly(self):
        from lifeline import server

        err = io.StringIO()
        with mock.patch.object(server, "main", side_effect=RuntimeError("refresh-SECRET")), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["serve"]), 1)
        self.assertEqual(err.getvalue(), "Lifeline could not start in the background.\n")

    def test_serve_takes_no_words(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["serve", "now"]), 2)
        self.assertEqual(err.getvalue(), "Lifeline did not expect now after serve. Run lifeline help to see its commands.\n")


class FakeService:
    def __init__(self, replies):
        self.replies = {key: list(value) if isinstance(value, list) else [value] for key, value in replies.items()}
        self.calls = []

    def call(self, method, route, body=None, timeout=cli.REQUEST_SECONDS):
        if len(self.calls) >= 200:
            raise AssertionError("The command line kept asking the service.")
        self.calls.append((method, route, copy.deepcopy(body), timeout))
        queue = self.replies[(method, route)]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException) or (isinstance(reply, type) and issubclass(reply, BaseException)):
            raise reply
        return reply


DAMAGED_NOTE = "A copy of the sign-in for Work Account in Lifeline's recovery folder is damaged."


def state_payload(jobs=(), countdown=None):
    return {
        "mac": {"head": "Alex's Account lasts until it resets today at 16:00.", "sub": "At this pace it ends the window 80% used."},
        "accounts": [
            {
                "uuid": WORK, "number": 2, "name": "Work Account", "plan": "Max 5x", "five": None, "week": None,
                "five_reset": None, "week_reset": None, "reading_at": None, "needs_signin": True, "cannot_read": False,
                "paused_until": None, "in_use": ["devbox"],
            },
            {
                "uuid": ALEX, "number": 1, "name": "Alex's Account", "plan": "Max 20x", "five": 42.7, "week": 17.2,
                "five_reset": "2026-11-19T16:00:00Z", "week_reset": "2026-11-22T14:00:00Z", "reading_at": "2026-11-19T13:55:00Z",
                "needs_signin": False, "cannot_read": False, "paused_until": None, "in_use": ["mac"],
            },
            {
                "uuid": PERSONAL, "number": 3, "name": "Personal Account", "plan": "", "five": 5.0, "week": 1.0,
                "five_reset": None, "week_reset": None, "reading_at": "2026-11-19T13:00:00Z", "needs_signin": False,
                "cannot_read": True, "paused_until": "2026-11-19T15:10:00Z", "in_use": ["idle"],
            },
        ],
        "jobs": list(jobs),
        "countdown": countdown,
        "devbox": {"reachable": True, "account_uuid": WORK},
    }


def job(state, result="", uuid=None, job_id="j1", kind="add"):
    return {"id": job_id, "kind": kind, "uuid": uuid, "state": state, "result": result}


class RoutingTests(unittest.TestCase):
    def routed(self, replies, **world_options):
        world = World(self, **world_options)
        fake = FakeService(replies)
        return world, fake

    def test_a_pause_days_away_names_its_day_without_on(self):
        payload = state_payload()
        for account in payload["accounts"]:
            if account["paused_until"] is not None:
                account["paused_until"] = "2026-11-21T15:10:00Z"
        world, fake = self.routed({("GET", "/state"): (200, payload)})
        code, out, err = world.run("status", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertIn("   Lifeline could not take a reading, so readings are paused until Saturday at 15:10.\n", out)

    def test_a_pause_says_why_in_plain_words(self):
        payload = state_payload()
        for account in payload["accounts"]:
            if account["paused_until"] is not None:
                account["reading_error"] = "The usage address asked Lifeline to wait before it reads again."
        world, fake = self.routed({("GET", "/state"): (200, payload)})
        code, out, err = world.run("status", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertIn("   Anthropic asked Lifeline to wait, so readings are paused until today at 15:10.\n", out)
        self.assertNotIn("address", out)

    def test_status_goes_through_the_service(self):
        world, fake = self.routed({("GET", "/state"): (200, state_payload(jobs=[job("running")], countdown={"target_uuid": PERSONAL}))})
        code, out, err = world.run("status", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Claude Code is using Alex's Account.\n"
            "Alex's Account lasts until it resets today at 16:00.\n"
            "At this pace it ends the window 80% used.\n"
            "The other computer is using Work Account.\n"
            "1. Alex's Account is on the Max 20x plan. It is in use now.\n"
            "   At 13:55 on 19 November it had used 42% of its 5 hours and 17% of its week.\n"
            "   Its 5 hours reset today at 16:00. Its week resets on Sunday at 14:00.\n"
            "2. Work Account is on the Max 5x plan. The other computer uses it.\n"
            "   It needs a new sign-in.\n"
            "   Lifeline has no reading for it yet.\n"
            "3. Personal Account is on an unknown plan.\n"
            "   At 13:00 on 19 November it had used 5% of its 5 hours and 1% of its week.\n"
            "   Its usage cannot be read right now.\n"
            "   Lifeline could not take a reading, so readings are paused until today at 15:10.\n"
            "Lifeline is about to switch this Mac to Personal Account.\n"
            "Lifeline in the background is still adding an account.\n",
        )
        self.assertEqual(fake.calls, [("GET", "/state", None, cli.REQUEST_SECONDS)])
        self.assertEqual(world.slot.reads, 0)
        self.assertEqual(world.profiles.calls, [])

    def test_status_with_a_damaged_or_odd_state_still_speaks_plainly(self):
        world, fake = self.routed({("GET", "/state"): (200, {"accounts": "nope", "mac": [], "jobs": None})})
        code, out, err = world.run("status", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, cli.SERVICE_UNKNOWN_ACCOUNT + "\n" + cli.NO_ACCOUNTS + "\n")

    def test_switch_goes_through_the_service_and_shows_its_note(self):
        reply = (200, {"from_uuid": ALEX, "to_uuid": WORK, "confirmed": True, "message": "This Mac now uses Work Account.", "note": DAMAGED_NOTE})
        for flags, last in (((), cli.SERVICE_WATCHES), (("--no-watch",), cli.SERVICE_ALWAYS_WATCHES)):
            with self.subTest(flags=flags):
                world, fake = self.routed({("POST", "/switch"): reply})
                code, out, err = world.run("switch", "2", *flags, service=lambda: fake)
                self.assertEqual(code, 0, err)
                self.assertEqual(out, "This Mac now uses Work Account.\n" + DAMAGED_NOTE + "\n" + last + "\n")
                self.assertEqual(fake.calls, [("POST", "/switch", {"number": 2, "via": "cli"}, cli.CHANGE_SECONDS)])
                self.assertEqual((world.slot.reads, world.slot.writes, world.time.sleeps), (0, 0, []))
                self.assertFalse(world.note_path.exists())

    def test_service_refusals_are_printed_as_given(self):
        for reply, sentence in (
            ((409, {"error": "Claude Code is already using Alex's Account, so nothing was changed."}), "Claude Code is already using Alex's Account, so nothing was changed.\n"),
            ((500, {"oops": 1}), cli.SERVICE_REFUSED + "\n"),
            ((404, {"error": "Lifeline is busy\x1b[31m."}), "Lifeline is busy?[31m.\n"),
            ((409, {"error": "not a sentence"}), cli.SERVICE_REFUSED + "\n"),
            ((409, {"error": 7}), cli.SERVICE_REFUSED + "\n"),
        ):
            with self.subTest(reply=reply):
                world, fake = self.routed({("POST", "/switch"): reply})
                code, out, err = world.run("switch", "2", service=lambda: fake)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err, sentence)

    def test_undo_goes_through_the_service(self):
        world, fake = self.routed({("POST", "/undo"): (200, {"from_uuid": WORK, "to_uuid": ALEX, "confirmed": None, "message": "This Mac now uses Alex's Account. Lifeline could not check the switch with Anthropic yet."})})
        code, out, err = world.run("undo", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline undid the last switch.\n"
            "This Mac now uses Alex's Account. Lifeline could not check the switch with Anthropic yet.\n"
            + cli.SERVICE_WATCHES + "\n",
        )
        self.assertEqual(fake.calls, [("POST", "/undo", {"via": "cli"}, cli.CHANGE_SECONDS)])

    def test_add_follows_its_job_to_the_result(self):
        world, fake = self.routed({
            ("POST", "/add"): (202, {"job": "j1"}),
            ("GET", "/state"): [(200, {"jobs": [job("running")]}), (200, {"jobs": [job("done", "Lifeline added Ama's Account.", PERSONAL)]})],
        }, registered=(ALEX, WORK), parked=(WORK,))
        code, out, err = world.run("add", "--email", "ama@example.invalid", "--name", "Ama's Account", "--number", "3", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline in the background will open the sign-in page for ama@example.invalid in your browser. Sign in there, then come back here.\n"
            "Lifeline added Ama's Account.\n",
        )
        self.assertEqual(fake.calls[0], ("POST", "/add", {"email": "ama@example.invalid", "name": "Ama's Account", "number": 3}, cli.REQUEST_SECONDS))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(world.time.sleeps, [cli.JOB_POLL_SECONDS])
        self.assertEqual(world.process.calls, [])

    def test_an_add_of_an_account_already_added_says_to_add_again(self):
        already = (
            "Lifeline already has Work Account. Google Chrome signed in as 22222222@example.invalid, so nothing changed. "
            "To add a different account, sign out of claude.ai in Google Chrome or choose the other account, then press Try again."
        )
        flagged = dict(job("failed", already), already_has=True)
        world, fake = self.routed({("POST", "/add"): (202, {"job": "j1"}), ("GET", "/state"): [(200, {"jobs": [flagged]})]})
        code, out, err = world.run("add", service=lambda: fake)
        self.assertEqual(code, 1)
        self.assertEqual(err, already.replace("then press Try again.", "then run lifeline add again.") + "\n")
        self.assertEqual(err.count("again"), 1)
        plain = "Lifeline already has Work Account. Your browser signed in as 22222222@example.invalid, so nothing changed."
        world, fake = self.routed({("POST", "/add"): (202, {"job": "j1"}), ("GET", "/state"): [(200, {"jobs": [dict(job("failed", plain), already_has=True)]})]})
        code, out, err = world.run("add", service=lambda: fake)
        self.assertEqual(err, plain + "\n" + ADD_AGAIN_NOTE + "\n")
        other = "Account number 2 is already in use."
        world, fake = self.routed({("POST", "/add"): (202, {"job": "j1"}), ("GET", "/state"): [(200, {"jobs": [job("failed", other)]})]})
        code, out, err = world.run("add", service=lambda: fake)
        self.assertEqual(err, other + "\n")

    def test_a_failed_lost_or_endless_job_is_reported(self):
        cases = (
            ([(200, {"jobs": [job("failed", "Account number 2 is already in use.")]})], "Account number 2 is already in use.\n"),
            ([(200, {"jobs": []})], cli.JOB_LOST + "\n"),
            ([(200, {"jobs": [job("running")]})], cli.JOB_TOO_LONG + "\n"),
        )
        for states, sentence in cases:
            with self.subTest(sentence=sentence):
                world, fake = self.routed({("POST", "/add"): (202, {"job": "j1"}), ("GET", "/state"): states})
                with mock.patch.object(cli, "JOB_WAIT_SECONDS", 3):
                    code, out, err = world.run("add", service=lambda: fake)
                self.assertEqual(code, 1)
                self.assertEqual(err, sentence)

    def test_stopping_the_wait_leaves_the_job_to_the_service(self):
        for hook, code_expected, sentence in (
            (lambda: (_ for _ in ()).throw(KeyboardInterrupt()), 130, cli.WAIT_STOPPED),
            (send_signal(signal.SIGTERM), 143, cli.WAIT_STOPPED_SIGNAL),
        ):
            with self.subTest(code=code_expected):
                world, fake = self.routed({("POST", "/add"): (202, {"job": "j1"}), ("GET", "/state"): (200, {"jobs": [job("running")]})})
                world.time.hooks[1] = hook
                with interrupt_net() as caught:
                    code, out, err = world.run("add", service=lambda: fake)
                self.assertEqual(caught, [])
                self.assertEqual(code, code_expected)
                self.assertEqual(err, sentence + "\n")

    def test_resign_goes_through_the_service_with_its_email(self):
        world, fake = self.routed({
            ("POST", f"/resign/{WORK}"): (202, {"job": "j2"}),
            ("GET", "/state"): (200, {"jobs": [job("done", "Work Account has a new sign-in.", WORK, "j2", "resign")]}),
        })
        code, out, err = world.run("resign", "2", "--email", "hello@example.net", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline in the background will open the sign-in page for hello@example.net in your browser. Sign in there, then come back here.\n"
            "Work Account has a new sign-in.\n",
        )
        self.assertEqual(fake.calls[0], ("POST", f"/resign/{WORK}", {"email": "hello@example.net"}, cli.REQUEST_SECONDS))
        world, fake = self.routed({
            ("POST", f"/resign/{WORK}"): (202, {"job": "j2"}),
            ("GET", "/state"): (200, {"jobs": [job("done", "Work Account has a new sign-in.", WORK, "j2", "resign")]}),
        })
        code, out, err = world.run("resign", "2", service=lambda: fake)
        self.assertEqual(fake.calls[0], ("POST", f"/resign/{WORK}", {}, cli.REQUEST_SECONDS))
        self.assertTrue(out.startswith(cli.SERVICE_LOGIN + "\n"), out)

    def test_a_job_opening_line_appears_only_after_the_service_accepts(self):
        world, fake = self.routed({("POST", "/add"): (409, {"error": "Lifeline is already adding an account. Finish that sign-in first."})})
        code, out, err = world.run("add", service=lambda: fake)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Lifeline is already adding an account. Finish that sign-in first.\n")
        world, fake = self.routed({("POST", "/add"): cli._ServiceGone()}, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = PERSONAL
        code, out, err = world.run("add", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Lifeline in the background", out)
        self.assertTrue(out.startswith(ADD_HINT + "\n"), out)

    def test_a_first_stop_during_a_routed_request_says_the_service_will_finish_it(self):
        reply = (200, {"from_uuid": ALEX, "to_uuid": WORK, "confirmed": True, "message": "This Mac now uses Work Account."})
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(number=number):
                world, fake = self.routed({})

                def slow_switch(*args, **kwargs):
                    os.kill(os.getpid(), number)
                    time.sleep(0.4)
                    return reply

                fake.call = slow_switch
                with interrupt_net() as caught:
                    code, out, err = world.run("switch", "2", service=lambda: fake)
                self.assertEqual(caught, [])
                self.assertEqual(code, 0, err)
                self.assertEqual(out, cli.SERVICE_FINISHES + "\nThis Mac now uses Work Account.\n" + cli.SERVICE_WATCHES + "\n")

    def test_a_second_stop_during_a_routed_request_stops_waiting(self):
        world, fake = self.routed({})
        reply = (200, {"from_uuid": ALEX, "to_uuid": WORK, "message": "This Mac now uses Work Account."})

        def slower_switch(*args, **kwargs):
            send_interrupt()
            time.sleep(0.3)
            send_interrupt()
            time.sleep(0.5)
            return reply

        fake.call = slower_switch
        with interrupt_net() as caught:
            code, out, err = world.run("rename", "2", "Command", "C", service=lambda: fake)
        self.assertEqual(caught, [])
        self.assertEqual(code, 130)
        self.assertEqual(out, cli.SERVICE_FINISHES + "\n")
        self.assertEqual(err, cli.WAIT_STOPPED + "\n")
        self.assertEqual(world.registry.by_key(2).name, "Work Account")

    def test_adopt_through_the_service_keeps_the_naming_rules(self):
        world, fake = self.routed({("POST", "/adopt"): (202, {"job": "j3"})})
        code, out, err = world.run("adopt", "--name", "X", service=lambda: fake)
        self.assertEqual(code, 1)
        self.assertEqual(err, "Alex's Account is already in Lifeline as number 1, so nothing was changed. Run lifeline rename 1 to change its names.\n")
        self.assertEqual(fake.calls, [])
        world, fake = self.routed({
            ("POST", "/adopt"): (202, {"job": "j3"}),
            ("GET", "/state"): (200, {"jobs": [job("done", "Lifeline updated Alex's Account with the account details Claude Code holds now.", ALEX, "j3", "adopt")]}),
        })
        with mock.patch.object(cli, "_already_added", return_value=None):
            code, out, err = world.run("adopt", "--short", "J", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline updated Alex's Account with the account details Claude Code holds now.\n"
            "It did not use the names or number you gave. Run lifeline rename 1 to change its names.\n",
        )
        self.assertEqual(fake.calls[0], ("POST", "/adopt", {"short": "J"}, cli.REQUEST_SECONDS))

    def test_rename_goes_through_the_service(self):
        world, fake = self.routed({("POST", f"/rename/{WORK}"): (200, {"name": "Command", "short": "C"})})
        code, out, err = world.run("rename", "2", "Command", "C", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Account 2 is now called Command, or C for short.\n")
        self.assertEqual(fake.calls, [("POST", f"/rename/{WORK}", {"name": "Command", "short": "C"}, cli.CHANGE_SECONDS)])
        self.assertEqual(world.registry.by_key(2).name, "Work Account")

    def test_the_data_fix_for_account_1_through_the_service(self):
        world, fake = self.routed({("POST", f"/rename/{ALEX}"): (200, {"uuid": ALEX, "name": "Alex's Account", "short": "Alex"})})
        code, out, err = world.run("rename", "1", "Alex's Account", "Alex", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Account 1 is now called Alex's Account, or Alex for short.\n")
        self.assertEqual(fake.calls, [("POST", f"/rename/{ALEX}", {"name": "Alex's Account", "short": "Alex"}, cli.CHANGE_SECONDS)])

    def test_a_service_that_went_away_before_anything_was_sent_means_local(self):
        world, fake = self.routed({("POST", "/switch"): cli._ServiceGone()})
        code, out, err = world.run("switch", "2", "--no-watch", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(world.live(), world.signins[WORK])
        self.assertTrue(out.startswith("Claude Code now uses Work Account."))

    def test_status_names_devbox_signins_the_service_runs(self):
        jobs = [job("running", uuid=PERSONAL, job_id="j1", kind="devbox-add"), job("running", job_id="j2", kind="devbox-adopt")]
        world, fake = self.routed({("GET", "/state"): (200, state_payload(jobs=jobs))})
        code, out, err = world.run("status", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertIn("Lifeline in the background is still signing the other computer in to Personal Account.\n", out)
        self.assertIn("Lifeline in the background is still taking over the other computer's own sign-in.\n", out)

    def test_a_service_that_does_not_answer_is_never_bypassed_for_a_change(self):
        world, fake = self.routed({("POST", "/switch"): cli._ServiceSilent()})
        code, out, err = world.run("switch", "2", service=lambda: fake)
        self.assertEqual(code, 1)
        self.assertEqual(err, cli.SERVICE_SILENT + "\n")
        self.assertEqual(world.live(), world.signins[ALEX])
        self.assertEqual(world.slot.reads, 0)

    def test_status_falls_back_to_local_files_when_the_service_does_not_answer(self):
        world, fake = self.routed({("GET", "/state"): cli._ServiceSilent()})
        code, out, err = world.run("status", service=lambda: fake)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(cli.SERVICE_FALLBACK + "\nClaude Code is using Alex's Account.\n"), out)

    def test_sessions_serve_and_devbox_refusals_never_ask_the_service(self):
        world, fake = self.routed({})
        world.run("sessions", service=lambda: fake)
        for argv in (("switch", "2"), ("undo",)):
            code, out, err = world.run(*argv, changes=False, service=lambda: fake)
            self.assertEqual(err, DEVBOX_SENTENCE)
        self.assertEqual(fake.calls, [])

    def test_a_service_probe_that_breaks_means_local(self):
        world = World(self)

        def broken():
            raise RuntimeError("probe")

        code, out, err = world.run("switch", "2", "--no-watch", service=broken)
        self.assertEqual(code, 0, err)
        self.assertEqual(world.live(), world.signins[WORK])


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def address_string(self):
        return "lifeline"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, body))
        status, raw = self.server.reply
        if raw is None:
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _answer
    do_POST = _answer


class FakeHttpServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ServiceClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="lifeline-test-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = os.path.join(self.folder, "s.sock")

    def serve(self, status, raw):
        server = FakeHttpServer(self.path, FakeHandler)
        server.seen = []
        server.reply = (status, raw)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)

        self.addCleanup(stop)
        return server

    def test_find_needs_something_listening(self):
        self.assertIsNone(cli.ServiceClient.find(self.path))
        Path(self.path).write_text("not a socket")
        self.assertIsNone(cli.ServiceClient.find(self.path))
        os.unlink(self.path)
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.bind(self.path)
        self.addCleanup(silent.close)
        self.assertIsNone(cli.ServiceClient.find(self.path))

    def test_a_round_trip_through_a_real_socket(self):
        server = self.serve(202, b'{"job": "abc"}')
        client = cli.ServiceClient.find(self.path)
        self.assertIsNotNone(client)
        self.assertEqual(client.call("POST", "/add", {"name": "Ama"}), (202, {"job": "abc"}))
        self.assertEqual(client.call("GET", "/state"), (202, {"job": "abc"}))
        self.assertEqual(server.seen[-2], ("POST", "/add", b'{"name": "Ama"}'))
        self.assertEqual(server.seen[-1], ("GET", "/state", b""))

    def test_replies_that_are_not_json_objects_or_never_come_are_silence(self):
        for raw in (b"<html>", b"[1, 2]", None):
            with self.subTest(raw=raw):
                server = self.serve(200, raw)
                client = cli.ServiceClient(self.path)
                with self.assertRaises(cli._ServiceSilent):
                    client.call("GET", "/state")
                server.shutdown()
                server.server_close()
                os.unlink(self.path)

    def test_a_service_that_vanishes_after_a_request_is_silence_not_absence(self):
        client = cli.ServiceClient(self.path)
        with self.assertRaises(cli._ServiceGone):
            client.call("GET", "/state")
        server = self.serve(200, b"{}")
        self.assertEqual(client.call("GET", "/state"), (200, {}))
        server.shutdown()
        server.server_close()
        os.unlink(self.path)
        with self.assertRaises(cli._ServiceSilent):
            client.call("POST", "/switch", {"number": 2})


def served_world(case, **options):
    real = tempfile.mkdtemp

    def mine(suffix=None, prefix=None, dir=None):
        return real(suffix=suffix, prefix="lifeline-test-", dir=dir)

    with mock.patch.object(tempfile, "mkdtemp", mine):
        return served.World(case, **options)


class RealServiceTests(unittest.TestCase):
    def setUp(self):
        self.world = served_world(self, registered=(served.ALEX, served.WORK, served.PERSONAL))
        self.stop, _ = self.world.run(self.world.service())
        patcher = mock.patch.object(cli, "JOB_POLL_SECONDS", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        world = self.world

        def short_sleep(seconds):
            if seconds > 1:
                raise AssertionError("A local watch started beside the service.")
            time.sleep(seconds)

        wiring = cli.Wiring(
            home=world.home, user=served.USER, slot=world.slot, store=world.store, registry=world.registry,
            profile_fn=world.profiles, runner=world.keychain, run_process=mock.Mock(side_effect=AssertionError("local login")),
            lock_path=world.lock_path, backups=world.home / ".lifeline-backups", history_path=world.folder / "history.db",
            temp_root=world.temp_root, out=out, err=err, now=lambda: world.clock.now, tz=timezone.utc,
            monotonic=time.monotonic, wall=time.time, sleep=short_sleep, pid_alive=lambda pid: None,
            service=lambda: cli.ServiceClient.find(world.socket_path),
        )
        code = cli.main(list(argv), wiring=wiring)
        texts = out.getvalue() + err.getvalue()
        for secret in world.secrets():
            self.assertNotIn(secret, texts)
        self.assertNotIn("Traceback", texts)
        return code, out.getvalue(), err.getvalue()

    def test_status_switch_undo_and_rename_through_the_real_service(self):
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Claude Code is using Alex's Account.\n"), out)
        self.assertIn("1. Alex's Account is on the Max 5x plan. It is in use now.\n", out)
        code, out, err = self.run_cli("switch", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "This Mac now uses Work Account.\n" + cli.SERVICE_WATCHES + "\n")
        self.assertEqual(self.world.live_owner(), served.WORK)
        code, out, err = self.run_cli("switch", "2")
        self.assertEqual(code, 1)
        self.assertEqual(err, "Claude Code is already using Work Account, so nothing was changed.\n")
        code, out, err = self.run_cli("undo")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Lifeline undid the last switch.\nThis Mac now uses Alex's Account.\n"), out)
        self.assertEqual(self.world.live_owner(), served.ALEX)
        code, out, err = self.run_cli("rename", "2", "Command", "C")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.world.registry.by_key(2).name, "Command")

    def test_add_resign_and_adopt_follow_their_jobs_through_the_real_service(self):
        self.world.login_uuid = served.NEWCOMER
        code, out, err = self.run_cli("add", "--name", "Ama's Account", "--short", "Ama")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, cli.SERVICE_LOGIN + "\nLifeline added Ama's Account.\n")
        self.assertEqual(self.world.registry.by_uuid(served.NEWCOMER).key, 4)
        self.world.login_uuid = served.WORK
        code, out, err = self.run_cli("resign", "2", "--email", "hello@example.net")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("Work Account has a new sign-in.\n"), out)
        self.assertEqual(self.world.logins[-1][1], "hello@example.net")
        self.assertEqual(self.world.login_users[-1], served.USER)
        code, out, err = self.run_cli("adopt")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Lifeline updated Alex's Account with the account details Claude Code holds now.\n")

    def test_a_stopped_service_means_local_again(self):
        self.stop()
        self.assertIsNone(cli.ServiceClient.find(self.world.socket_path))
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "Claude Code is using Alex's Account.")
        self.assertNotIn(cli.SERVICE_FALLBACK, out)
        code, out, err = self.run_cli("rename", "3", "Personal", "P")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.world.registry.by_key(3).name, "Personal")


class KeychainAccountCliTests(unittest.TestCase):
    def test_login_user_and_live_slot_follow_the_keychain_account_rule(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = PERSONAL
        code, out, err = world.run("add", user="john doe")
        self.assertEqual(code, 0, err)
        self.assertEqual(world.process.calls[0][1]["env"]["USER"], "claude-code-user")
        self.assertIsNotNone(world.registry.by_uuid(PERSONAL))
        self.assertEqual(world.keychain.temporary(), [])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"HOME": tmp.name, "USER": "john doe"}):
            self.assertEqual(cli.production(system="darwin").slot.account, "claude-code-user")
        with mock.patch.dict(os.environ, {"HOME": tmp.name, "USER": "Sam"}):
            self.assertEqual(cli.production(system="darwin").slot.account, "Sam")

    def test_a_terminate_signal_during_the_sign_in_is_not_called_a_press(self):
        world = World(self, registered=(ALEX, WORK), parked=(WORK,))
        world.process.account = PERSONAL
        world.process.on_call = send_signal(signal.SIGTERM)
        world.process.returncode = 1
        with interrupt_net() as caught:
            code, out, err = world.run("add")
        self.assertEqual(caught, [])
        self.assertEqual(code, 1)
        self.assertEqual(err, "Claude Code's sign-in command stopped with code 1, so nothing was added.\n")


class StopWordingTests(unittest.TestCase):
    def test_a_stop_outside_the_watch_names_its_cause(self):
        for stop, code_expected, sentence in (
            (KeyboardInterrupt(), 130, cli.STOPPED),
            (cli._Stop(signal.SIGINT), 130, cli.STOPPED),
            (cli._Stop(signal.SIGTERM), 143, cli.STOPPED_SIGNAL),
            (cli._Stop(signal.SIGHUP), 129, cli.STOPPED_HANGUP),
        ):
            with self.subTest(stop=stop):
                world = World(self)
                with mock.patch.dict(cli.HANDLERS, {"rename": mock.Mock(side_effect=stop)}):
                    code, out, err = world.run("rename", "2", "A", "B")
                self.assertEqual(code, code_expected)
                self.assertEqual(err, sentence + "\n")



class FileProcess(FakeProcess):
    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        folder = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        if self.returncode == 0 and self.account is not None:
            signin = self.world.profiles.issue(self.account, 10 + len(self.issued))
            self.issued.append(signin)
            (folder / ".claude.json").write_text(json.dumps({"oauthAccount": details(self.account)}))
            (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": signin}))
        return subprocess.CompletedProcess(args, self.returncode)


LINUX = dict(changes=False, saved_in=enroll.SAVED_IN_FILE, routed=cli.ROUTED - {"add", "adopt", "resign"}, system="linux")


class LinuxSigninTests(unittest.TestCase):
    def linux_world(self, **options):
        world = World(self, **options)
        world.process = FileProcess(world)
        fake = FakeService({})
        return world, fake

    def test_resign_on_the_devbox_runs_here_with_the_file_and_never_asks_the_service(self):
        world, fake = self.linux_world()
        world.process.account = PERSONAL
        code, out, err = world.run("resign", "3", service=lambda: fake, **LINUX)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, cli.LOGIN_HINT_PASTE + "\nLifeline has a new sign-in for Personal Account.\n")
        self.assertEqual(world.store.inner.get(PERSONAL), world.process.issued[0])
        self.assertEqual(fake.calls, [])
        self.assertEqual(world.keychain.calls, [])
        self.assertEqual(list(world.temp_root.iterdir()), [])

    def test_add_and_adopt_on_the_devbox_run_here(self):
        world, fake = self.linux_world(registered=(ALEX, WORK, PERSONAL))
        world.process.account = ORG
        code, out, err = world.run("add", service=lambda: fake, **LINUX)
        self.assertEqual(code, 0, err)
        self.assertEqual(world.store.inner.get(ORG), world.process.issued[0])
        code, out, err = world.run("adopt", service=lambda: fake, **LINUX)
        self.assertEqual(code, 0, err)
        self.assertIn("Lifeline updated Alex's Account, number 1", out)
        self.assertEqual(fake.calls, [])
        self.assertEqual(world.keychain.calls, [])

    def test_the_mac_keeps_its_browser_hint_and_the_keychain(self):
        world = World(self)
        world.process.account = PERSONAL
        code, out, err = world.run("resign", "3")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(
            "Lifeline opens Claude's sign-in in Google Chrome, your default browser. Sign in there as "
            + world.registry.by_uuid(PERSONAL).name + ". If Claude asks you to choose an organization, choose the one this account uses, "
            "then press Authorize.\n"
        ), out)
        self.assertTrue(world.keychain.calls)


class FakeMacRelay:
    def __init__(self, port, forward):
        self.port = port
        self.forward = forward
        self.open = True

    def close(self):
        self.open = False


class DevboxCommandRig:
    def __init__(self, case, owner=PERSONAL, link=None, **options):
        from tests.test_devbox_signin import DevboxSigninWorld

        self.case = case
        self.mac = World(case)
        self.mac.process.error = AssertionError("the Mac never signs in for the other computer")
        self.dev = DevboxSigninWorld(case, owner=owner, **options)
        self.relays = []
        self.opened = []
        self.link = link
        self.authorize = True

    def make_link(self):
        from lifeline import devbox
        from tests.test_devbox import TEST_KEY
        return self.link if self.link is not None else devbox.HttpLink(("127.0.0.1", self.dev.world.port()), key=TEST_KEY)

    def relay(self, port, forward):
        found = FakeMacRelay(port, forward)
        self.relays.append(found)
        return found

    def open_page(self, url):
        self.opened.append((url, [(relay.port, relay.open) for relay in self.relays]))
        if self.authorize:
            self.dev.login.go.set()
        return True

    def run(self, *argv, **changes):
        options = dict(
            devbox_link=self.make_link,
            relay=self.relay,
            open_page=self.open_page,
            system="darwin",
            sleep=lambda seconds: time.sleep(0.02),
            monotonic=time.monotonic,
        )
        options.update(changes)
        return self.mac.run(*argv, **options)

    def assert_mac_untouched(self):
        self.case.assertEqual(self.mac.slot.writes, 0)
        self.case.assertEqual(self.mac.keychain.calls, [])
        self.case.assertEqual([call for call in self.mac.store.calls if call[0] != "get"], [])


class DevboxCommandTests(unittest.TestCase):
    def test_devbox_add_opens_the_page_with_the_way_back_open_and_reports_the_result(self):
        rig = DevboxCommandRig(self)
        code, out, err = rig.run("computer", "add", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "Lifeline on the other computer is starting the sign-in for Personal Account.\n"
            "Check that the page shows 33333333@example.invalid, then press Authorize.\n"
            "The other computer has its own sign-in for Personal Account now.\n",
        )
        from tests.test_devbox_signin import authorize_url

        self.assertEqual(rig.opened, [(authorize_url(40123), [(40123, True)])])
        self.assertFalse(rig.relays[0].open)
        self.assertEqual(rig.dev.login.calls[0]["email"], "33333333@example.invalid")
        self.assertEqual(rig.dev.world.store.get(PERSONAL), rig.dev.login.issued)
        rig.dev.assert_never_switched()
        rig.assert_mac_untouched()
        pushed = {account.uuid: account for account in rig.dev.world.registry.load()}
        self.assertEqual(pushed[ORG].plan, "")
        self.assertEqual(pushed[WORK].plan, "default_claude_max_5x")

    def test_devbox_add_passes_a_chosen_email(self):
        rig = DevboxCommandRig(self)
        code, out, err = rig.run("computer", "add", "3", "--email", "ama@example.invalid")
        self.assertEqual(code, 0, err)
        self.assertEqual(rig.dev.login.calls[0]["email"], "ama@example.invalid")

    def test_devbox_resign_and_adopt(self):
        rig = DevboxCommandRig(self, owner=ALEX)
        code, out, err = rig.run("computer", "resign", "1")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("The other computer has a new sign-in for Alex's Account.\n"), out)
        self.assertEqual(rig.dev.world.store.get(ALEX), rig.dev.login.issued)
        code, out, err = rig.run("computer", "adopt")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "Lifeline on the other computer took over the sign-in its Claude Code holds for Work Account.\n")
        self.assertEqual(len(rig.opened), 1)
        rig.dev.assert_never_switched()
        rig.assert_mac_untouched()

    def test_a_busy_devbox_says_how_to_stop_its_signin(self):
        rig = DevboxCommandRig(self)
        rig.authorize = False
        status, reply = rig.dev.tcp("POST", "/add", {"uuid": ORG})
        self.assertEqual(status, 202)
        code, out, err = rig.run("computer", "add", "3")
        self.assertEqual(code, 1)
        from lifeline import devbox

        self.assertEqual(err, devbox.DEVBOX_BUSY + "\n" + cli.DEVBOX_STOP_HINT + "\n")
        code, out, err = rig.run("computer", "stop")
        self.assertEqual((code, out), (0, cli.DEVBOX_STOPPED_THERE + "\n"), err)
        self.assertEqual(rig.dev.service.jobs_running(), [])
        code, out, err = rig.run("computer", "stop")
        self.assertEqual((code, out), (0, devbox.NOTHING_RUNNING + "\n"))

    def test_devbox_stop_says_how_the_signin_ended(self):
        from lifeline import devbox

        class StopLink:
            def __init__(self, answers):
                self.answers = list(answers)

            def request(self, method, route, body=None):
                return 200, {"stopping": True, "job": "j1"}

            def job(self, job_id):
                return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

        done = "The other computer has its own sign-in for Team Account now."
        cases = (
            ("done", [(200, {"state": "done", "result": done})], done),
            ("stopped", [(200, {"state": "failed", "result": "The sign-in was stopped before it finished."})], cli.DEVBOX_STOPPED_THERE),
            ("gone", [(404, {"error": "Lifeline on the other computer does not know that sign-in."})], devbox.DEVBOX_JOB_LOST),
            ("waiting", [(200, {"state": "running"})], cli.DEVBOX_STILL_STOPPING),
        )
        rig = DevboxCommandRig(self)
        for name, answers, sentence in cases:
            with self.subTest(name):
                link = StopLink(answers)
                clock = FakeTime()
                code, out, err = rig.run("computer", "stop", devbox_link=lambda: link, sleep=clock.sleep, monotonic=clock.monotonic)
                self.assertEqual((code, out, err), (0, sentence + "\n", ""))

    def test_adding_the_account_the_devbox_uses_keeps_its_sign_in_and_nothing_opens(self):
        rig = DevboxCommandRig(self)
        code, out, err = rig.run("computer", "add", "2")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("The other computer already uses Work Account, so Lifeline kept the sign-in the other computer has.\n", out)
        self.assertEqual((rig.opened, rig.relays), ([], []))

    def test_a_failed_signin_ends_with_its_reason(self):
        rig = DevboxCommandRig(self, owner=ORG)
        code, out, err = rig.run("computer", "add", "3")
        self.assertEqual(code, 1)
        self.assertEqual(err, "This sign-in belongs to a different account, so nothing was changed.\n")
        self.assertFalse(rig.relays[0].open)
        self.assertIsNone(rig.dev.world.store.get(PERSONAL))

    def test_without_a_link_or_an_answer_nothing_happens(self):
        from lifeline import devbox

        rig = DevboxCommandRig(self)
        code, out, err = rig.run("computer", "add", "3", devbox_link=lambda: None)
        self.assertEqual((code, err), (1, devbox.NO_DEVBOX_LINK + "\n"))
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        code, out, err = rig.run("computer", "add", "3", devbox_link=lambda: devbox.HttpLink(("127.0.0.1", port)))
        self.assertEqual((code, err), (1, cli.DEVBOX_SILENT + "\n"))

        def bad_address():
            raise ValueError("bad")

        code, out, err = rig.run("computer", "adopt", devbox_link=bad_address)
        self.assertEqual((code, err), (1, devbox.NO_DEVBOX_LINK + "\n"))
        self.assertEqual(rig.dev.login.calls, [])
        self.assertEqual(rig.opened, [])

    def test_an_untrusted_page_is_never_opened_and_the_signin_is_stopped(self):
        from tests.test_devbox_signin import authorize_url

        link = ScriptedLink(job={"id": "j1", "state": "running", "result": "", "port": 40123, "url": authorize_url(40123, host="evil.example")})
        rig = DevboxCommandRig(self, link=link)
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        code, out, err = rig.run("computer", "add", "3", sleep=sleep, monotonic=lambda: clock["now"])
        self.assertEqual(code, 1)
        self.assertEqual(err, cli.UNTRUSTED_PAGE + "\n")
        self.assertEqual((rig.opened, rig.relays), ([], []))
        self.assertEqual(link.stopped, ["j1"])

    def test_a_page_whose_port_disagrees_is_never_opened(self):
        from tests.test_devbox_signin import authorize_url

        link = ScriptedLink(job={"id": "j1", "state": "running", "result": "", "port": 40124, "url": authorize_url(40123)})
        rig = DevboxCommandRig(self, link=link)
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        code, out, err = rig.run("computer", "add", "3", sleep=sleep, monotonic=lambda: clock["now"])
        self.assertEqual((code, err), (1, cli.UNTRUSTED_PAGE + "\n"))
        self.assertEqual((rig.opened, rig.relays), ([], []))
        self.assertEqual(link.stopped, ["j1"])

    def test_a_taken_port_on_the_mac_stops_the_signin(self):
        from lifeline import devbox

        def taken(port, forward):
            raise devbox.RelayError("taken")

        rig = DevboxCommandRig(self)
        code, out, err = rig.run("computer", "add", "3", relay=taken)
        self.assertEqual(code, 1)
        self.assertEqual(err, "Port 40123 on this Mac is in use, so the sign-in could not come back. Run the command again.\n")
        self.assertEqual(rig.opened, [])
        self.assertTrue(rig.dev.world.wait_for(lambda: not rig.dev.service.jobs_running()))
        self.assertIsNone(rig.dev.world.store.get(PERSONAL))

    def test_a_relay_that_is_not_alive_never_opens_the_page(self):
        rig = DevboxCommandRig(self)

        def dead(port, forward):
            found = FakeMacRelay(port, forward)
            found.alive = False
            rig.relays.append(found)
            return found

        code, out, err = rig.run("computer", "add", "3", relay=dead)
        self.assertEqual(code, 1)
        self.assertEqual(err, "Port 40123 on this Mac is in use, so the sign-in could not come back. Run the command again.\n")
        self.assertEqual(rig.opened, [])
        self.assertFalse(rig.relays[0].open)
        self.assertTrue(rig.dev.world.wait_for(lambda: not rig.dev.service.jobs_running()))

    def test_stopping_the_wait_stops_the_signin_on_the_devbox(self):
        rig = DevboxCommandRig(self)
        rig.authorize = False
        count = {"sleeps": 0}

        def sleep(seconds):
            count["sleeps"] += 1
            if rig.opened and count["sleeps"] < 10_000 and not count.get("raised"):
                count["raised"] = True
                raise KeyboardInterrupt()
            time.sleep(0.02)

        code, out, err = rig.run("computer", "add", "3", sleep=sleep)
        self.assertEqual(code, 130)
        self.assertEqual(err, cli.DEVBOX_STOPPED + "\n")
        self.assertEqual(rig.dev.service.jobs_running(), [])
        self.assertFalse(rig.relays[0].open)
        self.assertTrue(rig.dev.world.wait_for(lambda: not rig.dev.service.jobs_running()))
        self.assertIsNone(rig.dev.world.store.get(PERSONAL))

    def test_a_page_that_never_comes_ends_the_wait(self):
        link = ScriptedLink(job={"id": "j1", "state": "running", "result": "", "port": None, "url": None})
        rig = DevboxCommandRig(self, link=link)
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        code, out, err = rig.run("computer", "add", "3", sleep=sleep, monotonic=lambda: clock["now"])
        self.assertEqual((code, err), (1, cli.NO_DEVBOX_PAGE + "\n"))
        self.assertEqual(link.stopped, ["j1"])

    def test_devbox_commands_run_only_on_the_mac_and_check_their_words(self):
        rig = DevboxCommandRig(self)
        code, out, err = rig.run("computer", "add", "3", system="linux")
        self.assertEqual((code, err), (1, cli.DEVBOX_ON_MAC + "\n"))
        for argv in (("computer", "add", "4"), ("computer", "resign", "4"), ("computer", "adopt")):
            rig.mac.registry.save([account for account in rig.mac.registry.load() if account.key != 4])
            code, out, err = rig.run(*argv, system="linux")
            self.assertEqual((code, err), (1, cli.DEVBOX_ON_MAC + "\n"), argv)
        for argv, sentence in (
            (("computer",), cli.DEVBOX_SHAPE),
            (("computer", "switch", "2"), cli.DEVBOX_SHAPE),
            (("computer", "add"), cli.DEVBOX_SHAPES["add"]),
            (("computer", "resign"), cli.DEVBOX_SHAPES["resign"]),
            (("computer", "add", "0"), cli.BAD_NUMBER),
            (("computer", "adopt", "--name", "X"), "The computer adopt command has no option called --name. Run lifeline help to see its options."),
            (("computer", "adopt", "2"), "Lifeline did not expect 2 after computer adopt. Run lifeline help to see its commands."),
        ):
            with self.subTest(argv=argv):
                code, out, err = rig.run(*argv)
                self.assertEqual((code, err), (2, sentence + "\n"))
        code, out, err = rig.run("computer", "add", "4", "--email", "--console")
        self.assertEqual(code, 2)
        self.assertEqual(rig.dev.login.calls, [])

    def test_the_help_names_the_devbox_commands(self):
        world = World(self)
        code, out, err = world.run("help")
        self.assertIn("Run lifeline computer add 3 on the Mac", out)
        self.assertIn("lifeline computer adopt", out)
        self.assertIn("lifeline computer resign 3", out)


class ScriptedLink:
    def __init__(self, job):
        self._job = dict(job)
        self.stopped = []
        self.pushed = []

    def request(self, method, route, body=None):
        self.pushed.append((method, route, body))
        return 200, {"accounts": len(body["accounts"])}

    def start_signin(self, route, body):
        return 202, {"job": self._job["id"]}

    def job(self, job_id):
        return 200, dict(self._job)

    def stop_signin(self, job_id):
        self.stopped.append(job_id)
        self._job["state"] = "failed"
        return 200, {"stopping": True}


class DevboxWholeWayTests(unittest.TestCase):
    def test_the_whole_way_back_from_the_mac_browser_to_the_devbox_login(self):
        from lifeline import devbox
        from tests.test_devbox import TEST_KEY, DevboxWorld
        from tests.test_devbox_signin import STATE, write_fake_login

        dev = DevboxWorld(self, parked=())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        command = write_fake_login(tmp.name)
        oauth = dev.issue(ORG)
        env = {
            "FAKE_MODE": "ok",
            "FAKE_LOG": str(Path(tmp.name) / "seen.json"),
            "FAKE_STATE": STATE,
            "FAKE_ACCESS": oauth["accessToken"],
            "FAKE_REFRESH": oauth["refreshToken"],
            "FAKE_EXPIRES": str(oauth["expiresAt"]),
            "FAKE_DETAILS": json.dumps(served.details(ORG)),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        login = devbox.DevboxLogin(command=str(command), scope=lambda: ["/usr/bin/env"], oom_path=str(Path(tmp.name) / "oom"), poll=0.05)
        service = dev.service(run_login=login, temp_root=dev.folder / "login", callback_fn=devbox.replay_callback)
        dev.run(service)
        before = dev.slot_path.read_bytes()
        mac = World(self)
        relays = []
        answers = []

        def relay(port, forward):
            found = devbox.CallbackRelay(0, forward)
            relays.append(found)
            return found

        def open_page(url):
            from urllib.parse import parse_qs, urlsplit

            state = parse_qs(urlsplit(url).query)["state"][0]
            conn = http.client.HTTPConnection("127.0.0.1", relays[-1].port, timeout=10)
            try:
                conn.request("GET", "/callback?code=the-code&state=" + state)
                response = conn.getresponse()
                response.read()
                answers.append((response.status, response.getheader("Location")))
            finally:
                conn.close()
            return True

        code, out, err = mac.run(
            "computer",
            "add",
            "4",
            devbox_link=lambda: devbox.HttpLink(("127.0.0.1", dev.port()), key=TEST_KEY),
            relay=relay,
            open_page=open_page,
            system="darwin",
            sleep=lambda seconds: time.sleep(0.05),
            monotonic=time.monotonic,
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("The other computer has its own sign-in for Team Account now.\n"), out)
        self.assertEqual(answers, [(302, "https://platform.claude.com/oauth/code/success?app=claude-code")])
        stored = dev.store.get(ORG)
        self.assertEqual((stored["accessToken"], stored["refreshToken"]), (oauth["accessToken"], oauth["refreshToken"]))
        self.assertEqual(dev.slot_path.read_bytes(), before)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", relays[0].port), timeout=2).close()
        self.assertEqual(mac.keychain.calls, [])



class OrganizationCliTests(unittest.TestCase):
    def world(self, team_registered=True, live=TEAM, record=False):
        world = World(self, live=ALEX, parked=(WORK, PERSONAL), registered=(ALEX, WORK, PERSONAL), record=False)
        if team_registered:
            world.registry.upsert(
                Account(uuid=TEAM, email="33333333@example.invalid", name="Team Account", short="Team",
                        full="Example Org Team Account", plan="Max 5x", key=4, oauth_account=details(TEAM),
                        login_uuid=PERSONAL)
            )
        if live == TEAM:
            world.store.inner.put(ALEX, world.signins[ALEX])
            world.signins[TEAM] = world.profiles.issue(TEAM, 1)
            world.slot.item["claudeAiOauth"] = world.signins[TEAM]
            world.write_config(config_data(details(TEAM)))
        if record:
            switching.record_confirmed(world.record_path, live, {"claudeAiOauth": world.signins[live]}, datetime.now(timezone.utc))
        return world

    def test_status_names_the_organization_claude_code_uses(self):
        world = self.world(record=True)
        code, out, err = world.run("status")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Claude Code is using Team Account.\n"), out)
        self.assertIn("4. Team Account is on the Max 5x plan. It is in use now.\n", out)
        self.assertIn("3. Personal Account is on the Max 5x plan.\n", out)
        switching.write_record(world.record_path, PERSONAL)
        code, out, err = world.run("status")
        self.assertTrue(out.startswith(
            "Claude Code's settings name Team Account, but Lifeline's record names Personal Account, "
            "so Lifeline cannot tell which one is in use.\n"), out)

    def test_status_never_names_the_other_organization_of_a_login(self):
        world = self.world(team_registered=False)
        code, out, err = world.run("status")
        self.assertTrue(out.startswith("Claude Code is using 33333333@example.invalid, which is not in Lifeline. Add it with lifeline adopt.\n"), out)
        self.assertNotIn("It is in use now.", out)
        data = world.config()
        data["oauthAccount"].pop("organizationUuid")
        world.write_config(data)
        code, out, err = world.run("status")
        self.assertTrue(out.startswith(
            "Claude Code's settings name 33333333@example.invalid but not its organization, "
            "so Lifeline cannot tell which of its accounts is in use.\n"), out)
        self.assertNotIn("It is in use now.", out)

    def test_a_switch_to_the_other_organization_ends_with_both_records_agreeing(self):
        world = World(self, live=ALEX, parked=(WORK, PERSONAL), registered=(ALEX, WORK, PERSONAL))
        world.registry.upsert(
            Account(uuid=TEAM, email="33333333@example.invalid", name="Team Account", short="Team",
                    full="Example Org Team Account", plan="Max 5x", key=4, oauth_account=details(TEAM),
                    login_uuid=PERSONAL)
        )
        world.signins[TEAM] = world.profiles.issue(TEAM, 1)
        world.store.inner.put(TEAM, world.signins[TEAM])

        def press():
            raise KeyboardInterrupt

        world.time.hooks[3] = press
        code, out, err = world.run("switch", "4")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("You stopped the watch. The switch to Team Account stands.\n"), out)
        self.assertEqual(world.slot.item["claudeAiOauth"], world.signins[TEAM])
        self.assertEqual(world.store.inner.get(ALEX), world.signins[ALEX])
        self.assertEqual(world.store.inner.get(PERSONAL), world.signins[PERSONAL])
        world.time.hooks[len(world.time.sleeps) + 3] = press
        code, out, err = world.run("switch", "3")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("You stopped the watch. The switch to Personal Account stands.\n"), out)
        self.assertEqual(world.slot.item["claudeAiOauth"], world.signins[PERSONAL])
        self.assertEqual(world.store.inner.get(TEAM), world.signins[TEAM])

    def test_adopt_with_names_is_not_refused_for_the_other_organization(self):
        world = self.world(team_registered=False)
        code, out, err = world.run("adopt", "--name", "Team Account", "--short", "Team")
        self.assertEqual(code, 0, err)
        added = world.registry.by_uuid(TEAM)
        self.assertEqual((added.name, added.login, added.org_uuid), ("Team Account", PERSONAL, TEAM_ORG))
        self.assertEqual(world.registry.by_uuid(PERSONAL).name, "Personal Account")


if __name__ == "__main__":
    unittest.main()
