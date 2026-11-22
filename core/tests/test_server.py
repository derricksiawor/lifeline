import contextlib
import copy
import dataclasses
import http.client
import io
import json
import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import signal
import socket
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import devbox
from lifeline import enroll as enrolling
from lifeline import forecast as forecasting
from lifeline import server
from lifeline import statusline
from lifeline import switch as switching
from lifeline.accounts import Account, Registry
from lifeline.anthropic import Profile, SignInDead, Unavailable, Usage, Window
from lifeline.history import History, Reading
from lifeline.reader import AccountStatus
from lifeline.slot import SecurityError, Slot
from lifeline.store import FileStore, StoreError

UTC = timezone.utc
ALEX = "11111111-aaaa-4aaa-8aaa-000000000001"
WORK = "22222222-bbbb-4bbb-8bbb-000000000002"
PERSONAL = "33333333-cccc-4ccc-8ccc-000000000003"
ORG = "44444444-dddd-4ddd-8ddd-000000000004"
NEWCOMER = "55555555-eeee-4eee-8eee-000000000005"
TEAM_ORG = "66666666-ffff-4fff-8fff-000000000006"
TEAM = f"{PERSONAL}-{TEAM_ORG}"
ORDER = (ALEX, WORK, PERSONAL, ORG)
NAMES = {
    ALEX: ("Alex's Account", "Alex", "Alex's Account", "default_claude_max_5x"),
    WORK: ("Work Account", "Work", "Example Team's Account", "default_claude_max_20x"),
    PERSONAL: ("Personal Account", "Personal", "Example Org Personal Account", "default_claude_max_20x"),
    ORG: ("Team Account", "Team", "Example Org Team Account", "default_claude_max_5x"),
    NEWCOMER: ("Ama's Account", "Ama", "Ama Rivera's Account", "default_claude_max_5x"),
    TEAM: ("Team Account", "Team", "Example Org Team Account", "default_claude_max_5x"),
}
PAIRS = {TEAM: (PERSONAL, TEAM_ORG)}
TAGS = {TEAM: "teamorg0"}


def pair_of(uuid):
    return PAIRS.get(uuid, (uuid, f"org-{uuid[:8]}"))
START = datetime(2026, 11, 19, 15, 41, tzinfo=UTC)
R_ALEX = datetime(2026, 11, 19, 17, 0, tzinfo=UTC)
R_WORK = datetime(2026, 11, 19, 18, 30, tzinfo=UTC)
R_PERSONAL = datetime(2026, 11, 19, 19, 10, tzinfo=UTC)
WEEK = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
FUTURE_MS = 4102444800000
USER = "tester"
MCP_SECRET = "mcp-access-SECRET-planted"
LOGIN_SECRET = "login-output-SECRET-planted"
CALM = {
    ALEX: (42.0, R_ALEX, 18.0, WEEK),
    WORK: (12.0, R_WORK, 55.0, WEEK),
    PERSONAL: (4.0, R_PERSONAL, 31.0, WEEK),
    ORG: (0.0, None, 9.0, WEEK),
}
LIMIT = {
    ALEX: (100.0, R_ALEX, 24.0, WEEK),
    WORK: (21.0, R_WORK, 56.0, WEEK),
    PERSONAL: (7.0, R_PERSONAL, 32.0, WEEK),
    ORG: (0.0, None, 9.0, WEEK),
}


def details(uuid):
    login, org = pair_of(uuid)
    return {
        "accountUuid": login,
        "emailAddress": f"{login[:8]}@example.invalid",
        "organizationUuid": org,
        "organizationName": "Example Org" if uuid == TEAM else f"{login[:8]}@example.invalid's Organization",
        "organizationRateLimitTier": NAMES[uuid][3],
        "displayName": NAMES[uuid][0],
    }


def account(uuid, key, signed_in_at=None):
    name, short, full, tier = NAMES[uuid]
    plan = {"default_claude_max_5x": "Max 5x", "default_claude_max_20x": "Max 20x"}[tier]
    login = pair_of(uuid)[0]
    return Account(
        uuid, f"{login[:8]}@example.invalid", name, short, full, plan, key, details(uuid), signed_in_at,
        login_uuid=login if login != uuid else None,
    )


def epoch(moment):
    return int(moment.timestamp())


def iso(moment):
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Clock:
    def __init__(self, now=START):
        self.now = now
        self.slept = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.now.timestamp() - self.slept

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)

    def sleep(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)
        self.slept += seconds


class FakeProfiles:
    def __init__(self, world):
        self.world = world
        self.calls = 0

    def __call__(self, token):
        self.calls += 1
        uuid = self.world.owners.get(token)
        if uuid is None:
            raise SignInDead()
        login, org = pair_of(uuid)
        return Profile(login, f"{login[:8]}@example.invalid", org, NAMES[uuid][3])


class FakeSlot(Slot):
    def __init__(self):
        self.item = None
        self.writes = 0

    def read(self):
        return copy.deepcopy(self.item)

    def write(self, item):
        self.writes += 1
        self.item = copy.deepcopy(item)


class FakeUsage:
    def __init__(self, world):
        self.world = world
        self.calls = []

    def __call__(self, token):
        uuid = self.world.owners.get(token)
        self.calls.append(uuid)
        if uuid is None:
            raise SignInDead()
        five, five_reset, week, week_reset = self.world.figures[uuid]
        return Usage(
            five_hour=None if five is None else Window(five, five_reset),
            seven_day=None if week is None else Window(week, week_reset),
            fetched_at=self.world.clock.now,
        )


class FakeRefresh:
    def __init__(self, world):
        self.world = world
        self.calls = 0

    def __call__(self, signin):
        self.calls += 1
        if self.world.owners.get(signin["claudeAiOauth"].get("refreshToken")) is None:
            raise SignInDead()
        raise AssertionError("no renewal is expected in these tests")


class FakeLink:
    def __init__(self):
        self.up = False
        self.readings = []
        self.devbox = None
        self.fetches = 0

    def reachable(self):
        return self.up

    def fetch_readings(self):
        self.fetches += 1
        return list(self.readings)

    def devbox_account(self):
        return self.devbox

    def live_ready(self):
        return True

    def can_sign_in(self):
        return True



class FakeTimer:
    def __init__(self, world, seconds, function, args=()):
        self.seconds = seconds
        self.function = function
        self.args = tuple(args or ())
        self.started = False
        self.cancelled = False
        self.daemon = False
        world.timers.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args)


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def put(self, service, value, account=USER):
        self.items[(service, account)] = json.dumps(value)

    def __call__(self, args, stdin=None):
        if args[0] == "-i":
            words = shlex.split(stdin.split("\n")[0])
            name = (words[words.index("-s") + 1], words[words.index("-a") + 1])
            self.items[name] = bytes.fromhex(words[words.index("-X") + 1]).decode("utf-8")
            return ""
        service = args[args.index("-s") + 1]
        account_name = args[args.index("-a") + 1] if "-a" in args else None
        if args[0] == "find-generic-password":
            if (service, account_name) not in self.items:
                raise SecurityError(args[0], 44, "")
            return self.items[(service, account_name)] + "\n"
        if args[0] == "delete-generic-password":
            found = [k for k in self.items if k[0] == service and (account_name is None or k[1] == account_name)]
            if not found:
                raise SecurityError(args[0], 44, "")
            del self.items[found[0]]
            return ""
        raise AssertionError("unexpected Keychain call")


class FlakyHistory:
    def __init__(self, inner):
        self.inner = inner
        self.broken = set()

    def __getattr__(self, name):
        value = getattr(self.inner, name)
        if name in self.broken:
            def fail(*args, **kwargs):
                raise RuntimeError("history broke with " + MCP_SECRET)

            return fail
        return value


class Collector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("lifeline", timeout=10)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


class World:
    def __init__(self, case, registered=ORDER, live=ALEX, figures=None):
        self.case = case
        tmp = TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        case.addCleanup(patcher.stop)
        self.folder = self.root / "lifeline"
        self.folder.mkdir(mode=0o700)
        self.lock_path = self.folder / "lifeline.lock"
        self.record_path = self.folder / "last_loaded.json"
        self.temp_root = self.root / "login"
        self.temp_root.mkdir()
        self.clock = Clock()
        self.owners = {}
        self.serial = 0
        self.figures = dict(figures or CALM)
        self.registry = Registry(self.folder / "accounts.json")
        self.registry.save([account(uuid, ORDER.index(uuid) + 1) for uuid in registered])
        self.store = FileStore(self.root / "signins")
        self.history = History(self.folder / "history.db")
        self.slot = FakeSlot()
        self.profiles = FakeProfiles(self)
        self.usage = FakeUsage(self)
        self.refresh = FakeRefresh(self)
        self.link = FakeLink()
        self.keychain = FakeKeychain()
        self.timers = []
        self.sessions = set()
        self.logins = []
        self.login_users = []
        self.login_uuid = NEWCOMER
        self.login_fails = False
        self.login_gate = None
        self.login_page = None
        self.signins = {}
        for uuid in registered:
            self.signins[uuid] = self.issue(uuid)
            if uuid != live:
                self.store.put(uuid, self.signins[uuid])
        self.slot.item = {"claudeAiOauth": self.signins[live], "mcpOAuth": {"github|1": {"accessToken": MCP_SECRET}}}
        self.write_config(live)
        switching.record_confirmed(self.record_path, live, {"claudeAiOauth": self.signins[live]}, self.clock.now)
        self.socket_dir = tempfile.mkdtemp(prefix="lifeline-test-", dir="/tmp")
        case.addCleanup(shutil.rmtree, self.socket_dir)
        self.socket_path = os.path.join(self.socket_dir, "l.sock")
        self.replies = []
        self.collector = Collector()
        self.collector.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("lifeline")
        logger.addHandler(self.collector)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        case.addCleanup(logger.removeHandler, self.collector)
        case.addCleanup(logger.setLevel, previous)

    def issue(self, uuid):
        self.serial += 1
        tag = TAGS.get(uuid, uuid[:8])
        oauth = {
            "accessToken": f"access-{tag}-{self.serial}-SECRET",
            "refreshToken": f"refresh-{tag}-{self.serial}-SECRET",
            "expiresAt": FUTURE_MS,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
        self.owners[oauth["accessToken"]] = uuid
        self.owners[oauth["refreshToken"]] = uuid
        return oauth

    def secrets(self):
        return [token for token in self.owners] + [MCP_SECRET, LOGIN_SECRET]

    def write_config(self, uuid):
        data = {"numStartups": 7, "oauthAccount": details(uuid), "projects": {"/x": {"history": []}}}
        (self.home / ".claude.json").write_text(json.dumps(data, indent=2))

    def live_owner(self):
        item = self.slot.item or {}
        oauth = item.get("claudeAiOauth") or {}
        return self.owners.get(oauth.get("refreshToken"))

    def login(self, folder, email=None, user=None, on_page=None, stop=None):
        self.logins.append((Path(folder), email))
        self.login_users.append(user)
        if on_page is not None and self.login_page is not None:
            on_page(self.login_page, "Google Chrome", True)
        if self.login_gate is not None:
            self.login_gate.wait(10)
        if self.login_fails:
            raise RuntimeError("the browser closed " + LOGIN_SECRET)
        uuid = self.login_uuid
        (Path(folder) / ".claude.json").write_text(json.dumps({"oauthAccount": details(uuid), "other": 1}))
        self.keychain.put(enrolling.hashed_service(Path(folder)), {"claudeAiOauth": self.issue(uuid)})

    def timer(self, seconds, function, args=()):
        return FakeTimer(self, seconds, function, args)

    def parts(self, **extra):
        options = dict(
            home=self.home,
            folder=self.folder,
            slot=self.slot,
            store=self.store,
            registry=self.registry,
            history=self.history,
            profile_fn=self.profiles,
            usage_fn=self.usage,
            refresh_fn=self.refresh,
            run_login=self.login,
            runner=self.keychain,
            user=USER,
            link=self.link,
            clock=self.clock,
            tz=UTC,
            monotonic=self.clock.monotonic,
            sessions_fn=lambda: set(self.sessions),
            timer_factory=self.timer,
            settle=0,
            lock_timeout=5.0,
            session_timeout=5.0,
            temp_root=self.temp_root,
            backups=self.home / ".lifeline-backups",
            open_browser=self.no_browser,
            browser_fn=lambda: "Google Chrome",
        )
        options.update(extra)
        return server.Parts(**options)

    @staticmethod
    def no_browser(url):
        raise AssertionError("A test tried to open a browser.")

    def service(self, **extra):
        made = server.Service(self.parts(**extra))
        self.case.addCleanup(made.finish_checks)
        return made

    def usage_reading(self, uuid, five, five_reset, week=10.0, week_reset=WEEK, minutes_ago=2):
        self.history.add_reading(uuid, "usage", five, five_reset, week, week_reset, self.clock.now - timedelta(minutes=minutes_ago))

    def devbox_readings(self):
        return [
            Reading(uuid, "usage", five, five_reset, week, week_reset, self.clock.now)
            for uuid, (five, five_reset, week, week_reset) in self.figures.items()
        ]

    def run(self, service):
        holder = {}
        ready = threading.Event()

        def on_ready(stop):
            holder["stop"] = stop
            ready.set()

        def target():
            holder["code"] = server.serve(lambda: service, self.socket_path, install_signals=False, ready=on_ready, ticking=False)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.case.assertTrue(ready.wait(10), "the server did not start")

        def stop():
            if "stop" in holder:
                holder["stop"]()
            thread.join(10)

        self.case.addCleanup(stop)
        return stop, holder

    def call(self, method, route, body=None):
        conn = UnixConnection(self.socket_path)
        try:
            payload = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, route, body=payload, headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8")
            status = response.status
            content_type = response.getheader("Content-Type")
        finally:
            conn.close()
        self.replies.append(text)
        self.case.assertTrue(content_type.startswith("application/json"), content_type)
        return status, json.loads(text)

    def raw(self, data):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(self.socket_path)
        chunks = []
        try:
            sock.sendall(data)
            while True:
                try:
                    chunk = sock.recv(65536)
                except (ConnectionResetError, socket.timeout):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        reply = b"".join(chunks).decode("utf-8", "replace")
        self.replies.append(reply)
        return reply

    def wait_for(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.01)
        return check()

    def wait_job(self, job_id, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status, state = self.call("GET", "/state")
            for job in state["jobs"]:
                if job["id"] == job_id and job["state"] != "running":
                    return job
            time.sleep(0.05)
        self.case.fail("the job did not finish")

    def assert_clean(self):
        texts = self.replies + self.collector.lines
        for text in texts:
            self.case.assertNotIn("Traceback", text)
            for secret in self.secrets():
                self.case.assertNotIn(secret, text)


STATED_ORG = object()


def statusline_body(session, account_uuid, reset, five=50.0, week=20.0, at=START, reset_as_text=False, org=STATED_ORG):
    five_hour = None
    if reset is not None or five is not None:
        five_hour = {"used_percentage": five, "resets_at": (iso(reset) if reset_as_text else epoch(reset)) if reset else None}
    if org is STATED_ORG:
        org = pair_of(account_uuid)[1] if account_uuid else None
    return {
        "source": "statusline",
        "session_id": session,
        "account_uuid": account_uuid,
        "org_uuid": org,
        "at": iso(at),
        "five_hour": five_hour,
        "seven_day": {"used_percentage": week, "resets_at": epoch(WEEK)},
    }


def refused_serve(factory, path):
    started = []

    def ready(stop):
        started.append(True)
        stop()

    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        code = server.serve(factory, path, install_signals=False, ready=ready, ticking=False)
    return code, errors.getvalue().strip(), started


class StateRouteTests(unittest.TestCase):
    def test_state_shape_and_appearance(self):
        world = World(self)
        service = world.service()
        service.tick()
        world.run(service)
        status, state = world.call("GET", "/state")
        self.assertEqual(status, 200)
        for key in ("version", "at", "mac", "dev", "appearance", "accounts", "notifications", "jobs", "settings", "devbox"):
            self.assertIn(key, state)
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["at"], iso(START))
        self.assertEqual(state["appearance"], "dark")
        self.assertEqual(state["mac"]["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
        self.assertEqual(state["mac"]["view"], "mac")
        self.assertEqual(state["dev"]["view"], "dev")
        self.assertEqual(state["devbox"], {"name": None, "address": None, "reachable": False, "answers": False, "account_uuid": None, "live_registered": None, "set_up": False, "live": None, "can_sign_in": None})
        self.assertEqual([a["uuid"] for a in state["accounts"]], list(ORDER))
        alex = state["accounts"][0]
        self.assertEqual(alex["number"], 1)
        self.assertEqual(alex["name"], "Alex's Account")
        self.assertEqual(alex["short"], "Alex")
        self.assertEqual((alex["full"], alex["organization_kind"]), (world.registry.by_uuid(ALEX).full, "personal"))
        self.assertEqual(alex["email"], "11111111@example.invalid")
        self.assertEqual(alex["plan"], "Max 5x")
        self.assertEqual(alex["five"], 42.0)
        self.assertEqual(alex["week"], 18.0)
        self.assertEqual(alex["five_reset"], iso(R_ALEX))
        self.assertEqual(alex["week_reset"], iso(WEEK))
        self.assertEqual(alex["reading_at"], iso(START))
        self.assertEqual(alex["in_use"], ["mac"])
        self.assertFalse(alex["needs_signin"])
        self.assertFalse(alex["cannot_read"])
        self.assertIsNone(alex["paused_until"])
        self.assertIn("forecast", alex)
        self.assertEqual(state["accounts"][3]["in_use"], ["idle"])
        self.assertEqual(state["settings"]["auto_switch"], True)
        light = world.call("GET", "/state?appearance=light")[1]
        self.assertEqual(light["appearance"], "light")
        self.assertEqual(light["mac"]["appearance"], "light")
        status, body = world.call("GET", "/state?appearance=blue")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "The appearance must be light or dark.")
        world.assert_clean()

    def test_signin_end_is_30_days_after_signing_in(self):
        world = World(self)
        world.registry.upsert(account(WORK, 2, "2026-11-01T08:30:00Z"))
        service = world.service()
        world.run(service)
        state = world.call("GET", "/state")[1]
        work = state["accounts"][1]
        self.assertEqual(work["signed_in_at"], "2026-11-01T08:30:00Z")
        self.assertEqual(work["signin_ends"], "2026-12-01T08:30:00Z")
        self.assertIsNone(state["accounts"][0]["signin_ends"])

    def test_window_that_has_reset_reads_as_unused(self):
        world = World(self)
        world.usage_reading(ORG, 100.0, START - timedelta(minutes=1), minutes_ago=30)
        service = world.service()
        world.run(service)
        org = world.call("GET", "/state")[1]["accounts"][3]
        self.assertEqual(org["five"], 0.0)
        self.assertIsNone(org["five_reset"])

    def test_hide_name_blanks_the_menu_bar_name(self):
        world = World(self)
        service = world.service()
        service.tick()
        world.run(service)
        state = world.call("GET", "/state")[1]
        self.assertTrue(state["settings"]["hide_name"])
        self.assertEqual((state["mac"]["icon_name"], state["dev"]["icon_name"]), ("", ""))
        self.assertEqual(state["mac"]["icon_pct"], "42%")
        status, body = world.call("POST", "/settings", {"hide_name": False})
        self.assertEqual((status, body["settings"]["hide_name"]), (200, False))
        self.assertEqual(world.call("GET", "/state")[1]["mac"]["icon_name"], "Alex")
        self.assertEqual(world.call("POST", "/settings", {"hide_name": True})[0], 200)
        state = world.call("GET", "/state")[1]
        self.assertEqual(state["mac"]["icon_name"], "")
        self.assertEqual(state["mac"]["icon_pct"], "42%")

    def test_hide_name_is_true_unless_the_settings_file_says_false(self):
        self.assertTrue(server.default_settings()["hide_name"])
        world = World(self)
        path = world.folder / "settings.json"
        path.write_text(json.dumps({"auto_switch": False}))
        os.chmod(path, 0o600)
        state = world.service().state()
        self.assertEqual((state["settings"]["auto_switch"], state["settings"]["hide_name"]), (False, True))
        path.write_text(json.dumps({"auto_switch": True, "hide_name": False}))
        self.assertFalse(world.service().state()["settings"]["hide_name"])
        path.write_text(json.dumps({"auto_switch": "broken"}))
        self.assertTrue(world.service().state()["settings"]["hide_name"])

    def test_a_plan_lifeline_does_not_know_is_never_shown(self):
        self.assertEqual(server.plan_text("Max 5x"), "Max 5x")
        self.assertEqual(server.plan_text("default_claude_max_20x"), "Max 20x")
        for unknown in ("default_claude_pro", "default_claude_max_40x", "Raven", "default_raven", "raven", "", None):
            with self.subTest(plan=unknown):
                self.assertEqual(server.plan_text(unknown), "")
        world = World(self)
        saved = world.registry.by_uuid(WORK)
        world.registry.upsert(dataclasses.replace(saved, plan="Raven"))
        service = world.service()
        rows = {a["uuid"]: a for a in service.state()["accounts"]}
        self.assertEqual(rows[WORK]["plan"], "")
        self.assertNotIn("Raven", json.dumps(service.state()))


class ActiveCancelTests(unittest.TestCase):
    """every cancel sentence says who cancelled."""

    def test_no_sentence_says_something_was_cancelled(self):
        import re

        from tests.test_plain_words import PACKAGE, sentences

        found = [
            f"{path.name}:{line}: {text}"
            for path in sorted(PACKAGE.glob("*.py"))
            for line, text in sentences(path.read_text())
            if re.search(r"\b(was|were|is|are|been|be)\s+cancel", text)
        ]
        self.assertEqual(found, [])
        self.assertEqual(server.STOP_CANCELLED, "Lifeline stopped, so it cancelled the sign-in. Nothing changed.")


class OneSentenceSummaryTests(unittest.TestCase):
    """the 7-day summary is one sentence that still names its window."""

    def summary(self, read, five=(), week=(), kind="five"):
        world = World(self)
        now = world.clock.now
        days = lambda back: [(now.date() - timedelta(days=b)).isoformat() for b in sorted(back, reverse=True)]
        return world.service()._seven_days(days(read), days(five), days(week), kind, now)

    def test_each_case_is_one_sentence_that_names_its_window(self):
        read = range(7)
        cases = (
            ((), (), "five", "It did not run out in the last 7 days."),
            ((5, 3), (), "five", "It ran out of its 5 hours on Saturday and on Monday."),
            ((), (6,), "five", "It ran out of its week on Friday."),
            ((5, 3), (6,), "five", "It ran out of its 5 hours on Saturday and on Monday, and of its week on Friday."),
            ((6, 5, 4, 3), (), "five", "It ran out of its 5 hours on 4 of the last 7 days."),
            ((1,), (), "week", "It ran out of its week yesterday."),
            ((), (), "week", "It did not run out in the last 7 days."),
        )
        for five, week, kind, sentence in cases:
            with self.subTest(sentence):
                said = self.summary(read, five, week, kind)
                self.assertEqual(said, sentence)
                self.assertEqual(said.count("."), 1)

    def test_a_new_account_says_when_reading_started_in_one_sentence(self):
        self.assertEqual(self.summary((0,)), "Lifeline started reading it today, so the last 7 days fill in from here.")
        self.assertEqual(self.summary((1,)), "Lifeline started reading it yesterday, so the last 7 days fill in from here.")
        self.assertEqual(self.summary(()), "Lifeline has no readings for this account from the last 7 days.")


class HistoryRouteTests(unittest.TestCase):
    def test_history_lists_readings_and_limit_days(self):
        world = World(self, figures=LIMIT)
        world.usage_reading(ALEX, 100.0, R_ALEX - timedelta(days=1), minutes_ago=60 * 24 + 30)
        service = world.service()
        service.tick()
        world.run(service)
        status, body = world.call("GET", f"/history/{ALEX}")
        self.assertEqual(status, 200)
        self.assertEqual(body["uuid"], ALEX)
        self.assertEqual(len(body["readings"]), 2)
        self.assertEqual(body["readings"][-1]["five"], 100.0)
        self.assertEqual(body["readings"][-1]["source"], "usage")
        self.assertEqual([d["date"] for d in body["daily"]], ["2026-11-18", "2026-11-19"])
        self.assertTrue(all(d["hit_limit"] for d in body["daily"]))
        status, body = world.call("GET", f"/history/{ALEX}?days=0")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "The days value must be a whole number from 1 to 366.")
        status, body = world.call("GET", "/history/not-an-account")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "That account is not in Lifeline.")
        world.assert_clean()


class SwitchRouteTests(unittest.TestCase):
    def test_switch_by_uuid_and_number_then_refusals(self):
        world = World(self)
        service = world.service()
        world.run(service)
        status, body = world.call("POST", "/switch", {"uuid": ORG})
        self.assertEqual(status, 200)
        self.assertEqual(body["to_uuid"], ORG)
        self.assertEqual(body["from_uuid"], ALEX)
        self.assertEqual(body["message"], "This Mac now uses Team Account.")
        self.assertEqual(world.live_owner(), ORG)
        self.assertEqual(world.slot.item["mcpOAuth"], {"github|1": {"accessToken": MCP_SECRET}})
        status, body = world.call("POST", "/switch", {"uuid": ORG})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "Claude Code is already using Team Account, so nothing was changed.")
        status, body = world.call("POST", "/switch", {"number": 2})
        self.assertEqual(status, 200)
        self.assertEqual(world.live_owner(), WORK)
        for payload, code, sentence in (
            ({"uuid": "nobody"}, 404, "That account is not in Lifeline."),
            ({"number": 9}, 404, "That account is not in Lifeline."),
            ({"number": "2"}, 400, server.BAD_TARGET),
            ({}, 400, server.BAD_TARGET),
            ({"uuid": ORG, "machine": "devbox"}, 400, "Lifeline switches only this Mac, so nothing was changed."),
        ):
            status, body = world.call("POST", "/switch", payload)
            self.assertEqual((status, body["error"]), (code, sentence))
        self.assertEqual(world.live_owner(), WORK)
        world.assert_clean()

    def test_undo_switches_back_and_refuses_with_nothing_to_undo(self):
        world = World(self)
        service = world.service()
        world.run(service)
        status, body = world.call("POST", "/undo")
        self.assertEqual((status, body["error"]), (409, "There is no switch to undo."))
        world.call("POST", "/switch", {"uuid": PERSONAL})
        last = server.read_last_switch(server.last_switch_path(world.folder))
        self.assertEqual((last["from_uuid"], last["to_uuid"]), (ALEX, PERSONAL))
        status, body = world.call("POST", "/undo")
        self.assertEqual(status, 200)
        self.assertEqual(body["to_uuid"], ALEX)
        self.assertEqual(world.live_owner(), ALEX)
        last = server.read_last_switch(server.last_switch_path(world.folder))
        self.assertEqual((last["from_uuid"], last["to_uuid"]), (PERSONAL, ALEX))
        world.assert_clean()

    def test_a_damaged_undo_note_says_so(self):
        world = World(self)
        world.run(world.service())
        server.last_switch_path(world.folder).write_text("{not json")
        status, body = world.call("POST", "/undo")
        self.assertEqual((status, body["error"]), (409, "Lifeline could not read its note of the last switch, so nothing was changed."))
        self.assertEqual(world.live_owner(), ALEX)

    def test_undo_reads_the_file_the_command_line_writes(self):
        world = World(self)
        service = world.service()
        world.run(service)
        world.call("POST", "/switch", {"uuid": WORK})
        note = {"from_uuid": ORG, "to_uuid": WORK, "at": START.isoformat(timespec="seconds")}
        server.last_switch_path(world.folder).write_text(json.dumps(note) + "\n")
        self.assertEqual(server.read_last_switch(server.last_switch_path(world.folder))["at"], START)
        status, body = world.call("POST", "/undo")
        self.assertEqual(status, 200)
        self.assertEqual(world.live_owner(), ORG)

    def test_last_switch_file_is_private_and_holds_no_secret(self):
        world = World(self)
        service = world.service()
        world.run(service)
        world.call("POST", "/switch", {"uuid": ORG})
        path = server.last_switch_path(world.folder)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        data = json.loads(path.read_text())
        self.assertEqual(set(data), {"from_uuid", "to_uuid", "at"})
        text = path.read_text()
        for secret in world.secrets():
            self.assertNotIn(secret, text)


class SmallRouteTests(unittest.TestCase):
    def test_a_refused_request_reads_its_small_body_before_answering(self):
        import select

        world = World(self)
        world.run(world.service())
        for route in ("/devbox/add", "/nowhere", "/state"):
            with self.subTest(route=route):
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(conn.close)
                conn.settimeout(5)
                conn.connect(str(world.socket_path))
                body = b'{"uuid": "x"}'
                conn.sendall(f"POST {route} HTTP/1.1\r\nHost: lifeline\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode())
                early, _, _ = select.select([conn], [], [], 0.5)
                self.assertEqual(early, [], "the service answered before it read the body")
                conn.sendall(body)
                reply = b""
                while b"\r\n\r\n" not in reply:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
                self.assertTrue(reply.startswith(b"HTTP/1.1 4"), reply[:40])

    def test_cancel_refuses_with_no_countdown(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call("POST", "/cancel")
        self.assertEqual((status, body["error"]), (409, "There is no switch to cancel."))

    def test_presence_route(self):
        world = World(self)
        service = world.service()
        world.run(service)
        status, body = world.call("POST", "/presence", {"idle_seconds": 12, "locked": False, "asleep": False})
        self.assertEqual((status, body), (200, {"present": True}))
        for payload in (
            {"idle_seconds": -1, "locked": False, "asleep": False},
            {"idle_seconds": "12", "locked": False, "asleep": False},
            {"idle_seconds": 12, "locked": "no", "asleep": False},
            {"idle_seconds": 12, "locked": False},
            {"idle_seconds": True, "locked": False, "asleep": False},
        ):
            status, body = world.call("POST", "/presence", payload)
            self.assertEqual(status, 400)
            self.assertEqual(
                body["error"], "Presence needs idle_seconds in seconds, and locked and asleep as true or false."
            )
        text = b'{"idle_seconds": 1e999, "locked": false, "asleep": false}'
        reply = world.raw(
            b"POST /presence HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(len(text)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + text
        )
        self.assertIn("400", reply.split("\r\n")[0])
        self.assertIn("Presence needs idle_seconds in seconds", reply)

    def test_reading_route_saves_and_refuses(self):
        world = World(self)
        world.usage_reading(ALEX, 40.0, R_ALEX)
        world.run(world.service())
        status, body = world.call("POST", "/reading", statusline_body("s1", ALEX, R_ALEX, five=44.0))
        expected = {
            "saved": True, "account_uuid": ALEX, "org_uuid": pair_of(ALEX)[1], "reading_named": True,
            "five": 44.0, "five_reset": iso(R_ALEX), "week": 20.0, "week_reset": iso(WEEK),
        }
        self.assertEqual((status, body), (200, expected))
        latest = world.history.latest(ALEX)
        self.assertEqual((latest.source, latest.five), ("statusline", 44.0))
        for payload in (
            {"source": "elsewhere"},
            dict(statusline_body("s1", ALEX, R_ALEX), five_hour="full"),
            dict(statusline_body("s1", ALEX, R_ALEX), five_hour={"used_percentage": -3, "resets_at": epoch(R_ALEX)}),
            dict(statusline_body("s1", ALEX, R_ALEX), session_id=5),
        ):
            status, body = world.call("POST", "/reading", payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "The reading is not in the status line's format.")
        world.assert_clean()

    def test_rename_route(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call("POST", f"/rename/{WORK}", {"name": "Command Account", "short": "Command"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"uuid": WORK, "name": "Command Account", "short": "Command"})
        saved = world.registry.by_uuid(WORK)
        self.assertEqual((saved.name, saved.short, saved.full), ("Command Account", "Command", "Example Team's Account"))
        self.assertEqual(saved.oauth_account, details(WORK))
        payloads = (
            {"short": "Office"},
            {"name": "Command"},
            {"name": "  ", "short": "C"},
            {"name": "Two\nlines", "short": "C"},
            {},
            {"name": 4, "short": "C"},
            {"name": "C", "short": "C", "nickname": "x"},
            {"name": "C", "short": "C", "full": "Example Team Account"},
            {"full": "Example Team Account", "short": "C"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                status, body = world.call("POST", f"/rename/{WORK}", payload)
                self.assertEqual((status, body["error"]), (400, "A rename takes a name and a short name, each plain text on one line."))
        self.assertEqual(world.registry.by_uuid(WORK).name, "Command Account")
        status, body = world.call("POST", "/rename/nobody", {"name": "X", "short": "X"})
        self.assertEqual((status, body["error"]), (404, "That account is not in Lifeline."))

    def test_an_add_or_adopt_takes_no_separate_full_name(self):
        world = World(self)
        world.run(world.service())
        for route in ("/add", "/adopt"):
            with self.subTest(route=route):
                status, body = world.call("POST", route, {"name": "Work Account", "full": "Example Team's Account"})
                self.assertEqual(status, 400)
        self.assertEqual(world.call("GET", "/state")[1]["jobs"], [])

    def test_settings_route_saves_a_private_file(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call(
            "POST", "/settings", {"auto_switch": False, "alerts": {"week": False}, "channels": {"mac": False}}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["settings"]["auto_switch"])
        self.assertFalse(body["settings"]["alerts"]["week"])
        self.assertTrue(body["settings"]["alerts"]["limit"])
        self.assertFalse(body["settings"]["channels"]["mac"])
        path = world.folder / "settings.json"
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(json.loads(path.read_text())["auto_switch"])
        again = server.Service(world.parts())
        self.assertFalse(again.state()["settings"]["auto_switch"])
        for payload, sentence in (
            ({"auto_switch": "yes"}, "Each setting must be true or false."),
            ({"alerts": {"week": 1}}, "Each setting must be true or false."),
            ({"colour": True}, "Lifeline has no setting called colour."),
            ({"alerts": {"lunch": True}}, "Lifeline has no alert called lunch."),
            ({"channels": {"pager": True}}, "Lifeline has no alert channel called pager."),
        ):
            status, body = world.call("POST", "/settings", payload)
            self.assertEqual((status, body["error"]), (400, sentence))

    def test_unknown_route_and_wrong_method(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call("GET", "/nowhere")
        self.assertEqual((status, body["error"]), (404, "Lifeline does not know that address."))
        status, body = world.call("GET", "/switch")
        self.assertEqual((status, body["error"]), (405, "That address does not answer this type of request."))
        status, body = world.call("DELETE", "/state")
        self.assertEqual(status, 405)
        status, body = world.call("POST", "/switch", None)
        self.assertEqual(status, 400)

    def test_a_refusal_before_the_body_closes_the_connection(self):
        world = World(self)
        world.run(world.service())
        body = b'{"a":1}'
        reply = world.raw(
            b"POST /nothing HTTP/1.1\r\nHost: l\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
            + b"GET /state HTTP/1.1\r\nHost: l\r\n\r\n"
        )
        self.assertEqual(reply.count("HTTP/1.1 "), 1)
        self.assertTrue(reply.startswith("HTTP/1.1 404 Not Found"))
        self.assertIn("Connection: close", reply)
        smuggled = b"GET /state HTTP/1.1\r\nHost: l\r\n\r\n"
        reply = world.raw(
            b"POST /nothing HTTP/1.1\r\nHost: l\r\nContent-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled
        )
        self.assertEqual(reply.count("HTTP/1.1 "), 1)
        self.assertNotIn('"version"', reply)

    def test_digits_must_be_plain_ascii(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call("GET", f"/history/{ALEX}?days=%D9%A3")
        self.assertEqual((status, body["error"]), (400, "The days value must be a whole number from 1 to 366."))
        reply = world.raw(
            "POST /presence HTTP/1.1\r\nHost: l\r\nContent-Length: \u0663\r\nConnection: close\r\n\r\n".encode("utf-8")
        )
        self.assertIn("400", reply.split("\r\n")[0])
        world.assert_clean()

    def test_body_over_64_kb_is_refused(self):
        world = World(self)
        world.run(world.service())
        head = (
            "POST /reading HTTP/1.1\r\nHost: lifeline\r\nContent-Type: application/json\r\n"
            "Content-Length: {}\r\nConnection: close\r\n\r\n"
        )
        reply = world.raw(head.format(server.BODY_LIMIT + 1).encode())
        self.assertIn("413", reply.split("\r\n")[0])
        self.assertIn("The request is larger than 64 KB, so Lifeline did not read it.", reply)
        body = json.dumps({"source": "statusline", "pad": "x" * (server.BODY_LIMIT - 40)})
        body = body + " " * (server.BODY_LIMIT - len(body))
        self.assertEqual(len(body), server.BODY_LIMIT)
        reply = world.raw(head.format(server.BODY_LIMIT).encode() + body.encode())
        self.assertIn("400", reply.split("\r\n")[0])
        reply = world.raw(
            b"POST /reading HTTP/1.1\r\nHost: lifeline\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\n"
        )
        self.assertIn("411", reply.split("\r\n")[0])
        reply = world.raw(head.format(5).encode() + b"{bad}")
        self.assertIn("The request body is not valid JSON.", reply)
        reply = world.raw(head.format(2).encode() + b"[]")
        self.assertIn("The request body must be a JSON object.", reply)
        reply = world.raw(b"NONSENSE\r\n\r\n")
        self.assertNotIn("<html", reply.lower())
        world.assert_clean()


class JobRouteTests(unittest.TestCase):
    def test_add_runs_as_a_job_and_lists_its_result(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.run(world.service())
        world.login_fails = True
        status, body = world.call("POST", "/add", {})
        job = world.wait_job(body["job"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"], "The browser sign-in did not finish, so nothing was added.")
        self.assertIsNone(world.registry.by_uuid(NEWCOMER))
        self.assertIn("WARNING The browser sign-in did not finish, so nothing was added.", world.collector.lines)
        world.login_fails = False
        status, body = world.call("POST", "/add", {"email": "ama@example.invalid", "name": "Ama's Account", "short": "Ama"})
        self.assertEqual(status, 202)
        job = world.wait_job(body["job"])
        self.assertEqual(job["kind"], "add")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"], "Lifeline added Ama's Account.")
        self.assertEqual(world.logins[-1][1], "ama@example.invalid")
        added = world.registry.by_uuid(NEWCOMER)
        self.assertEqual((added.name, added.short, added.key), ("Ama's Account", "Ama", 4))
        self.assertEqual(job["uuid"], NEWCOMER)
        self.assertEqual(world.store.get(NEWCOMER)["accessToken"][:6], "access")
        self.assertEqual(world.keychain.items, {})
        self.assertEqual(list(world.temp_root.iterdir()), [])
        world.login_uuid = TEAM
        status, body = world.call("POST", "/add", {})
        job = world.wait_job(body["job"])
        self.assertEqual(job["state"], "done", job["result"])
        self.assertEqual(world.registry.by_uuid(TEAM).key, 5)
        for payload in ({"number": "four"}, {"email": 3}, {"name": ""}, {"color": "red"}):
            status, body = world.call("POST", "/add", payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "Adding an account takes an email, name, short name and number, each optional.")
        world.assert_clean()

    def test_resign_runs_as_a_job(self):
        world = World(self)
        world.login_uuid = WORK
        world.run(world.service())
        old = world.store.get(WORK)
        status, body = world.call("POST", f"/resign/{WORK}")
        self.assertEqual(status, 202)
        job = world.wait_job(body["job"])
        self.assertEqual((job["kind"], job["uuid"], job["state"]), ("resign", WORK, "done"))
        self.assertEqual(job["result"], "Work Account has a new sign-in.")
        self.assertNotEqual(world.store.get(WORK), old)
        self.assertIsNotNone(world.registry.by_uuid(WORK).signed_in_at)
        status, body = world.call("POST", "/resign/nobody")
        self.assertEqual((status, body["error"]), (404, "That account is not in Lifeline."))
        world.login_uuid = ORG
        status, body = world.call("POST", f"/resign/{WORK}")
        job = world.wait_job(body["job"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"], "This sign-in belongs to a different account, so nothing was changed.")
        world.assert_clean()


class DuplicateJobTests(unittest.TestCase):
    def test_a_second_job_for_the_same_account_is_refused(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_gate = threading.Event()
        self.addCleanup(world.login_gate.set)
        world.login_uuid = WORK
        world.run(world.service())
        status, first = world.call("POST", f"/resign/{WORK}")
        self.assertEqual(status, 202)
        status, body = world.call("POST", f"/resign/{WORK}")
        self.assertEqual((status, body["error"]), (409, "Lifeline is already signing in to that account again. Finish that sign-in first."))
        status, other = world.call("POST", f"/resign/{PERSONAL}")
        self.assertEqual(status, 202)
        status, adding = world.call("POST", "/add", {})
        self.assertEqual(status, 202)
        status, body = world.call("POST", "/add", {})
        self.assertEqual((status, body["error"]), (409, "Lifeline is already adding an account. Finish that sign-in first."))
        world.login_gate.set()
        for job in (first["job"], other["job"], adding["job"]):
            world.wait_job(job)
        self.assertEqual(world.call("POST", f"/resign/{WORK}")[0], 202)


class ServeTests(unittest.TestCase):
    def test_socket_folder_and_file_are_private(self):
        world = World(self)
        world.run(world.service())
        self.assertEqual(stat.S_IMODE(os.stat(world.socket_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(world.socket_dir).st_mode), 0o700)
        self.assertTrue(stat.S_ISSOCK(os.lstat(world.socket_path).st_mode))

    def test_second_serve_refuses_while_one_runs(self):
        world = World(self)
        world.run(world.service())
        built = []
        code, said, started = refused_serve(lambda: built.append(1) or world.service(), world.socket_path)
        self.assertEqual((code, said, started, built), (1, "Lifeline is already running.", [], []))
        self.assertEqual(world.call("GET", "/state")[0], 200)

    def test_a_listener_without_the_lock_also_counts_as_running(self):
        world = World(self)
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(world.socket_path)
        other.listen(1)
        self.addCleanup(other.close)
        before = os.lstat(world.socket_path).st_ino
        code, said, started = refused_serve(lambda: world.service(), world.socket_path)
        self.assertEqual((code, said, started), (1, "Lifeline is already running.", []))
        self.assertEqual(os.lstat(world.socket_path).st_ino, before)

    def test_stale_socket_file_is_replaced(self):
        world = World(self)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(world.socket_path)
        stale.close()
        before = os.lstat(world.socket_path).st_ino
        world.run(world.service())
        self.assertEqual(world.call("GET", "/state")[0], 200)
        self.assertNotEqual(os.lstat(world.socket_path).st_ino, before)

    def test_something_else_at_the_path_is_left_alone(self):
        world = World(self)
        Path(world.socket_path).write_text("keep me")
        code, said, started = refused_serve(lambda: world.service(), world.socket_path)
        self.assertEqual(
            (code, said, started),
            (1, "Another file sits where Lifeline opens its connection for the menu, so Lifeline did not start.", []),
        )
        self.assertEqual(Path(world.socket_path).read_text(), "keep me")

    def test_open_socket_folder_is_refused(self):
        world = World(self)
        os.chmod(world.socket_dir, 0o755)
        code, said, started = refused_serve(lambda: world.service(), world.socket_path)
        self.assertEqual(
            (code, said, started),
            (1, "The folder for Lifeline's connection must belong to you and be closed to others, so Lifeline did not start.", []),
        )
        self.assertFalse(os.path.lexists(world.socket_path))

    def test_one_service_per_lifeline_folder_even_on_another_socket(self):
        world = World(self)
        world.run(world.service())
        other_dir = tempfile.mkdtemp(prefix="lifeline-test-", dir="/tmp")
        self.addCleanup(shutil.rmtree, other_dir)
        built = []
        code, said, started = refused_serve(lambda: built.append(1) or world.service(), os.path.join(other_dir, "l.sock"))
        self.assertEqual((code, said, started, built), (1, "Lifeline is already running.", [], []))
        self.assertFalse(os.path.lexists(os.path.join(other_dir, "l.sock")))

    def test_the_mac_only_reason_is_printed(self):
        world = World(self)

        def linux():
            raise server.ServiceError(server.MAC_ONLY)

        code, said, started = refused_serve(linux, world.socket_path)
        self.assertEqual((code, said), (1, "This part of Lifeline runs only on the Mac."))
        with mock.patch.object(server.sys, "platform", "linux"):
            with self.assertRaises(server.ServiceError):
                server.production_parts()

    def test_sigterm_stops_cleanly_and_removes_the_socket(self):
        world = World(self)
        service = world.service()
        seen = {}

        def ready(stop):
            seen["ready"] = True
            seen["mode"] = stat.S_IMODE(os.stat(world.socket_path).st_mode)
            threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM)).start()

        before = signal.getsignal(signal.SIGTERM)
        code = server.serve(lambda: service, world.socket_path, install_signals=True, ready=ready, ticking=False)
        self.assertEqual(code, 0)
        self.assertTrue(seen["ready"])
        self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(os.path.lexists(world.socket_path))
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_sigint_stops_cleanly_too(self):
        world = World(self)
        service = world.service()

        def ready(stop):
            threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGINT)).start()

        code = server.serve(lambda: service, world.socket_path, install_signals=True, ready=ready, ticking=False)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.lexists(world.socket_path))


class SegmentTests(unittest.TestCase):
    def test_a_segment_starts_only_at_a_usage_drop_below_earlier_usage(self):
        segments = {}
        rows = [
            Reading(ALEX, "usage", None, None, 97.0, WEEK, START),
            Reading(ALEX, "statusline", None, None, 100.0, WEEK, START + timedelta(minutes=1)),
            Reading(ALEX, "usage", None, None, 98.9, WEEK, START + timedelta(minutes=5)),
        ]
        self.assertEqual([server._segment(segments, r, r.week, r.week_reset)[1] for r in rows], [0, 0, 0])
        drop = Reading(ALEX, "usage", None, None, 4.0, WEEK, START + timedelta(minutes=10))
        self.assertEqual(server._segment(segments, drop, drop.week, drop.week_reset)[1], 1)


class AttributionTests(unittest.TestCase):
    def world(self):
        world = World(self)
        world.usage_reading(ALEX, 60.0, R_ALEX)
        world.usage_reading(WORK, 12.0, R_WORK)
        world.usage_reading(PERSONAL, 4.0, R_PERSONAL)
        world.service_obj = world.service()
        return world

    def statusline_rows(self, world, uuid):
        return [r for r in world.history.readings(uuid, START - timedelta(hours=1)) if r.source == "statusline"]

    def test_a_reading_that_matches_its_own_account(self):
        world = self.world()
        found = world.service_obj.take_reading(statusline_body("s1", ALEX, R_ALEX + timedelta(seconds=40), five=61.0))
        self.assertEqual(found, ALEX)
        self.assertEqual([r.five for r in self.statusline_rows(world, ALEX)], [61.0])
        self.assertEqual(world.service_obj.state()["sessions"]["mac"], {"open": 0, "behind": 0})
        world.sessions.add("s1")
        self.assertEqual(world.service_obj.state()["sessions"]["mac"], {"open": 1, "behind": 0})

    def test_the_match_window_is_120_seconds(self):
        world = self.world()
        service = world.service_obj
        self.assertEqual(service.take_reading(statusline_body("s1", WORK, R_ALEX + timedelta(seconds=120))), ALEX)
        self.assertIsNone(service.take_reading(statusline_body("s1", WORK, R_ALEX - timedelta(seconds=121))))
        self.assertEqual(service.take_reading(statusline_body("s1", WORK, R_WORK - timedelta(seconds=119))), WORK)
        self.assertEqual(service.take_reading(statusline_body("s1", WORK, R_ALEX, reset_as_text=True)), ALEX)

    def test_a_session_that_did_not_follow_a_switch(self):
        world = self.world()
        service = world.service_obj
        service.switch_to(ORG)
        world.usage_reading(ORG, 1.0, R_ALEX + timedelta(hours=3), minutes_ago=1)
        world.sessions.update({"old", "new"})
        found = service.take_reading(statusline_body("old", ORG, R_ALEX, five=63.0))
        self.assertEqual(found, ALEX)
        found = service.take_reading(statusline_body("new", ORG, R_ALEX + timedelta(hours=3), five=2.0))
        self.assertEqual(found, ORG)
        self.assertEqual([r.five for r in self.statusline_rows(world, ALEX)], [63.0])
        self.assertEqual([r.five for r in self.statusline_rows(world, ORG)], [2.0])
        state = service.state()
        self.assertEqual(state["sessions"]["mac"], {"open": 2, "behind": 1})
        self.assertEqual(state["mac"]["sessions"], "2 sessions open")
        service.take_reading(statusline_body("old", ORG, R_ALEX + timedelta(hours=3), five=3.0))
        self.assertEqual(service.state()["sessions"]["mac"], {"open": 2, "behind": 0})
        world.sessions.discard("old")
        self.assertEqual(service.state()["sessions"]["mac"], {"open": 1, "behind": 0})

    def test_with_no_other_match_a_post_goes_to_its_own_account_unless_its_running_window_differs(self):
        world = World(self)
        world.usage_reading(ALEX, 60.0, R_ALEX)
        world.usage_reading(PERSONAL, 4.0, R_PERSONAL)
        service = world.service()
        nowhere = R_ALEX + timedelta(hours=2)
        self.assertEqual(service.take_reading(statusline_body("s1", ORG, nowhere, five=9.0)), ORG)
        self.assertEqual([r.five for r in self.statusline_rows(world, ORG)], [9.0])
        self.assertIsNone(service.take_reading(statusline_body("s2", PERSONAL, nowhere, five=9.0)))
        self.assertEqual(self.statusline_rows(world, PERSONAL), [])
        self.assertEqual(service.take_reading(statusline_body("s0", WORK, nowhere, five=2.0)), WORK)
        world.usage_reading(WORK, 100.0, START - timedelta(minutes=10), minutes_ago=1)
        self.assertEqual(service.take_reading(statusline_body("s3", WORK, nowhere, five=2.0)), WORK)
        self.assertIsNone(service.take_reading(statusline_body("s4", None, nowhere)))
        self.assertIsNone(service.take_reading(statusline_body("s5", "unregistered-uuid", nowhere)))
        self.assertEqual(self.statusline_rows(world, ALEX), [])

    def test_a_left_behind_session_never_lands_on_the_macs_running_window(self):
        world = World(self, live=WORK)
        world.usage_reading(WORK, 12.0, R_WORK)
        world.usage_reading(ORG, 0.0, None)
        world.usage_reading(PERSONAL, 4.0, R_PERSONAL)
        service = world.service()
        self.assertEqual(service.mac_uuid(), WORK)
        self.assertIsNone(service.take_reading(statusline_body("old-alex-session", WORK, R_ALEX, five=100.0)))
        service._auto_step(world.clock.now)
        self.assertEqual(world.timers, [])
        work = service.state()["accounts"][1]
        self.assertEqual((work["five"], work["five_reset"]), (12.0, iso(R_WORK)))

    def test_a_new_window_on_a_left_behind_session_is_dropped(self):
        world = World(self, live=WORK)
        world.usage_reading(WORK, 12.0, R_WORK)
        world.usage_reading(ALEX, 100.0, R_ALEX, minutes_ago=20)
        world.clock.now = R_ALEX + timedelta(minutes=2)
        service = world.service()
        body = statusline_body("old-alex-session", WORK, R_ALEX + timedelta(hours=5), five=1.0, at=world.clock.now)
        self.assertIsNone(service.take_reading(body))
        work = service.state()["accounts"][1]
        self.assertEqual((work["five"], work["five_reset"]), (12.0, iso(R_WORK)))

    def test_without_a_five_hour_reset_the_week_reset_decides(self):
        world = self.world()
        world.usage_reading(ORG, 0.0, None, week=9.0, week_reset=WEEK + timedelta(days=2))
        service = world.service_obj
        week_only = dict(statusline_body("s1", ALEX, None), five_hour=None)
        week_only["seven_day"] = {"used_percentage": 11.0, "resets_at": epoch(WEEK + timedelta(days=2, seconds=30))}
        self.assertEqual(service.take_reading(week_only), ORG)
        latest = world.history.latest(ORG)
        self.assertEqual((latest.source, latest.week, latest.week_reset), ("statusline", 11.0, WEEK + timedelta(days=2)))
        shared = dict(week_only, seven_day={"used_percentage": 30.0, "resets_at": epoch(WEEK)})
        self.assertEqual(service.take_reading(shared), ALEX)
        self.assertIsNone(service.take_reading(dict(shared, account_uuid=ORG, org_uuid=pair_of(ORG)[1])))
        other = dict(week_only, account_uuid=ORG, seven_day={"used_percentage": 30.0, "resets_at": epoch(WEEK + timedelta(days=4))})
        self.assertIsNone(service.take_reading(other))
        nothing = dict(week_only, seven_day={"used_percentage": 30.0, "resets_at": None})
        self.assertIsNone(service.take_reading(nothing))

    def test_a_status_line_row_keeps_the_usage_readings_reset(self):
        world = World(self, figures=dict(LIMIT, **{ALEX: (100.0, R_ALEX - timedelta(milliseconds=400), 24.0, WEEK)}))
        service = world.service()
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        self.assertEqual(len(world.timers), 1)
        service.cancel()
        self.assertEqual(service.take_reading(statusline_body("s1", ALEX, R_ALEX, five=100.0)), ALEX)
        self.assertEqual(world.history.latest(ALEX).five_reset, R_ALEX - timedelta(milliseconds=400))
        world.clock.advance(seconds=15)
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        self.assertEqual(len(world.timers), 1)
        limits = [n for n in service.state()["notifications"] if n["kind"] == "limit"]
        self.assertEqual(len(limits), 1)

    def test_countdown_events_match_within_120_seconds(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        service.cancel()
        world.history.add_reading(ALEX, "usage", 100.0, R_ALEX + timedelta(seconds=90), 24.0, WEEK, world.clock.now)
        service._auto_step(world.clock.now)
        self.assertEqual(len(world.timers), 1)
        world.history.add_reading(ALEX, "usage", 100.0, R_ALEX + timedelta(seconds=300), 24.0, WEEK, world.clock.now)
        service._auto_step(world.clock.now)
        self.assertEqual(len(world.timers), 2)

    def test_a_match_for_another_account_is_filed_there(self):
        world = self.world()
        service = world.service_obj
        self.assertEqual(service.take_reading(statusline_body("s1", ALEX, R_WORK, five=13.0)), WORK)
        self.assertEqual(self.statusline_rows(world, ALEX), [])
        self.assertEqual([r.five for r in self.statusline_rows(world, WORK)], [13.0])

    def test_two_matches_keep_a_post_on_its_own_account_and_drop_a_post_that_would_move(self):
        world = self.world()
        world.usage_reading(ORG, 5.0, R_ALEX + timedelta(seconds=30))
        service = world.service_obj
        self.assertEqual(service.take_reading(statusline_body("s1", ALEX, R_ALEX, five=61.0)), ALEX)
        self.assertEqual([r.five for r in self.statusline_rows(world, ALEX)], [61.0])
        self.assertIsNone(service.take_reading(statusline_body("s2", WORK, R_ALEX, five=62.0)))
        self.assertEqual(self.statusline_rows(world, WORK), [])
        self.assertEqual(self.statusline_rows(world, ORG), [])

    def test_a_reading_with_no_figures_is_not_evidence(self):
        world = self.world()
        service = world.service_obj
        world.sessions.add("s1")
        body = dict(statusline_body("s1", WORK, None), five_hour=None, seven_day=None)
        self.assertIsNone(service.take_reading(body))
        self.assertEqual(service.state()["sessions"]["mac"], {"open": 1, "behind": 0})

    def test_the_status_line_body_and_post_work_end_to_end(self):
        world = self.world()
        world.run(world.service_obj)
        data = {
            "session_id": "live-session",
            "rate_limits": {
                "five_hour": {"used_percentage": 61, "resets_at": epoch(R_ALEX)},
                "seven_day": {"used_percentage": 21, "resets_at": epoch(WEEK)},
            },
        }
        body = statusline.build_body(data, details(WORK))
        statusline.post(world.socket_path, body, limit=5.0)
        rows = [r for r in world.history.readings(ALEX, datetime(2000, 1, 1, tzinfo=UTC)) if r.source == "statusline"]
        self.assertEqual([(r.five, r.week, r.five_reset) for r in rows], [(61.0, 21.0, R_ALEX)])

    def test_each_post_reads_only_rows_newer_than_the_last_usage_row(self):
        world = self.world()
        for minutes in range(10, 60):
            world.history.add_reading(ALEX, "statusline", 50.0, R_ALEX, 10.0, WEEK, START - timedelta(minutes=minutes))
        counted = {}
        inner = world.history

        class Counting:
            def __getattr__(self, name):
                return getattr(inner, name)

            def readings(self, uuid, since, **options):
                found = inner.readings(uuid, since, **options)
                counted.setdefault(uuid, []).append(len(found))
                return found

        service = server.Service(world.parts(history=Counting()))
        service.take_reading(statusline_body("s1", ALEX, R_ALEX, five=61.0))
        service.take_reading(statusline_body("s1", ALEX, R_ALEX, five=61.0))
        self.assertEqual(counted[ALEX][0], 51)
        self.assertLessEqual(counted[ALEX][1], 2)

    def test_a_post_for_an_account_with_no_usage_reading_stays_on_that_account(self):
        world = World(self, live=WORK)
        world.usage_reading(ORG, 0.0, None)
        world.usage_reading(PERSONAL, 4.0, R_PERSONAL)
        service = world.service()
        self.assertEqual(service.take_reading(statusline_body("s1", WORK, R_ALEX, five=10.0)), WORK)
        idle = World(self, live=WORK)
        idle.usage_reading(WORK, 0.0, None)
        idle.usage_reading(PERSONAL, 4.0, R_PERSONAL)
        service = idle.service()
        idle.usage_reading(ALEX, 90.0, R_ALEX)
        self.assertEqual(service.take_reading(statusline_body("old-alex-session", WORK, R_ALEX, five=100.0)), ALEX)
        self.assertEqual(idle.history.latest(WORK).source, "usage")
        self.assertEqual(idle.history.latest(ALEX).five, 100.0)

    def test_a_usage_row_older_than_6_hours_is_never_used(self):
        world = World(self, live=WORK)
        world.usage_reading(WORK, 12.0, R_WORK)
        service = world.service()
        self.assertIsNotNone(service._latest_usage(WORK, world.clock.now))
        world.clock.advance(hours=8)
        self.assertIsNone(service._latest_usage(WORK, world.clock.now))
        self.assertEqual(service.take_reading(statusline_body("s1", WORK, R_WORK + timedelta(hours=8), five=3.0, at=world.clock.now)), WORK)

    def test_only_usage_readings_are_matched(self):
        world = self.world()
        world.history.add_reading(ORG, "statusline", 5.0, R_WORK, 10.0, WEEK, START - timedelta(minutes=1))
        service = world.service_obj
        self.assertEqual(service.take_reading(statusline_body("s1", ALEX, R_WORK)), WORK)



def stale_signin_file(folder):
    path = folder / ".credentials.json"
    path.write_bytes(b"stale copy")
    os.chmod(path, 0o600)
    age_signin_file(path)
    return path


def age_signin_file(path):
    old = time.time() - 86400
    os.utime(path, (old, old))


def sessions_moved(path):
    return time.time() - path.stat().st_mtime < 5


NEAR = {
    ALEX: (99.0, R_ALEX, 24.0, WEEK),
    WORK: (21.0, R_WORK, 56.0, WEEK),
    PERSONAL: (7.0, R_PERSONAL, 32.0, WEEK),
    ORG: (0.0, None, 9.0, WEEK),
}


class SessionMoveServiceTests(unittest.TestCase):
    def test_every_switch_path_moves_open_sessions(self):
        world = World(self)
        path = stale_signin_file(world.home / ".claude")
        world.run(world.service())
        for body, route in (
            ({"uuid": ORG, "via": "row"}, "/switch"),
            ({"number": 2, "via": "key"}, "/switch"),
            ({"uuid": PERSONAL, "via": "footer"}, "/switch"),
            ({"uuid": ALEX, "via": "cli"}, "/switch"),
            ({"via": "menu-undo"}, "/undo"),
        ):
            with self.subTest(body=body):
                age_signin_file(path)
                status, reply = world.call("POST", route, body)
                self.assertEqual(status, 200, reply)
                self.assertTrue(sessions_moved(path))
        self.assertEqual(world.live_owner(), PERSONAL)
        self.assertEqual(path.read_bytes(), b"stale copy")

    def test_the_automatic_switch_moves_open_sessions(self):
        world = World(self, figures=LIMIT)
        path = stale_signin_file(world.home / ".claude")
        service = world.service()
        service.tick()
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ORG)
        self.assertTrue(sessions_moved(path))

    def test_an_adopt_moves_open_sessions_to_the_sign_in_it_added(self):
        world = World(self)
        world.registry.save([account(uuid, ORDER.index(uuid) + 1) for uuid in (WORK, PERSONAL, ORG)])
        path = stale_signin_file(world.home / ".claude")
        world.run(world.service())
        status, body = world.call("POST", "/adopt", {})
        job = world.wait_job(body["job"])
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(sessions_moved(path))

    def test_the_mac_service_takes_no_folder_from_the_environment(self):
        world = World(self)
        other = str(world.root / "config")
        with mock.patch.object(server.sys, "platform", "darwin"), mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": other, "CLAUDE_SECURESTORAGE_CONFIG_DIR": other}
        ):
            parts = server.production_parts()
        self.assertEqual(parts.slot.service, "Claude Code-credentials")
        self.assertIsNone(getattr(parts, "claude_folder", None))

    def test_a_service_switch_moves_the_plain_items_sessions_whatever_the_environment(self):
        world = World(self)
        other = world.root / "config"
        other.mkdir()
        elsewhere = stale_signin_file(other)
        usual = stale_signin_file(world.home / ".claude")
        service = world.service()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other), "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(other)}):
            service.switch_to(ORG)
        self.assertTrue(sessions_moved(usual))
        self.assertFalse(sessions_moved(elsewhere))



class SwitchedLineTests(unittest.TestCase):
    def idle_after_switch(self):
        world = World(self, live=ALEX)
        service = world.service()
        service.tick()
        return world, service, statusline_body("s-idle", ALEX, R_PERSONAL, five=5.0, week=31.0)

    def test_an_idle_session_gets_the_figures_of_the_account_it_names(self):
        world, service, body = self.idle_after_switch()
        reply = service.reading_reply(body)
        self.assertEqual((reply["account_uuid"], reply["five"], reply["week"]), (ALEX, 42.0, 18.0))
        self.assertIs(reply["reading_named"], False)
        self.assertEqual(world.history.latest(PERSONAL).five, 5.0)
        self.assertEqual(world.history.latest(ALEX).five, 42.0)

    def test_a_session_on_the_named_account_says_its_reading_is_that_accounts(self):
        world, service, _ = self.idle_after_switch()
        reply = service.reading_reply(statusline_body("s-live", ALEX, R_ALEX, five=43.0, week=18.0))
        self.assertEqual((reply["account_uuid"], reply["five"]), (ALEX, 43.0))
        self.assertIs(reply["reading_named"], True)

    def test_a_session_with_no_figures_yet_still_gets_the_named_accounts_figures(self):
        world, service, _ = self.idle_after_switch()
        body = statusline_body("s-new", ALEX, None, five=None, week=None)
        body["seven_day"] = None
        reply = service.reading_reply(body)
        self.assertEqual((reply["account_uuid"], reply["five"]), (ALEX, 42.0))

    def test_an_account_lifeline_does_not_have_never_vouches_for_the_sessions_numbers(self):
        world, service, _ = self.idle_after_switch()
        stranger = "99999999-eeee-4eee-8eee-000000000009"
        body = statusline_body("s-other", stranger, R_PERSONAL, five=87.0, week=31.0, org=None)
        reply = service.reading_reply(body)
        self.assertIs(reply["reading_named"], False)
        self.assertIsNone(reply["account_uuid"])
        unmatched = statusline_body("s-other", stranger, START + timedelta(hours=3, minutes=7), five=87.0, week=31.0, org=None)
        unmatched["seven_day"] = None
        reply = service.reading_reply(unmatched)
        self.assertIs(reply["saved"], False)
        self.assertIs(reply["reading_named"], False)

    def test_a_post_never_goes_to_the_other_organization_of_the_named_login(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.registry.upsert(account(TEAM, 4))
        half_hour = START + timedelta(hours=4)
        world.history.add_reading(TEAM, "usage", 3.0, half_hour, 10.0, WEEK, START - timedelta(minutes=5))
        world.history.add_reading(PERSONAL, "usage", 0.0, None, 20.0, WEEK, START - timedelta(minutes=3))
        service = world.service()
        reply = service.reading_reply(statusline_body("p", PERSONAL, half_hour, five=4.0, at=START))
        self.assertIs(reply["saved"], False)
        self.assertEqual([r.source for r in world.history.readings(TEAM, START - timedelta(hours=1))], ["usage"])
        stranger = world.registry.by_uuid(PERSONAL).login
        body = statusline_body("q", stranger, half_hour, five=4.0, at=START, org="org-not-in-lifeline")
        self.assertIs(service.reading_reply(body)["saved"], False)
        self.assertEqual([r.source for r in world.history.readings(TEAM, START - timedelta(hours=1))], ["usage"])

    def test_a_reading_is_saved_to_its_account_when_the_named_account_has_no_reset_yet(self):
        world = World(self, live=ORG)
        service = world.service()
        service.tick()
        service.reading_reply(statusline_body("s-idle", ORG, R_PERSONAL, five=5.0, week=31.0))
        self.assertEqual(world.history.latest(PERSONAL).five, 5.0)
        stranger = "99999999-eeee-4eee-8eee-000000000009"
        world.clock.advance(seconds=5)
        service.reading_reply(statusline_body("s-idle", stranger, R_PERSONAL, five=6.0, week=31.0, org=None, at=world.clock.now))
        self.assertEqual(world.history.latest(PERSONAL).five, 6.0)

    def test_the_line_pairs_the_name_with_that_accounts_own_figures(self):
        world, service, body = self.idle_after_switch()
        world.run(service)
        registry = Registry(statusline.registry_path(world.home))
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.save(world.registry.load())
        raw = json.dumps({"session_id": "s-idle", "rate_limits": {
            "five_hour": {"used_percentage": 5.0, "resets_at": epoch(R_PERSONAL)},
            "seven_day": {"used_percentage": 31.0, "resets_at": epoch(WEEK)},
        }})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            statusline.main(raw, world.home, {"LIFELINE_SOCKET": world.socket_path}, now=START)
        self.assertTrue(out.getvalue().startswith("Alex's Account, 42% of its 5 hours used"), out.getvalue())
        self.assertEqual(world.history.latest(PERSONAL).five, 5.0)




class MenuEdgeTests(unittest.TestCase):
    def linked(self, find, stop):
        world = World(self)

        class Answering(FullLink):
            def find_request(self, token):
                return find(token)

            def stop_signin(self, job_id):
                return stop(job_id)

        world.link = Answering()
        world.link.up = True
        world.link.devbox = WORK
        world.link.readings = world.devbox_readings()
        return world, world.service()

    def test_a_stop_that_fails_keeps_the_request_and_tries_again(self):
        attempts = []

        def stop(job_id):
            attempts.append(job_id)
            if len(attempts) == 1:
                raise OSError("no answer")
            if len(attempts) == 2:
                return 409, {"error": "busy"}
            return 200, {"stopping": True}

        world, service = self.linked(lambda token: (200, {"job": "e" * 32, "state": "running"}), stop)
        service._keep_orphan("f" * 32)
        for expected in ({"f" * 32}, {"f" * 32}, set()):
            service.tick()
            self.assertEqual(service._orphans, expected)
            world.clock.advance(minutes=1)
        self.assertEqual(attempts, ["e" * 32] * 3)

    def test_a_running_job_with_no_id_is_kept_for_the_next_exchange(self):
        world, service = self.linked(lambda token: (200, {"state": "running"}), lambda job_id: (200, {}))
        service._keep_orphan("f" * 32)
        service.tick()
        self.assertEqual(service._orphans, {"f" * 32})

    def test_a_kept_request_is_dropped_once_the_devbox_has_ended_it_itself(self):
        self.assertEqual(server.ORPHAN_KEEP_SECONDS, 15 * 60)
        looked = []

        def find(token):
            looked.append(token)
            raise OSError("no answer")

        world, service = self.linked(find, lambda job_id: (200, {}))
        service._keep_orphan("f" * 32)
        world.clock.advance(minutes=14)
        service.tick()
        self.assertEqual((service._orphans, looked), ({"f" * 32}, ["f" * 32]))
        world.clock.advance(minutes=2)
        service.tick()
        self.assertEqual((service._orphans, looked), (set(), ["f" * 32]))
        world.clock.advance(minutes=1)
        service._keep_orphan("a" * 32)
        self.assertEqual(service._orphans, {"a" * 32})


class MacAdoptExpectedTests(unittest.TestCase):
    def test_the_mac_adopts_only_the_account_it_was_told_to_expect(self):
        world = World(self)
        world.run(world.service())
        before = (world.registry.path.read_bytes(), world.record_path.read_bytes())
        status, body = world.call("POST", "/adopt", {"expected": WORK})
        self.assertEqual(
            (status, body), (409, {"error": "Claude Code on this Mac uses Alex's Account, not Work Account, so nothing was changed."})
        )
        self.assertEqual((world.registry.path.read_bytes(), world.record_path.read_bytes()), before)
        status, body = world.call("POST", "/adopt", {"expected": ALEX})
        self.assertEqual(status, 202, body)
        self.assertEqual(world.wait_job(body["job"])["state"], "done")
        self.assertEqual(world.call("POST", "/adopt", {"expected": 7}), (400, {"error": server.BAD_ADOPT}))
        self.assertEqual(world.call("POST", "/adopt", {"expected": "x" * 36})[0], 404)

    def test_the_job_passes_the_expected_account_to_the_adopt_itself(self):
        world = World(self)
        service = world.service()
        seen = []
        real = enrolling.adopt_live

        def spy(**options):
            seen.append(options.get("expected"))
            return real(**options)

        with mock.patch.object(server.enrolling, "adopt_live", spy):
            job = service.start_job("adopt", None, {"expected": ALEX})["job"]
            self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(seen, [ALEX])

    def test_a_change_after_the_request_fails_the_job_and_changes_nothing(self):
        world = World(self)
        service = world.service()
        real = enrolling.adopt_live

        def moved(**options):
            world.slot.item = {"claudeAiOauth": world.signins[WORK], "mcpOAuth": {"github|1": {"accessToken": MCP_SECRET}}}
            world.write_config(WORK)
            return real(**options)

        before = world.registry.path.read_bytes()
        with mock.patch.object(server.enrolling, "adopt_live", moved):
            job = service.start_job("adopt", None, {"expected": ALEX})["job"]
            self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        found = service.state()["jobs"][0]
        self.assertEqual(
            (found["state"], found["result"]),
            ("failed", "Claude Code on this Mac no longer uses Alex's Account, so nothing was changed."),
        )
        self.assertEqual(world.registry.path.read_bytes(), before)


class NearLimitTests(unittest.TestCase):
    def test_99_percent_starts_the_countdown_and_98_does_not(self):
        self.assertEqual((server.AUTO_SWITCH_AT, forecasting.LIMIT), (99.0, 100.0))
        for five, starts in ((98.0, False), (98.9, False), (99.0, True), (99.5, True)):
            with self.subTest(five=five):
                figures = dict(NEAR)
                figures[ALEX] = (five, R_ALEX, 24.0, WEEK)
                world = World(self, figures=figures)
                service = world.service()
                service.tick()
                self.assertEqual(len(world.timers), 1 if starts else 0)

    def test_the_switch_at_99_percent_goes_through_and_says_why(self):
        world = World(self, figures=NEAR)
        path = stale_signin_file(world.home / ".claude")
        service = world.service()
        service.tick()
        state = service.state()
        self.assertEqual(state["countdown"]["target_uuid"], ORG)
        self.assertEqual(state["mac"]["sub"], "Alex's Account has used 99% of its 5 hours.")
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ORG)
        self.assertTrue(sessions_moved(path))
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn("Lifeline switched this Mac to Team Account because Alex's Account reached 99% of its 5 hours.", titles)
        self.assertTrue(any("because Alex's Account reached 99% of its 5 hours (via auto)." in line for line in world.collector.lines))

    def test_the_week_names_the_week(self):
        figures = dict(NEAR)
        figures[ALEX] = (40.0, R_ALEX, 99.2, WEEK)
        world = World(self, figures=figures)
        service = world.service()
        service.tick()
        world.timers[0].fire()
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn("Lifeline switched this Mac to Team Account because Alex's Account reached 99% of its week.", titles)

    def test_a_drop_below_99_before_the_countdown_ends_stops_the_switch(self):
        world = World(self, figures=NEAR)
        service = world.service()
        service.tick()
        world.figures[ALEX] = (98.0, R_ALEX, 24.0, WEEK)
        world.clock.advance(seconds=3)
        world.usage_reading(ALEX, 98.0, R_ALEX, 24.0, WEEK, minutes_ago=0)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ALEX)

    def test_a_target_at_99_percent_or_more_is_never_chosen(self):
        figures = {
            ALEX: (99.5, R_ALEX, 24.0, WEEK),
            WORK: (99.0, R_WORK, 56.0, WEEK),
            PERSONAL: (40.0, R_PERSONAL, 99.0, WEEK),
            ORG: (99.4, None, 9.0, WEEK),
        }
        world = World(self, figures=figures)
        service = world.service()
        service.tick()
        self.assertEqual(world.timers, [])
        self.assertIsNone(service.state()["countdown"])
        figures[PERSONAL] = (40.0, R_PERSONAL, 98.0, WEEK)
        other = World(self, figures=figures)
        service = other.service()
        service.tick()
        self.assertEqual(service.state()["countdown"]["target_uuid"], PERSONAL)

    def test_when_every_other_account_is_at_99_or_more_nothing_switches_and_the_menu_names_the_next_back(self):
        cases = (
            (100.0, R_ALEX, "Alex's Account is out until today at 17:00.", "No other account has room, and this one is back first."),
            (100.0, R_ALEX + timedelta(hours=3), "Alex's Account is out until today at 20:00.", "No other account has room. Work Account is back today at 18:30."),
            (99.5, R_ALEX, "Alex's Account lasts until its 5 hours reset today at 17:00.", "No other account has room, and this one resets first."),
            (99.0, R_ALEX + timedelta(hours=3), "Alex's Account lasts until its 5 hours reset today at 20:00.", "No other account has room. Work Account is back today at 18:30."),
        )
        for five, reset, head, sub in cases:
            with self.subTest(five=five, reset=reset):
                figures = {
                    ALEX: (five, reset, 24.0, WEEK),
                    WORK: (100.0, R_WORK, 56.0, WEEK),
                    PERSONAL: (40.0, R_PERSONAL, 99.0, WEEK),
                    ORG: (10.0, None, 100.0, WEEK),
                }
                world = World(self, figures=figures)
                service = world.service()
                service.tick()
                self.assertEqual(world.timers, [])
                state = service.state()
                self.assertIsNone(state["countdown"])
                self.assertEqual((state["mac"]["head"], state["mac"]["sub"]), (head, sub))
                self.assertNotEqual(state["mac"]["action_kind"], "primary")
                self.assertFalse(any(row["is_best"] for row in state["mac"]["rows"]))
        self.assertEqual(world.live_owner(), ALEX)

    def test_the_settings_row_text_comes_from_the_core(self):
        world = World(self)
        text = world.service().state()["settings_text"]["auto_switch"]
        self.assertEqual(text["title"], "Switch this Mac for me before an account runs out")
        self.assertEqual(
            text["description"],
            "At 99%, this Mac moves to the account with the most room after 5 seconds, unless you cancel. "
            "Claude Code sessions follow.",
        )

    def fire_after_the_target_fills(self, others):
        figures = dict(NEAR)
        figures.update(others)
        world = World(self, figures=figures)
        service = world.service()
        service.tick()
        self.assertEqual(service.state()["countdown"]["target_uuid"], ORG)
        world.clock.advance(seconds=3)
        world.usage_reading(ORG, 99.7, R_ALEX, 9.0, WEEK, minutes_ago=0)
        world.timers[0].fire()
        return world, service

    def test_a_target_that_fills_during_the_countdown_gives_way_to_the_next_best(self):
        world, service = self.fire_after_the_target_fills({WORK: (99.5, R_WORK, 56.0, WEEK), PERSONAL: (50.0, R_PERSONAL, 32.0, WEEK)})
        self.assertEqual(world.live_owner(), PERSONAL)

    def test_a_countdown_with_no_room_left_switches_nothing_and_says_why(self):
        world, service = self.fire_after_the_target_fills({WORK: (99.5, R_WORK, 56.0, WEEK), PERSONAL: (40.0, R_PERSONAL, 99.0, WEEK)})
        self.assertEqual(world.live_owner(), ALEX)
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn(server.AUTO_NO_ROOM, titles)
        self.assertEqual(server.AUTO_NO_ROOM, "Lifeline did not switch this Mac, because no other account has room now.")
        self.assertIsNone(service.state()["countdown"])

    def test_a_reading_at_99_wakes_the_loop_only_while_auto_switch_is_on(self):
        for auto, five, wakes in ((True, 99.0, True), (True, 98.0, False), (False, 99.0, False), (False, 100.0, True)):
            with self.subTest(auto=auto, five=five):
                world = World(self)
                service = world.service()
                service.update_settings({"auto_switch": auto})
                service._wake.clear()
                world.clock.advance(seconds=2)
                service.take_reading(statusline_body("s1", ALEX, R_ALEX, five=five, at=world.clock.now))
                self.assertEqual(service._wake.is_set(), wakes)


class AutoSwitchTests(unittest.TestCase):
    def test_auto_switch_never_targets_an_account_lifeline_cannot_read(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        snap = service._snapshot(world.clock.now)
        self.assertEqual(service._best_target(snap), ORG)
        rows = tuple(
            dataclasses.replace(r, status=dataclasses.replace(r.status, cannot_read=True)) if r.account.uuid == ORG else r for r in snap.rows
        )
        self.assertEqual(service._best_target(dataclasses.replace(snap, rows=rows)), PERSONAL)

    def test_the_alerts_hear_which_accounts_cannot_be_read(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        snap = service._snapshot(world.clock.now)
        rows = tuple(
            dataclasses.replace(r, status=dataclasses.replace(r.status, cannot_read=True)) if r.account.uuid == ORG else r for r in snap.rows
        )
        states = service._alert_states(dataclasses.replace(snap, rows=rows))
        self.assertEqual({s.uuid: s.cannot_read for s in states}, {ALEX: False, WORK: False, PERSONAL: False, ORG: True})
        self.assertEqual(server.alerting.room(states, ALEX, now=world.clock.now, tz=UTC), "Personal Account has the most room.")

    def test_a_row_that_needs_a_sign_in_says_so(self):
        world = World(self)
        service = world.service()
        service.tick()
        parked = world.store.get(WORK)
        world.owners.pop(parked["accessToken"])
        world.owners.pop(parked["refreshToken"])
        world.clock.advance(minutes=6)
        service.tick()
        self.assertTrue({a["uuid"]: a for a in service.state()["accounts"]}[WORK]["needs_signin"])
        rows = {r["uuid"]: r for r in service.state()["mac"]["rows"]}
        self.assertEqual(rows[WORK]["where"], "Needs a new sign-in. Click it to sign in.")

    def test_the_switch_target_ranks_by_the_fuller_window_not_by_what_is_shown(self):
        figures = dict(LIMIT)
        figures[WORK] = (12.0, R_WORK, 55.0, WEEK)
        figures[PERSONAL] = (30.0, R_PERSONAL, 20.0, WEEK)
        figures[ORG] = (60.0, R_PERSONAL, 70.0, WEEK)
        world = World(self, figures=figures)
        service = world.service()
        service.tick()
        state = service.state()
        rows = {r["uuid"]: r for r in state["mac"]["rows"]}
        self.assertEqual((rows[WORK]["figure"], rows[PERSONAL]["figure"]), ("12%", "30%"))
        self.assertEqual(state["countdown"]["target_uuid"], PERSONAL)
        self.assertEqual([uuid for uuid, row in rows.items() if row["is_best"]], [PERSONAL])

    def test_an_account_in_use_on_the_devbox_can_be_the_switch_target(self):
        figures = dict(LIMIT)
        figures[WORK] = (1.0, R_WORK, 5.0, WEEK)
        world = World(self, figures=figures)
        world.link.up = True
        world.link.devbox = WORK
        world.link.readings = world.devbox_readings()
        service = world.service()
        service.tick()
        self.assertEqual(service.state()["countdown"]["target_uuid"], WORK)

    def test_auto_switch_after_5_seconds_unless_cancelled(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        world.run(service)
        service.tick()
        self.assertEqual(len(world.timers), 1)
        timer = world.timers[0]
        self.assertTrue(timer.started)
        self.assertEqual(timer.seconds, 5.0)
        self.assertEqual(server.COUNTDOWN_SECONDS, 5.0)
        state = world.call("GET", "/state")[1]
        self.assertTrue(state["mac"]["countdown"])
        self.assertEqual(state["mac"]["head"], "Switching this Mac to Team Account in 5 seconds.")
        self.assertEqual(state["mac"]["sub"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(state["mac"]["action_kind"], "cancel")
        self.assertEqual(state["countdown"]["target_uuid"], ORG)
        self.assertEqual(state["countdown"]["ends_at"], iso(START + timedelta(seconds=5)))
        self.assertEqual(world.live_owner(), ALEX)
        timer.fire()
        self.assertEqual(world.live_owner(), ORG)
        state = world.call("GET", "/state")[1]
        self.assertFalse(state["mac"]["countdown"])
        self.assertIsNone(state["countdown"])
        self.assertEqual(state["mac"]["head"], "This Mac now uses Team Account.")
        self.assertEqual(state["mac"]["action_kind"], "undo")
        self.assertEqual(service.watcher.chosen_uuid, ORG)
        self.assertIn(
            "Lifeline switched this Mac to Team Account because Alex's Account reached 99% of its 5 hours.",
            [n["title"] for n in state["notifications"]],
        )
        last = server.read_last_switch(server.last_switch_path(world.folder))
        self.assertEqual((last["from_uuid"], last["to_uuid"]), (ALEX, ORG))

        cancelled = World(self, figures=LIMIT)
        service = cancelled.service()
        cancelled.run(service)
        service.tick()
        timer = cancelled.timers[0]
        status, body = cancelled.call("POST", "/cancel")
        self.assertEqual((status, body), (200, {"cancelled": True}))
        self.assertTrue(timer.cancelled)
        timer.fire()
        self.assertEqual(cancelled.live_owner(), ALEX)
        state = cancelled.call("GET", "/state")[1]
        self.assertFalse(state["mac"]["countdown"])
        self.assertEqual(state["mac"]["head"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(state["mac"]["action_kind"], "primary")
        cancelled.assert_clean()

    def test_a_status_line_reading_at_the_limit_starts_the_countdown_at_once(self):
        world = World(self)
        world.usage_reading(ORG, 0.0, None)
        service = server.Service(world.parts(), tick_seconds=3600)
        service.start(ticking=True)
        self.addCleanup(service.stop)
        deadline = time.monotonic() + 10
        while service.ticks < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(world.timers, [])
        self.assertEqual(service.take_reading(statusline_body("s1", ALEX, R_ALEX, five=100.0)), ALEX)
        while not world.timers and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(world.timers), 1)
        self.assertEqual(service.ticks, 1)

    def test_an_unknown_reset_rearms_once_the_account_is_below_its_limit(self):
        world = World(self, figures=dict(LIMIT, **{ALEX: (100.0, None, 24.0, WEEK)}))
        service = world.service()
        service.tick()
        service.cancel()
        world.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(len(world.timers), 1)
        world.figures[ALEX] = (40.0, None, 24.0, WEEK)
        world.clock.advance(minutes=6)
        service.tick()
        world.figures[ALEX] = (100.0, None, 26.0, WEEK)
        world.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(len(world.timers), 2)

    def test_cancelled_countdown_waits_for_a_new_limit_event(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        service.cancel()
        service.tick()
        world.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(len(world.timers), 1)
        world.clock.now = R_ALEX + timedelta(minutes=5)
        world.figures[ALEX] = (100.0, R_ALEX + timedelta(hours=5), 30.0, WEEK)
        service.tick()
        self.assertEqual(len(world.timers), 2)
        self.assertTrue(world.timers[1].started)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ALEX)
        self.assertEqual(service.state()["countdown"]["target_uuid"], ORG)
        world.timers[1].fire()
        self.assertEqual(world.live_owner(), ORG)

    def test_a_countdown_that_fired_does_not_repeat_for_the_same_event(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        world.timers[0].fire()
        service.undo()
        self.assertEqual(world.live_owner(), ALEX)
        service.tick()
        self.assertEqual(len(world.timers), 1)

    def test_no_countdown_when_auto_switch_is_off_or_no_account_has_room(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.tick()
        self.assertEqual(world.timers, [])
        full = World(self, figures={uuid: (100.0, R_ALEX, 50.0, WEEK) for uuid in ORDER})
        service = full.service()
        service.tick()
        self.assertEqual(full.timers, [])

    def test_auto_switch_turned_off_during_a_round_starts_no_countdown(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        service.cancel()
        world.clock.now = R_ALEX + timedelta(minutes=5)
        world.figures[ALEX] = (100.0, R_ALEX + timedelta(hours=5), 30.0, WEEK)
        service.reader.tick()
        hooked = FlakyHistory(world.history)
        service.parts.history = hooked
        original = world.history.latest
        calls = []

        def latest(uuid):
            if not calls:
                calls.append(uuid)
                service.update_settings({"auto_switch": False})
            return original(uuid)

        hooked.latest = latest
        service._auto_step(world.clock.now)
        self.assertEqual(calls, [ALEX])
        self.assertEqual(len(world.timers), 1)
        self.assertIsNone(service.state()["countdown"])

    def test_turning_auto_switch_off_cancels_a_countdown(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        service.update_settings({"auto_switch": False})
        self.assertTrue(world.timers[0].cancelled)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ALEX)

    def test_a_manual_switch_replaces_the_countdown(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        service.switch_to(PERSONAL)
        self.assertTrue(world.timers[0].cancelled)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), PERSONAL)

    def test_watcher_replaced_after_each_switch_with_the_seed(self):
        world = World(self)
        service = world.service()
        service.tick()
        first = service.watcher
        self.assertEqual(first.chosen_uuid, ALEX)
        service.switch_to(ORG)
        second = service.watcher
        self.assertIsNot(second, first)
        self.assertEqual(second.chosen_uuid, ORG)
        self.assertIsNone(world.store.get(ORG))
        world.slot.item = dict(world.slot.item, claudeAiOauth=world.store.get(ALEX))
        service.tick()
        self.assertEqual(world.live_owner(), ORG)
        self.assertIsNotNone(world.store.get(ALEX))
        self.assertIsNone(world.store.get(ORG))
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn(
            "Another app switched Claude Code to Alex's Account. "
            "Lifeline kept the newest sign-in for Alex's Account and switched back to Team Account.",
            titles,
        )
        service.undo()
        third = service.watcher
        self.assertIsNot(third, second)
        self.assertEqual(third.chosen_uuid, ALEX)
        world.assert_clean()

    def test_a_later_watcher_never_reuses_an_old_switch_signin(self):
        world = World(self)
        service = world.service()
        service.tick()
        service.switch_to(ORG)
        first = copy.deepcopy(world.slot.item["claudeAiOauth"])
        renewed = world.issue(ORG)
        renewed["expiresAt"] = FUTURE_MS + 1000
        world.slot.item = dict(world.slot.item, claudeAiOauth=renewed)
        service.tick()
        options = dict(
            slot=world.slot,
            store=world.store,
            registry=world.registry,
            home=world.home,
            profile_fn=world.profiles,
            lock_path=world.lock_path,
            backups=world.home / ".lifeline-backups",
            settle=0,
        )
        switching.switch(ALEX, **options)
        service.tick()
        service.tick()
        self.assertEqual(service.watcher.chosen_uuid, ALEX)
        switching.switch(ORG, **options)
        self.assertEqual(world.slot.item["claudeAiOauth"], renewed)
        service.tick()
        world.slot.item = dict(world.slot.item, claudeAiOauth=world.store.get(ALEX))
        service.tick()
        self.assertEqual(service.watcher.chosen_uuid, ORG)
        self.assertNotEqual(world.slot.item["claudeAiOauth"]["refreshToken"], first["refreshToken"])
        self.assertEqual(world.live_owner(), ALEX)

    def test_a_switch_lets_the_old_watcher_park_the_signin_it_holds(self):
        world = World(self)
        service = world.service()
        service.tick()
        renewed = world.issue(ALEX)
        world.slot.item = {"claudeAiOauth": renewed, "mcpOAuth": {"github|1": {"accessToken": MCP_SECRET}}}
        service.tick()
        old = service.watcher
        world.slot.item = {"claudeAiOauth": world.issue(WORK), "mcpOAuth": {"github|1": {"accessToken": MCP_SECRET}}}
        service.switch_to(PERSONAL)
        self.assertIsNot(service.watcher, old)
        self.assertTrue(service.finish_checks())
        self.assertFalse(old.active)
        self.assertEqual(world.store.get(ALEX), renewed)
        self.assertIn(
            "Lifeline stopped watching Alex's Account because Claude Code was switched to Personal Account. "
            "Alex's Account is saved in Lifeline, ready to switch back to.",
            [n["title"] for n in service.state()["notifications"]],
        )
        world.assert_clean()

    def test_a_watcher_that_cannot_save_yet_keeps_trying_after_the_switch(self):
        world = World(self)
        service = world.service()
        service.tick()
        renewed = world.issue(ALEX)
        world.slot.item = {"claudeAiOauth": renewed, "mcpOAuth": {}}
        service.tick()
        old = service.watcher
        world.slot.item = {"claudeAiOauth": world.issue(WORK), "mcpOAuth": {}}
        real_put = world.store.put
        refuse = {"on": True}

        def put(uuid, oauth):
            if refuse["on"] and uuid == ALEX and oauth == renewed:
                raise OSError("disk full")
            return real_put(uuid, oauth)

        world.store.put = put
        service.switch_to(PERSONAL)
        self.assertTrue(old.active)
        self.assertIn(ALEX, service.guarded())
        refuse["on"] = False
        service.tick()
        self.assertFalse(old.active)
        self.assertEqual(world.store.get(ALEX), renewed)
        self.assertNotIn(ALEX, service.guarded())

    def test_stop_lets_the_watcher_finish_before_the_flush(self):
        world = World(self)
        service = world.service()
        service.tick()
        renewed = world.issue(ALEX)
        world.slot.item = {"claudeAiOauth": renewed, "mcpOAuth": {}}
        service.tick()
        world.slot.item = {"claudeAiOauth": world.issue(WORK), "mcpOAuth": {}}
        service.stop()
        self.assertEqual(world.slot.item["claudeAiOauth"], renewed)
        self.assertIsNotNone(world.store.get(WORK))
        with self.assertRaises(server.Refusal) as caught:
            service.switch_to(ORG)
        self.assertEqual((caught.exception.status, caught.exception.sentence), (503, "Lifeline is stopping, so it did not switch."))
        self.assertEqual(world.live_owner(), ALEX)

    def test_a_countdown_whose_account_moved_on_is_dropped_quietly(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        world.history.add_reading(ALEX, "usage", 30.0, R_ALEX + timedelta(hours=5), 24.0, WEEK, world.clock.now)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ALEX)
        self.assertIsNone(service.state()["countdown"])
        moved = World(self, figures=LIMIT)
        service = moved.service()
        service.tick()
        switching.switch(
            WORK,
            slot=moved.slot,
            store=moved.store,
            registry=moved.registry,
            home=moved.home,
            profile_fn=moved.profiles,
            lock_path=moved.lock_path,
            backups=moved.home / ".lifeline-backups",
            settle=0,
        )
        moved.timers[0].fire()
        self.assertEqual(moved.live_owner(), WORK)
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertFalse([title for title in titles if title.startswith("Lifeline switched this Mac")])

    def test_the_auto_switch_step_waits_for_a_switch_in_progress(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.reader.tick()
        service._switching.acquire()
        released = []
        try:
            worker = threading.Thread(target=service._auto_step, args=(world.clock.now,), daemon=True)
            worker.start()
            time.sleep(0.3)
            self.assertEqual(world.timers, [])
        finally:
            service._switching.release()
            released.append(True)
        worker.join(10)
        self.assertEqual(len(world.timers), 1)

    def test_watcher_follows_a_switch_made_elsewhere(self):
        world = World(self)
        service = world.service()
        service.tick()
        self.assertEqual(service.watcher.chosen_uuid, ALEX)
        switching.switch(
            WORK,
            slot=world.slot,
            store=world.store,
            registry=world.registry,
            home=world.home,
            profile_fn=world.profiles,
            lock_path=world.lock_path,
            backups=world.home / ".lifeline-backups",
            settle=0,
        )
        service.tick()
        service.tick()
        self.assertEqual(service.watcher.chosen_uuid, WORK)
        self.assertEqual(world.live_owner(), WORK)

    def test_reader_never_renews_the_account_the_watcher_guards(self):
        world = World(self)
        service = world.service()
        service.tick()
        self.assertEqual(service.guarded(), {ALEX})


class RecordingReader:
    def __init__(self, world, inner):
        self.world = world
        self.inner = inner
        self.log = []
        self.unsaved = set()
        self.reports = []
        self.gate = None
        self.entered = threading.Event()
        self.guarded_at_flush = []

    def tick(self):
        self.log.append("tick")
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        result = self.inner.tick()
        self.log.append("tick done")
        return result

    def status(self, uuid):
        return self.inner.status(uuid)

    def flush(self):
        self.log.append(("flush", self.world.live_owner()))
        service = getattr(self, "service", None)
        if service is not None:
            self.guarded_at_flush.append(service.guarded())
        return list(self.reports), set(self.unsaved)


class FlushTests(unittest.TestCase):
    def recording(self, **world_options):
        world = World(self, **world_options)
        service = world.service()
        reader = RecordingReader(world, service.reader)
        service.reader = reader
        return world, service, reader

    def test_the_service_keeps_exactly_one_reader(self):
        world = World(self)
        built = []
        real = server.Reader

        def counting(**options):
            built.append(options)
            return real(**options)

        with mock.patch.object(server, "Reader", counting):
            service = world.service()
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["guarded_fn"], service.guarded)
        readers = [value for value in vars(service).values() if isinstance(value, real)]
        self.assertEqual(readers, [service.reader])

    def test_every_switch_flushes_the_reader_before_it_loads(self):
        world, service, reader = self.recording(figures=LIMIT)
        service.switch_to(PERSONAL)
        self.assertEqual(reader.log, [("flush", ALEX)])
        service.undo()
        self.assertEqual(reader.log[-1], ("flush", PERSONAL))
        service.tick()
        reader.log.clear()
        world.timers[-1].fire()
        self.assertEqual(reader.log, [("flush", ALEX)])
        self.assertEqual(world.live_owner(), ORG)

    def test_flush_reports_become_notifications(self):
        world, service, reader = self.recording()
        reader.reports = ["Lifeline saved the renewed sign-in for Work Account from its recovery folder."]
        service.switch_to(WORK)
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn("Lifeline saved the renewed sign-in for Work Account from its recovery folder.", titles)

    def test_a_switch_to_an_account_held_only_in_memory_is_refused(self):
        world, service, reader = self.recording()
        reader.unsaved = {ORG}
        world.run(service)
        status, body = world.call("POST", "/switch", {"uuid": ORG})
        self.assertEqual(status, 409)
        self.assertEqual(
            body["error"],
            "Lifeline renewed the sign-in for Team Account but has not saved it yet, so it did not switch. Try again in a moment.",
        )
        self.assertEqual(world.live_owner(), ALEX)
        self.assertEqual(world.call("POST", "/switch", {"uuid": WORK})[0], 200)
        self.assertEqual(world.live_owner(), WORK)

    def test_an_auto_switch_to_an_unsaved_account_is_refused(self):
        world, service, reader = self.recording(figures=LIMIT)
        reader.unsaved = {ORG}
        service.tick()
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ALEX)
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn(
            "Lifeline renewed the sign-in for Team Account but has not saved it yet, so it did not switch. Try again in a moment. "
            "This Mac is still on Alex's Account, which has used 100% of its 5 hours.",
            titles,
        )
        self.assertIsNone(service.state()["countdown"])

    def test_a_failing_flush_stops_the_switch(self):
        world, service, reader = self.recording()

        def broken():
            raise RuntimeError("flush broke " + MCP_SECRET)

        reader.flush = broken
        world.run(service)
        status, body = world.call("POST", "/switch", {"uuid": ORG})
        self.assertEqual((status, body["error"]), (409, "Lifeline could not save its renewed sign-ins, so it did not switch. Try again in a moment."))
        self.assertEqual(world.live_owner(), ALEX)
        world.assert_clean()

    def test_a_click_does_not_wait_for_a_slow_round(self):
        world, service, reader = self.recording()
        reader.service = service
        reader.gate = threading.Event()
        self.addCleanup(reader.gate.set)
        ticking = threading.Thread(target=service.tick, daemon=True)
        ticking.start()
        self.assertTrue(reader.entered.wait(10))
        started = time.monotonic()
        service.switch_to(ORG)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(world.live_owner(), ORG)
        self.assertEqual(reader.log, ["tick", ("flush", ALEX)])
        self.assertIn(ORG, reader.guarded_at_flush[0])
        self.assertTrue(ticking.is_alive())
        reader.gate.set()
        ticking.join(10)
        self.assertFalse(ticking.is_alive())
        self.assertEqual(service.guarded(), {ORG})

    def test_the_devbox_round_flushes_instead_of_reading(self):
        world, service, reader = self.recording()
        world.link.up = True
        world.link.readings = world.devbox_readings()
        service.tick()
        self.assertEqual(reader.log, [("flush", ALEX)])
        self.assertEqual(world.usage.calls, [])

    def test_shutdown_stops_the_scheduler_then_flushes_and_names_what_is_unsaved(self):
        world, service, reader = self.recording()
        reader.reports = ["Lifeline saved the renewed sign-in for Work Account from its recovery folder."]
        reader.unsaved = {ORG, PERSONAL}
        service.tick_seconds = 0.02
        service.start(ticking=True)
        deadline = time.monotonic() + 10
        while service.ticks < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        service.stop()
        self.assertFalse(service._thread.is_alive())
        self.assertEqual(reader.log[-1], ("flush", ALEX))
        self.assertEqual(reader.log[-2], "tick done")
        self.assertIn("INFO Lifeline saved the renewed sign-in for Work Account from its recovery folder.", world.collector.lines)
        self.assertIn(
            "WARNING Lifeline stopped before it could save the renewed sign-in for Personal Account and Team Account, "
            "so sign in to them again.",
            world.collector.lines,
        )
        world.assert_clean()


class AlertRoutingTests(unittest.TestCase):
    def limited(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.update_settings({"auto_switch": False})
        return world, service

    def limit_key(self):
        return "limit:{}:{}".format(ALEX, (R_ALEX - timedelta(hours=5)).isoformat())

    def notified(self, service):
        return [n for n in service.state()["notifications"] if n["kind"] != "report"]

    def present(self, service):
        service.set_presence({"idle_seconds": 30, "locked": False, "asleep": False})

    def away(self, service):
        service.set_presence({"idle_seconds": 700, "locked": False, "asleep": False})

    def test_an_alert_that_comes_while_away_waits_on_the_mac(self):
        world, service = self.limited()
        self.away(service)
        service.tick()
        world.clock.advance(seconds=60)
        service.tick()
        alerts = self.notified(service)
        self.assertEqual([(n["id"], n["keep"]) for n in alerts], [(self.limit_key(), True)])

    def test_present_gets_a_mac_notification_only(self):
        world, service = self.limited()
        self.present(service)
        service.tick()
        alerts = self.notified(service)
        self.assertEqual([(n["id"], n["kind"]) for n in alerts], [(self.limit_key(), "limit")])
        self.assertEqual(alerts[0]["title"], "Alex's Account is out until today at 17:00")
        self.assertEqual(alerts[0]["body"], "Team Account has the most room.")
        world.clock.advance(seconds=120)
        service.tick()
        self.assertEqual(len(self.notified(service)), 1)

    def test_no_alert_until_presence_or_two_minutes(self):
        world, service = self.limited()
        service.tick()
        self.assertEqual(self.notified(service), [])
        self.present(service)
        service.tick()
        self.assertEqual([n["id"] for n in self.notified(service)], [self.limit_key()])
        quiet, quiet_service = self.limited()
        quiet.clock.advance(seconds=119)
        quiet_service.tick()
        self.assertEqual(self.notified(quiet_service), [])
        quiet.clock.advance(seconds=1)
        quiet_service.tick()
        self.assertEqual([n["keep"] for n in self.notified(quiet_service)], [True])

    def test_a_report_made_stale_by_a_sleep_waits_for_a_fresh_one(self):
        world = World(self)
        service = world.service()
        service.update_settings({"auto_switch": False})
        self.present(service)
        service.tick()
        world.figures[ALEX] = (100.0, R_ALEX + timedelta(hours=1), 24.0, WEEK)
        world.clock.sleep(600)
        service.tick()
        self.assertEqual(self.notified(service), [])
        self.present(service)
        service.tick()
        self.assertEqual([n["kind"] for n in self.notified(service)], ["limit"])

    def test_a_wake_report_before_the_first_round_after_a_sleep_counts(self):
        world = World(self)
        service = world.service()
        self.present(service)
        service.tick()
        world.clock.sleep(600)
        self.present(service)
        self.assertTrue(service._alerts_allowed())
        service.tick()
        self.assertTrue(service._alerts_allowed())
        self.assertTrue(service.present())

    def test_a_round_after_an_asleep_report_waits_again(self):
        world, service = self.limited()
        world.figures = dict(CALM)
        self.present(service)
        service.tick()
        world.clock.advance(seconds=70)
        service.tick()
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": True})
        world.clock.sleep(50)
        world.usage_reading(ALEX, 100.0, R_ALEX, week=18.0, minutes_ago=0)
        service.tick()
        self.assertFalse(service._alerts_allowed())
        self.assertEqual(self.notified(service), [])
        service.tick()
        self.assertEqual(self.notified(service), [])
        self.present(service)
        service.tick()
        self.assertEqual([n["kind"] for n in self.notified(service)], ["limit"])

    def test_presence_rules(self):
        world = World(self)
        service = world.service()
        self.assertFalse(service.present())
        for report, expected in (
            ({"idle_seconds": 599, "locked": False, "asleep": False}, True),
            ({"idle_seconds": 600, "locked": False, "asleep": False}, False),
            ({"idle_seconds": 0, "locked": True, "asleep": False}, False),
            ({"idle_seconds": 0, "locked": False, "asleep": True}, False),
        ):
            service.set_presence(report)
            self.assertEqual(service.present(), expected, report)
        service.set_presence({"idle_seconds": 0, "locked": False, "asleep": False})
        world.clock.advance(seconds=179)
        self.assertTrue(service.present())
        world.clock.advance(seconds=1)
        self.assertFalse(service.present())

    def test_alert_settings_turn_an_alert_off(self):
        world, service = self.limited()
        service.update_settings({"alerts": {"limit": False}})
        self.present(service)
        service.tick()
        self.assertEqual(self.notified(service), [])


class LoopTests(unittest.TestCase):
    def test_reads_skipped_while_the_devbox_is_reachable(self):
        world = World(self)
        world.link.up = True
        world.link.devbox = WORK
        world.link.q = PERSONAL
        world.figures[ORG] = (33.0, R_ALEX + timedelta(hours=2), 12.0, WEEK)
        world.link.readings = world.devbox_readings()
        service = world.service()
        service.tick()
        self.assertEqual(world.usage.calls, [])
        latest = world.history.latest(ORG)
        self.assertEqual((latest.source, latest.five), (server.DEVBOX_SOURCE, 33.0))
        service.tick()
        self.assertEqual(len([r for r in world.history.readings(ORG, START - timedelta(hours=1))]), 1)
        state = service.state()
        live = state["devbox"].pop("live")
        self.assertIs(state["devbox"].pop("can_sign_in"), True)
        self.assertEqual(state["devbox"], {"name": None, "address": None, "reachable": True, "answers": True, "account_uuid": WORK, "live_registered": None, "set_up": False})
        self.assertEqual((live["uuid"], live["added"], live["name"], live["adopt_ready"]), (WORK, True, "Work Account", True))
        self.assertEqual(state["accounts"][1]["in_use"], ["devbox"])
        self.assertEqual(state["dev"]["hero_name"], "Work Account")
        self.assertEqual(state["mac"]["rows"][2]["where"], "Not in use. Resets today at 19:10.")
        self.assertTrue(state["mac"]["rows"][1]["where"].startswith("Used by the other computer."), state["mac"]["rows"][1]["where"])
        world.link.up = False
        service.tick()
        self.assertFalse(service.state()["mac"]["rows"][1]["where"].startswith("Used by the other computer"))
        self.assertEqual(world.usage.calls, [])
        world.clock.advance(seconds=301)
        service.tick()
        self.assertEqual(sorted(world.usage.calls), sorted(ORDER))
        self.assertEqual(service.state()["devbox"], {"name": None, "address": None, "reachable": False, "answers": False, "account_uuid": WORK, "live_registered": None, "set_up": False, "live": None, "can_sign_in": None})

    def test_a_round_during_a_switch_flushes_instead_of_reading(self):
        world = World(self)
        service = world.service()
        calls = []
        service.reader = RecordingReader(world, service.reader)
        service._switch_target = ORG
        service.tick()
        self.assertEqual(service.reader.log, [("flush", ALEX)])
        self.assertEqual(world.usage.calls, [])
        service._switch_target = None
        service.tick()
        self.assertIn("tick", service.reader.log)

    def test_one_failing_watcher_does_not_stop_the_others(self):
        world = World(self)
        service = world.service()
        service.tick()

        class Broken:
            chosen_uuid = WORK
            active = True

            def poll(self):
                raise RuntimeError("broken " + MCP_SECRET)

        class Finishing:
            chosen_uuid = PERSONAL
            active = True
            polled = 0

            def poll(self):
                self.polled += 1
                self.active = False
                return ["Lifeline stopped watching Personal Account."]

        finishing = Finishing()
        service._retiring.extend([Broken(), finishing])
        service._watcher = Broken()
        service._watcher.chosen_uuid = ALEX
        service.tick()
        self.assertEqual(finishing.polled, 1)
        self.assertNotIn(finishing, service._retiring)
        self.assertIn("Lifeline stopped watching Personal Account.", [n["title"] for n in service.state()["notifications"]])
        self.assertTrue(any("could not check Claude Code's sign-in this round" in line for line in world.collector.lines))
        world.assert_clean()

    def test_devbox_reading_times_are_clamped_to_now(self):
        world = World(self)
        world.link.up = True
        world.link.readings = [Reading(ORG, "usage", 7.0, R_ALEX, 9.0, WEEK, START + timedelta(hours=3))]
        service = world.service()
        service.tick()
        latest = world.history.latest(ORG)
        self.assertEqual((latest.five, latest.at), (7.0, START))
        world.link.up = False
        world.clock.advance(seconds=301)
        service.tick()
        self.assertEqual(len(world.usage.calls), 4)

    def test_the_mac_waits_300_seconds_after_the_last_devbox_reading(self):
        world = World(self)
        service = world.service()
        service.tick()
        self.assertEqual(len(world.usage.calls), 4)
        world.clock.advance(seconds=200)
        world.link.up = True
        world.link.readings = world.devbox_readings()
        service.tick()
        self.assertEqual(len(world.usage.calls), 4)
        world.link.up = False
        world.clock.advance(seconds=110)
        service.tick()
        self.assertEqual(len(world.usage.calls), 4)
        world.clock.advance(seconds=191)
        service.tick()
        self.assertEqual(len(world.usage.calls), 8)

    def test_loop_survives_a_step_that_raises(self):
        world = World(self, figures=LIMIT)
        flaky = FlakyHistory(world.history)
        flaky.broken.add("latest")
        service = world.service(history=flaky)
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        self.assertEqual(service.ticks, 1)
        self.assertEqual(service.watcher.chosen_uuid, ALEX)
        self.assertEqual(sorted(world.usage.calls), sorted(ORDER))
        failures = [line for line in world.collector.lines if "could not finish its" in line]
        self.assertTrue(failures)
        self.assertIn(
            "WARNING Lifeline could not finish its alerts step (RuntimeError). It will try again on the next round.", failures
        )
        flaky.broken.clear()
        service.tick()
        self.assertEqual(service.ticks, 2)
        self.assertEqual(len(world.timers), 1)
        world.assert_clean()

    def test_scheduler_thread_keeps_going_when_every_tick_fails(self):
        world = World(self)
        flaky = FlakyHistory(world.history)
        flaky.broken.update({"latest", "readings"})
        service = server.Service(world.parts(history=flaky), tick_seconds=0.02)
        service.start(ticking=True)
        self.addCleanup(service.stop)
        deadline = time.monotonic() + 10
        while service.ticks < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(service.ticks, 3)
        service.stop()
        world.assert_clean()

    def test_reader_reports_become_notifications(self):
        world = World(self)
        parked = world.store.get(WORK)
        world.owners.pop(parked["accessToken"])
        world.owners.pop(parked["refreshToken"])
        service = world.service()
        service.tick()
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn("Work Account needs a new sign-in.", titles)
        self.assertTrue(service.state()["accounts"][1]["needs_signin"])


class DevboxViewTests(unittest.TestCase):
    def test_devbox_view_has_no_switch_action(self):
        world = World(self)
        world.link.up = True
        world.link.devbox = WORK
        world.figures[WORK] = (100.0, R_WORK, 60.0, WEEK)
        world.link.readings = world.devbox_readings()
        service = world.service()
        service.tick()
        world.run(service)
        state = world.call("GET", "/state")[1]
        dev = state["dev"]
        self.assertEqual(dev["head"], "Work Account is out until today at 18:30.")
        self.assertEqual(dev["action_kind"], "none")
        self.assertEqual(dev["action_text"], "")
        self.assertEqual(dev["action_target"], "")
        self.assertEqual(dev["hint"], "Lifeline switches only this Mac, so switch the other computer there.")
        self.assertFalse(dev["countdown"])
        status, body = world.call("POST", "/switch", {"uuid": ORG, "machine": "devbox"})
        self.assertEqual((status, body["error"]), (400, server.DEVBOX_SWITCH_REFUSED))
        self.assertEqual(world.live_owner(), ALEX)


class SecretTests(unittest.TestCase):
    def test_no_secret_in_any_reply_or_log(self):
        world = World(self, figures=LIMIT)
        world.login_uuid = WORK
        service = world.service()
        world.run(service)
        service.tick()
        world.call("GET", "/state")
        world.call("GET", "/state?appearance=light")
        world.call("GET", f"/history/{ALEX}")
        world.call("POST", "/cancel")
        world.call("POST", "/switch", {"uuid": PERSONAL})
        world.call("POST", "/switch", {"uuid": PERSONAL})
        world.call("POST", "/undo")
        world.call("POST", "/undo")
        world.call("POST", "/reading", statusline_body("s1", ALEX, R_ALEX))
        world.call("POST", "/reading", {"source": "statusline", "five_hour": {"used_percentage": MCP_SECRET}})
        world.call("POST", "/presence", {"idle_seconds": 1, "locked": False, "asleep": False})
        world.call("POST", "/presence", {"idle_seconds": MCP_SECRET})
        world.call("POST", "/rename/" + WORK, {"name": "Work", "short": "Work"})
        world.call("POST", "/settings", {"hide_name": False})
        world.call("POST", "/settings", {MCP_SECRET: True})
        job = world.call("POST", f"/resign/{WORK}")[1]["job"]
        world.wait_job(job)
        world.login_fails = True
        job = world.call("POST", f"/resign/{WORK}")[1]["job"]
        failed = world.wait_job(job)
        self.assertEqual(failed["result"], "The browser sign-in did not finish, so nothing changed.")
        job = world.call("POST", "/add", {})[1]["job"]
        world.wait_job(job)
        world.raw(b"GET /state HTTP/1.1\r\nHost: x\r\nContent-Length: zz\r\nConnection: close\r\n\r\n")
        world.call("GET", "/state")
        self.assertGreater(len(world.replies), 20)
        world.assert_clean()

    def test_step_failures_log_a_sentence_without_the_error_text(self):
        world = World(self)
        flaky = FlakyHistory(world.history)
        flaky.broken.add("latest")
        service = world.service(history=flaky)
        service.tick()
        world.run(service)
        status, body = world.call("GET", "/state")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "Lifeline hit a problem and could not finish that request.")
        world.assert_clean()


class StartFailureTests(unittest.TestCase):
    def test_a_failing_start_prints_a_sentence_and_frees_the_socket(self):
        world = World(self)

        def broken():
            raise RuntimeError("cannot open " + MCP_SECRET)

        code, said, started = refused_serve(broken, world.socket_path)
        self.assertEqual((code, said, started), (1, "Lifeline could not start.", []))
        self.assertFalse(os.path.lexists(world.socket_path))
        world.run(world.service())
        self.assertEqual(world.call("GET", "/state")[0], 200)
        world.assert_clean()


class ProductionWiringTests(unittest.TestCase):
    def test_factory_wires_the_real_parts_without_touching_them(self):
        world = World(self)
        with mock.patch.object(server.sys, "platform", "darwin"):
            parts = server.production_parts()
        self.assertEqual(parts.home, world.home)
        self.assertEqual(parts.slot.service, "Claude Code-credentials")
        self.assertEqual(type(parts.store).__name__, "MacKeychainStore")
        self.assertEqual(parts.registry.path, world.home / "Library" / "Application Support" / "com.example.lifeline" / "accounts.json")
        self.assertEqual(parts.history.path, world.home / "Library" / "Application Support" / "com.example.lifeline" / "history.db")
        self.assertIsInstance(parts.link, server.NullLink)
        self.assertIs(parts.run_login, server.run_login)
        self.assertFalse(parts.link.reachable())

    def test_live_sessions_reads_only_session_files(self):
        world = World(self)
        folder = world.home / ".claude" / "sessions"
        folder.mkdir()
        (folder / "a.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "alive"}))
        (folder / "b.json").write_text(json.dumps({"pid": 2 ** 22 + 12345, "sessionId": "gone"}))
        (folder / "c.json").write_text("not json")
        (folder / "alive.key").write_text("SECRET")
        os.chmod(folder / "alive.key", 0)
        self.assertEqual(server.live_sessions(world.home), {"alive"})
        self.assertIsNone(server.live_sessions(world.home / "missing"))

    def test_run_login_sets_the_settings_folder_and_email(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        fake = FakeLogin(self, behaviour="quiet", finish_after=0)
        with mock.patch.object(server.shutil, "which", return_value="/opt/bin/login-tool"):
            server.run_login(folder, "ama@example.invalid", popen=fake, poll=0)
        self.assertEqual(fake.args, ["/opt/bin/login-tool", "auth", "login", "--claudeai", "--email", "ama@example.invalid"])
        self.assertEqual(fake.env["CLAUDE_CONFIG_DIR"], str(folder))
        with mock.patch.object(server.shutil, "which", return_value=None):
            with self.assertRaises(server.LoginFailed):
                server.run_login(folder)


class SwitchableStore:
    def __init__(self, inner):
        self.inner = inner
        self.failing = False

    def get(self, uuid):
        return self.inner.get(uuid)

    def put(self, uuid, oauth):
        if self.failing:
            raise StoreError("The new parked sign-in could not be saved.")
        self.inner.put(uuid, oauth)

    def remove(self, uuid):
        self.inner.remove(uuid)


def rescue_files(world, uuid):
    folder = world.folder / "recovery"
    if not folder.is_dir():
        return []
    return [path for path in folder.iterdir() if path.name.startswith(uuid + ".rescue.") and path.name.endswith(".json")]


PAGE = "https://claude.ai/oauth/authorize?code=true&client_id=c&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&code_challenge=c&code_challenge_method=S256&state=s"


class FakeLogin:
    def __init__(self, case, behaviour="browser", page=PAGE, finish_after=3):
        self.case = case
        self.behaviour = behaviour
        self.page = page
        self.finish_after = finish_after
        self.polls = 0
        self.args = None
        self.env = None
        self.ended = False
        self.pid = 999999

    def __call__(self, args, **options):
        self.args, self.env = args, options["env"]
        self.case.assertTrue(options.get("start_new_session"))
        return self

    def poll(self):
        self.polls += 1
        if self.polls == 1 and self.behaviour in ("browser", "open"):
            if self.behaviour == "browser":
                tool = self.env["BROWSER"]
            else:
                tool = next(os.path.join(d, "open") for d in self.env["PATH"].split(os.pathsep) if os.access(os.path.join(d, "open"), os.X_OK))
            subprocess.run([tool, self.page], env=self.env, check=False, timeout=10)
        if self.ended:
            return -15
        return 0 if self.behaviour != "silent" and self.polls > self.finish_after else None

    def terminate(self):
        self.ended = True

    def kill(self):
        self.ended = True

    def wait(self, timeout=None):
        return -15


class MacSigninTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "login"
        self.folder.mkdir()
        self.roots = Path(tmp.name) / "apps"
        self.roots.mkdir()

    def login(self, fake, **options):
        opened, pages = [], []
        with mock.patch.object(server.shutil, "which", return_value="/opt/bin/login-tool"), mock.patch.object(server.os, "killpg", lambda pid, sig: fake.terminate()):
            server.run_login(
                self.folder, "ama@example.invalid", user="Sam", on_page=lambda *found: pages.append(found),
                popen=fake, open_page=lambda url: opened.append(url) or ("Google Chrome", True), poll=0, **options,
            )
        return opened, pages

    def test_the_page_opens_through_the_browser_hook(self):
        fake = FakeLogin(self)
        opened, pages = self.login(fake)
        self.assertEqual(fake.args, ["/opt/bin/login-tool", "auth", "login", "--claudeai", "--email", "ama@example.invalid"])
        self.assertEqual(fake.env["BROWSER"], str(devbox.hook_path()))
        self.assertEqual(fake.env["CLAUDE_CONFIG_DIR"], str(self.folder))
        self.assertEqual((opened, pages), ([PAGE], [(PAGE, "Google Chrome", True)]))

    def test_the_service_opens_the_page_in_the_default_browser_by_default(self):
        fake = FakeLogin(self)
        opened = []

        def fake_open(url):
            opened.append(url)
            return "Safari", True

        pages = []
        with mock.patch.object(server.shutil, "which", return_value="/opt/bin/login-tool"), mock.patch.object(
            server.os, "killpg", lambda pid, sig: fake.terminate()
        ), mock.patch.object(devbox, "open_default", fake_open):
            server.run_login(self.folder, None, user="Sam", on_page=lambda *found: pages.append(found), popen=fake, poll=0)
        self.assertEqual((opened, pages), ([PAGE], [(PAGE, "Safari", True)]))

    def test_claude_codes_own_opener_is_caught_too(self):
        fake = FakeLogin(self, behaviour="open")
        opened, pages = self.login(fake)
        self.assertEqual(opened, [PAGE])
        self.assertTrue(fake.env["PATH"].startswith(str(self.folder)))

    def test_no_page_or_a_strange_page_stops_the_sign_in(self):
        cases = (
            ("silent", PAGE, server.NO_SIGNIN_PAGE),
            ("browser", "https://example.invalid/oauth/authorize?redirect_uri=x", server.STRANGE_SIGNIN_PAGE),
        )
        for behaviour, page, sentence in cases:
            with self.subTest(behaviour=behaviour):
                for leftover in self.folder.iterdir():
                    if leftover.is_file():
                        leftover.unlink()
                fake = FakeLogin(self, behaviour=behaviour, page=page, finish_after=10**6)
                clock = iter(range(0, 10**6, 30))
                with self.assertRaises(server.LoginFailed) as caught:
                    self.login(fake, clock=lambda: next(clock))
                self.assertEqual(str(caught.exception), sentence)
                self.assertTrue(fake.ended)

    def test_the_page_opens_in_the_default_browser_with_open_alone(self):
        calls = []

        def run(args, **options):
            calls.append(args)
            return mock.Mock(returncode=0)

        self.assertEqual(devbox.open_default(PAGE, run=run, browser_fn=lambda: "Safari"), ("Safari", True))
        self.assertEqual(calls, [["/usr/bin/open", PAGE]])

        def refused(args, **options):
            return mock.Mock(returncode=1)

        def broken(args, **options):
            raise OSError("no open")

        self.assertEqual(devbox.open_default(PAGE, run=refused, browser_fn=lambda: "Safari"), ("Safari", False))
        self.assertEqual(devbox.open_default(PAGE, run=broken, browser_fn=lambda: None), (None, False))
        for gone in ("private_open", "private_browser", "PRIVATE_BROWSERS", "FALLBACK_BROWSER", "APP_ROOTS"):
            self.assertFalse(hasattr(devbox, gone), gone)

    def handlers(self, path, items):
        path.write_bytes(plistlib.dumps({"LSHandlers": items}, fmt=plistlib.FMT_BINARY))

    def test_the_default_browser_comes_from_launchservices(self):
        prefs = self.roots / "launchservices.plist"
        apps = self.roots / "Applications"
        apps.mkdir()
        self.handlers(prefs, [
            {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.google.chrome"},
            {"LSHandlerURLScheme": "http", "LSHandlerRoleAll": "com.apple.safari"},
        ])
        self.assertEqual(devbox.default_browser(prefs, [apps]), "Google Chrome")
        self.handlers(prefs, [{"LSHandlerURLScheme": "http", "LSHandlerRoleAll": "org.mozilla.firefox"}])
        self.assertEqual(devbox.default_browser(prefs, [apps]), "Firefox")
        self.handlers(prefs, [{"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.Brave.Browser"}])
        self.assertEqual(devbox.default_browser(prefs, [apps]), "Brave Browser")
        self.handlers(prefs, [{"LSHandlerContentType": "public.html", "LSHandlerRoleAll": "com.google.chrome"}])
        self.assertEqual(devbox.default_browser(prefs, [apps]), "Safari")
        self.assertEqual(devbox.default_browser(self.roots / "missing.plist", [apps]), "Safari")
        info = apps / "Zen.app" / "Contents"
        info.mkdir(parents=True)
        (info / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "app.zen-browser.zen", "CFBundleName": "Zen"}))
        self.handlers(prefs, [{"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "app.zen-browser.zen"}])
        self.assertEqual(devbox.default_browser(prefs, [apps]), "Zen")
        self.handlers(prefs, [{"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.example.nothing"}])
        self.assertIsNone(devbox.default_browser(prefs, [apps]))
        prefs.write_text("not a property list")
        self.assertIsNone(devbox.default_browser(prefs, [apps]))


class SigninResultTests(unittest.TestCase):
    def test_an_add_job_records_the_page_and_the_browser(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_page = PAGE
        world.login_uuid = NEWCOMER
        world.run(world.service())
        job = world.wait_job(world.call("POST", "/add", {})[1]["job"])
        self.assertEqual((job["state"], job["url"], job["opened"], job["browser"]), ("done", PAGE, True, "Google Chrome"))
        world.assert_clean()

    def test_an_account_already_added_is_named_and_nothing_changes(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_uuid = PERSONAL
        service = world.service()
        world.run(service)
        parked = world.store.get(PERSONAL)
        live = json.dumps(world.slot.item, sort_keys=True)
        with self.assertLogs("lifeline.server", level="WARNING") as logged:
            job = world.wait_job(world.call("POST", "/add", {})[1]["job"])
        sentence = (
            "Lifeline already has Personal Account. Google Chrome signed in as 33333333@example.invalid, so nothing changed. "
            "To add a different account, sign out of claude.ai in Google Chrome or choose the other account, then press Try again."
        )
        self.assertEqual((job["state"], job["result"], job["already_has"]), ("failed", sentence, True))
        self.assertEqual(job["result_head"], "Lifeline already has Personal Account.")
        self.assertEqual(world.store.get(PERSONAL), parked)
        self.assertEqual(json.dumps(world.slot.item, sort_keys=True), live)
        self.assertEqual(rescue_files(world, PERSONAL), [])
        self.assertEqual(world.keychain.items, {})
        self.assertTrue(any(sentence in line for line in logged.output), logged.output)

    def test_every_failed_job_is_written_to_the_log(self):
        world = World(self)
        world.login_fails = True
        world.run(world.service())
        with self.assertLogs("lifeline.server", level="WARNING") as logged:
            job = world.wait_job(world.call("POST", "/add", {})[1]["job"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("WARNING:lifeline.server:" + job["result"], logged.output)
        self.assertFalse(any("Adding an account did not finish." in line for line in logged.output))
        self.assertNotIn(LOGIN_SECRET, " ".join(logged.output))


class DevboxAnswersTests(unittest.TestCase):
    def test_state_says_whether_lifeline_answers_from_the_devbox(self):
        world = World(self)
        service = world.service()
        service.tick()
        state = service.state()
        self.assertIs(state["devbox"]["answers"], False)

    def test_a_devbox_step_while_the_devbox_does_not_answer_ends_plainly_in_its_job(self):
        from lifeline import devbox

        world = World(self)
        link = mock.Mock()
        link.reachable.return_value = False
        link.fetch_readings.return_value = []
        link.request.side_effect = devbox.LinkUnreached()
        service = world.service(link=link)
        service.tick()
        for rest, body in (("add", {"uuid": ALEX}), ("adopt", {})):
            with self.subTest(rest=rest):
                job = service.start_devbox_job(rest, body)["job"]
                self.assertTrue(world.wait_for(lambda: [j for j in service.state()["jobs"] if j["id"] == job][0]["state"] != "running", 5))
                found = [j for j in service.state()["jobs"] if j["id"] == job][0]
                self.assertEqual((found["state"], found["result"]), ("failed", devbox.DEVBOX_SILENT))
        link.start_signin.assert_not_called()
        link.reachable.assert_called()


class SuggestedNameTests(unittest.TestCase):
    def test_names_follow_the_first_name_pattern(self):
        cases = (
            ({"displayName": "Alexandra Rivera", "organizationName": "alexandra@example.com's Organization"}, ("Alexandra's Account", "Alexandra")),
            ({"displayName": "Sam", "organizationName": "Example Org"}, ("Example Org Account", "Example Org")),
            ({"displayName": "", "emailAddress": "ama.rivera@example.com"}, ("Ama's Account", "Ama")),
            ({}, ("", "")),
        )
        for profile, names in cases:
            with self.subTest(profile=profile):
                self.assertEqual(server.suggested_names(profile), names)

    def test_a_successful_add_carries_the_suggested_names(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_uuid = NEWCOMER
        world.run(world.service())
        job = world.wait_job(world.call("POST", "/add", {})[1]["job"])
        self.assertEqual((job["state"], job["suggested_name"], job["suggested_short"]), ("done", "Ama's Account", "Ama"))
        world.assert_clean()


class LogLineTests(unittest.TestCase):
    def lines(self, logged):
        return [line.split(":", 2)[2] for line in logged.output]

    def test_a_switch_and_an_undo_are_written_to_the_log(self):
        world = World(self, live=ALEX)
        world.run(world.service())
        with self.assertLogs("lifeline.server", level="INFO") as logged:
            self.assertEqual(world.call("POST", "/switch", {"number": 2, "via": "row"})[0], 200)
            self.assertEqual(world.call("POST", "/undo", {"via": "menu-undo"})[0], 200)
            self.assertEqual(world.call("POST", "/switch", {"uuid": WORK})[0], 200)
            self.assertEqual(world.call("POST", "/undo", {})[0], 200)
        lines = self.lines(logged)
        self.assertIn("You switched this Mac from Alex's Account to Work Account (via row).", lines)
        self.assertIn("You undid the last switch, so this Mac went from Work Account back to Alex's Account (via menu-undo).", lines)
        self.assertIn("You switched this Mac from Alex's Account to Work Account (via unknown).", lines)
        self.assertIn("You undid the last switch, so this Mac went from Work Account back to Alex's Account (via unknown).", lines)
        world.assert_clean()

    def test_every_named_control_is_accepted_and_a_strange_one_is_refused(self):
        world = World(self, live=ALEX)
        world.run(world.service())
        with self.assertLogs("lifeline.server", level="INFO") as logged:
            for via in ("row", "footer", "key", "notification", "cli"):
                target = WORK if world.live_owner() == ALEX else ALEX
                self.assertEqual(world.call("POST", "/switch", {"uuid": target, "via": via})[0], 200)
        self.assertEqual(sum(line.endswith(f"(via {via}).") for line in self.lines(logged) for via in ("row", "footer", "key", "notification", "cli")), 5)
        owner = world.live_owner()
        for route, body in (("/switch", {"number": 2, "via": "auto"}), ("/switch", {"number": 2, "via": 3}), ("/undo", {"via": "elsewhere"}), ("/undo", {"via": "menu-undo", "x": 1})):
            with self.subTest(route=route, body=body):
                self.assertEqual(world.call("POST", route, body), (400, {"error": server.BAD_VIA if "x" not in body else server.BAD_UNDO}))
        self.assertEqual(world.live_owner(), owner)
        world.assert_clean()

    def test_an_auto_switch_is_written_to_the_log(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        with self.assertLogs("lifeline.server", level="INFO") as logged:
            world.timers[0].fire()
        self.assertIn("Auto-switch moved this Mac from Alex's Account to Team Account because Alex's Account reached 99% of its 5 hours (via auto).", self.lines(logged))

    def test_a_change_made_outside_lifeline_is_written_to_the_log(self):
        world = World(self, live=ALEX)
        world.record_path.unlink()
        service = world.service()
        service.tick()
        world.slot.item = {"claudeAiOauth": world.signins[WORK]}
        world.write_config(WORK)
        with self.assertLogs("lifeline.server", level="INFO") as logged:
            service.tick()
        self.assertIn("Claude Code on this Mac changed from Alex's Account to Work Account outside Lifeline.", self.lines(logged))

    def test_an_add_and_a_sign_in_are_written_to_the_log(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.run(world.service())
        world.login_uuid = NEWCOMER
        with self.assertLogs("lifeline.server", level="INFO") as logged:
            world.wait_job(world.call("POST", "/add", {})[1]["job"])
            world.login_uuid = WORK
            world.wait_job(world.call("POST", f"/resign/{WORK}", {})[1]["job"])
        lines = self.lines(logged)
        self.assertIn("You added Ama's Account to Lifeline as number 4.", lines)
        self.assertIn("You signed in to Work Account again.", lines)
        world.assert_clean()


class R87Tests(unittest.TestCase):
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

    def login_env(self, user, environ_user):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        fake = FakeLogin(self, behaviour="quiet", finish_after=0)
        planted = {**self.PLANTED, **self.KEPT, "PATH": "/usr/bin:/bin", "HOME": "/home/test", "USER": environ_user}
        with mock.patch.dict(os.environ, planted), mock.patch.object(server.shutil, "which", return_value="/opt/bin/login-tool"):
            server.run_login(self.folder, "ama@example.invalid", user=user, popen=fake, poll=0)
        return fake.env

    def test_run_login_never_passes_session_variables_and_sets_the_keychain_user(self):
        for user, environ_user, expected in (
            ("Sam", "other", "Sam"),
            ("john doe", "other", "claude-code-user"),
            (None, "Sam", "Sam"),
            (None, "john doe", "claude-code-user"),
        ):
            with self.subTest(user=user, environ_user=environ_user):
                env = self.login_env(user, environ_user)
                for key in self.PLANTED:
                    if key != "CLAUDE_CONFIG_DIR":
                        self.assertNotIn(key, env)
                self.assertEqual([key for key in env if key.startswith(("CLAUDE", "ANTHROPIC"))], ["CLAUDE_CONFIG_DIR"])
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.folder))
                self.assertEqual(env["USER"], expected)
                self.assertEqual((env["PATH"], env["HOME"]), (str(self.folder / server.OPEN_SHIM) + ":/usr/bin:/bin", "/home/test"))
                for key, value in self.KEPT.items():
                    self.assertEqual(env[key], value)

    def test_add_and_resign_jobs_pass_the_service_user_to_the_login(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.run(world.service())
        world.login_uuid = NEWCOMER
        world.wait_job(world.call("POST", "/add", {})[1]["job"])
        world.login_uuid = WORK
        world.wait_job(world.call("POST", f"/resign/{WORK}", {})[1]["job"])
        self.assertEqual(world.login_users, [USER, USER])

    def test_resign_route_accepts_an_email_and_refuses_anything_else(self):
        world = World(self)
        world.login_uuid = WORK
        world.run(world.service())
        status, body = world.call("POST", f"/resign/{WORK}", {"email": "hello@example.net"})
        self.assertEqual(status, 202)
        job = world.wait_job(body["job"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(world.logins[-1][1], "hello@example.net")
        for payload in ({"name": "X"}, {"email": "--console"}, {"email": "no-at-sign"}, {"email": 5}, {"email": "a b@example.invalid"}):
            with self.subTest(payload=payload):
                status, body = world.call("POST", f"/resign/{WORK}", payload)
                self.assertEqual((status, body["error"]), (400, "Signing in again takes only an email, and the email is optional."))
        status, body = world.call("POST", "/add", {"email": "--console"})
        self.assertEqual(status, 400)
        world.assert_clean()

    def test_switch_and_undo_replies_carry_the_note(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call("POST", "/switch", {"number": 2})
        self.assertEqual(status, 200)
        self.assertIn("note", body)
        self.assertIsNone(body["note"])
        real = switching.switch
        note = "A copy of the sign-in for Team Account in Lifeline's recovery folder is damaged."

        def noted(*args, **kwargs):
            result = real(*args, **kwargs)
            return switching.SwitchResult(
                result.from_uuid, result.to_uuid, result.backup, result.leftover, result.confirmed, result.signin, note=note
            )

        with mock.patch.object(switching, "switch", side_effect=noted):
            status, body = world.call("POST", "/switch", {"number": 4})
            self.assertEqual((status, body["note"]), (200, note))
            status, body = world.call("POST", "/undo", {})
            self.assertEqual((status, body["note"]), (200, note))

    def test_stop_rescues_a_watcher_that_could_not_save(self):
        world = World(self)
        store = SwitchableStore(world.store)
        service = world.service(store=store)
        service.switch_to(WORK)
        stranger = world.issue(NEWCOMER)
        world.slot.item = {**world.slot.item, "claudeAiOauth": stranger}
        store.failing = True
        service.stop()
        files = rescue_files(world, WORK)
        self.assertTrue(files)
        self.assertTrue(any(world.signins[WORK]["refreshToken"].encode() in path.read_bytes() for path in files))
        self.assertIn("INFO Lifeline saved the sign-in for Work Account in its recovery folder before it stopped.", world.collector.lines)
        world.assert_clean()

    def test_stop_leaves_a_watcher_alone_when_the_slot_holds_its_confirmed_signin(self):
        world = World(self)
        service = world.service()
        service.switch_to(WORK)
        service.stop()
        self.assertEqual(rescue_files(world, WORK), [])
        self.assertFalse(any("before it stopped" in line for line in world.collector.lines))

    def test_stop_says_when_a_rescue_fails(self):
        world = World(self)
        store = SwitchableStore(world.store)
        service = world.service(store=store)
        service.switch_to(WORK)
        world.slot.item = {**world.slot.item, "claudeAiOauth": world.issue(NEWCOMER)}
        store.failing = True
        with mock.patch.object(switching.Watcher, "rescue", return_value=False):
            service.stop()
        self.assertIn(
            "WARNING Lifeline could not save the sign-in for Work Account before it stopped. "
            "If Claude Code is not on Work Account, sign in to Work Account again.",
            world.collector.lines,
        )

    def test_production_parts_follow_the_login_rules(self):
        world = World(self)
        for environ, expected in (({"USER": "john doe"}, "claude-code-user"), ({"USER": "Sam", "LOGNAME": "x"}, "Sam")):
            with self.subTest(environ=environ):
                with mock.patch.object(server.sys, "platform", "darwin"), mock.patch.dict(os.environ, environ):
                    parts = server.production_parts()
                self.assertEqual(parts.slot.account, expected)
                self.assertEqual(parts.user, environ["USER"])


SETTINGS_SAMPLE = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "env": {"NAME": "René 中"},
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "afplay done.aiff"}]}]},
    "cleanupPeriodDays": 30,
    "enabledPlugins": {},
}
KEY_MARKER = "SESSION-KEY-MARKER-SECRET-0123456789"


class StatuslineTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.world.run(self.world.service())
        self.path = self.world.home / ".claude" / "settings.json"
        self.script = server.statusline_script()

    def write(self, data, indent=2, newline=True):
        text = json.dumps(data, indent=indent, ensure_ascii=False) if indent is not None else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        self.path.write_bytes((text + ("\n" if newline else "")).encode("utf-8"))
        os.chmod(self.path, 0o644)
        return self.path.read_bytes()

    def backups(self):
        return sorted(p for p in self.path.parent.iterdir() if p.name.startswith("settings.json.lifeline-backup-"))

    def state(self):
        return self.world.call("GET", "/state")[1]["statusline"]

    def test_turning_it_on_then_off_changes_only_our_entry_with_a_backup_each_time(self):
        before = self.write(SETTINGS_SAMPLE)
        self.assertEqual(self.state(), {"on": False, "ours": False, "other_present": False})
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["message"], "Lifeline's status line is on. Claude Code shows it from its next refresh.")
        self.assertEqual(body["statusline"], {"on": True, "ours": True, "other_present": False})
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(after), list(SETTINGS_SAMPLE) + ["statusLine"])
        self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, SETTINGS_SAMPLE)
        self.assertEqual(after["statusLine"], {"type": "command", "command": self.script, "refreshInterval": 5})
        self.assertEqual(self.path.read_text(encoding="utf-8"), json.dumps(after, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual([p.read_bytes() for p in self.backups()], [before])
        self.assertEqual(self.backups()[0].name, "settings.json.lifeline-backup-20261119-154100")
        self.assertEqual(self.state(), {"on": True, "ours": True, "other_present": False})
        status, body = self.world.call("POST", "/statusline", {"on": False})
        self.assertEqual((status, body["message"]), (200, "Lifeline's status line is off."))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual(self.backups()[1].name, "settings.json.lifeline-backup-20261119-154100-1")
        self.assertEqual(self.state(), {"on": False, "ours": False, "other_present": False})
        self.world.assert_clean()

    def test_the_line_reruns_every_5_seconds_and_nothing_else_in_the_file_moves(self):
        before = self.write(SETTINGS_SAMPLE)
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 200, body)
        entry = {"type": "command", "command": self.script, "refreshInterval": 5}
        self.assertEqual(server.STATUSLINE_REFRESH_SECONDS, 5)
        written = self.path.read_bytes()
        self.assertEqual(written, (json.dumps({**SETTINGS_SAMPLE, "statusLine": entry}, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        self.assertIn("René 中".encode("utf-8"), written)
        self.assertNotIn(b"\\u", written)
        self.assertTrue(written.startswith(before[: before.rindex(b"\n}")]))

    def test_an_older_install_gains_the_5_seconds_and_keeps_its_other_keys(self):
        old = {"type": "command", "command": self.script, "padding": 0}
        before = self.write({**SETTINGS_SAMPLE, "statusLine": old})
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 200, body)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(after["statusLine"]), ["type", "command", "padding", "refreshInterval"])
        self.assertEqual(after["statusLine"], {**old, "refreshInterval": 5})
        self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, SETTINGS_SAMPLE)
        self.assertEqual([p.read_bytes() for p in self.backups()], [before])

    def test_a_refresh_claude_code_would_ignore_is_replaced_with_5_seconds(self):
        for value in (0, None, "5", -1, True, 0.5):
            with self.subTest(value=value):
                self.write({**SETTINGS_SAMPLE, "statusLine": {"type": "command", "command": self.script, "refreshInterval": value}})
                status, body = self.world.call("POST", "/statusline", {"on": True})
                self.assertEqual(status, 200, body)
                after = json.loads(self.path.read_text(encoding="utf-8"))["statusLine"]
                self.assertEqual(after["refreshInterval"], 5)
        for value in (1, 10, 2.5):
            with self.subTest(value=value):
                before = self.write({**SETTINGS_SAMPLE, "statusLine": {"type": "command", "command": self.script, "refreshInterval": value}})
                self.world.call("POST", "/statusline", {"on": True})
                self.assertEqual(self.path.read_bytes(), before)

    def test_a_reinstall_keeps_the_refresh_it_finds(self):
        for seconds in (5, 10):
            with self.subTest(seconds=seconds):
                for old in self.backups():
                    old.unlink()
                before = self.write({**SETTINGS_SAMPLE, "statusLine": {"type": "command", "command": self.script, "refreshInterval": seconds}})
                status, body = self.world.call("POST", "/statusline", {"on": True})
                self.assertEqual(status, 200, body)
                self.assertEqual((self.path.read_bytes(), self.backups()), (before, []))
        moved = self.world.root / "older" / "bin" / "lifeline-statusline"
        moved.parent.mkdir(parents=True)
        moved.write_text("")
        (moved.parents[1] / "core" / "lifeline").mkdir(parents=True)
        (moved.parents[1] / "core" / "lifeline" / "statusline.py").write_text("")
        self.write({**SETTINGS_SAMPLE, "statusLine": {"type": "command", "command": str(moved), "refreshInterval": 10}})
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 200, body)
        after = json.loads(self.path.read_text(encoding="utf-8"))["statusLine"]
        self.assertEqual(after, {"type": "command", "command": self.script, "refreshInterval": 10})

    def test_the_devbox_install_writes_the_same_5_seconds(self):
        home = self.world.root / "devbox-home"
        (home / ".claude").mkdir(parents=True)
        self.assertEqual(server.change_statusline(home, True, START), server.STATUSLINE_ON)
        written = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertEqual(written["statusLine"]["refreshInterval"], 5)

    def test_asking_for_what_is_already_there_writes_nothing(self):
        self.write(SETTINGS_SAMPLE)
        self.world.call("POST", "/statusline", {"on": True})
        written = self.path.read_bytes()
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 200)
        self.assertEqual((self.path.read_bytes(), len(self.backups())), (written, 1))
        self.world.call("POST", "/statusline", {"on": False})
        status, body = self.world.call("POST", "/statusline", {"on": False})
        self.assertEqual((status, len(self.backups())), (200, 2))

    def test_your_own_status_line_is_never_replaced_or_removed(self):
        for entry in (
            {"type": "command", "command": "/usr/local/bin/my-line"},
            {"type": "command", "command": f"python3 {self.script}"},
            {"type": "command", "command": f"{self.script} --verbose"},
            {"type": "static", "command": self.script},
            "a string",
        ):
            with self.subTest(entry=entry):
                before = self.write({**SETTINGS_SAMPLE, "statusLine": entry})
                self.assertEqual(self.state(), {"on": True, "ours": False, "other_present": True})
                for on in (True, False):
                    status, body = self.world.call("POST", "/statusline", {"on": on})
                    self.assertEqual(
                        (status, body["error"]),
                        (409, "Claude Code shows your own status line, and Lifeline never replaces or removes it."),
                    )
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.backups(), [])

    def test_our_entry_through_a_link_or_quotes_still_counts_as_ours(self):
        link = self.world.root / "bin-link"
        link.symlink_to(self.script)
        for command in (str(link), f"'{self.script}'"):
            with self.subTest(command=command):
                self.write({**SETTINGS_SAMPLE, "statusLine": {"type": "command", "command": command, "padding": 0}})
                self.assertEqual(self.state(), {"on": True, "ours": True, "other_present": False})
                status, body = self.world.call("POST", "/statusline", {"on": False})
                self.assertEqual(status, 200)
                self.assertNotIn("statusLine", json.loads(self.path.read_text()))

    def test_indent_and_trailing_newline_are_kept(self):
        for indent, newline in (("\t", True), (4, False), (None, True), (None, False), (2, False)):
            with self.subTest(indent=indent, newline=newline):
                before = self.write(SETTINGS_SAMPLE, indent=indent, newline=newline)
                self.world.call("POST", "/statusline", {"on": True})
                self.world.call("POST", "/statusline", {"on": False})
                self.assertEqual(self.path.read_bytes(), before)

    def test_a_missing_file_is_created_and_a_missing_folder_is_refused(self):
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 200)
        self.assertEqual(
            self.path.read_text(), json.dumps({"statusLine": {"type": "command", "command": self.script, "refreshInterval": 5}}, indent=2) + "\n"
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.backups(), [])
        self.path.unlink()
        status, body = self.world.call("POST", "/statusline", {"on": False})
        self.assertEqual(status, 200)
        self.assertFalse(self.path.exists())
        shutil.rmtree(self.world.home / ".claude")
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual((status, body["error"]), (409, "Lifeline could not find Claude Code's settings folder, so it changed nothing."))

    def test_links_damage_and_bad_requests_are_refused_without_writing(self):
        real = self.world.root / "dotfiles-settings.json"
        real.write_text(json.dumps(SETTINGS_SAMPLE))
        self.path.symlink_to(real)
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual((status, body["error"]), (409, "Claude Code's settings file is a link, so Lifeline left it as it is."))
        self.assertTrue(self.path.is_symlink())
        self.path.unlink()
        before = self.write(SETTINGS_SAMPLE)
        twin = self.world.root / "twin.json"
        os.link(self.path, twin)
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 409)
        twin.unlink()
        self.path.write_bytes(b"{half written")
        status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual((status, body["error"]), (409, "Lifeline could not read Claude Code's settings file, so it changed nothing."))
        self.assertEqual(self.path.read_bytes(), b"{half written")
        self.assertEqual(self.state(), {"on": None, "ours": None, "other_present": None})
        self.write(SETTINGS_SAMPLE)
        for payload in ({}, {"on": "yes"}, {"on": 1}, {"on": True, "extra": 1}):
            with self.subTest(payload=payload):
                status, body = self.world.call("POST", "/statusline", payload)
                self.assertEqual((status, body["error"]), (400, "The status line setting takes on, as true or false."))
        self.assertEqual(self.backups(), [])
        self.assertEqual(list(self.path.parent.glob(".settings-*")), [])

    def test_a_file_changed_during_the_write_is_left_alone(self):
        self.write(SETTINGS_SAMPLE)
        with mock.patch.object(server, "_peek", return_value=b"{}\n"):
            status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(
            (status, body["error"]),
            (409, "Claude Code's settings file changed while Lifeline was updating it, so Lifeline wrote nothing."),
        )
        self.assertNotIn("statusLine", json.loads(self.path.read_text()))
        self.assertEqual(list(self.path.parent.glob(".settings-*")), [])

    def test_a_change_that_cannot_be_confirmed_names_the_backup(self):
        self.write(SETTINGS_SAMPLE)
        real_replace = os.replace

        def replace_then_other_writer(source, target):
            real_replace(source, target)
            if str(target).endswith("settings.json"):
                Path(target).write_text(json.dumps({"model": "other"}))

        with mock.patch.object(server.os, "replace", replace_then_other_writer):
            status, body = self.world.call("POST", "/statusline", {"on": True})
        self.assertEqual(status, 500)
        backup = self.backups()[0]
        self.assertEqual(
            body["error"],
            f"Lifeline changed the status line but could not confirm the rest of Claude Code's settings. A copy from before the change is at {backup}.",
        )

    def test_the_route_is_on_local_sockets_only(self):
        self.assertNotIn(("statusline", False), devbox.TCP_ROUTES)
        self.assertNotIn(("statusline", False), server.MAC_ROUTES)
        for routes in (None, devbox.LOCAL_ROUTES):
            built = server._Server(os.path.join(self.world.socket_dir, "x.sock"), routes)
            try:
                self.assertIn(("statusline", False), built.routes)
            finally:
                built.server_close()


class ServiceLogTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name).resolve() / "Logs" / "com.example.lifeline"
        self.logger = logging.getLogger("lifeline.logtest")
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.handler = server.private_log(self.folder)
        self.logger.addHandler(self.handler)
        self.addCleanup(self.handler.close)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def lines(self):
        return (self.folder / "lifeline.log").read_text(encoding="utf-8").splitlines()

    def test_the_log_is_private_plain_and_one_line_per_sentence(self):
        try:
            raise RuntimeError("a token " + MCP_SECRET)
        except RuntimeError:
            self.logger.warning("Lifeline could not finish its reading step (RuntimeError).", exc_info=True)
        self.logger.info("A line\nwith a break and a tab\tinside.")
        self.handler.flush()
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(" Lifeline could not finish its reading step (RuntimeError)."), lines[0])
        self.assertTrue(lines[1].endswith(" A line with a break and a tab inside."), lines[1])
        text = (self.folder / "lifeline.log").read_text()
        self.assertNotIn("Traceback", text)
        self.assertNotIn(MCP_SECRET, text)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder / "lifeline.log").st_mode), 0o600)

    def test_the_log_keeps_three_files_of_at_most_one_megabyte(self):
        sentence = "Lifeline read every account and found nothing to report this round. " * 20
        for _ in range(4000):
            self.logger.info(sentence)
        self.handler.flush()
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(names, ["lifeline.log", "lifeline.log.1", "lifeline.log.2"])
        for name in names:
            path = self.folder / name
            self.assertLessEqual(path.stat().st_size, server.LOG_BYTES)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_mac_service_writes_its_log_in_the_library_logs_folder(self):
        self.assertEqual(server.log_folder(Path("/home/x")), Path("/home/x/Library/Logs/com.example.lifeline"))


class BundledStatuslineTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.path = self.home / ".claude" / "settings.json"
        self.repo = self.tree(self.root / "projects" / "lifeline")
        self.app = self.tree(self.root / "Applications" / "Lifeline.app" / "Contents" / "Resources" / "core")
        self.moment = datetime(2026, 11, 19, 16, 0, tzinfo=UTC)

    def tree(self, root):
        (root / "core" / "lifeline").mkdir(parents=True)
        (root / "core" / "lifeline" / "statusline.py").write_text("")
        (root / "core" / "lifeline" / "server.py").write_text("")
        (root / "bin").mkdir()
        script = root / "bin" / "lifeline-statusline"
        script.write_text("")
        script.chmod(0o700)
        return root

    def source(self, root):
        return str(root / "core" / "lifeline" / "server.py")

    def write(self, command):
        data = dict(SETTINGS_SAMPLE)
        if command is not None:
            data["statusLine"] = {"type": "command", "command": command}
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        return self.path.read_bytes()

    def backups(self):
        return sorted(p for p in self.path.parent.iterdir() if p.name.startswith("settings.json.lifeline-backup-"))

    def test_a_moved_entry_keeps_its_other_fields(self):
        data = dict(SETTINGS_SAMPLE, statusLine={"type": "command", "command": str(self.repo / "bin" / "lifeline-statusline"), "padding": 2})
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        server.change_statusline(self.home, True, self.moment, self.source(self.app))
        after = json.loads(self.path.read_text())["statusLine"]
        self.assertEqual(after, {"type": "command", "command": str(self.app / "bin" / "lifeline-statusline"), "padding": 2, "refreshInterval": 5})

    def test_a_bundled_service_moves_the_repo_entry_to_its_own_script(self):
        repo_script = str(self.repo / "bin" / "lifeline-statusline")
        app_script = str(self.app / "bin" / "lifeline-statusline")
        before = self.write(repo_script)
        source = self.source(self.app)
        self.assertEqual(server.statusline_command(source), app_script)
        self.assertEqual(server.statusline_state(self.home, source), {"on": True, "ours": True, "other_present": False})
        self.assertEqual(server.change_statusline(self.home, True, self.moment, source), server.STATUSLINE_ON)
        after = json.loads(self.path.read_text())
        self.assertEqual(after["statusLine"], {"type": "command", "command": app_script, "refreshInterval": 5})
        self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, SETTINGS_SAMPLE)
        self.assertEqual(list(after), list(json.loads(before)))
        self.assertEqual([p.read_bytes() for p in self.backups()], [before])
        written = self.path.read_bytes()
        self.assertEqual(server.change_statusline(self.home, True, self.moment, source), server.STATUSLINE_ON)
        self.assertEqual((self.path.read_bytes(), len(self.backups())), (written, 1))

    def test_an_installed_app_entry_counts_as_ours_and_turns_off(self):
        app_script = str(self.app / "bin" / "lifeline-statusline")
        self.write(app_script)
        for source in (self.source(self.repo), self.source(self.app)):
            self.assertEqual(server.statusline_state(self.home, source)["ours"], True)
        gone = "/Applications/Lifeline.app/Contents/Resources/core/bin/lifeline-statusline"
        self.write(gone)
        self.assertEqual(server.statusline_state(self.home, self.source(self.repo))["ours"], True)
        self.write(app_script)
        self.assertEqual(server.change_statusline(self.home, False, self.moment, self.source(self.repo)), server.STATUSLINE_OFF)
        self.assertNotIn("statusLine", json.loads(self.path.read_text()))
        self.assertEqual(len(self.backups()), 1)

    def test_your_own_status_line_is_never_touched_from_a_bundle(self):
        stranger = self.root / "elsewhere" / "bin"
        stranger.mkdir(parents=True)
        (stranger / "lifeline-statusline").write_text("")
        lookalike = self.root / "Lifeline.app" / "Contents" / "Resources" / "bin" / "lifeline-statusline"
        for command in (
            "/usr/local/bin/my-line",
            str(stranger / "lifeline-statusline"),
            str(lookalike),
            f"python3 {self.app / 'bin' / 'lifeline-statusline'}",
            f"{self.repo / 'bin' / 'lifeline-statusline'} --verbose",
        ):
            with self.subTest(command=command):
                before = self.write(command)
                for source in (self.source(self.app), self.source(self.repo)):
                    self.assertEqual(server.statusline_state(self.home, source), {"on": True, "ours": False, "other_present": True})
                    for on in (True, False):
                        with self.assertRaises(server.Refusal) as caught:
                            server.change_statusline(self.home, on, self.moment, source)
                        self.assertEqual(caught.exception.sentence, server.OTHER_STATUSLINE)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.backups(), [])


class ProjectsTests(unittest.TestCase):
    def plant(self, world, pid, session, cwd, name, minutes_ago):
        folder = world.home / ".claude" / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        started = int((START - timedelta(minutes=minutes_ago)).timestamp() * 1000)
        body = {"pid": pid, "sessionId": session, "cwd": cwd, "name": name, "startedAt": started, "kind": "interactive"}
        (folder / f"{session}.json").write_text(json.dumps(body))
        key = folder / f"{session}.0123456789abcdef.key"
        key.write_text(KEY_MARKER)
        os.chmod(key, 0o600)
        return key

    def test_projects_follow_each_sessions_readings_and_never_guess(self):
        world = World(self)
        world.usage_reading(ALEX, 60.0, R_ALEX)
        world.usage_reading(WORK, 12.0, R_WORK)
        service = world.service()
        key = self.plant(world, os.getpid(), "s-alex", "/work/lifeline", "lifeline-build", 30)
        self.plant(world, os.getpid(), "s-work", "/work/site", "site", 10)
        self.plant(world, os.getpid(), "s-new", "/work/new", None, 5)
        self.plant(world, os.getpid(), "s-stranger", "/work/other", "other", 7)
        self.plant(world, 2**22 + 12345, "s-ended", "/work/old", "old", 60)
        (world.home / ".claude" / "sessions" / "evil.json").symlink_to(key)
        service.take_reading(statusline_body("s-alex", ALEX, R_ALEX))
        service.take_reading(statusline_body("s-work", WORK, R_WORK))
        with service._state:
            service._sessions["s-stranger"] = (NEWCOMER, START)
        opened = []
        real_os_open, real_io_open = os.open, io.open

        def spy_os_open(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_os_open(path, *args, **kwargs)

        def spy_io_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_io_open(path, *args, **kwargs)

        with mock.patch("os.open", spy_os_open), mock.patch("io.open", spy_io_open), mock.patch("builtins.open", spy_io_open):
            state = service.state()
        self.assertFalse([path for path in opened if path.endswith(".key") or path.endswith("evil.json")])
        by_uuid = {row["uuid"]: row for row in state["accounts"]}
        self.assertEqual(by_uuid[ALEX]["projects"], [{"cwd": "/work/lifeline", "name": "lifeline-build", "started_at": iso(START - timedelta(minutes=30))}])
        self.assertEqual(by_uuid[WORK]["projects"], [{"cwd": "/work/site", "name": "site", "started_at": iso(START - timedelta(minutes=10))}])
        self.assertEqual(by_uuid[PERSONAL]["projects"], [])
        self.assertEqual(
            state["projects_unknown"],
            [
                {"cwd": "/work/other", "name": "other", "started_at": iso(START - timedelta(minutes=7))},
                {"cwd": "/work/new", "name": None, "started_at": iso(START - timedelta(minutes=5))},
            ],
        )
        world.run(service)
        status, body = world.call("GET", "/state")
        self.assertEqual(status, 200)
        self.assertNotIn(KEY_MARKER, json.dumps(body))
        self.assertEqual(body["accounts"][0]["projects"][0]["cwd"], "/work/lifeline")
        world.assert_clean()

    def test_no_sessions_folder_means_empty_lists(self):
        world = World(self)
        state = world.service().state()
        self.assertEqual(state["projects_unknown"], [])
        self.assertTrue(all(row["projects"] == [] for row in state["accounts"]))

    def test_odd_session_files_are_skipped(self):
        world = World(self)
        folder = world.home / ".claude" / "sessions"
        folder.mkdir(parents=True)
        (folder / "bad.json").write_text("{nope")
        (folder / "list.json").write_text("[1]")
        (folder / "zero.json").write_text(json.dumps({"pid": 0, "sessionId": "z"}))
        (folder / "huge.json").write_text(json.dumps({"pid": 2**40, "sessionId": "h"}))
        (folder / "dir.json").mkdir()
        os.mkfifo(folder / "pipe.json")
        (folder / "ctl.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "c", "cwd": "/x\ny", "name": "\x1b[31m", "startedAt": True}))
        state = world.service().state()
        self.assertEqual(state["projects_unknown"], [{"cwd": "/x?y", "name": "?[31m", "started_at": None}])


class FullLink(FakeLink):
    def __init__(self):
        super().__init__()
        self.status = {"covers": list(ORDER), "statuses": {}, "reports": [], "sessions": 0, "signin_ends": {}}
        self.pushed = []
        self.known_pushed = []
        self.push_result = True
        self.readings_pushed = []
        self.readings_result = True

    def devbox_status(self):
        return copy.deepcopy(self.status)

    def push_registry(self, accounts, known=None):
        self.pushed.append(copy.deepcopy(accounts))
        self.known_pushed.append(copy.deepcopy(known))
        return self.push_result

    def push_readings(self, readings):
        self.readings_pushed.append(list(readings))
        return self.readings_result




class DevboxCoverageTests(unittest.TestCase):
    def linked(self, **options):
        world = World(self, **options)
        world.link = FullLink()
        world.link.up = True
        world.link.devbox = WORK
        world.link.readings = world.devbox_readings()
        return world

    def away(self, service):
        service.set_presence({"idle_seconds": 700, "locked": False, "asleep": False})

    def test_the_mac_reads_every_account_the_devbox_does_not_cover(self):
        world = self.linked()
        world.link.status["covers"] = [WORK, ORG]
        world.link.readings = [r for r in world.devbox_readings() if r.uuid in (WORK, ORG)]
        service = world.service()
        service.tick()
        self.assertEqual(sorted(world.usage.calls), sorted([ALEX, PERSONAL]))
        self.assertEqual(world.history.latest(ORG).source, server.DEVBOX_SOURCE)
        world.clock.advance(seconds=301)
        world.link.status["covers"] = list(ORDER)
        world.link.readings = world.devbox_readings()
        service.tick()
        self.assertEqual(sorted(world.usage.calls), sorted([ALEX, PERSONAL]))
        world.link.status["covers"] = [WORK]
        world.clock.advance(seconds=200)
        service.tick()
        self.assertEqual(sorted(world.usage.calls), sorted([ALEX, PERSONAL]))
        world.clock.advance(seconds=101)
        service.tick()
        self.assertEqual(sorted(world.usage.calls), sorted([ALEX, PERSONAL, ALEX, PERSONAL, ORG]))

    def test_a_link_without_coverage_counts_as_covering_every_account(self):
        world = World(self)
        world.link.up = True
        world.link.readings = [Reading(ORG, "usage", 7.0, R_ALEX, 9.0, WEEK, START)]
        service = world.service()
        service.tick()
        self.assertEqual(world.usage.calls, [])
        damaged = self.linked()
        damaged.link.status["covers"] = None
        damaged.link.readings = [Reading(ORG, "usage", 7.0, R_ALEX, 9.0, WEEK, START)]
        other = damaged.service()
        other.tick()
        self.assertEqual(damaged.usage.calls, [])

    def test_a_covered_account_shows_the_devbox_status(self):
        world = self.linked()
        paused = START + timedelta(minutes=30)
        waiting = "The usage address asked Lifeline to wait before it reads again."
        world.link.status["statuses"] = {WORK: AccountStatus(paused_until=paused, last_error=waiting)}
        service = world.service()
        service.tick()
        state = service.state()
        work = state["accounts"][1]
        self.assertEqual((work["paused_until"], work["reading_error"]), (iso(paused), waiting))
        self.assertIn("Anthropic asked Lifeline to wait, so readings are paused until today at 16:11.", state["dev"]["sub"])
        self.assertNotIn("address", state["dev"]["sub"])
        world.link.up = False
        service.tick()
        work = service.state()["accounts"][1]
        self.assertEqual((work["paused_until"], work["reading_error"]), (None, None))

    def test_a_future_dated_devbox_reading_is_saved_once(self):
        world = World(self)
        world.link.up = True
        world.link.readings = [Reading(ORG, "usage", 7.0, R_ALEX, 9.0, WEEK, START + timedelta(minutes=10))]
        service = world.service()
        for _ in range(3):
            service.tick()
            world.clock.advance(seconds=60)
        rows = [r for r in world.history.readings(ORG, START - timedelta(hours=1)) if r.source == "usage"]
        self.assertEqual([(r.five, r.at) for r in rows], [(7.0, START)])
        world.link.readings.append(Reading(ORG, "usage", 8.0, R_ALEX, 9.0, WEEK, START + timedelta(minutes=11)))
        service.tick()
        service.tick()
        rows = [r for r in world.history.readings(ORG, START - timedelta(hours=1)) if r.source == "usage"]
        self.assertEqual([(r.five, r.at) for r in rows], [(7.0, START), (8.0, START + timedelta(seconds=180))])

    def test_the_registry_is_pushed_at_start_and_on_every_change(self):
        world = self.linked()
        service = world.service()
        service.tick()
        self.assertEqual(len(world.link.pushed), 1)
        first = world.link.pushed[0]
        self.assertEqual([item["uuid"] for item in first], list(ORDER))
        self.assertEqual(
            first[0],
            {
                "uuid": ALEX,
                "number": 1,
                "name": "Alex's Account",
                "short": "Alex",
                "full": "Alex's Account",
                "email": "11111111@example.invalid",
                "plan": "Max 5x",
                "login_uuid": ALEX,
                "org_uuid": pair_of(ALEX)[1],
            },
        )
        service.tick()
        self.assertEqual(len(world.link.pushed), 1)
        service.rename(WORK, {"name": "Command Account", "short": "Work"})
        service.tick()
        self.assertEqual(len(world.link.pushed), 2)
        self.assertEqual((world.link.pushed[-1][1]["name"], world.link.pushed[-1][1]["full"]), ("Command Account", "Example Team's Account"))
        world.link.push_result = False
        service.rename(WORK, {"name": "Command Account", "short": "Command"})
        service.tick()
        service.tick()
        self.assertEqual(len(world.link.pushed), 4)
        world.link.push_result = True
        service.tick()
        service.tick()
        self.assertEqual(len(world.link.pushed), 5)
        world.link.up = False
        service.rename(WORK, {"name": "Office Account", "short": "Office"})
        service.tick()
        self.assertEqual(len(world.link.pushed), 5)
        text = json.dumps(world.link.pushed)
        for secret in world.secrets():
            self.assertNotIn(secret, text)

    def test_devbox_reports_and_sessions_reach_the_mac(self):
        world = self.linked()
        world.link.status["reports"] = [
            {"id": "report-0123456789abcdef", "title": "Lifeline could not send an alert by email.", "at": iso(START)},
            {"id": "../etc", "title": "Bad id.", "at": iso(START)},
            {"id": "report-1", "title": "Two\nlines.", "at": iso(START)},
        ]
        world.link.status["sessions"] = 3
        service = world.service()
        service.tick()
        service.tick()
        state = service.state()
        found = [n for n in state["notifications"] if n["id"].startswith("devbox-")]
        self.assertEqual([(n["id"], n["title"], n["body"]) for n in found], [
            ("devbox-report-0123456789abcdef", "Lifeline could not send an alert by email.", "This came from Lifeline on the other computer.")
        ])
        self.assertEqual(state["sessions"]["dev"], {"open": 3, "behind": 0})
        self.assertEqual(state["dev"]["sessions"], "3 sessions open")

    def test_the_devbox_signin_ends_reach_the_state(self):
        world = self.linked()
        world.link.status["signin_ends"] = {WORK: "2026-12-10T08:00:00Z", ORG: "not a time"}
        service = world.service()
        service.tick()
        accounts = service.state()["accounts"]
        self.assertEqual(accounts[1]["devbox_signin_ends"], "2026-12-10T08:00:00Z")
        self.assertIsNone(accounts[3]["devbox_signin_ends"])
        self.assertIsNone(accounts[0]["devbox_signin_ends"])
        self.assertNotIn("devbox_signed_in_at", accounts[1])




class LinkedServerTests(unittest.TestCase):
    def linked(self, **options):
        world = World(self, **options)
        world.link = FullLink()
        world.link.up = True
        world.link.devbox = WORK
        world.link.readings = world.devbox_readings()
        return world

    def present(self, service):
        service.set_presence({"idle_seconds": 5, "locked": False, "asleep": False})

    def away(self, service):
        service.set_presence({"idle_seconds": 700, "locked": False, "asleep": False})

    def notes(self, service):
        return {n["id"]: n for n in service.state()["notifications"]}

    def test_the_signin_end_comes_from_the_signin_item_first(self):
        world = World(self)
        world.registry.upsert(Account(**dict(vars(account(WORK, 2, "2026-11-01T08:30:00Z")), signin_ends="2026-11-28T10:00:00Z")))
        world.registry.upsert(account(PERSONAL, 3, "2026-11-01T08:30:00Z"))
        state = world.service().state()
        self.assertEqual(state["accounts"][1]["signin_ends"], "2026-11-28T10:00:00Z")
        self.assertEqual(state["accounts"][2]["signin_ends"], "2026-12-01T08:30:00Z")
        self.assertIsNone(state["accounts"][0]["signin_ends"])

    def test_the_macs_signin_end_alert_key_names_the_machine(self):
        world = World(self)
        world.registry.upsert(Account(**dict(vars(account(ALEX, 1)), signin_ends=iso(START + timedelta(days=2)))))
        service = world.service()
        self.present(service)
        service.tick()
        note = self.notes(service)["signin-ends:mac:{}:2026-11-21".format(ALEX)]
        self.assertEqual(note["title"], "A sign-in ends in 2 days")
        self.assertEqual(note["body"], "Alex's Account on this Mac needs a new sign-in by Saturday at 15:41. Press Sign in again, then Authorize in your browser.")

    def test_later_on_a_signin_warning_reminds_once_a_day_before_even_after_a_restart(self):
        world = World(self)
        ends = START + timedelta(days=2)
        world.registry.upsert(Account(**dict(vars(account(ALEX, 1)), signin_ends=iso(ends))))
        service = world.service()
        self.present(service)
        service.tick()
        key = "signin-ends:mac:{}:2026-11-21".format(ALEX)
        self.assertIn(key, self.notes(service))
        self.assertEqual(service.later("later", {"id": key}), {"reminder": iso(ends - timedelta(days=1)), "message": "Lifeline reminds you again tomorrow at 15:41."})
        self.assertEqual(service.later("later", {"id": "limit:" + ALEX}), {"reminder": None, "message": None})
        for bad in ({}, {"id": 3}, {"id": key, "more": 1}):
            with self.assertRaises(server.Refusal) as caught:
                service.later("later", bad)
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(server.Refusal) as caught:
            service.later("sooner", {"id": key})
        self.assertEqual(caught.exception.status, 404)
        again = world.service()
        self.present(again)
        world.clock.advance(days=1, minutes=-1)
        again.tick()
        self.assertNotIn(key + ":reminder", self.notes(again))
        world.clock.advance(minutes=1)
        self.present(again)
        again.tick()
        reminder = self.notes(again)[key + ":reminder"]
        self.assertEqual(reminder["title"], "A sign-in ends tomorrow")
        world.clock.advance(minutes=5)
        again.tick()
        self.assertEqual(len([n for n in again.state()["notifications"] if n["id"].endswith(":reminder")]), 1)
        self.assertTrue(world.history.event_at(key + ":reminder") is not None)

    def later_world(self, ends):
        world = World(self)
        world.registry.upsert(Account(**dict(vars(account(ALEX, 1)), signin_ends=iso(ends))))
        service = world.service()
        self.present(service)
        service.tick()
        return world, service

    def event_rows(self, world):
        import sqlite3

        with sqlite3.connect(world.history.path) as conn:
            return sorted(row[0] for row in conn.execute("select key from events"))

    def test_later_changes_nothing_for_an_id_that_is_not_a_warning_sent(self):
        nothing = {"reminder": None, "message": None}
        world, service = self.later_world(START + timedelta(days=2))
        before = self.event_rows(world)
        mine = "signin-ends:mac:{}:".format(ALEX)
        for ident in ("signin-ends:mac", mine + "2026-11-20", mine + "junk", mine + "2026-11-21:reminder", "signin-ends:q:{}:2026-11-21".format(ALEX),
                      "signin-ends:mac:{}:2026-11-21".format(WORK), "signin-ends:mac:{}:2026-11-21".format(ALEX[:-1] + "9")):
            with self.subTest(ident):
                self.assertEqual(service.later("later", {"id": ident}), nothing)
        self.assertEqual(self.event_rows(world), before)
        quiet, unsent = self.later_world(START + timedelta(days=5))
        before = self.event_rows(quiet)
        self.assertEqual(unsent.later("later", {"id": "signin-ends:mac:{}:2026-11-24".format(ALEX)}), nothing)
        self.assertEqual(self.event_rows(quiet), before)

    def test_later_gives_nothing_once_the_sign_in_has_ended_or_is_about_to(self):
        nothing = {"reminder": None, "message": None}
        world, service = self.later_world(START + timedelta(hours=2))
        key = "signin-ends:mac:{}:2026-11-19".format(ALEX)
        self.assertIn(key, self.notes(service))
        world.clock.advance(hours=1, minutes=30)
        before = self.event_rows(world)
        self.assertEqual(service.later("later", {"id": key}), nothing)
        world.clock.advance(hours=1)
        self.assertEqual(service.later("later", {"id": key}), nothing)
        self.assertEqual(self.event_rows(world), before)

    def test_a_second_later_after_the_reminder_time_gives_nothing(self):
        world, service = self.later_world(START + timedelta(days=2))
        key = "signin-ends:mac:{}:2026-11-21".format(ALEX)
        self.assertEqual(service.later("later", {"id": key})["reminder"], iso(START + timedelta(days=1)))
        world.clock.advance(hours=25)
        self.assertEqual(service.later("later", {"id": key}), {"reminder": None, "message": None})

    def test_later_works_for_a_devbox_sign_in(self):
        world = self.linked()
        world.link.status["signin_ends"] = {WORK: iso(START + timedelta(days=2))}
        service = world.service()
        service.update_settings({"auto_switch": False})
        self.present(service)
        service.tick()
        key = "signin-ends:devbox:{}:2026-11-21".format(WORK)
        self.assertIn(key, self.notes(service))
        self.assertEqual(service.later("later", {"id": key}), {"reminder": iso(START + timedelta(days=1)), "message": "Lifeline reminds you again tomorrow at 15:41."})
        self.assertIsNotNone(world.history.event_at("signin-ends:devbox:{}:2026-11-21:later".format(WORK)))

    def test_later_is_reachable_through_the_menu_socket(self):
        world = World(self)
        world.run(world.service())
        self.assertEqual(world.call("POST", "/notifications/later", {"id": "limit:" + ALEX}), (200, {"reminder": None, "message": None}))
        self.assertEqual(world.call("POST", "/notifications/sooner", {"id": "limit:" + ALEX})[0], 404)
        self.assertEqual(world.call("POST", "/notifications/later", {"id": 7})[0], 400)

    def test_the_mac_shows_the_devbox_signin_end_while_you_are_present(self):
        world = self.linked()
        world.link.status["signin_ends"] = {WORK: iso(START + timedelta(days=2))}
        service = world.service()
        service.update_settings({"auto_switch": False})
        self.present(service)
        service.tick()
        key = "signin-ends:devbox:{}:2026-11-21".format(WORK)
        note = self.notes(service)[key]
        self.assertEqual(note["title"], "A sign-in ends in 2 days")
        self.assertEqual(note["body"], "Work Account on the other computer needs a new sign-in by Saturday at 15:41. Press Sign in again, then Authorize in your browser.")

    def test_devbox_signin_trouble_reaches_the_state_the_dev_view_and_the_light(self):
        world = self.linked()
        world.link.devbox = ORG
        world.link.status["covers"] = [WORK]
        world.link.status["statuses"] = {
            ORG: AccountStatus(needs_signin=True, last_error="The sign-in was refused and needs to be replaced."),
            PERSONAL: AccountStatus(cannot_read=True),
            WORK: AccountStatus(),
        }
        service = world.service()
        service.tick()
        state = service.state()
        rows = {a["uuid"]: a for a in state["accounts"]}
        self.assertEqual((rows[ORG]["devbox_needs_signin"], rows[ORG]["devbox_cannot_read"]), (True, False))
        self.assertEqual((rows[PERSONAL]["devbox_needs_signin"], rows[PERSONAL]["devbox_cannot_read"]), (False, True))
        self.assertEqual((rows[ALEX]["devbox_needs_signin"], rows[ALEX]["devbox_cannot_read"]), (None, None))
        self.assertFalse(rows[ORG]["needs_signin"])
        dev_rows = {r["uuid"]: r for r in state["dev"]["rows"]}
        mac_rows = {r["uuid"]: r for r in state["mac"]["rows"]}
        self.assertEqual(dev_rows[ORG]["where"], "Needs a new sign-in.")
        self.assertNotEqual(mac_rows[ORG]["where"], "Needs a new sign-in.")
        self.assertEqual(state["dev"]["head"], "Team Account needs a new sign-in.")
        self.assertTrue(state["dev"]["icon_dot"])
        self.assertTrue(state["mac"]["icon_dot"])
        world.link.status["statuses"] = {ORG: AccountStatus()}
        service.tick()
        self.assertFalse(service.state()["mac"]["icon_dot"])
        world.link.status["signin_ends"] = {ORG: iso(START + timedelta(days=1))}
        service.tick()
        self.assertTrue(service.state()["mac"]["icon_dot"])

    def test_devbox_reports_are_not_replayed_after_a_restart(self):
        world = self.linked()
        world.link.status["reports"] = [{"id": "report-00000000000000a1", "title": "First note.", "at": iso(START)}]
        world.service().tick()
        restarted = world.service()
        restarted.tick()
        self.assertFalse([n for n in restarted.state()["notifications"] if n["id"].startswith("devbox-")])
        world.link.status["reports"].append({"id": "report-00000000000000a2", "title": "Second note.", "at": iso(START + timedelta(seconds=1))})
        restarted.tick()
        restarted.tick()
        found = [n["title"] for n in restarted.state()["notifications"] if n["id"].startswith("devbox-")]
        self.assertEqual(found, ["Second note."])
        self.assertEqual(stat.S_IMODE(os.stat(world.folder / "devbox_reports.json").st_mode), 0o600)

    def test_the_registry_is_pushed_again_after_the_devbox_comes_back(self):
        world = self.linked()
        service = world.service()
        service.tick()
        service.tick()
        self.assertEqual(len(world.link.pushed), 1)
        world.link.up = False
        service.tick()
        world.link.up = True
        service.tick()
        service.tick()
        self.assertEqual(len(world.link.pushed), 2)

    def test_the_mac_pushes_its_own_readings_for_what_the_devbox_does_not_cover(self):
        world = self.linked()
        world.link.status["covers"] = [WORK, ORG]
        world.link.readings = [r for r in world.devbox_readings() if r.uuid in (WORK, ORG)]
        service = world.service()
        service.tick()
        pushed = [r for batch in world.link.readings_pushed for r in batch]
        self.assertEqual(sorted(r.uuid for r in pushed), sorted([ALEX, PERSONAL]))
        service.tick()
        self.assertEqual(sum(len(batch) for batch in world.link.readings_pushed), 2)
        world.clock.advance(seconds=301)
        world.link.readings_result = False
        service.tick()
        world.link.readings_result = True
        service.tick()
        self.assertEqual([sorted(r.uuid for r in batch) for batch in world.link.readings_pushed[-2:]], [sorted([ALEX, PERSONAL])] * 2)
        service.tick()
        self.assertEqual(sum(len(batch) for batch in world.link.readings_pushed), 6)
        for _ in range(250):
            world.history.add_reading(ALEX, "statusline", 50.0, R_ALEX, 20.0, WEEK, world.clock.now)
            world.clock.advance(seconds=1)
        service.tick()
        self.assertLessEqual(len(world.link.readings_pushed[-1]), 200)

    def test_a_reading_the_mac_already_holds_is_not_saved_again(self):
        world = self.linked()
        service = world.service()
        service.tick()
        mine = START + timedelta(seconds=60, milliseconds=250)
        world.clock.advance(seconds=120)
        world.history.add_reading(ALEX, "statusline", 43.0, R_ALEX, 18.0, WEEK, mine)
        world.link.readings = [Reading(ALEX, "statusline", 43.0, R_ALEX, 18.0, WEEK, mine)]
        service.tick()
        times = [r.at for r in world.history.readings(ALEX, START - timedelta(hours=1))]
        self.assertEqual(times.count(mine), 1)

    def test_adopt_runs_as_a_job(self):
        world = World(self)
        world.registry.save([account(uuid, ORDER.index(uuid) + 1) for uuid in (WORK, PERSONAL, ORG)])
        world.run(world.service())
        status, body = world.call("POST", "/adopt", {"name": "Alex's Account", "short": "Alex", "number": 1})
        self.assertEqual(status, 202)
        job = world.wait_job(body["job"])
        self.assertEqual((job["kind"], job["state"], job["uuid"]), ("adopt", "done", ALEX))
        self.assertEqual(job["result"], "Lifeline added Alex's Account as number 1. Claude Code uses it now.")
        self.assertEqual((job["suggested_name"], job["suggested_short"]), ("Alex's Account", "Alex"))
        self.assertEqual(world.registry.by_uuid(ALEX).key, 1)
        job = world.wait_job(world.call("POST", "/adopt", {})[1]["job"])
        self.assertEqual(job["result"], "Lifeline updated Alex's Account with the account details Claude Code holds now.")
        self.assertEqual((job["suggested_name"], job["suggested_short"]), (None, None))
        for payload in ({"email": "x@example.invalid"}, {"number": "one"}, {"name": ""}):
            status, body = world.call("POST", "/adopt", payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "Adding the account Claude Code uses takes a name, short name and number, each optional.")
        world.assert_clean()

    def test_history_is_pruned_once_a_day(self):
        world = World(self)
        service = world.service()

        def old():
            return [r.five for r in world.history.readings(ORG, START - timedelta(days=200)) if r.at < START - timedelta(days=80)]

        world.history.add_reading(ORG, "usage", 1.0, None, 1.0, None, START - timedelta(days=91))
        world.history.add_reading(ORG, "usage", 9.0, None, 1.0, None, START - timedelta(days=89))
        service.tick()
        self.assertEqual(old(), [9.0])
        world.history.add_reading(ORG, "usage", 2.0, None, 1.0, None, START - timedelta(days=91))
        world.clock.advance(hours=1)
        service.tick()
        self.assertEqual(old(), [2.0, 9.0])
        world.clock.advance(hours=23)
        service.tick()
        self.assertEqual(old(), [9.0])


class MacLinkWiringTests(unittest.TestCase):
    def test_the_service_links_only_to_a_paired_computer_kept_in_the_keychain(self):
        world = World(self)
        with mock.patch.object(server.sys, "platform", "darwin"):
            self.assertIsInstance(server.production_parts().link, server.NullLink)
        self.assertIsInstance(world.service().parts.link, type(world.link))
        server._write_private(world.folder / server.COMPUTER_NAME, {"name": "build-box", "address": "192.0.2.10:7999"})
        self.assertIsInstance(world.service().parts.link, type(world.link), "no key, no link")
        world.keychain.put("com.example.lifeline", {"key": "ab" * 32}, account=server.COMPUTER_ACCOUNT)
        link = world.service().parts.link
        self.assertIsInstance(link, devbox.HttpLink)
        self.assertEqual((link.host, link.port, link.timeout, link.key), ("192.0.2.10", 7999, 8.0, b"\xab" * 32))



NOT_ON_MAC = "This Mac did not switch, because Team Account is set to be used on the other computer only."
BAD_PLACEMENT = "Each account's place must be this Mac, the other computer or both."


class PlacementTests(unittest.TestCase):
    def placed(self, service, **places):
        uuids = {"alex": ALEX, "work": WORK, "personal": PERSONAL, "org": ORG}
        return service.update_settings({"placement": {uuids[name]: value for name, value in places.items()}})

    def test_every_account_is_placed_both_until_you_say_otherwise(self):
        world = World(self)
        world.run(world.service())
        settings = world.call("GET", "/state")[1]["settings"]
        self.assertEqual(settings["placement"], {ALEX: "both", WORK: "both", PERSONAL: "both", ORG: "both"})

    def test_placement_is_saved_per_account_and_survives_a_restart(self):
        world = World(self)
        world.run(world.service())
        status, body = world.call("POST", "/settings", {"placement": {ORG: "devbox"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["placement"], {ALEX: "both", WORK: "both", PERSONAL: "both", ORG: "devbox"})
        status, body = world.call("POST", "/settings", {"placement": {WORK: "mac"}})
        self.assertEqual(body["settings"]["placement"][ORG], "devbox")
        self.assertEqual(body["settings"]["placement"][WORK], "mac")
        saved = json.loads((world.folder / "settings.json").read_text())
        self.assertEqual(saved["placement"], {ORG: "devbox", WORK: "mac"})
        again = server.Service(world.parts()).state()["settings"]["placement"]
        self.assertEqual(again, {ALEX: "both", WORK: "mac", PERSONAL: "both", ORG: "devbox"})
        status, body = world.call("POST", "/settings", {"placement": {ORG: "both"}})
        self.assertEqual(body["settings"]["placement"][ORG], "both")

    def test_a_place_that_is_nowhere_or_unknown_is_never_saved(self):
        world = World(self)
        world.run(world.service())
        world.call("POST", "/settings", {"placement": {ORG: "devbox"}})
        before = (world.folder / "settings.json").read_bytes()
        for payload in (
            {"placement": {ORG: "nowhere"}},
            {"placement": {ORG: None}},
            {"placement": {ORG: ""}},
            {"placement": {ORG: "none"}},
            {"placement": {ORG: ["mac"]}},
            {"placement": {ORG: "mac", WORK: "elsewhere"}},
            {"placement": [ORG]},
            {"placement": "devbox"},
            {"placement": {"": "mac"}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(world.call("POST", "/settings", payload), (400, {"error": BAD_PLACEMENT}))
        self.assertEqual(world.call("POST", "/settings", {"placement": {"nobody": "mac"}}), (404, {"error": "That account is not in Lifeline."}))
        self.assertEqual(world.call("POST", "/settings", {"hide_names": True}), (400, {"error": "Lifeline has no setting called hide_names."}))
        self.assertEqual((world.folder / "settings.json").read_bytes(), before)
        self.assertEqual(world.call("GET", "/state")[1]["settings"]["placement"][WORK], "both")

    def test_a_damaged_place_in_the_file_falls_back_to_the_defaults(self):
        world = World(self)
        (world.folder / "settings.json").write_text(json.dumps({"auto_switch": False, "placement": {ORG: "nowhere"}}))
        os.chmod(world.folder / "settings.json", 0o600)
        settings = world.service().state()["settings"]
        self.assertEqual(settings["placement"][ORG], "both")
        self.assertTrue(settings["auto_switch"])

    def test_a_switch_to_a_devbox_only_account_is_refused_with_one_sentence(self):
        world = World(self)
        service = world.service()
        world.run(service)
        self.placed(service, org="devbox", work="mac")
        for body in ({"uuid": ORG}, {"number": 4}):
            self.assertEqual(world.call("POST", "/switch", body), (409, {"error": NOT_ON_MAC}))
        self.assertEqual(world.live_owner(), ALEX)
        status, body = world.call("POST", "/switch", {"uuid": WORK})
        self.assertEqual(status, 200, body)
        self.assertEqual(world.live_owner(), WORK)
        status, body = world.call("POST", "/switch", {"uuid": PERSONAL})
        self.assertEqual(status, 200, body)
        self.placed(service, work="devbox")
        self.assertEqual(
            world.call("POST", "/undo"),
            (409, {"error": "This Mac did not switch, because Work Account is set to be used on the other computer only."}),
        )
        self.assertEqual(world.live_owner(), PERSONAL)
        world.assert_clean()

    def test_the_menu_and_its_notification_button_never_offer_a_devbox_only_account(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.tick()
        before = service.state()["mac"]
        self.assertEqual(before["action_target"], ORG)
        self.placed(service, org="devbox")
        mac = service.state()["mac"]
        self.assertEqual(mac["action_target"], PERSONAL)
        self.assertEqual([row["uuid"] for row in mac["rows"] if row["is_best"]], [PERSONAL])
        self.placed(service, work="devbox", personal="devbox")
        mac = service.state()["mac"]
        self.assertEqual(mac["action_kind"], "none")
        self.assertEqual([row["uuid"] for row in mac["rows"] if row["is_best"]], [])
        self.assertIn("The other accounts are set to be used on the other computer only.", mac["sub"])

    def test_auto_switch_skips_accounts_not_allowed_on_the_mac(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        self.placed(service, org="devbox")
        service.tick()
        self.assertEqual(service.state()["countdown"]["target_uuid"], PERSONAL)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), PERSONAL)
        nowhere = World(self, figures=LIMIT)
        service = nowhere.service()
        self.placed(service, work="devbox", personal="devbox", org="devbox")
        service.tick()
        self.assertIsNone(service.state()["countdown"])
        self.assertEqual(nowhere.timers, [])

    def test_a_place_changed_during_the_countdown_stops_that_switch(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        self.assertEqual(service.state()["countdown"]["target_uuid"], ORG)
        self.placed(service, org="devbox")
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ALEX)
        self.assertIn(
            f"{NOT_ON_MAC} This Mac is still on Alex's Account, which has used 100% of its 5 hours.",
            [n["title"] for n in service.state()["notifications"]],
        )

    def test_the_limit_alert_names_an_account_the_mac_can_use(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.update_settings({"auto_switch": False})
        self.placed(service, org="devbox")
        service.set_presence({"idle_seconds": 30, "locked": False, "asleep": False})
        service.tick()
        alerts = [n for n in service.state()["notifications"] if n["kind"] == "limit"]
        self.assertEqual(alerts[0]["body"], "Personal Account has the most room.")

    def test_a_back_offer_names_only_an_account_the_mac_can_use(self):
        world = World(self, live=WORK)
        now = world.clock.now
        server.write_last_switch(server.last_switch_path(world.folder), ALEX, WORK, now - timedelta(hours=2))
        world.history.add_reading(ALEX, "usage", 100.0, now - timedelta(minutes=10), 30.0, WEEK, now - timedelta(minutes=40))
        world.history.add_reading(ALEX, "usage", 0.0, None, 30.0, WEEK, now - timedelta(minutes=2))
        world.history.add_reading(WORK, "usage", 12.0, R_WORK, 50.0, WEEK, now - timedelta(minutes=2))
        service = world.service()
        mac = service.state()["mac"]
        self.assertEqual((mac["head"], mac["action_target"]), ("Alex's Account is back.", ALEX))
        self.assertEqual(mac["sub"], "It has all of its 5 hours again. This Mac stays on Work Account until you switch.")
        self.placed(service, alex="devbox")
        mac = service.state()["mac"]
        self.assertNotEqual(mac["head"], "Alex's Account is back.")
        self.assertNotEqual(mac["action_target"], ALEX)

    def test_an_early_reset_brings_the_account_back(self):
        world = World(self, live=WORK)
        now = world.clock.now
        server.write_last_switch(server.last_switch_path(world.folder), ALEX, WORK, now - timedelta(hours=2))
        world.history.add_reading(ALEX, "usage", 100.0, R_ALEX, 30.0, WEEK, now - timedelta(minutes=40))
        world.history.add_reading(ALEX, "usage", 2.0, R_ALEX, 30.0, WEEK, now - timedelta(minutes=5))
        world.history.add_reading(WORK, "usage", 12.0, R_WORK, 50.0, WEEK, now - timedelta(minutes=2))
        mac = world.service().state()["mac"]
        self.assertEqual((mac["head"], mac["action_target"]), ("Alex's Account is back.", ALEX))


DAMAGED_NOTE = "A copy of the sign-in for Team Account in Lifeline's recovery folder is damaged."


def gated_profile(world):
    gate = {"free": None}
    released = threading.Event()

    def profile(token):
        if gate["free"] is not None:
            if gate["free"] > 0:
                gate["free"] -= 1
            else:
                released.wait(10)
        return world.profiles(token)

    return profile, gate, released


def noted_switch(note):
    real = switching.switch

    def noted(*args, **kwargs):
        result = real(*args, **kwargs)
        return switching.SwitchResult(
            result.from_uuid,
            result.to_uuid,
            result.backup,
            result.leftover,
            result.confirmed,
            result.signin,
            note=note,
            proven=result.proven,
        )

    return mock.patch.object(switching, "switch", side_effect=noted)


class ReplyFirstTests(unittest.TestCase):
    def test_a_switch_reply_comes_before_the_check_and_the_outgoing_watch(self):
        world = World(self)
        profile, gate, released = gated_profile(world)
        service = world.service(profile_fn=profile)
        self.addCleanup(released.set)
        service.tick()
        outgoing = service.watcher
        self.assertTrue(outgoing is not None and outgoing.active)
        gate["free"] = 1
        started = time.monotonic()
        result = service.switch_to(WORK)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(world.live_owner(), WORK)
        self.assertIsNone(result.confirmed)
        self.assertEqual(service.switch_message(result), "This Mac now uses Work Account.")
        entry = switching.read_record_entry(world.record_path)
        self.assertEqual((entry["uuid"], entry["refresh_sha256"]), (WORK, None))
        self.assertTrue(outgoing.active)
        self.assertIn(outgoing, service._retiring)
        self.assertEqual(service.watcher.chosen_uuid, WORK)
        released.set()
        mark = switching.fingerprint(world.signins[WORK])
        self.assertTrue(world.wait_for(lambda: switching.read_record_entry(world.record_path)["refresh_sha256"] == mark))
        self.assertTrue(world.wait_for(lambda: not outgoing.active and outgoing not in service._retiring))
        service.stop()
        world.assert_clean()

    def test_the_switch_route_answers_while_the_check_waits(self):
        world = World(self)
        profile, gate, released = gated_profile(world)
        service = world.service(profile_fn=profile)
        self.addCleanup(released.set)
        world.run(service)
        service.tick()
        gate["free"] = 1
        started = time.monotonic()
        status, body = world.call("POST", "/switch", {"number": 2})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((status, body["to_uuid"], body["confirmed"]), (200, WORK, None))
        self.assertEqual(body["message"], "This Mac now uses Work Account.")
        released.set()

    def test_a_later_check_that_disagrees_is_reported_and_the_new_watch_lets_go(self):
        world = World(self)
        profile, gate, released = gated_profile(world)
        service = world.service(profile_fn=profile)
        self.addCleanup(released.set)
        service.tick()
        gate["free"] = 1
        service.switch_to(WORK)
        loaded = world.signins[WORK]
        world.owners[loaded["accessToken"]] = PERSONAL
        released.set()
        self.assertTrue(
            world.wait_for(lambda: server.DISAGREES in [n["title"] for n in service.state()["notifications"]])
        )
        self.assertIsNone(switching.read_record(world.record_path))
        self.assertTrue(world.wait_for(lambda: service._follow_ups_done()))
        self.assertIsNone(service.watcher._good)

    def test_a_check_that_cannot_reach_anthropic_is_reported(self):
        world = World(self)
        service = world.service()
        service.tick()

        def offline(token):
            raise RuntimeError("offline")

        service.parts.profile_fn = lambda token: world.profiles(token) if service.watcher.chosen_uuid == ALEX else offline(token)
        service.switch_to(WORK)
        self.assertTrue(world.wait_for(lambda: service._follow_ups_done()))
        titles = [n["title"] for n in service.state()["notifications"]]
        self.assertIn(server.UNCHECKED, titles)

    def test_stop_waits_for_the_check_after_a_switch(self):
        world = World(self)
        profile, gate, released = gated_profile(world)
        service = world.service(profile_fn=profile)
        service.tick()
        gate["free"] = 1
        service.switch_to(WORK)
        threading.Timer(0.3, released.set).start()
        service.stop()
        entry = switching.read_record_entry(world.record_path)
        self.assertEqual(entry["refresh_sha256"], switching.fingerprint(world.signins[WORK]))
        self.assertEqual(rescue_files(world, WORK), [])


class SwitchNoteTests(unittest.TestCase):
    def test_state_carries_the_last_switch_note(self):
        world = World(self)
        service = world.service()
        self.assertIsNone(service.state()["switch_note"])
        with noted_switch(DAMAGED_NOTE):
            service.switch_to(ORG)
        self.assertEqual(service.state()["switch_note"], DAMAGED_NOTE)
        service.undo()
        self.assertIsNone(service.state()["switch_note"])
        with noted_switch(DAMAGED_NOTE):
            service.undo()
        self.assertEqual(service.state()["switch_note"], DAMAGED_NOTE)

    def test_the_automatic_switch_report_carries_the_note(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        with noted_switch(DAMAGED_NOTE):
            world.timers[0].fire()
        self.assertEqual(world.live_owner(), ORG)
        state = service.state()
        self.assertIn(
            "Lifeline switched this Mac to Team Account because Alex's Account reached 99% of its 5 hours. " + DAMAGED_NOTE,
            [n["title"] for n in state["notifications"]],
        )
        self.assertEqual(state["switch_note"], DAMAGED_NOTE)


class SessionProofTests(unittest.TestCase):
    def test_the_switch_sentence_counts_sessions_by_their_readings_since_the_switch(self):
        world = World(self)
        world.usage_reading(ALEX, 60.0, R_ALEX)
        world.usage_reading(WORK, 12.0, R_WORK)
        service = world.service()
        world.sessions = {"s-before", "s-moved", "s-behind", "s-quiet"}
        for session in world.sessions:
            service.take_reading(statusline_body(session, ALEX, R_ALEX))
        world.clock.advance(seconds=30)
        service.switch_to(WORK)
        mac = service.state()["mac"]
        self.assertEqual(mac["head"], "This Mac now uses Work Account.")
        self.assertEqual(mac["sub"], "Undo takes you back to Alex's Account.")
        world.clock.advance(seconds=3)
        service.take_reading(statusline_body("s-moved", WORK, R_WORK))
        service.take_reading(statusline_body("s-behind", ALEX, R_ALEX))
        service.take_reading(statusline_body("s-closed", WORK, R_WORK))
        mac = service.state()["mac"]
        self.assertEqual(
            mac["sub"],
            "1 of your 4 open Claude Code sessions has moved so far. 1 open Claude Code session was still on Alex's Account at its last message. "
            "Undo takes you back to Alex's Account.",
        )
        self.assertEqual(mac["sessions"], "4 sessions open")


class FirstRunTests(unittest.TestCase):
    def test_an_account_lifeline_does_not_know_yet_is_named_as_such(self):
        world = World(self)
        world.registry.save([])
        os.unlink(world.record_path)
        service = world.service()
        service.tick()
        mac = service.state()["mac"]
        self.assertEqual(mac["head"], "This Mac uses an account Lifeline has not added yet.")
        self.assertIs(mac["live"]["adopt_ready"], True)
        self.assertEqual(
            (mac["action_kind"], mac["action_text"], mac["sub"]),
            ("adopt", "Add this account", "Lifeline adds the sign-in this Mac already has, so no browser opens."),
        )

    def test_a_signed_in_account_not_yet_identified_is_named_as_such(self):
        world = World(self)
        os.unlink(world.record_path)
        world.profiles = lambda token: (_ for _ in ()).throw(RuntimeError("offline"))
        service = world.service(profile_fn=world.profiles)
        service.tick()
        mac = service.state()["mac"]
        self.assertEqual(mac["head"], "Lifeline cannot tell yet which account Claude Code uses.")
        world.slot.item = None
        service._identity = None
        service.tick()
        self.assertEqual(service.state()["mac"]["head"], "No account is in use on this Mac.")


class DevboxAdoptStateTests(unittest.TestCase):
    def test_state_says_whether_the_devbox_took_over_its_live_account(self):
        world = World(self)
        world.link.up = True
        world.link.devbox = WORK
        service = world.service()
        self.assertIsNone(service.state()["devbox"]["live_registered"])
        world.link.live_registered = lambda: False
        service.tick()
        self.assertIs(service.state()["devbox"]["live_registered"], False)
        world.link.live_registered = lambda: True
        service.tick()
        self.assertIs(service.state()["devbox"]["live_registered"], True)
        world.link.up = False
        service.tick()
        self.assertIsNone(service.state()["devbox"]["live_registered"])


THURSDAY = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)


def week_only(session, account_uuid, week, reset=THURSDAY, at=START):
    return {
        "source": "statusline",
        "session_id": session,
        "account_uuid": account_uuid,
        "org_uuid": pair_of(account_uuid)[1] if account_uuid else None,
        "at": iso(at),
        "five_hour": None,
        "seven_day": {"used_percentage": week, "resets_at": epoch(reset)},
    }


class BindingTests(unittest.TestCase):
    def mixed(self, live=ALEX, personal=57.0, **extra):
        figures = dict(CALM)
        figures[PERSONAL] = (None, None, personal, THURSDAY)
        world = World(self, live=live, figures=figures)
        return world, world.service(**extra)

    def test_one_five_binding_and_one_week_binding_account_in_one_state(self):
        from lifeline.anthropic import Limit

        world, _ = self.mixed()
        real = world.usage

        def usage(token):
            found = real(token)
            if world.owners.get(token) != PERSONAL:
                return found
            scoped = Limit("weekly_scoped", "weekly", 0.0, THURSDAY, "Fable", None, "normal", False, {"allowance_share": 50})
            surface = Limit("weekly_scoped", "weekly", 5.0, THURSDAY, None, "Design", "normal", False)
            return Usage(found.five_hour, found.seven_day, fetched_at=found.fetched_at, limits=(scoped, surface))

        service = world.service(usage_fn=usage)
        service.tick()
        state = service.state()
        accounts = {a["uuid"]: a for a in state["accounts"]}
        self.assertEqual(accounts[ALEX]["binding"], "five")
        self.assertEqual((accounts[PERSONAL]["binding"], accounts[PERSONAL]["five"], accounts[PERSONAL]["week"]), ("week", None, 57.0))
        self.assertEqual(accounts[PERSONAL]["model_limits"], [{"model": "Fable", "week": 0.0, "week_reset": iso(THURSDAY)}])
        self.assertEqual(accounts[ALEX]["model_limits"], [])
        self.assertNotIn("scoped", accounts[PERSONAL])
        self.assertEqual((state["dev"]["binding"], state["mac"]["binding"]), ("", "five"))
        rows = {row["uuid"]: row for row in state["mac"]["rows"]}
        self.assertEqual((rows[PERSONAL]["binding"], rows[PERSONAL]["pct"], rows[PERSONAL]["week_pct"]), ("week", "57%", ""))
        self.assertEqual((rows[ALEX]["binding"], rows[ALEX]["week_pct"]), ("five", "Week 18%"))
        self.assertEqual(rows[PERSONAL]["where"], "Not in use. Resets next Thursday at 02:00.")
        self.assertEqual(state["mac"]["binding"], "five")

    def test_state_reports_the_build_it_was_started_from(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "build.txt").write_text("2.0.3 (abc1234)\nsecond line\n")
        world = World(self)
        with mock.patch.object(server, "CODE_ROOT", root):
            service = world.service()
        (root / "build.txt").write_text("changed later\n")
        self.assertEqual(service.state()["build"], "2.0.3 (abc1234)")
        (root / "build.txt").unlink()
        with mock.patch.object(server, "CODE_ROOT", root):
            missing = world.service()
        self.assertIn("build", missing.state())
        self.assertIsNone(missing.state()["build"])
        for bad in (b"", b"\n", b"\xff\xfe\n", ("x" * 300).encode()):
            (root / "build.txt").write_bytes(bad)
            self.assertIsNone(server.build_label(root), bad)

    def test_a_week_binding_mac_account_heads_the_menu_with_its_week(self):
        world, service = self.mixed(live=PERSONAL)
        service.tick()
        mac = service.state()["mac"]
        self.assertEqual((mac["head"], mac["sub"]), ("Personal Account lasts until its week resets next Thursday at 02:00.", ""))
        self.assertEqual((mac["binding"], mac["icon_binding"], mac["icon_pct"]), ("week", "week", "57%"))

    def test_auto_switch_starts_when_a_week_binding_account_reaches_100(self):
        world, service = self.mixed(live=PERSONAL, personal=100.0)
        service.tick()
        self.assertEqual(len(world.timers), 1)
        self.assertEqual(service.state()["countdown"]["target_uuid"], ORG)
        self.assertEqual(service.state()["mac"]["sub"], "Personal Account is out until next Thursday at 02:00.")
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ORG)

    def test_the_best_target_ranks_each_account_on_its_own_limit(self):
        figures = dict(CALM)
        figures[ALEX] = (100.0, R_ALEX, 20.0, WEEK)
        figures[WORK] = (40.0, R_WORK, 5.0, WEEK)
        figures[PERSONAL] = (None, None, 10.0, THURSDAY)
        world = World(self, registered=(ALEX, WORK, PERSONAL), figures=figures)
        service = world.service()
        service.tick()
        snap = service._snapshot(world.clock.now)
        self.assertEqual(service._best_target(snap), PERSONAL)
        figures[PERSONAL] = (None, None, 60.0, THURSDAY)
        world.figures = figures
        world.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(service._best_target(service._snapshot(world.clock.now)), WORK)

    def test_an_account_with_no_reading_is_never_the_target_and_says_so(self):
        figures = dict(CALM)
        figures[ALEX] = (100.0, R_ALEX, 20.0, WEEK)
        world = World(self, registered=(ALEX, ORG), figures=figures)
        service = world.service(usage_fn=lambda token: world.usage(token) if world.owners.get(token) == ALEX else (_ for _ in ()).throw(Unavailable("x")))
        service.tick()
        snap = service._snapshot(world.clock.now)
        self.assertIsNone(service._best_target(snap))
        world.registry.save([account(ALEX, 1), account(ORG, 4)])
        rows = {row["uuid"]: row for row in service.state()["mac"]["rows"]}
        self.assertEqual(rows[ORG]["pct"], "")
        bare = World(self, registered=(ALEX,), figures=figures)
        bare.history = History(bare.folder / "empty.db")
        quiet = bare.service(history=bare.history, usage_fn=lambda token: (_ for _ in ()).throw(Unavailable("x")))
        self.assertEqual(quiet.state()["mac"]["head"], "Lifeline has no reading for Alex's Account yet.")

    def test_a_week_only_status_line_reading_is_filed_by_its_week_reset(self):
        world, service = self.mixed()
        service.tick()
        self.assertEqual(service.take_reading(week_only("s-solo", ALEX, 58.0)), PERSONAL)
        latest = world.history.latest(PERSONAL)
        self.assertEqual((latest.source, latest.five, latest.week), ("statusline", None, 58.0))

    def test_the_week_forecast_is_worked_out_once_per_new_reading(self):
        world, service = self.mixed(live=PERSONAL, personal=60.0)
        world.usage_reading(PERSONAL, None, None, week=40.0, week_reset=THURSDAY, minutes_ago=60 * 5)
        world.usage_reading(PERSONAL, None, None, week=50.0, week_reset=THURSDAY, minutes_ago=60 * 2)
        service.tick()
        fresh = service._snapshot(world.clock.now)
        real = forecasting.forecast
        calls = []

        def counted(*args, **kwargs):
            calls.append(kwargs.get("value", "five"))
            return real(*args, **kwargs)

        with mock.patch.object(forecasting, "forecast", counted):
            first = service._snapshot(world.clock.now)
            world.clock.advance(seconds=30)
            second = service._snapshot(world.clock.now)
            self.assertEqual(len(calls), 0)
            world.usage_reading(PERSONAL, None, None, week=61.0, week_reset=THURSDAY, minutes_ago=0)
            third = service._snapshot(world.clock.now)
            self.assertEqual(len(calls), 1)
            service._snapshot(world.clock.now)
            self.assertEqual(len(calls), 1)
        rows = lambda snap: {r.account.uuid: r for r in snap.rows}[PERSONAL].forecast
        self.assertEqual(rows(first), rows(fresh))
        self.assertEqual(rows(second), rows(fresh))
        service._forecasts.clear()
        self.assertEqual(rows(third), rows(service._snapshot(world.clock.now)))
        self.assertIsNotNone(rows(third))

    def test_both_forecasts_are_kept_and_dropped_after_a_day_without_a_reading(self):
        figures = dict(CALM)
        figures[WORK] = (30.0, R_WORK, 50.0, WEEK)
        world = World(self, live=WORK, figures=figures)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.tick()
        service._snapshot(world.clock.now)
        real = forecasting.forecast
        calls = []

        def counted(*args, **kwargs):
            calls.append(kwargs.get("value", "five"))
            return real(*args, **kwargs)

        with mock.patch.object(forecasting, "forecast", counted):
            service._snapshot(world.clock.now)
            service._snapshot(world.clock.now)
            self.assertEqual(calls, [])
            world.clock.advance(hours=25)
            service._snapshot(world.clock.now)
            service._snapshot(world.clock.now)
            self.assertEqual(sorted(calls), ["week"] * 8)

    def test_a_kept_5_hour_forecast_always_equals_a_fresh_one(self):
        figures = dict(CALM)
        figures[WORK] = (19.6, R_WORK, 10.0, WEEK)
        world = World(self, live=WORK, figures=figures)
        for minutes_ago in range(15, 0, -1):
            world.usage_reading(WORK, round(0.1 * (15 - minutes_ago) ** 2, 1), R_WORK, week=10.0, week_reset=WEEK, minutes_ago=minutes_ago)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.tick()
        five = lambda: {r.account.uuid: r for r in service._snapshot(world.clock.now).rows}[WORK].forecast
        first = five()
        for minutes in (8, 12, 40):
            world.clock.advance(minutes=minutes - (world.clock.now - START).total_seconds() / 60)
            kept = five()
            service._forecasts.clear()
            self.assertEqual(kept, five(), minutes)
        self.assertNotEqual(first, kept)
        real = forecasting.forecast
        calls = []

        def counted(*args, **kwargs):
            if args[0] and args[0][-1].uuid == WORK:
                calls.append(kwargs.get("value", "five"))
            return real(*args, **kwargs)

        with mock.patch.object(forecasting, "forecast", counted):
            five()
            five()
        self.assertEqual(calls, ["five", "five"])

    def test_both_windows_full_are_out_until_the_week_resets_with_one_alert(self):
        figures = dict(CALM)
        figures[WORK] = (100.0, R_WORK, 100.0, THURSDAY)
        world = World(self, live=WORK, figures=figures)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        state = service.state()
        self.assertEqual((state["mac"]["head"], state["mac"]["binding"]), ("Work Account is out until next Thursday at 02:00.", "week"))
        limits = [n["title"] for n in state["notifications"] if n["kind"] == "limit"]
        self.assertEqual(limits, ["Work Account is out until next Thursday at 02:00"])

    def test_the_5_hours_running_out_first_lead_though_the_week_binds(self):
        figures = dict(CALM)
        figures[WORK] = (30.0, R_WORK, 50.0, WEEK)
        world = World(self, live=WORK, figures=figures)
        for i in range(15):
            world.usage_reading(WORK, 2.0 * i, R_WORK, week=50.0, week_reset=WEEK, minutes_ago=15 - i)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        state = service.state()
        mac = state["mac"]
        self.assertEqual(mac["head"], "Work Account runs out today at 16:16.")
        self.assertEqual(mac["sub"], "That is 2 hours 14 minutes before its 5 hours reset today at 18:30. Team Account has the most room.")
        self.assertEqual((mac["binding"], mac["hero_used"], mac["hero_tone"]), ("week", "28% of its 5 hours used", "low"))
        self.assertEqual((mac["display"], mac["second_window"], mac["second_w"]), ("five", "week", "50%"))
        self.assertEqual(mac["second_text"], "Its week is 50% used and resets on Tuesday at 09:00.")
        work = {a["uuid"]: a for a in state["accounts"]}[WORK]
        self.assertEqual(work["other_forecast"]["runs_out_at"], "2026-11-19T16:16:00Z")
        self.assertIsNone(work["forecast"]["runs_out_at"])
        self.assertIsNone({a["uuid"]: a for a in state["accounts"]}[ORG]["other_forecast"])
        alerts = [n for n in state["notifications"] if n["kind"] == "will_run_out"]
        self.assertEqual([n["title"] for n in alerts], ["Work Account runs out today at 16:16"])

    def test_a_run_out_time_that_has_passed_is_none_in_the_state(self):
        world, service = self.mixed(live=PERSONAL, personal=60.0)
        service.update_settings({"auto_switch": False})
        for i in range(10):
            world.usage_reading(PERSONAL, None, None, week=50.0 + i, week_reset=THURSDAY, minutes_ago=200 - 10 * i)
        service.tick()
        passed = forecasting.Forecast(180.0, world.clock.now - timedelta(minutes=5), 0.1, "recent")
        with mock.patch.object(forecasting, "forecast", lambda *args, **kwargs: passed):
            service._forecasts.clear()
            state = service.state()
        personal = {a["uuid"]: a for a in state["accounts"]}[PERSONAL]
        self.assertIsNone(personal["forecast"]["runs_out_at"])
        self.assertEqual(state["mac"]["head"], "Personal Account runs out now.")

    def test_a_week_binding_reading_at_100_wakes_the_countdown(self):
        world, service = self.mixed(live=PERSONAL)
        service.tick()
        service._wake.clear()
        self.assertEqual(service.take_reading(week_only("s-solo", PERSONAL, 100.0)), PERSONAL)
        self.assertTrue(service._wake.is_set())

    def test_alerts_for_a_week_binding_account_use_its_week(self):
        world, service = self.mixed(live=PERSONAL, personal=100.0)
        service.update_settings({"auto_switch": False})
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        alerts = [n for n in service.state()["notifications"] if n["kind"] == "limit"]
        self.assertEqual(alerts[0]["id"], "limit-week:%s:2026-11-19T02:00:00+00:00" % PERSONAL)
        self.assertEqual(alerts[0]["title"], "Personal Account is out until next Thursday at 02:00")

    def test_the_target_has_more_room_than_the_account_in_use(self):
        for live, personal, best in ((ALEX, 86.0, None), (PERSONAL, 83.0, ALEX)):
            with self.subTest(live=live):
                figures = {ALEX: (54.0, R_ALEX, 20.0, WEEK), PERSONAL: (personal, R_PERSONAL, 30.0, WEEK)}
                world = World(self, registered=(ALEX, PERSONAL), live=live, figures=figures)
                service = world.service()
                service.update_settings({"auto_switch": False})
                service.tick()
                self.assertEqual(service._best_target(service._snapshot(world.clock.now)), best)
                rows = {row["uuid"]: row["is_best"] for row in service.state()["mac"]["rows"]}
                self.assertEqual(rows, {ALEX: best == ALEX, PERSONAL: False})

    def test_a_five_account_with_a_full_week_is_never_the_best_target(self):
        figures = {
            ALEX: (100.0, R_ALEX, 24.0, WEEK),
            WORK: (0.0, R_WORK, 100.0, WEEK),
            PERSONAL: (21.0, R_PERSONAL, 56.0, WEEK),
        }
        world = World(self, registered=(ALEX, WORK, PERSONAL), figures=figures)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.tick()
        snap = service._snapshot(world.clock.now)
        self.assertEqual(service._best_target(snap), PERSONAL)
        rows = {row["uuid"]: row for row in service.state()["mac"]["rows"]}
        self.assertFalse(rows[WORK]["is_best"])
        self.assertTrue(rows[PERSONAL]["is_best"])
        self.assertEqual((rows[WORK]["binding"], rows[WORK]["pct"], rows[WORK]["week_pct"]), ("week", "100%", ""))
        self.assertEqual(rows[WORK]["where"], "Not in use. Back on Tuesday at 09:00.")
        accounts = {a["uuid"]: a for a in service.state()["accounts"]}
        self.assertEqual((accounts[WORK]["binding"], accounts[WORK]["five"], accounts[WORK]["week"]), ("week", 0.0, 100.0))
        world.figures = {ALEX: figures[ALEX], WORK: figures[WORK], PERSONAL: (100.0, R_PERSONAL, 56.0, WEEK)}
        world.clock.advance(minutes=6)
        service.tick()
        self.assertIsNone(service._best_target(service._snapshot(world.clock.now)))
        self.assertEqual(service.state()["mac"]["sub"], "No other account has room, and this one is back first.")

    def test_the_mac_account_out_for_its_week_says_so_and_switches(self):
        figures = dict(CALM)
        figures[WORK] = (10.0, R_WORK, 100.0, WEEK)
        world = World(self, live=WORK, figures=figures)
        service = world.service()
        service.tick()
        mac = service.state()["mac"]
        self.assertEqual(mac["sub"], "Work Account is out until Tuesday at 09:00.")
        self.assertEqual((mac["binding"], mac["hero_tone"], mac["hero_reset"]), ("week", "limit", "Back on Tuesday at 09:00"))
        self.assertEqual(len(world.timers), 1)
        self.assertEqual(service.state()["countdown"]["target_uuid"], ORG)
        world.timers[0].fire()
        self.assertEqual(world.live_owner(), ORG)

    def test_a_week_fuller_than_the_five_hours_binds_on_the_week(self):
        figures = dict(CALM)
        figures[WORK] = (10.0, R_WORK, 95.0, WEEK)
        world = World(self, live=WORK, figures=figures)
        service = world.service()
        service.update_settings({"auto_switch": False})
        service.tick()
        state = service.state()
        mac = state["mac"]
        self.assertEqual((mac["binding"], mac["icon_pct"], mac["hero_w"]), ("week", "95%", "95%"))
        self.assertEqual(mac["head"], "Work Account lasts until its week resets on Tuesday at 09:00.")
        self.assertEqual({a["uuid"]: a["binding"] for a in state["accounts"]}[WORK], "week")
        self.assertEqual(len(world.timers), 0)

    def test_status_line_readings_are_stored_at_most_once_a_minute(self):
        world = World(self, live=ALEX, figures=CALM)
        service = world.service()
        service.tick()
        for seconds, five in ((0, 50.0), (20, 51.0), (20, 52.0), (21, 53.0)):
            world.clock.advance(seconds=seconds)
            body = statusline_body("s-min", ALEX, R_ALEX, five=five, at=world.clock.now)
            self.assertEqual(service.take_reading(body), ALEX)
        rows = [r for r in world.history.readings(ALEX, START - timedelta(minutes=1)) if r.source == "statusline"]
        self.assertEqual([r.five for r in rows], [52.0, 53.0])

    def test_the_limit_alert_never_names_an_account_that_needs_a_new_sign_in(self):
        figures = {ALEX: (100.0, R_ALEX, 24.0, WEEK), WORK: (0.0, R_WORK, 0.0, WEEK)}
        world = World(self, registered=(ALEX, WORK), figures=figures)
        service = world.service()
        service.update_settings({"auto_switch": False, "alerts": {"limit": False}})
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        parked = world.store.get(WORK)
        world.owners.pop(parked["accessToken"])
        world.owners.pop(parked["refreshToken"])
        world.clock.advance(minutes=6)
        service.tick()
        self.assertTrue({a["uuid"]: a for a in service.state()["accounts"]}[WORK]["needs_signin"])
        service.update_settings({"alerts": {"limit": True}})
        world.clock.advance(minutes=1)
        service.set_presence({"idle_seconds": 1, "locked": False, "asleep": False})
        service.tick()
        limits = [n for n in service.state()["notifications"] if n["kind"] == "limit"]
        self.assertEqual([(n["title"], n["body"]) for n in limits], [("Alex's Account is out until today at 17:00", "Every other account needs a new sign-in.")])

    def test_a_stale_status_line_post_is_not_stored_and_gets_the_current_figures(self):
        world = World(self, live=ALEX, figures=CALM)
        service = world.service()
        service.tick()
        world.run(service)
        service._wake.clear()
        status, body = world.call("POST", "/reading", statusline_body("s-old", ALEX, R_ALEX, five=30.0, week=18.0))
        self.assertEqual(status, 200)
        self.assertEqual((body["saved"], body["account_uuid"], body["five"], body["week"]), (False, ALEX, 42.0, 18.0))
        self.assertEqual((body["five_reset"], body["week_reset"]), (iso(R_ALEX), iso(WEEK)))
        self.assertEqual([r for r in world.history.readings(ALEX, START - timedelta(hours=1)) if r.source == "statusline"], [])
        status, body = world.call("POST", "/reading", statusline_body("s-new", ALEX, R_ALEX, five=44.0, week=19.0))
        self.assertEqual((body["saved"], body["five"], body["week"]), (True, 44.0, 19.0))
        old_week = statusline_body("s-old", ALEX, R_ALEX, five=100.0, week=18.0)
        old_week["five_hour"]["resets_at"] = epoch(R_ALEX - timedelta(hours=5))
        status, body = world.call("POST", "/reading", old_week)
        self.assertFalse(body["saved"])
        self.assertFalse(service._wake.is_set())
        earlier_week = statusline_body("s-old", ALEX, R_ALEX, five=50.0, week=100.0)
        earlier_week["seven_day"]["resets_at"] = epoch(WEEK - timedelta(days=1))
        status, body = world.call("POST", "/reading", earlier_week)
        self.assertEqual((body["saved"], body["account_uuid"], body["week"]), (False, ALEX, 19.0))
        self.assertFalse(service._wake.is_set())
        self.assertEqual([r.five for r in world.history.readings(ALEX, START - timedelta(hours=1)) if r.source == "statusline"], [44.0])
        world.assert_clean()

    def test_an_early_reset_by_anthropic_starts_a_new_segment(self):
        world, service = self.mixed(live=PERSONAL, personal=81.0)
        service.update_settings({"auto_switch": False})
        for i in range(12):
            world.usage_reading(PERSONAL, None, None, week=70.0 + i, week_reset=THURSDAY, minutes_ago=130 - 10 * i)
        service.tick()
        before = {a["uuid"]: a for a in service.state()["accounts"]}[PERSONAL]
        self.assertEqual((before["week"], before["forecast"]["basis"]), (81.0, "recent"))
        world.figures[PERSONAL] = (None, None, 0.0, THURSDAY)
        world.clock.advance(minutes=6)
        service.tick()
        state = service.state()
        after = {a["uuid"]: a for a in state["accounts"]}[PERSONAL]
        self.assertEqual(after["week"], 0.0)
        self.assertEqual((after["forecast"]["at_reset_pct"], after["forecast"]["runs_out_at"]), (0.0, None))
        self.assertEqual(state["mac"]["head"], "Personal Account lasts until its week resets next Thursday at 02:00.")
        world.run(service)
        status, body = world.call("POST", "/reading", week_only("s-idle", PERSONAL, 81.0, at=world.clock.now))
        self.assertEqual((body["saved"], body["week"]), (False, 0.0))
        status, body = world.call("POST", "/reading", week_only("s-live", PERSONAL, 0.4, at=world.clock.now))
        self.assertEqual((body["saved"], body["week"]), (True, 0.4))
        world.assert_clean()

    def test_a_7_day_history_starting_inside_a_window_still_drops_a_stale_row(self):
        world, service = self.mixed(personal=40.0)
        earlier = THURSDAY - timedelta(days=7)
        ms = lambda moment: int(moment.timestamp() * 1000)
        rows = [
            (PERSONAL, "usage", None, None, 80.0, ms(earlier), ms(datetime(2026, 11, 12, 10, 0, tzinfo=UTC))),
            (PERSONAL, "statusline", None, None, 70.0, ms(earlier), ms(datetime(2026, 11, 12, 16, 0, tzinfo=UTC))),
            (PERSONAL, "usage", None, None, 85.0, ms(earlier), ms(datetime(2026, 11, 13, 10, 0, tzinfo=UTC))),
        ]
        import sqlite3

        with sqlite3.connect(world.history.path) as conn:
            conn.executemany("insert into readings (uuid, source, five, five_reset, week, week_reset, at) values (?,?,?,?,?,?,?)", rows)
        service.tick()
        world.run(service)
        status, body = world.call("GET", f"/history/{PERSONAL}")
        self.assertEqual(status, 200)
        self.assertEqual(body["daily"][0], {"date": "2026-11-13", "peak_five": 85.0, "hit_limit": False})
        self.assertEqual(len(body["readings"]), len([r for r in body["readings"] if r["week"] != 70.0]))
        world.assert_clean()

    def test_the_seven_day_sentence_names_past_days_by_the_day_rules(self):
        world, service = self.mixed(personal=100.0)
        world.usage_reading(PERSONAL, None, None, week=100.0, week_reset=THURSDAY, minutes_ago=2 * 60 * 24 + 30)
        world.usage_reading(PERSONAL, None, None, week=1.0, week_reset=THURSDAY, minutes_ago=60 * 24 + 60)
        world.usage_reading(PERSONAL, None, None, week=100.0, week_reset=THURSDAY, minutes_ago=60 * 24 + 30)
        body = service.history_for(PERSONAL, {})
        self.assertIn("It ran out of its week on Tuesday and yesterday.", body["summary"])
        today = World(self)
        today.usage_reading(WORK, 10.0, R_WORK, week=10.0, minutes_ago=2 * 60 * 24)
        today.usage_reading(WORK, 20.0, R_WORK, week=100.0, minutes_ago=30)
        body = today.service().history_for(WORK, {})
        self.assertIn("It ran out of its week today.", body["summary"])
        self.assertNotIn("Thursday", body["summary"])
        fresh = World(self)
        fresh.usage_reading(WORK, 10.0, R_WORK, week=10.0, minutes_ago=30)
        self.assertEqual(
            fresh.service().history_for(WORK, {})["summary"],
            "Lifeline started reading it today, so the last 7 days fill in from here.",
        )

    def test_history_counts_a_limit_again_after_an_early_reset(self):
        world, service = self.mixed(personal=100.0)
        world.usage_reading(PERSONAL, None, None, week=100.0, week_reset=THURSDAY, minutes_ago=2 * 60 * 24 + 30)
        world.usage_reading(PERSONAL, None, None, week=1.0, week_reset=THURSDAY, minutes_ago=60 * 24 + 60)
        world.usage_reading(PERSONAL, None, None, week=100.0, week_reset=THURSDAY, minutes_ago=60 * 24 + 30)
        service.tick()
        world.run(service)
        status, body = world.call("GET", f"/history/{PERSONAL}")
        self.assertEqual([d["hit_limit"] for d in body["daily"]], [True, True, False])
        self.assertEqual(body["week_ran_out"], ["2026-11-17", "2026-11-18"])
        world.assert_clean()

    def test_the_live_pattern_of_stale_rows_keeps_the_week_forecast(self):
        world, service = self.mixed(live=PERSONAL, personal=81.0)
        service.update_settings({"auto_switch": False})
        rows = []
        for i in range(24):
            at = START - timedelta(minutes=10 * (24 - i))
            rows.append((PERSONAL, "usage", None, None, 69.0 + 0.5 * i, int(THURSDAY.timestamp() * 1000), int(at.timestamp() * 1000)))
            rows.append((PERSONAL, "statusline", None, None, 75.0, int(THURSDAY.timestamp() * 1000), int((at + timedelta(minutes=5)).timestamp() * 1000)))
        import sqlite3

        with sqlite3.connect(world.history.path) as conn:
            conn.executemany("insert into readings (uuid, source, five, five_reset, week, week_reset, at) values (?,?,?,?,?,?,?)", rows)
        service.tick()
        personal = {a["uuid"]: a for a in service.state()["accounts"]}[PERSONAL]
        self.assertEqual(personal["week"], 81.0)
        self.assertEqual(personal["forecast"]["basis"], "recent")
        self.assertIsNotNone(personal["forecast"]["runs_out_at"])

    def test_a_status_line_reading_with_a_full_week_wakes_the_countdown(self):
        world = World(self, live=ALEX, figures=CALM)
        service = world.service()
        service.tick()
        service._wake.clear()
        self.assertEqual(service.take_reading(statusline_body("s-wk", ALEX, R_ALEX, five=44.0, week=100.0)), ALEX)
        self.assertTrue(service._wake.is_set())

    def test_history_peaks_follow_the_week_for_a_week_binding_account(self):
        world, service = self.mixed(personal=100.0)
        world.usage_reading(PERSONAL, None, None, week=100.0, week_reset=THURSDAY - timedelta(days=7), minutes_ago=2 * 60 * 24 + 30)
        world.usage_reading(PERSONAL, None, None, week=100.0, week_reset=THURSDAY + timedelta(milliseconds=322), minutes_ago=60 * 24 + 30)
        world.usage_reading(ALEX, 40.0, R_ALEX - timedelta(days=1), week=100.0, minutes_ago=60 * 24 + 30)
        service.tick()
        world.run(service)
        status, body = world.call("GET", f"/history/{PERSONAL}")
        self.assertEqual(status, 200)
        self.assertEqual(body["peaks_of"], "week")
        self.assertEqual([d["date"] for d in body["daily"]], ["2026-11-17", "2026-11-18", "2026-11-19"])
        self.assertEqual([d["peak_five"] for d in body["daily"]], [100.0, 100.0, 100.0])
        self.assertEqual([d["hit_limit"] for d in body["daily"]], [True, True, False])
        self.assertEqual(body["week_ran_out"], ["2026-11-17", "2026-11-18"])
        status, body = world.call("GET", f"/history/{ALEX}")
        self.assertEqual(body["peaks_of"], "five")
        self.assertEqual(body["daily"][0], {"date": "2026-11-18", "peak_five": 40.0, "hit_limit": False})
        self.assertEqual(body["week_ran_out"], ["2026-11-18"])
        world.assert_clean()

    def test_history_follows_the_kind_of_account_not_the_binding_of_the_moment(self):
        world = World(self, figures=CALM)
        world.usage_reading(WORK, 100.0, R_WORK - timedelta(days=1), week=40.0, minutes_ago=60 * 24 + 30)
        world.usage_reading(WORK, 60.0, R_WORK - timedelta(days=1), week=41.0, minutes_ago=60 * 24 + 20)
        service = world.service()
        service.tick()
        world.run(service)
        self.assertEqual({a["uuid"]: a for a in service.state()["accounts"]}[WORK]["binding"], "week")
        status, body = world.call("GET", f"/history/{WORK}")
        self.assertEqual((status, body["peaks_of"], body["week_ran_out"]), (200, "five", []))
        self.assertEqual(body["daily"][0], {"date": "2026-11-18", "peak_five": 100.0, "hit_limit": True})
        self.assertEqual(body["daily"][-1]["peak_five"], 12.0)
        world.assert_clean()



class OrganizationServerTests(unittest.TestCase):
    def two_organizations(self, live=ALEX):
        world = World(self, registered=(ALEX, WORK, PERSONAL), live=live)
        world.registry.upsert(account(TEAM, 4))
        world.signins[TEAM] = world.issue(TEAM)
        world.store.put(TEAM, world.signins[TEAM])
        return world

    def statusline_rows(self, world, uuid):
        return [r.five for r in world.history.readings(uuid, START - timedelta(hours=1)) if r.source == "statusline"]

    def test_a_status_line_post_for_one_organization_is_never_stored_under_the_other(self):
        world = self.two_organizations()
        passed = START - timedelta(minutes=30)
        world.usage_reading(PERSONAL, 4.0, passed)
        world.usage_reading(TEAM, 9.0, passed - timedelta(minutes=20))
        service = world.service()
        fresh = START + timedelta(hours=4)
        self.assertEqual(service.take_reading(statusline_body("t", PERSONAL, fresh, five=11.0, org=TEAM_ORG)), TEAM)
        self.assertEqual(self.statusline_rows(world, TEAM), [11.0])
        self.assertEqual(self.statusline_rows(world, PERSONAL), [])
        later = fresh + timedelta(minutes=30)
        self.assertEqual(service.take_reading(statusline_body("p", PERSONAL, later, five=3.0)), PERSONAL)
        self.assertEqual(self.statusline_rows(world, PERSONAL), [3.0])
        self.assertEqual(self.statusline_rows(world, TEAM), [11.0])
        for org in (None, "99999999-cccc-4ccc-8ccc-000000000009"):
            with self.subTest(org=org):
                self.assertIsNone(service.take_reading(statusline_body("x", PERSONAL, later + timedelta(hours=1), five=1.0, org=org)))
        reply = service.reading_reply(statusline_body("t", PERSONAL, fresh, five=12.0, org=TEAM_ORG))
        self.assertEqual((reply["saved"], reply["account_uuid"], reply["org_uuid"], reply["five"]), (True, PERSONAL, TEAM_ORG, 12.0))
        self.assertEqual(self.statusline_rows(world, PERSONAL), [3.0])
        self.assertEqual(self.statusline_rows(world, TEAM), [12.0])

    def fives(self, world, uuid):
        return [five for five in self.statusline_rows(world, uuid) if five is not None]

    def shared_reset(self, team_reading):
        world = self.two_organizations()
        half_hour = START + timedelta(hours=2, minutes=19)
        world.usage_reading(PERSONAL, 30.0, half_hour)
        if team_reading:
            world.usage_reading(TEAM, 8.0, half_hour)
        world.history.add_reading(TEAM, "statusline", None, None, 40.0, WEEK, START - timedelta(minutes=30))
        return world, world.service(), half_hour

    def test_two_organizations_sharing_a_half_hour_reset_each_keep_their_own_posts(self):
        world, service, half_hour = self.shared_reset(team_reading=True)
        self.assertEqual(service.take_reading(statusline_body("t", PERSONAL, half_hour, five=9.0, org=TEAM_ORG)), TEAM)
        self.assertEqual(service.take_reading(statusline_body("p", PERSONAL, half_hour, five=31.0)), PERSONAL)
        self.assertEqual(self.fives(world, TEAM), [9.0])
        self.assertEqual(self.fives(world, PERSONAL), [31.0])

    def test_a_shared_reset_never_moves_a_post_to_the_other_organization_when_its_own_has_no_reading(self):
        world, service, half_hour = self.shared_reset(team_reading=False)
        reply = service.reading_reply(statusline_body("t", PERSONAL, half_hour, five=9.0, org=TEAM_ORG))
        self.assertFalse(reply["saved"])
        self.assertEqual((reply["account_uuid"], reply["org_uuid"], reply["week"]), (PERSONAL, TEAM_ORG, 40.0))
        self.assertEqual(self.fives(world, PERSONAL), [])
        self.assertEqual(self.fives(world, TEAM), [])

    def test_a_post_with_a_malformed_organization_is_refused(self):
        world = World(self)
        service = world.service()
        for org in (5, "", ["x"]):
            with self.subTest(org=org):
                with self.assertRaises(server.Refusal) as caught:
                    service.take_reading(statusline_body("s", ALEX, R_ALEX, org=org))
                self.assertEqual(caught.exception.status, 400)

    def test_the_mac_names_the_live_organization(self):
        world = self.two_organizations(live=PERSONAL)
        world.record_path.unlink()
        world.store.put(PERSONAL, world.signins[PERSONAL])
        world.store.remove(TEAM)
        world.slot.item = {"claudeAiOauth": world.signins[TEAM]}
        world.write_config(TEAM)
        service = world.service()
        service.tick()
        self.assertEqual(service.mac_uuid(), TEAM)
        rows = {a["uuid"]: a for a in service.state()["accounts"]}
        self.assertEqual(rows[TEAM]["in_use"], ["mac"])
        self.assertEqual(rows[PERSONAL]["in_use"], ["idle"])

    def test_the_readers_live_identity_uses_the_whole_registry_even_for_an_account_the_devbox_reads(self):
        world = self.two_organizations(live=PERSONAL)
        world.figures[TEAM] = (3.0, R_WORK, 5.0, WEEK)
        world.link = FullLink()
        world.link.up = True
        world.link.status["covers"] = [PERSONAL]
        world.link.readings = [r for r in world.devbox_readings() if r.uuid == PERSONAL]
        service = world.service()
        seen = []
        service.reader.on_identified = lambda item, uuid: seen.append(uuid)
        service.tick()
        self.assertIn(PERSONAL, service._skip)
        self.assertEqual(seen, [PERSONAL])
        self.assertIn(TEAM, world.usage.calls)
        self.assertNotIn(PERSONAL, world.usage.calls)
        self.assertEqual(world.history.latest(TEAM).five, 3.0)

    def test_the_state_names_each_accounts_organization(self):
        world = self.two_organizations()
        rows = {a["uuid"]: a for a in world.service().state()["accounts"]}
        self.assertEqual((rows[TEAM]["org_uuid"], rows[TEAM]["organization"]), (TEAM_ORG, "Example Org"))
        self.assertEqual((rows[PERSONAL]["org_uuid"], rows[PERSONAL]["organization"]), (pair_of(PERSONAL)[1], details(PERSONAL)["organizationName"]))

    def test_a_placement_takes_an_id_made_of_login_and_organization(self):
        world = self.two_organizations()
        service = world.service()
        service.update_settings({"placement": {TEAM: "mac"}})
        self.assertEqual(service.state()["settings"]["placement"][TEAM], "mac")



class FakeClaude:
    def __init__(self, world, account=NEWCOMER):
        self.world = world
        self.account = account
        self.env = None
        self.ended = False
        self.pid = 999999
        self.started = threading.Event()
        self.stop = None
        self.exit_on_stop = None
        self.finish = threading.Event()

    def __call__(self, args, **options):
        self.env = options["env"]
        folder = Path(self.env["CLAUDE_CONFIG_DIR"])
        Path(self.env[devbox.PAGE_VARIABLE]).write_text(PAGE + "\n")
        self.world.keychain.put(enrolling.hashed_service(folder), {"claudeAiOauth": self.world.issue(self.account)})
        self.started.set()
        return self

    def poll(self):
        if self.ended:
            return -15
        if self.exit_on_stop is not None and self.stop is not None and self.stop.is_set():
            return self.exit(self.exit_on_stop)
        if self.finish.is_set():
            return self.exit(0)
        return None

    def exit(self, code):
        if code == 0:
            (Path(self.env["CLAUDE_CONFIG_DIR"]) / ".claude.json").write_text(json.dumps({"oauthAccount": details(self.account)}))
        return code

    def terminate(self):
        self.ended = True

    def kill(self):
        self.ended = True

    def wait(self, timeout=None):
        return -15


class CancelTests(unittest.TestCase):
    def rig(self, runner=None):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        claude = FakeClaude(world)
        world.keychain.put("Claude Code-credentials", {"claudeAiOauth": {"accessToken": "live-SECRET-kept"}})
        world.keychain.put("com.example.lifeline", {"claudeAiOauth": world.signins[WORK]}, account=WORK)

        def run_login(folder, email=None, user=None, on_page=None, stop=None):
            claude.stop = stop
            return server.run_login(
                folder, email, user=user, on_page=on_page, popen=claude,
                open_page=lambda url: ("Google Chrome", True), poll=0.01, stop=stop,
            )

        which = mock.patch.object(server.shutil, "which", return_value="/opt/bin/fake-claude")
        killpg = mock.patch.object(server.os, "killpg", lambda pid, sig: claude.terminate())
        which.start()
        killpg.start()
        self.addCleanup(which.stop)
        self.addCleanup(killpg.stop)
        self.opened = []

        def open_browser(url):
            self.opened.append(url)
            return "Google Chrome", True

        extra = {} if runner is None else {"runner": runner(world.keychain)}
        service = world.service(run_login=run_login, open_browser=open_browser, **extra)
        self.addCleanup(claude.terminate)
        return world, claude, service

    def test_a_cancel_whose_cleanup_failed_never_says_nothing_changed(self):
        class NoDelete:
            def __init__(self, inner):
                self.inner = inner

            def __call__(self, args, stdin=None):
                if args[0] == "delete-generic-password":
                    raise SecurityError(args[0], 1, "")
                return self.inner(args, stdin)

        world, claude, service = self.rig(runner=NoDelete)
        job = service.start_job("add", None, {})["job"]
        self.assertTrue(self.started(world, service))
        service.cancel_job(f"{job}/cancel")
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running"))
        finished = service.state()["jobs"][0]
        self.assertEqual(finished["state"], "cancelled")
        self.assertTrue(finished["result"].startswith("You cancelled the sign-in. Lifeline could not remove"), finished["result"])
        self.assertNotIn("Nothing changed", finished["result"])
        tail = finished["result"][len("You cancelled the sign-in. "):]
        logged = [line for line in world.collector.lines if "You cancelled" in line]
        self.assertEqual(logged, ["INFO You cancelled adding an account. " + tail])

    def test_a_stop_whose_cleanup_failed_logs_the_leftover_too(self):
        class NoDelete:
            def __init__(self, inner):
                self.inner = inner

            def __call__(self, args, stdin=None):
                if args[0] == "delete-generic-password":
                    raise SecurityError(args[0], 1, "")
                return self.inner(args, stdin)

        world, claude, service = self.rig(runner=NoDelete)
        service.start_job("add", None, {})
        self.assertTrue(self.started(world, service))
        service.stop()
        finished = service.state()["jobs"][0]
        self.assertEqual(finished["state"], "cancelled")
        self.assertTrue(finished["result"].startswith("Lifeline stopped, so it cancelled the sign-in. Lifeline could not remove"), finished["result"])
        tail = finished["result"][len("Lifeline stopped, so it cancelled the sign-in. "):]
        logged = [line for line in world.collector.lines if "so it cancelled" in line]
        self.assertEqual(logged, ["INFO Lifeline stopped, so it cancelled adding an account. " + tail])
        self.assertFalse(any("Nothing changed" in line for line in logged))

    def started(self, world, service):
        return world.wait_for(lambda: service.state()["jobs"][0]["url"] == PAGE)

    def kept(self, world):
        return (
            copy.deepcopy(world.slot.item),
            {path.name: path.read_bytes() for path in sorted((world.root / "signins").iterdir())},
            {key: value for key, value in world.keychain.items.items() if not key[0].startswith(enrolling.SERVICE_PREFIX)},
            world.registry.path.read_bytes(),
            world.record_path.read_bytes(),
        )

    def test_cancel_stops_the_login_and_removes_only_its_own_folder_and_keychain_item(self):
        for kind, route_uuid in (("add", None), ("resign", WORK)):
            with self.subTest(kind=kind):
                world, claude, service = self.rig()
                before = self.kept(world)
                job = service.start_job(kind, route_uuid, {})["job"]
                self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["url"] == PAGE))
                folder = Path(claude.env["CLAUDE_CONFIG_DIR"])
                hashed = enrolling.hashed_service(folder)
                self.assertTrue(folder.is_dir())
                self.assertIn((hashed, USER), world.keychain.items)
                self.assertEqual(service.cancel_job(f"{job}/cancel"), {"cancelled": True})
                self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running"))
                finished = service.state()["jobs"][0]
                self.assertEqual((finished["state"], finished["result"]), ("cancelled", server.JOB_CANCELLED))
                self.assertTrue(claude.ended)
                self.assertFalse(folder.exists())
                self.assertEqual(list(world.temp_root.iterdir()), [])
                self.assertNotIn((hashed, USER), world.keychain.items)
                self.assertEqual(self.kept(world), before)
                cancelled = [line for line in world.collector.lines if "You cancelled" in line]
                self.assertEqual(len(cancelled), 1)
                with self.assertRaises(server.Refusal) as caught:
                    service.cancel_job(f"{job}/cancel")
                self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.JOB_ENDED))
                world.assert_clean()

    def test_the_cancel_route_answers_plainly_for_unknown_and_finished_jobs(self):
        world, claude, service = self.rig()
        world.run(service)
        self.assertEqual(world.call("POST", "/jobs/nothing-here/cancel", {}), (404, {"error": server.NO_SUCH_JOB}))
        self.assertEqual(world.call("POST", "/jobs/nothing-here/stop", {}), (404, {"error": server.NOT_FOUND}))
        self.assertEqual(world.call("GET", "/jobs/nothing-here/cancel")[0], 405)
        status, reply = world.call("POST", "/add", {})
        self.assertEqual(status, 202)
        self.assertTrue(world.wait_for(lambda: claude.started.is_set()))
        self.assertEqual(world.call("POST", f"/jobs/{reply['job']}/cancel", {}), (200, {"cancelled": True}))
        self.assertTrue(world.wait_for(lambda: world.call("GET", "/state")[1]["jobs"][0]["state"] == "cancelled"))
        self.assertEqual(world.call("POST", f"/jobs/{reply['job']}/cancel", {}), (409, {"error": server.JOB_ENDED}))
        world.assert_clean()

    def test_a_sign_in_already_being_saved_is_not_cancelled(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_uuid = NEWCOMER
        gate, checking = threading.Event(), threading.Event()
        real = world.profiles

        def slow_profile(token):
            checking.set()
            gate.wait(10)
            return real(token)

        service = world.service(profile_fn=slow_profile)
        job = service.start_job("add", None, {})["job"]
        self.assertTrue(checking.wait(10))
        with self.assertRaises(server.Refusal) as caught:
            service.cancel_job(f"{job}/cancel")
        self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.JOB_SAVING))
        gate.set()
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(service.state()["jobs"][0]["state"], "done")
        self.assertIsNotNone(world.registry.by_uuid(NEWCOMER))

    def test_an_adopt_has_no_sign_in_to_cancel(self):
        world = World(self)
        gate = threading.Event()
        real = world.profiles

        def slow_profile(token):
            gate.wait(10)
            return real(token)

        service = world.service(profile_fn=slow_profile)
        job = service.start_job("adopt", None, {})["job"]
        with self.assertRaises(server.Refusal) as caught:
            service.cancel_job(f"{job}/cancel")
        self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.JOB_NOT_CANCELLABLE))
        gate.set()
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))


    def test_a_cancel_after_the_job_finished_removes_nothing_and_says_so(self):
        world, claude, service = self.rig()
        job = service.start_job("add", None, {})["job"]
        self.assertTrue(self.started(world, service))
        claude.finish.set()
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(service.state()["jobs"][0]["state"], "done")
        self.assertIsNotNone(world.registry.by_uuid(NEWCOMER))
        added = world.store.get(NEWCOMER)
        self.assertIsNotNone(added)
        before = self.kept(world)
        with self.assertRaises(server.Refusal) as caught:
            service.cancel_job(f"{job}/cancel")
        self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.JOB_ENDED))
        self.assertEqual(self.kept(world), before)
        self.assertEqual(world.store.get(NEWCOMER), added)
        self.assertEqual(service.state()["jobs"][0]["state"], "done")
        self.assertFalse(any("You cancelled" in line for line in world.collector.lines))

    def test_a_cancelled_login_that_exits_by_itself_is_still_cancelled_never_failed(self):
        for code in (0, 1):
            with self.subTest(code=code):
                world, claude, service = self.rig()
                claude.exit_on_stop = code
                before = self.kept(world)
                job = service.start_job("add", None, {})["job"]
                self.assertTrue(self.started(world, service))
                folder = Path(claude.env["CLAUDE_CONFIG_DIR"])
                self.assertEqual(service.cancel_job(f"{job}/cancel"), {"cancelled": True})
                self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running"))
                finished = service.state()["jobs"][0]
                self.assertEqual((finished["state"], finished["result"]), ("cancelled", server.JOB_CANCELLED))
                self.assertFalse(claude.ended)
                self.assertFalse(folder.exists())
                self.assertEqual(self.kept(world), before)
                self.assertIsNone(world.registry.by_uuid(NEWCOMER))
                self.assertFalse(any("did not finish" in line for line in world.collector.lines))

    def test_open_reopens_the_page_in_a_private_window_and_says_which_browser(self):
        world, claude, service = self.rig()
        world.run(service)
        status, reply = world.call("POST", "/add", {})
        self.assertTrue(self.started(world, service))
        self.opened.clear()
        self.assertEqual(world.call("POST", f"/jobs/{reply['job']}/open", {}), (200, {"browser": "Google Chrome", "opened": True}))
        self.assertEqual(self.opened, [PAGE])
        self.assertEqual(world.call("POST", "/jobs/nothing-here/open", {}), (404, {"error": server.NO_SUCH_JOB}))
        self.assertEqual(world.call("POST", f"/jobs/{reply['job']}/cancel", {}), (200, {"cancelled": True}))
        self.assertTrue(world.wait_for(lambda: world.call("GET", "/state")[1]["jobs"][0]["state"] == "cancelled"))
        self.assertEqual(world.call("POST", f"/jobs/{reply['job']}/open", {}), (409, {"error": server.PAGE_ENDED}))
        self.assertEqual(self.opened, [PAGE])
        world.assert_clean()

    def test_open_refuses_a_job_with_no_page_yet_an_adopt_and_an_untrusted_page(self):
        world, claude, service = self.rig()
        gate = threading.Event()
        real = world.profiles

        def slow_profile(token):
            gate.wait(10)
            return real(token)

        adopting = world.service(profile_fn=slow_profile, open_browser=lambda url: self.fail("opened"))
        adopt = adopting.start_job("adopt", None, {})["job"]
        with self.assertRaises(server.Refusal) as caught:
            adopting.open_job(f"{adopt}/open")
        self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.NO_PAGE_TO_OPEN))
        gate.set()
        waiting = World(self, registered=(ALEX, WORK, PERSONAL))
        waiting.login_gate = threading.Event()
        quiet = waiting.service(open_browser=lambda url: self.fail("opened"))
        job = quiet.start_job("add", None, {})["job"]
        self.assertTrue(waiting.wait_for(lambda: waiting.logins))
        with self.assertRaises(server.Refusal) as caught:
            quiet.open_job(f"{job}/open")
        self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.PAGE_NOT_READY))
        waiting.login_gate.set()
        job = service.start_job("add", None, {})["job"]
        self.assertTrue(self.started(world, service))
        with service._state:
            service._jobs[job]["url"] = "https://example.invalid/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback"
        self.opened.clear()
        with self.assertRaises(server.Refusal) as caught:
            service.open_job(f"{job}/open")
        self.assertEqual((caught.exception.status, caught.exception.sentence), (409, server.PAGE_UNTRUSTED))
        self.assertEqual(self.opened, [])


    def test_stopping_the_service_cancels_a_running_sign_in_and_removes_only_its_own_things(self):
        for kind, route_uuid, what in (("add", None, "adding an account"), ("resign", WORK, "signing in to Work Account again")):
            with self.subTest(kind=kind):
                world, claude, service = self.rig()
                before = self.kept(world)
                job = service.start_job(kind, route_uuid, {})["job"]
                self.assertTrue(self.started(world, service))
                folder = Path(claude.env["CLAUDE_CONFIG_DIR"])
                hashed = enrolling.hashed_service(folder)
                service.stop()
                finished = service.state()["jobs"][0]
                self.assertEqual((finished["id"], finished["state"], finished["result"]), (job, "cancelled", server.STOP_CANCELLED))
                self.assertTrue(claude.ended)
                self.assertFalse(folder.exists())
                self.assertNotIn((hashed, USER), world.keychain.items)
                self.assertEqual(self.kept(world), before)
                logged = [line for line in world.collector.lines if "so it cancelled" in line]
                self.assertEqual(logged, [f"INFO Lifeline stopped, so it cancelled {what}. Nothing changed."])
                world.assert_clean()

    def test_a_sign_in_already_saving_finishes_before_the_service_stops(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        gate, checking = threading.Event(), threading.Event()
        real = world.profiles

        def slow_profile(token):
            if token in world.owners and world.owners[token] == NEWCOMER:
                checking.set()
                gate.wait(10)
            return real(token)

        service = world.service(profile_fn=slow_profile)
        service.start_job("add", None, {})
        self.assertTrue(checking.wait(10))
        stopping = threading.Thread(target=service.stop, daemon=True)
        stopping.start()
        stopping.join(0.5)
        self.assertTrue(stopping.is_alive())
        self.assertEqual(service.state()["jobs"][0]["state"], "running")
        gate.set()
        stopping.join(30)
        self.assertFalse(stopping.is_alive())
        finished = service.state()["jobs"][0]
        self.assertEqual(finished["state"], "done")
        self.assertIsNotNone(world.registry.by_uuid(NEWCOMER))



class MinorServerTests(unittest.TestCase):
    def test_devbox_rows_keep_their_own_source(self):
        world = World(self)
        world.link = FullLink()
        world.link.up = True
        world.link.status["covers"] = [ALEX]
        world.link.readings = [
            Reading(ALEX, "usage", 29.0, R_ALEX, 10.0, WEEK, START - timedelta(minutes=2)),
            Reading(ALEX, "statusline", 30.0, R_ALEX, 10.0, WEEK, START - timedelta(minutes=1)),
        ]
        world.service().tick()
        rows = world.history.readings(ALEX, START - timedelta(hours=1))
        self.assertEqual([(r.source, r.five) for r in rows], [("usage", 29.0), ("statusline", 30.0)])

    def test_the_macs_identity_check_never_runs_inside_a_switch(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL), live=ALEX)
        world.record_path.unlink()
        service = world.service()
        service._identify(world.clock.now)
        self.assertEqual(service.mac_uuid(), ALEX)
        paused, release = threading.Event(), threading.Event()
        real = switching.accountfile.replace_oauth_account

        def slow(*args, **kwargs):
            paused.set()
            release.wait(10)
            return real(*args, **kwargs)

        with mock.patch.object(switching.accountfile, "replace_oauth_account", slow):
            moving = threading.Thread(target=service.switch_to, args=(WORK,), daemon=True)
            moving.start()
            self.assertTrue(paused.wait(10))
            checking = threading.Thread(target=service._identify, args=(world.clock.now + timedelta(minutes=6),), daemon=True)
            checking.start()
            checking.join(0.5)
            release.set()
            moving.join(10)
            checking.join(10)
        self.assertEqual(world.live_owner(), WORK)
        self.assertFalse(any("outside Lifeline" in line for line in world.collector.lines), world.collector.lines)

    def test_an_undo_from_an_account_no_longer_in_lifeline_says_it_was_an_undo(self):
        service = World(self).service()
        result = switching.SwitchResult("55555555-gone-4eee-8eee-000000000009", ALEX, None)
        self.assertEqual(
            service._switch_line("undo", result, "menu-undo"),
            "You undid the last switch, so this Mac went back to Alex's Account (via menu-undo).",
        )
        self.assertEqual(service._switch_line("you", result, "row"), "You switched this Mac to Alex's Account (via row).")

    def test_an_untrusted_page_is_named_as_untrusted(self):
        self.assertEqual(
            server.STRANGE_SIGNIN_PAGE,
            "Claude Code gave a sign-in page Lifeline does not trust, so Lifeline did not open it and stopped the sign-in.",
        )



KNOWN = [
    {"name": "Alex's Account", "short": "Alex", "full": "Alex's Account", "email": "11111111@example.invalid", "organization": "personal"},
    {"name": "Work Account", "short": "Work", "full": "Example Team's Account", "email": "22222222@example.invalid", "organization": "personal"},
    {"name": "Personal Account", "short": "Personal", "full": "Example Org Personal Account", "email": "33333333@example.invalid", "organization": "personal"},
    {"name": "Team Account", "short": "Team", "full": "Example Org Team Account", "email": "33333333@example.invalid", "organization": "Example Org"},
]


class PlainServerTests(unittest.TestCase):
    def known(self, world, entries=KNOWN):
        (world.folder / "known_accounts.json").write_text(json.dumps(entries))

    def test_the_countdown_counts_down(self):
        world = World(self, figures=LIMIT)
        service = world.service()
        service.tick()
        self.assertEqual(service.state()["mac"]["head"], "Switching this Mac to Team Account in 5 seconds.")
        world.clock.advance(seconds=3)
        mac = service.state()["mac"]
        self.assertEqual((mac["head"], mac["countdown_left"]), ("Switching this Mac to Team Account in 2 seconds.", 2))

    def test_a_devbox_switch_is_refused_plainly(self):
        service = World(self).service()
        with self.assertRaises(server.Refusal) as caught:
            service.resolve({"uuid": ALEX, "machine": "devbox"})
        self.assertEqual(caught.exception.sentence, "Lifeline switches only this Mac, so nothing was changed.")

    def test_sentences_you_can_see_hold_no_internal_words(self):
        self.assertEqual(server.NO_SUCH_JOB, "Lifeline cannot find that sign-in.")
        self.assertEqual(server.JOB_NOT_CANCELLABLE, "That sign-in has nothing to cancel.")
        self.assertEqual(server.NO_PAGE_TO_OPEN, "That sign-in has no page to open.")
        self.assertEqual(server.BAD_TARGET, "Give the account's number, as in lifeline switch 2.")
        self.assertEqual(server.STOP_CANCELLED, "Lifeline stopped, so it cancelled the sign-in. Nothing changed.")
        self.assertEqual(server.DEVBOX_REFUSED_CHANGES, "The other computer no longer accepts this Mac's key, so pair it again in Alerts and settings.")

    def test_rename_never_changes_the_full_name_and_the_state_shows_it(self):
        world = World(self)
        service = world.service()
        service.rename(WORK, {"name": "Command Account", "short": "Command"})
        self.assertEqual(world.registry.by_uuid(WORK).full, "Example Team's Account")
        rows = {a["uuid"]: a for a in service.state()["accounts"]}
        self.assertEqual((rows[WORK]["name"], rows[WORK]["full"]), ("Command Account", "Example Team's Account"))

    def test_known_accounts_not_yet_added_are_listed(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        self.known(world)
        listed = world.service().state()["known_accounts"]
        self.assertEqual([{key: entry[key] for key in KNOWN[3]} for entry in listed], [KNOWN[3]])
        self.assertEqual(set(listed[0]) - set(KNOWN[3]), {"pick_line", "explain"})
        world.registry.upsert(account(TEAM, 4))
        self.assertEqual(world.service().state()["known_accounts"], [])
        broken = World(self)
        (broken.folder / "known_accounts.json").write_text("{not json")
        self.assertEqual(broken.service().state()["known_accounts"], [])

    def test_an_add_that_names_a_known_account_signs_in_with_its_email_and_names_its_organization(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        self.known(world)
        world.login_gate = threading.Event()
        service = world.service()
        job = service.start_job("add", None, {"known": "Team Account"})["job"]
        self.assertTrue(world.wait_for(lambda: world.logins))
        self.assertEqual(world.logins[0][1], "33333333@example.invalid")
        found = [j for j in service.state()["jobs"] if j["id"] == job][0]
        self.assertEqual((found["known"], found["organization"]), ("Team Account", "Example Org"))
        self.assertEqual(found["guidance"], "If Claude asks you to choose an organization, choose Example Org, then press Authorize.")
        world.login_gate.set()
        self.assertTrue(world.wait_for(lambda: all(j["state"] != "running" for j in service.state()["jobs"]), 10))
        with self.assertRaises(server.Refusal) as caught:
            service.start_job("add", None, {"known": "Nobody's Account"})
        self.assertEqual((caught.exception.status, caught.exception.sentence), (404, "Lifeline does not know an account called Nobody's Account."))

    def test_a_new_account_takes_its_names_and_full_name_from_the_known_file(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        self.known(world, KNOWN + [{"name": "Ama's Account", "short": "Ama", "full": "Ama Rivera's Account", "email": "55555555@example.invalid", "organization": "personal"}])
        service = world.service()
        job = service.start_job("add", None, {})["job"]
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        found = [j for j in service.state()["jobs"] if j["id"] == job][0]
        self.assertEqual((found["state"], found["suggested_name"], found["suggested_short"]), ("done", "Ama's Account", "Ama"))
        self.assertEqual(world.registry.by_uuid(NEWCOMER).full, "Ama Rivera's Account")

    def test_full_names_lost_to_an_old_rename_come_back_from_the_known_file_at_start(self):
        world = World(self, registered=(ALEX, WORK))
        erased = world.registry.by_uuid(WORK)
        world.registry.upsert(dataclasses.replace(erased, full=erased.name))
        self.known(world)
        world.service().start(ticking=False)
        self.assertEqual(world.registry.by_uuid(WORK).full, "Example Team's Account")
        self.assertIn("INFO Lifeline restored the full name of Work Account from your list of known accounts.", world.collector.lines)

    def test_a_sign_in_again_names_the_organization_to_choose(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_gate = threading.Event()
        self.addCleanup(world.login_gate.set)
        world.login_uuid = WORK
        service = world.service()
        job = service.start_job("resign", WORK, {})["job"]
        found = [j for j in service.state()["jobs"] if j["id"] == job][0]
        organization = world.registry.by_uuid(WORK).organization
        self.assertTrue(organization.endswith("'s Organization"))
        self.assertEqual(found["guidance"], "If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize.")
        world.login_gate.set()
        self.assertTrue(world.wait_for(lambda: all(j["state"] != "running" for j in service.state()["jobs"]), 10))
        plain = World(self, registered=(ALEX, WORK, PERSONAL))
        plain.login_gate = threading.Event()
        self.addCleanup(plain.login_gate.set)
        service = plain.service()
        job = service.start_job("add", None, {})["job"]
        self.assertIsNone([j for j in service.state()["jobs"] if j["id"] == job][0]["guidance"])
        plain.login_gate.set()
        self.assertTrue(plain.wait_for(lambda: all(j["state"] != "running" for j in service.state()["jobs"]), 10))

    def on_team(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.signins[TEAM] = world.issue(TEAM)
        world.slot.item = {"claudeAiOauth": world.signins[TEAM]}
        world.write_config(TEAM)
        world.record_path.unlink()
        return world

    def test_this_mac_names_its_login_not_added_from_the_known_list(self):
        world = self.on_team()
        self.known(world)
        service = world.service()
        service._identify(world.clock.now)
        self.assertEqual(service.mac_uuid(), TEAM)
        mac = service.state()["mac"]
        self.assertEqual(
            (mac["head"], mac["sub"], mac["action_kind"], mac["action_text"]),
            ("This Mac uses Team Account, which Lifeline has not added yet.",
             "Lifeline adds the sign-in this Mac already has, so no browser opens.", "adopt", "Add Team Account"),
        )
        self.assertIs(mac["live"]["adopt_ready"], True)
        self.assertEqual(mac["unadded_known"], {"name": "Team Account", "short": "Team", "email": "33333333@example.invalid"})
        world.slot.item = {}
        service._live_step(world.clock.now)
        mac = service.state()["mac"]
        self.assertIs(mac["live"]["adopt_ready"], False)
        self.assertEqual((mac["action_kind"], mac["action_text"], mac["sub"]), ("add", "Add Team Account", "It takes one Authorize in your browser."))
        plain = self.on_team()
        service = plain.service()
        service._identify(plain.clock.now)
        mac = service.state()["mac"]
        self.assertEqual((mac["head"], mac["unadded_known"]), ("This Mac uses an account Lifeline has not added yet.", None))
        self.assertIsNone(World(self).service().state()["mac"]["unadded_known"])
        moved = self.on_team()
        self.known(moved)
        service = moved.service()
        service._identify(moved.clock.now)
        moved.write_config(PERSONAL)
        mac = service.state()["mac"]
        self.assertEqual((mac["head"], mac["unadded_known"]), ("This Mac uses an account Lifeline has not added yet.", None))

    def mission_org(self, world):
        self.known(world, KNOWN[:3] + [{"name": "Mission Org", "short": "MOrg", "full": "Mission Org Full",
                                       "email": "33333333@example.invalid", "organization": "Example Org"}])

    def saved_names(self, world):
        seen = []
        real = world.registry.save

        def watching(accounts):
            seen.append([account.name for account in accounts])
            seen.extend((a.name, a.short, a.full) for a in accounts if a.uuid == TEAM)
            return real(accounts)

        world.registry.save = watching
        return seen

    def test_an_add_that_names_a_known_account_stores_its_names_at_once(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        self.mission_org(world)
        world.login_uuid = TEAM
        seen = self.saved_names(world)
        service = world.service()
        service.start_job("add", None, {"known": "Mission Org"})
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(service.state()["jobs"][0]["state"], "done", service.state()["jobs"][0]["result"])
        added = world.registry.by_uuid(TEAM)
        self.assertEqual((added.name, added.short, added.full), ("Mission Org", "MOrg", "Mission Org Full"))
        self.assertFalse(any("Team Account" in names for names in seen), seen)
        self.assertEqual({item for item in seen if isinstance(item, tuple)}, {("Mission Org", "MOrg", "Mission Org Full")})
        self.assertIn(f"INFO You added Mission Org to Lifeline as number {added.key}.", world.collector.lines)

    def test_an_add_takes_the_names_of_the_account_that_actually_signed_in(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        self.known(world, KNOWN + [{"name": "Ama's Account", "short": "Ama", "full": "Ama Rivera's Account",
                                    "email": "55555555@example.invalid", "organization": "personal"}])
        service = world.service()
        service.start_job("add", None, {"known": "Team Account"})
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        added = world.registry.by_uuid(NEWCOMER)
        self.assertEqual((added.name, added.short, added.full), ("Ama's Account", "Ama", "Ama Rivera's Account"))

    def test_names_you_type_win_over_the_known_list(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        self.mission_org(world)
        world.login_uuid = TEAM
        service = world.service()
        service.start_job("add", None, {"known": "Mission Org", "name": "Org Two", "short": "O2"})
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        added = world.registry.by_uuid(TEAM)
        self.assertEqual((added.name, added.short), ("Org Two", "O2"))

    def test_an_adopt_of_a_known_account_stores_its_names_at_once(self):
        world = self.on_team()
        self.mission_org(world)
        seen = self.saved_names(world)
        service = world.service()
        service.start_job("adopt", None, {})
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(service.state()["jobs"][0]["state"], "done", service.state()["jobs"][0]["result"])
        added = world.registry.by_uuid(TEAM)
        self.assertEqual((added.name, added.short, added.full), ("Mission Org", "MOrg", "Mission Org Full"))
        self.assertFalse(any("Team Account" in names for names in seen), seen)
        self.assertEqual({item for item in seen if isinstance(item, tuple)}, {("Mission Org", "MOrg", "Mission Org Full")})

    def test_a_sign_in_that_times_out_says_so_in_one_sentence(self):
        def timed_out(folder, email=None, user=None, on_page=None, stop=None):
            raise server.LoginFailed(server.LOGIN_TIMED_OUT)

        world = World(self, registered=(ALEX, WORK, PERSONAL))
        service = world.service(run_login=timed_out)
        service.start_job("add", None, {})
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        sentence = "Lifeline stopped waiting for the sign-in after 10 minutes, so nothing was added."
        self.assertEqual(service.state()["jobs"][0]["result"], sentence)
        self.assertIn("WARNING " + sentence, world.collector.lines)
        again = World(self, registered=(ALEX, WORK, PERSONAL))
        service = again.service(run_login=timed_out)
        service.start_job("resign", WORK, {})
        self.assertTrue(again.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(service.state()["jobs"][0]["result"], "Lifeline stopped waiting for the sign-in after 10 minutes, so nothing changed.")

    def test_each_account_carries_its_open_row_lines(self):
        world = World(self)
        world.history.add_reading(WORK, "usage", 12.0, R_WORK, 55.0, WEEK, world.clock.now)
        state = world.service().state()
        work = {a["uuid"]: a for a in state["accounts"]}[WORK]
        self.assertEqual(set(work["detail"]), {"usage", "models", "account", "projects"})
        self.assertTrue(work["detail"]["usage"].startswith("It has used 12% of its 5 hours and 55% of its week."), work["detail"]["usage"])
        self.assertEqual(
            work["detail"]["account"],
            "Its full name is Example Team's Account, and the menu bar calls it Work. It is on the Max 20x plan, signed in here.",
        )
        self.assertEqual(work["signin_text"], "It is signed in here, but the other computer is not answering.")
        self.assertNotIn("and on the other computer", work["signin_text"])
        self.assertIsNone(work["devbox_signed_in"])
        self.assertLessEqual(len([line for line in work["detail"].values() if line]) + 1, 5)

    def test_climbing_counts_only_readings_after_it_left_this_mac(self):
        world = World(self)
        service = world.service()
        for minutes, five in ((14, 10.0), (10, 20.0), (6, 30.0)):
            world.history.add_reading(ALEX, "usage", five, R_ALEX, 18.0, WEEK, world.clock.now - timedelta(minutes=minutes))
        service.switch_to(WORK)
        views = {v.uuid: v for v in service._views(service._snapshot(world.clock.now))}
        self.assertIs(views[ALEX].climbing, False)
        world.clock.advance(minutes=2)
        world.history.add_reading(ALEX, "usage", 31.0, R_ALEX, 18.0, WEEK, world.clock.now)
        world.clock.advance(minutes=3)
        world.history.add_reading(ALEX, "usage", 33.0, R_ALEX, 18.0, WEEK, world.clock.now)
        views = {v.uuid: v for v in service._views(service._snapshot(world.clock.now))}
        self.assertIs(views[ALEX].climbing, True)
        self.assertIsNone(views[WORK].climbing)

    def test_undo_shows_for_5_seconds_then_the_head_returns(self):
        world = World(self)
        service = world.service()
        service.switch_to(WORK)
        switched = world.clock.now
        state = service.state()
        self.assertEqual((state["mac"]["head"], state["mac"]["action_kind"]), ("This Mac now uses Work Account.", "undo"))
        self.assertEqual(state["mac"]["undo_until"], iso(switched + timedelta(seconds=5)))
        self.assertIsNone(state["dev"]["undo_until"])
        world.clock.advance(seconds=4)
        self.assertEqual(service.state()["mac"]["action_kind"], "undo")
        world.clock.advance(seconds=1)
        state = service.state()
        self.assertNotEqual(state["mac"]["head"], "This Mac now uses Work Account.")
        self.assertNotEqual(state["mac"]["action_kind"], "undo")
        self.assertIsNone(state["mac"]["undo_until"])

    def test_a_known_account_not_yet_added_is_named_in_the_live_line(self):
        from lifeline.reader import UNKNOWN, _Live

        world = self.on_team()
        self.mission_org(world)
        service = world.service()
        sentence = "Claude Code on this Mac uses Mission Org, which Lifeline has not added yet, so Lifeline will not read it or renew its sign-in."
        self.assertEqual(service._live_note(), sentence)
        reports = []
        service.reader._note_live(_Live(UNKNOWN, None, None), reports)
        self.assertEqual(reports, [sentence])
        plain = self.on_team()
        self.assertEqual(
            plain.service()._live_note(),
            "Claude Code on this Mac uses 33333333@example.invalid, which Lifeline has not added yet, so Lifeline will not read it or renew its sign-in.",
        )
        self.assertIsNone(World(self).service()._live_note())

    def test_the_devbox_exchange_never_holds_up_the_service_loop(self):
        world = World(self)
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Slow(FakeLink):
            def reachable(self):
                gate.wait(10)
                return False

        world.link = Slow()
        service = world.service(link=world.link, link_in_background=True)
        started = time.monotonic()
        service.tick()
        service.state()
        self.assertLess(time.monotonic() - started, 1.5)
        service.tick()
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "lifeline-devbox-link"]), 1)
        gate.set()
        self.assertTrue(world.wait_for(lambda: not service._link_busy, 5))

    def test_numbers_go_past_four(self):
        world = World(self)
        service = world.service()
        self.assertEqual(service._job_options("add", {"number": 7}), {"key": 7})
        with self.assertRaises(server.Refusal):
            service._job_options("add", {"number": 0})

    def test_the_state_names_the_browser_a_sign_in_will_use(self):
        world = World(self)
        self.assertEqual(world.service(browser_fn=lambda: "Firefox").state()["signin_browser"], "Firefox")
        with mock.patch.object(server.sys, "platform", "darwin"), mock.patch.object(devbox, "default_browser", lambda: "Safari"):
            self.assertEqual(world.service(browser_fn=None).state()["signin_browser"], "Safari")

    def test_the_reopen_uses_the_default_browser_by_default(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_page = PAGE
        world.login_gate = threading.Event()
        self.addCleanup(world.login_gate.set)
        opened = []

        def fake_open(url):
            opened.append(url)
            return "Safari", True

        service = world.service(open_browser=None)
        job = service.start_job("add", None, {})["job"]
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["url"] == PAGE))
        with mock.patch.object(devbox, "open_default", fake_open):
            self.assertEqual(service.open_job(f"{job}/open"), {"browser": "Safari", "opened": True})
        self.assertEqual(opened, [PAGE])
        world.login_gate.set()
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))

    def test_new_sign_ins_are_refused_once_a_stop_begins(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        service = world.service()
        service.stop()
        for start in (lambda: service.start_job("add", None, {}), lambda: service.start_devbox_job("add", {"uuid": ALEX})):
            with self.assertRaises(server.Refusal) as caught:
                start()
            self.assertEqual((caught.exception.status, caught.exception.sentence), (503, server.STOPPING_SIGNIN))
        self.assertEqual(server.STOPPING_SIGNIN, "Lifeline is stopping, so it did not start the sign-in.")
        self.assertEqual(world.logins, [])

    def test_the_identity_check_asks_anthropic_outside_the_switch_lock(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL), live=ALEX)
        world.record_path.unlink()
        held = []
        holder = {}
        real = world.profiles

        def watching(token):
            held.append(holder["service"]._switching.locked())
            return real(token)

        service = world.service(profile_fn=watching)
        holder["service"] = service
        service._identify(world.clock.now)
        self.assertEqual(service.mac_uuid(), ALEX)
        self.assertEqual(held, [False])

    def test_the_identity_check_rechecks_after_asking_anthropic(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL), live=ALEX)
        world.record_path.unlink()
        holder = {"switched": False}
        real = world.profiles

        def switching_meanwhile(token):
            if not holder["switched"]:
                holder["switched"] = True
                mover = threading.Thread(target=holder["service"].switch_to, args=(WORK,), daemon=True)
                mover.start()
                mover.join(5)
                holder["moved"] = not mover.is_alive()
            return real(token)

        service = world.service(profile_fn=switching_meanwhile)
        holder["service"] = service
        service._identify(world.clock.now)
        self.assertTrue(holder["moved"], "the switch waited on the identity check")
        self.assertEqual(world.live_owner(), WORK)
        self.assertIsNone(service._identity)
        self.assertFalse(any("outside Lifeline" in line for line in world.collector.lines), world.collector.lines)

    def test_an_idle_own_account_never_loses_its_post_to_the_other_organization(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.registry.upsert(account(TEAM, 4))
        world.clock.now = datetime(2026, 11, 20, 0, 40, tzinfo=UTC)
        half_hour = datetime(2026, 11, 20, 5, 30, tzinfo=UTC)
        world.history.add_reading(TEAM, "usage", 3.0, half_hour, 10.0, WEEK, datetime(2026, 11, 20, 0, 35, tzinfo=UTC))
        world.history.add_reading(PERSONAL, "usage", 0.0, None, 20.0, WEEK, datetime(2026, 11, 20, 0, 38, tzinfo=UTC))
        service = world.service()
        body = statusline_body("p", PERSONAL, half_hour, five=4.0, at=world.clock.now)
        self.assertIsNone(service.take_reading(body))
        self.assertEqual([r.source for r in world.history.readings(TEAM, world.clock.now - timedelta(hours=1))], ["usage"])
        alone = World(self, registered=(ALEX, WORK, PERSONAL))
        alone.clock.now = world.clock.now
        alone.history.add_reading(PERSONAL, "usage", 0.0, None, 20.0, WEEK, datetime(2026, 11, 20, 0, 38, tzinfo=UTC))
        passed = datetime(2026, 11, 20, 0, 30, tzinfo=UTC)
        alone.history.add_reading(WORK, "usage", 50.0, passed, 20.0, WEEK, datetime(2026, 11, 20, 0, 20, tzinfo=UTC))
        self.assertEqual(alone.service().take_reading(statusline_body("p", PERSONAL, half_hour, five=4.0, at=alone.clock.now)), PERSONAL)
        late = World(self, registered=(ALEX, WORK, PERSONAL))
        late.clock.now = datetime(2026, 11, 20, 0, 41, tzinfo=UTC)
        just_passed = datetime(2026, 11, 20, 0, 40, tzinfo=UTC)
        late.history.add_reading(PERSONAL, "usage", 0.0, None, 20.0, WEEK, datetime(2026, 11, 20, 0, 38, tzinfo=UTC))
        late.history.add_reading(WORK, "usage", 99.0, just_passed, 20.0, WEEK, datetime(2026, 11, 20, 0, 39, tzinfo=UTC))
        body = statusline_body("q", PERSONAL, just_passed, five=6.0, at=late.clock.now)
        self.assertEqual(late.service().take_reading(body), PERSONAL)



class ExchangeEdgeTests(unittest.TestCase):
    def away(self, service):
        service.set_presence({"idle_seconds": 700, "locked": False, "asleep": False})

    def limit_key(self):
        return "limit:{}:{}".format(ALEX, (R_ALEX - timedelta(hours=5)).isoformat())

    def test_a_devbox_step_starts_at_once_and_checks_the_devbox_inside_its_job(self):
        world = World(self)
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Silent(FakeLink):
            def reachable(self):
                gate.wait(10)
                return False

            def request(self, method, route, body=None):
                raise OSError("no route")

            def start_signin(self, route, body):
                raise AssertionError("no start without the other computer")

        service = world.service(link=Silent())
        started = time.monotonic()
        job = service.start_devbox_job("add", {"uuid": ALEX})["job"]
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 5))
        found = [j for j in service.state()["jobs"] if j["id"] == job][0]
        self.assertEqual((found["state"], found["result"]), ("failed", "The other computer did not answer in time, so nothing was changed."))

    def test_the_mac_always_sends_its_known_list_even_when_empty(self):
        world = World(self)
        world.link = FullLink()
        world.link.up = True
        world.link.devbox = WORK
        world.link.readings = world.devbox_readings()
        (world.folder / "known_accounts.json").write_text("[]")
        service = world.service()
        service.tick()
        self.assertTrue(world.link.known_pushed)
        self.assertEqual(world.link.known_pushed[-1], [])

    def test_a_good_exchange_stops_the_devbox_jobs_left_behind(self):
        world = World(self)
        stops, looked = [], []

        class Answering(FullLink):
            def find_request(self, token):
                looked.append(token)
                return 200, {"job": "e" * 32, "state": "running"}

            def stop_signin(self, job_id):
                stops.append(job_id)
                return 200, {"stopping": True}

        world.link = Answering()
        world.link.up = True
        world.link.devbox = WORK
        world.link.readings = world.devbox_readings()
        service = world.service()
        service._orphans.add("f" * 32)
        service.tick()
        self.assertEqual((looked, stops, service._orphans), (["f" * 32], ["e" * 32], set()))

    def test_the_command_line_can_hand_a_request_to_the_service(self):
        world = World(self)
        service = world.service()
        world.run(service)
        status, body = world.call("POST", "/devbox/unsure", {"request": "a" * 32})
        self.assertEqual((status, body), (202, {"kept": True}))
        self.assertEqual(service._orphans, {"a" * 32})
        for bad in ({"request": "short"}, {"request": "a" * 32, "x": 1}, {}, {"request": 7}):
            status, body = world.call("POST", "/devbox/unsure", bad)
            self.assertEqual((status, body), (400, {"error": server.BAD_UNSURE}))

    def test_a_thread_that_will_not_start_still_lets_the_mac_read_its_accounts(self):
        world = World(self)
        service = world.service(link_in_background=True)
        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            service.tick()
        self.assertTrue(world.usage.calls)

    def test_an_exchange_that_fails_still_lets_the_mac_read_its_accounts(self):
        world = World(self)
        service = world.service(link_in_background=True)
        service._link_exchange = mock.Mock(side_effect=RuntimeError("broken"))
        service.tick()
        self.assertTrue(world.wait_for(lambda: not service._link_busy, 5))
        service.tick()
        self.assertTrue(world.usage.calls)

    def test_a_first_exchange_that_hangs_holds_the_mac_back_one_tick_at_most(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Hanging(FakeLink):
            def reachable(self):
                gate.wait(10)
                return False

        world = World(self)
        service = world.service(link=Hanging(), link_in_background=True)
        self.assertEqual(service.tick_seconds, server.TICK_SECONDS)
        service.tick()
        self.assertEqual(world.usage.calls, [])
        world.clock.advance(seconds=service.tick_seconds - 1)
        service.tick()
        self.assertEqual(world.usage.calls, [])
        world.clock.advance(seconds=1)
        service.tick()
        self.assertTrue(world.usage.calls)

    def test_the_first_background_tick_leaves_the_devbox_accounts_alone(self):
        world = World(self)
        world.link = FullLink()
        world.link.up = True
        world.link.devbox = WORK
        world.link.status["covers"] = [WORK, ORG]
        world.link.readings = [reading for reading in world.devbox_readings() if reading.uuid in (WORK, ORG)]
        service = world.service(link=world.link, link_in_background=True)
        service.tick()
        self.assertTrue(world.wait_for(lambda: not service._link_busy, 5))
        self.assertTrue(world.wait_for(lambda: sorted(world.usage.calls) == sorted([ALEX, PERSONAL]), 5), world.usage.calls)

    def test_a_long_exchange_stamps_readings_with_its_own_time(self):
        world = World(self)

        class Slow(FullLink):
            def reachable(self):
                world.clock.advance(seconds=40)
                return True

        world.link = Slow()
        world.link.up = True
        world.link.devbox = WORK
        tick_time = world.clock.now
        later = tick_time + timedelta(seconds=40)
        world.link.readings = [Reading(ORG, "usage", 3.0, R_ALEX, 9.0, WEEK, later)]
        service = world.service(link=world.link, link_in_background=True)
        service._start_link_exchange(tick_time)
        self.assertTrue(world.wait_for(lambda: not service._link_busy, 5))
        self.assertEqual(world.history.latest(ORG).at, later)

    def test_a_failed_thread_start_never_leaves_the_exchange_busy(self):
        world = World(self)
        service = world.service(link_in_background=True)
        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            service._start_link_exchange(world.clock.now)
        self.assertFalse(service._link_busy)

    def test_stop_waits_for_the_exchange(self):
        world = World(self, figures=LIMIT)
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Slow(FakeLink):
            def reachable(self):
                gate.wait(10)
                return False

        service = world.service(link=Slow(), link_in_background=True)
        service.tick()
        threading.Timer(0.3, gate.set).start()
        service.stop()
        self.assertFalse(any(t.name == "lifeline-devbox-link" and t.is_alive() for t in threading.enumerate()))

    def test_a_devbox_job_that_starts_after_the_stop_is_stopped(self):
        world = World(self)
        stops = []

        class Late(FakeLink):
            def request(self, method, route, body=None):
                return 200, {"accounts": 4}

            def start_signin(self, route, body):
                service.stop()
                return 202, {"job": "d" * 32}

            def stop_signin(self, job_id):
                stops.append(job_id)
                return 200, {"stopping": True}

            def job(self, job_id):
                return 200, {"state": "running", "url": None}

        service = world.service(link=Late())
        service._devbox_proxy("job-1", "devbox-adopt", "/adopt", {}, service.parts.link)
        self.assertIn("d" * 32, stops)

    def test_the_live_line_keeps_the_old_sentence_when_the_login_is_in_lifeline(self):
        world = World(self, registered=(ALEX, WORK, PERSONAL))
        found = details(PERSONAL)
        found.pop("organizationUuid")
        (world.home / ".claude.json").write_text(json.dumps({"oauthAccount": found}))
        self.assertIsNone(world.service()._live_note())

    def test_the_mac_live_block_and_its_adopt_match_the_adopt_route(self):
        world = World(self)
        service = world.service()
        service.tick()
        live = service.state()["mac"]["live"]
        self.assertEqual(
            {key: live[key] for key in ("uuid", "added", "known", "name", "short", "adopt_ready")},
            {"uuid": ALEX, "added": True, "known": False, "name": "Alex's Account", "short": "Alex", "adopt_ready": True},
        )
        self.assertEqual(
            set(live), {"uuid", "added", "known", "name", "short", "email", "organization", "adopt_ready", "adopt_title"}
        )
        world.slot.item = {}
        service.tick()
        self.assertFalse(service.state()["mac"]["live"]["adopt_ready"])
        with self.assertRaises(server.Refusal) as caught:
            service.start_job("adopt", None, {})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.sentence, server.NO_LIVE_TO_ADOPT)


if __name__ == "__main__":
    unittest.main()
