import copy
import dataclasses
import fcntl
import json
import os
import sqlite3
import stat
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import ledger
from lifeline import reader as reading
from lifeline import recovery as recovering
from lifeline import switch as switching
from lifeline.accounts import Account, Registry, signin_end_text
from lifeline.anthropic import (
    NotAllowed,
    Profile,
    RateLimited,
    SignInDead,
    Unavailable,
    UnexpectedReply,
    Usage,
    Window,
)
from lifeline.history import History
from lifeline.reader import AccountStatus, Reader
from lifeline.slot import Slot, SlotError
from lifeline.store import FileStore, StoreError
from lifeline.writelock import held

ALEX = "11111111-aaaa-4aaa-8aaa-000000000001"
WORK = "22222222-bbbb-4bbb-8bbb-000000000002"
PERSONAL = "33333333-cccc-4ccc-8ccc-000000000003"
ORG = "44444444-dddd-4ddd-8ddd-000000000004"
TEAM_ORG = "66666666-ffff-4fff-8fff-000000000006"
TEAM = f"{PERSONAL}-{TEAM_ORG}"
ORDER = (ALEX, WORK, PERSONAL, ORG)
SAME_LOGIN = (ALEX, WORK, PERSONAL, TEAM)
NAMES = {
    ALEX: ("Alex's Account", "Alex", "Alex's Account"),
    WORK: ("Work Account", "Work", "Example Team's Account"),
    PERSONAL: ("Personal Account", "Personal", "Example Org Personal Account"),
    ORG: ("Team Account", "Team", "Example Org Team Account"),
    TEAM: ("Team Account", "Team", "Example Org Team Account"),
}
PAIRS = {TEAM: (PERSONAL, TEAM_ORG)}
TAGS = {TEAM: "teamorg0"}


def pair_of(uuid):
    return PAIRS.get(uuid, (uuid, f"org-{uuid[:8]}"))
START = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
FIVE_RESET = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)
WEEK_RESET = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
UNCONFIRMED = (
    "Lifeline could not confirm which account Claude Code is using. "
    "Until it can, it will not read that account or renew any saved sign-in."
)
SPARE = (
    "Claude Code is using {name}, and Lifeline also holds a spare saved sign-in for it. "
    "Lifeline will not use or renew the spare."
)
KEPT = (
    "Lifeline renewed the sign-in for {name} but could not save it. "
    "The new sign-in is kept in Lifeline's recovery folder until it can be saved. "
    "Readings for it are paused until {time}."
)
HELD = (
    "Lifeline renewed the sign-in for {name} but could not save it or write it to its recovery folder. "
    "The new sign-in is only in memory, so it is lost if Lifeline stops before it is saved. "
    "Lifeline tries again every minute. Readings for it are paused until {time}."
)
RECOVERED = "Lifeline saved the renewed sign-in for {name} from its recovery folder."
SAVED_FROM_MEMORY = "Lifeline saved the renewed sign-in for {name}."
GUARD = (
    "Lifeline could not check whether it may renew the sign-in for {name}, so it did not renew it. "
    "Readings for it are paused until {time}."
)
REAL_WRITE = recovering.write
REAL_REMOVE = recovering.remove


class Crash(BaseException):
    pass


def failing_writes(count):
    left = [count]

    def write(folder, uuid, oauth, **options):
        if left[0]:
            left[0] -= 1
            raise recovering.RecoveryError("The renewed sign-in could not be written to the recovery folder.")
        return REAL_WRITE(folder, uuid, oauth, **options)

    return write


def details(uuid):
    login, org = pair_of(uuid)
    return {
        "accountUuid": login,
        "emailAddress": f"{login[:8]}@example.invalid",
        "organizationUuid": org,
        "displayName": NAMES[uuid][0],
    }


def ms(moment):
    return int(moment.timestamp() * 1000)


def later(seconds):
    return START + timedelta(seconds=seconds)


def clock_text(moment):
    local = (moment.astimezone() + timedelta(seconds=30)).replace(second=0, microsecond=0)
    day = "today" if local.date() == START.astimezone().date() else "tomorrow"
    return f"{day} at {local:%H:%M}"


def paused(name, seconds):
    return f"Readings for {name} are paused until {clock_text(later(seconds))}."


def cache_entry(uuid, fetched, five=33.5, week=12.0):
    entry = {
        "fetchedAtMs": ms(fetched),
        "utilization": {
            "five_hour": {"utilization": five, "resets_at": "2026-11-19T17:00:00+00:00"},
            "seven_day": {"utilization": week, "resets_at": "2026-11-24T09:00:00Z"},
            "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
        },
    }
    if uuid is not None:
        entry["accountUuid"] = uuid
    return entry


class FakeClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class FakeSlot(Slot):
    def __init__(self):
        self.item = None
        self.writes = []
        self.reads = 0
        self.broken = False

    def read(self):
        self.reads += 1
        if self.broken:
            raise SlotError("The stored sign-in is not valid JSON.")
        return copy.deepcopy(self.item)

    def write(self, item):
        self.writes.append(copy.deepcopy(item))


class LoggingStore:
    def __init__(self, inner, log):
        self.inner = inner
        self.log = log
        self.put_failures = []
        self.lost_puts = 0
        self.put_hook = None
        self.get_hook = None
        self.broken = {}
        self.gets = []

    def get(self, uuid):
        self.gets.append(uuid)
        if self.get_hook is not None:
            self.get_hook(uuid)
        if uuid in self.broken:
            raise StoreError(self.broken[uuid])
        return self.inner.get(uuid)

    def put(self, uuid, oauth):
        self.log.append(("put", uuid, oauth.get("accessToken")))
        if self.put_hook is not None:
            self.put_hook(uuid, oauth)
        if self.put_failures and self.put_failures.pop(0):
            raise StoreError("The new parked sign-in could not be saved.")
        if self.lost_puts:
            self.lost_puts -= 1
            return
        self.inner.put(uuid, oauth)

    def remove(self, uuid):
        self.inner.remove(uuid)


class FakeUsage:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.plans = {}

    def plan(self, uuid, *outcomes):
        self.plans[uuid] = list(outcomes)

    def __call__(self, token):
        uuid = self.world.owner(token)
        self.calls.append((self.world.clock.now, uuid, token))
        self.world.log.append(("usage", uuid, token))
        if self.world.on_usage is not None:
            self.world.on_usage(uuid, token)
        outcomes = self.plans.get(uuid) or [(10.0, 5.0)]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, Usage):
            return outcome
        five, week = outcome
        return Usage(
            five_hour=Window(five, FIVE_RESET),
            seven_day=Window(week, WEEK_RESET),
            fetched_at=self.world.clock.now,
        )

    def times(self, uuid):
        return [at for at, owner, _ in self.calls if owner == uuid]

    def tokens(self, uuid):
        return [token for _, owner, token in self.calls if owner == uuid]


class FakeRefresh:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.errors = {}
        self.issued = []

    def __call__(self, signin):
        oauth = signin["claudeAiOauth"]
        uuid = self.world.owner(oauth.get("refreshToken"))
        self.calls.append(uuid)
        self.world.log.append(("refresh", uuid, self.world.lock_held()))
        if uuid in self.errors:
            raise self.errors[uuid]
        fresh = self.world.issue(uuid)
        renewed = copy.deepcopy(signin)
        for key in ("accessToken", "refreshToken", "expiresAt"):
            renewed["claudeAiOauth"][key] = fresh[key]
        self.issued.append(copy.deepcopy(renewed["claudeAiOauth"]))
        return renewed


class FakeProfiles:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.under_lock = []
        self.offline = False

    def __call__(self, token):
        self.calls.append(token)
        self.under_lock.append(self.world.lock_held())
        if self.offline:
            raise Unavailable("Could not reach the server")
        uuid = self.world.owner(token)
        if uuid is None:
            raise SignInDead()
        login, org = pair_of(uuid)
        return Profile(
            account_uuid=login,
            email=f"{login[:8]}@example.invalid",
            org_uuid=org,
            rate_limit_tier="default_claude_max_5x",
        )


class World:
    def __init__(self, test, order=ORDER):
        tmp = TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.folder = root / "lifeline"
        self.folder.mkdir()
        self.lock_path = self.folder / "lifeline.lock"
        self.record_path = self.folder / "last_loaded.json"
        self.recovery = self.folder / "recovery"
        self.state = self.folder / "reader_state.json"
        self.clock = FakeClock()
        self.log = []
        self.owners = {}
        self.serial = 0
        self.on_usage = None
        self.registry = Registry(self.folder / "accounts.json")
        self.registry.save([self.account(uuid, order) for uuid in order])
        self.store = LoggingStore(FileStore(root / "signins"), self.log)
        self.history = History(self.folder / "history.db")
        self.slot = FakeSlot()
        self.usage = FakeUsage(self)
        self.refresh = FakeRefresh(self)
        self.profile = FakeProfiles(self)

    @staticmethod
    def account(uuid, order=ORDER):
        name, short, full = NAMES[uuid]
        login = pair_of(uuid)[0]
        return Account(
            uuid, f"{login[:8]}@example.invalid", name, short, full, "Max 5x", order.index(uuid) + 1, details(uuid),
            login_uuid=login if login != uuid else None,
        )

    def owner(self, token):
        return self.owners.get(token)

    def issue(self, uuid, seconds=3600):
        self.serial += 1
        tag = TAGS.get(uuid, uuid[:8])
        oauth = {
            "accessToken": f"access-{tag}-{self.serial}-SECRET",
            "refreshToken": f"refresh-{tag}-{self.serial}-SECRET",
            "expiresAt": ms(self.clock.now + timedelta(seconds=seconds)),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
        self.owners[oauth["accessToken"]] = uuid
        self.owners[oauth["refreshToken"]] = uuid
        return oauth

    def park(self, uuid, seconds=3600):
        oauth = self.issue(uuid, seconds)
        self.store.inner.put(uuid, oauth)
        return oauth

    def go_live(self, uuid, seconds=3600):
        oauth = self.issue(uuid, seconds)
        self.slot.item = {
            "claudeAiOauth": oauth,
            "mcpOAuth": {"github|1a2b": {"accessToken": "mcp-access-SECRET"}},
        }
        return oauth

    def remember_live(self, uuid, cache=None, oauth=None):
        loaded = oauth if oauth is not None else (self.slot.item or {}).get("claudeAiOauth")
        switching.record_confirmed(self.record_path, uuid, {"claudeAiOauth": loaded}, self.clock.now)
        data = {"numStartups": 3, "oauthAccount": details(uuid)}
        if cache is not None:
            data["cachedUsageUtilization"] = cache
        (self.home / ".claude.json").write_text(json.dumps(data, indent=2))

    def reader(self, **options):
        return Reader(
            registry=self.registry,
            store=self.store,
            slot=self.slot,
            history=self.history,
            clock=self.clock,
            home=self.home,
            record_path=self.record_path,
            lock_path=self.lock_path,
            usage_fn=self.usage,
            refresh_fn=self.refresh,
            profile_fn=self.profile,
            **options,
        )

    def lock_held(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def recovery_file(self, uuid):
        return self.recovery / f"{uuid}.json"

    def puts(self, uuid=None):
        return [e for e in self.log if e[0] == "put" and (uuid is None or e[1] == uuid)]


ORIGINAL_IDENTIFY = switching.identify_live


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        backoff = mock.patch.object(reading, "SAVE_BACKOFF_S", (0.0, 0.0))
        backoff.start()
        self.addCleanup(backoff.stop)

    def tick_at(self, reader, seconds):
        self.world.clock.now = later(seconds)
        return reader.tick()

    def fail_renewal_save(self, seconds=-30):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=seconds)
        w.store.put_failures = [False, True, True, True]
        r = w.reader()
        reports = self.tick_at(r, 0)
        return r, old, w.refresh.issued[0], reports

    def test_reader_honours_retry_after(self):
        w = self.world
        w.park(WORK)
        earlier = w.history.add_reading(WORK, "usage", 40.0, FIVE_RESET, 20.0, WEEK_RESET, START - timedelta(minutes=10))
        w.usage.plan(WORK, RateLimited(3600), (41.0, 20.0))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [paused("Work Account", 3600)])
        status = r.status(WORK)
        self.assertEqual(status.paused_until, later(3600))
        self.assertFalse(status.needs_signin)
        self.assertFalse(status.cannot_read)
        self.assertIsInstance(status.last_error, str)
        self.assertEqual(w.history.latest(WORK), earlier)
        for seconds in (180, 300, 1800, 3599):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.usage.times(WORK), [later(0)])
        self.assertEqual(self.tick_at(r, 3600), [])
        self.assertEqual(w.usage.times(WORK), [later(0), later(3600)])
        self.assertEqual(r.status(WORK), AccountStatus(False, False, None, None))
        self.assertEqual(w.history.latest(WORK).five, 41.0)

    def test_restart_during_a_rate_limit_keeps_the_pause(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, RateLimited(3600), (41.0, 20.0))
        first = w.reader()
        self.tick_at(first, 0)
        self.assertEqual(stat.S_IMODE(os.stat(w.state).st_mode), 0o600)
        saved = w.state.read_text()
        self.assertNotIn("SECRET", saved)
        self.assertIn(WORK, json.loads(saved)["accounts"])
        second = w.reader()
        self.assertEqual(self.tick_at(second, 30), [])
        self.assertEqual(second.status(WORK).paused_until, later(3600))
        self.assertEqual(second.status(WORK).last_error, reading.READ_ERRORS[RateLimited])
        self.assertEqual(self.tick_at(second, 3599), [])
        self.assertEqual(w.usage.times(WORK), [later(0)])
        self.tick_at(second, 3600)
        self.assertEqual(w.usage.times(WORK), [later(0), later(3600)])
        self.assertEqual(json.loads(w.state.read_text())["accounts"], {})
        third = w.reader()
        self.tick_at(third, 3610)
        self.assertEqual(len(w.usage.times(WORK)), 2)

    def test_state_file_with_a_strange_error_still_starts(self):
        w = self.world
        w.park(WORK)
        for error in (["x"], {"a": 1}, 7, None):
            w.state.write_text(json.dumps({"accounts": {WORK: {"paused_until_ms": ms(later(900)), "last_error": error}}}))
            r = w.reader()
            self.tick_at(r, 0)
            self.assertEqual(r.status(WORK), AccountStatus(False, False, later(900), None))
        self.assertEqual(w.usage.times(WORK), [])

    def test_loaded_pause_is_capped(self):
        w = self.world
        w.park(WORK)
        far = datetime(2030, 1, 1, tzinfo=timezone.utc)
        w.state.write_text(json.dumps({"accounts": {WORK: {"paused_until_ms": ms(far), "last_error": None}}}))
        r = w.reader()
        self.tick_at(r, 0)
        self.assertEqual(r.status(WORK).paused_until, later(reading.MAX_WAIT_S))
        self.tick_at(r, reading.MAX_WAIT_S - 1)
        self.assertEqual(w.usage.times(WORK), [])
        self.tick_at(r, reading.MAX_WAIT_S)
        self.assertEqual(w.usage.times(WORK), [later(reading.MAX_WAIT_S)])

    def test_failed_state_write_is_retried_every_tick(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, RateLimited(3600), (41.0, 20.0))
        r = w.reader()
        real = reading._write_private
        attempts = []
        failing = [True]

        def flaky(path, data):
            attempts.append(path)
            if failing[0]:
                raise OSError("disk hiccup")
            return real(path, data)

        with mock.patch.object(reading, "_write_private", flaky):
            self.tick_at(r, 0)
            self.assertFalse(w.state.exists())
            tried = len(attempts)
            self.tick_at(r, 15)
            self.assertFalse(w.state.exists())
            self.assertEqual(len(attempts), tried + 1)
            failing[0] = False
            self.tick_at(r, 30)
            self.assertTrue(w.state.exists())
            done = len(attempts)
            self.tick_at(r, 45)
            self.assertEqual(len(attempts), done)
        restarted = w.reader()
        self.tick_at(restarted, 700)
        self.assertEqual(w.usage.times(WORK), [later(0)])
        self.assertEqual(restarted.status(WORK).paused_until, later(3600))

    def test_damaged_state_file_is_ignored(self):
        w = self.world
        w.park(WORK)
        for text in ("{not json", '{"accounts": []}', '{"accounts": {"x": {"paused_until_ms": "soon"}}}'):
            w.state.write_text(text)
            r = w.reader()
            self.tick_at(r, 0)
        self.assertEqual(len(w.usage.times(WORK)), 1)
        w.state.write_text(json.dumps({"accounts": {WORK: {"paused_until_ms": ms(later(900)), "last_error": "access-x-SECRET"}}}))
        r = w.reader()
        self.tick_at(r, 300)
        self.assertEqual(r.status(WORK).paused_until, later(900))
        self.assertIsNone(r.status(WORK).last_error)
        self.assertEqual(len(w.usage.times(WORK)), 1)

    def test_short_retry_after_still_waits_180_seconds(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, RateLimited(5), (41.0, 20.0))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [paused("Work Account", 180)])
        self.assertEqual(self.tick_at(r, 179), [])
        self.assertEqual(len(w.usage.times(WORK)), 1)
        self.tick_at(r, 180)
        self.assertEqual(w.usage.times(WORK), [later(0), later(180)])

    def test_huge_or_broken_retry_after_is_bounded(self):
        w = self.world
        w.park(WORK)
        w.park(ORG)
        w.usage.plan(WORK, RateLimited(10**400))
        w.usage.plan(ORG, RateLimited(float("nan")))
        r = w.reader()
        self.assertEqual(
            self.tick_at(r, 0),
            [paused("Work Account", reading.MAX_WAIT_S), paused("Team Account", 300)],
        )
        self.assertEqual(r.status(WORK).paused_until, later(reading.MAX_WAIT_S))
        self.assertEqual(r.status(ORG).paused_until, later(300))

    def test_reply_with_no_windows_is_treated_as_unexpected(self):
        w = self.world
        w.park(WORK)
        earlier = w.history.add_reading(WORK, "usage", 72.0, FIVE_RESET, 40.0, WEEK_RESET, START - timedelta(minutes=10))
        empty = Usage(five_hour=None, seven_day=None)
        w.usage.plan(WORK, empty, empty, (73.0, 40.0))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [paused("Work Account", 300)])
        self.assertEqual(w.history.latest(WORK), earlier)
        self.assertEqual(r.status(WORK).last_error, reading.READ_ERRORS[UnexpectedReply])
        self.assertEqual(self.tick_at(r, 299), [])
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.history.latest(WORK), earlier)
        self.assertEqual(self.tick_at(r, 600), [])
        self.assertEqual(w.history.latest(WORK).five, 73.0)
        self.assertEqual(r.status(WORK), AccountStatus(False, False, None, None))
        self.assertEqual(w.usage.times(WORK), [later(0), later(300), later(600)])

    def test_reply_with_one_window_is_stored(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, Usage(five_hour=None, seven_day=Window(12.0, WEEK_RESET)))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        latest = w.history.latest(WORK)
        self.assertEqual((latest.five, latest.week), (None, 12.0))

    def test_active_slot_never_renewed(self):
        w = self.world
        w.go_live(ALEX, seconds=-10)
        w.remember_live(ALEX)
        r = w.reader()
        for seconds in (0, 300, 600, 3600, 7200):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(ALEX), [])
        self.assertEqual(w.slot.writes, [])
        self.assertEqual(w.puts(), [])
        fresh = w.go_live(ALEX)
        self.assertEqual(self.tick_at(r, 7500), [])
        self.assertEqual(w.usage.tokens(ALEX), [fresh["accessToken"]])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.slot.writes, [])
        self.assertEqual(w.puts(), [])

    def test_live_key_within_300_seconds_is_left_to_claude_code(self):
        w = self.world
        w.go_live(ALEX, seconds=300)
        w.remember_live(ALEX)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.usage.times(ALEX), [])
        w.clock.now = later(300)
        fresh = w.go_live(ALEX, seconds=301)
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.usage.tokens(ALEX), [fresh["accessToken"]])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.slot.writes, [])

    def test_live_401_during_claude_code_renewal_marks_nothing(self):
        w = self.world
        w.go_live(ALEX)
        renewed = {}

        def renew_during_read(uuid, token):
            if uuid == ALEX and not renewed:
                renewed.update(w.go_live(ALEX))
                raise SignInDead()

        w.on_usage = renew_during_read
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(r.status(ALEX), AccountStatus(False, False, None, None))
        self.assertEqual(self.tick_at(r, 299), [])
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.usage.tokens(ALEX)[-1], renewed["accessToken"])
        self.assertEqual(len(w.usage.times(ALEX)), 2)
        self.assertFalse(r.status(ALEX).needs_signin)

    def test_live_401_with_the_same_signin_needs_a_new_signin(self):
        w = self.world
        w.go_live(ALEX)
        w.usage.plan(ALEX, SignInDead())
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), ["Alex's Account needs a new sign-in."])
        self.assertTrue(r.status(ALEX).needs_signin)
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(len(w.usage.times(ALEX)), 1)

    def test_parked_renewal_saved_before_use(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=30)
        seen = []

        def check(uuid, token):
            if uuid == WORK:
                seen.append(w.store.inner.get(WORK))

        w.on_usage = check
        on_disk_at_put = []
        w.store.put_hook = lambda uuid, oauth: on_disk_at_put.append(recovering.pending(w.recovery, uuid))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [WORK])
        new = w.store.inner.get(WORK)
        self.assertEqual(on_disk_at_put, [None, new])
        self.assertEqual(new, w.refresh.issued[0])
        self.assertNotEqual(new["refreshToken"], old["refreshToken"])
        self.assertEqual(new["scopes"], old["scopes"])
        events = [e for e in w.log if e[1] == WORK]
        self.assertEqual(
            events,
            [
                ("put", WORK, old["accessToken"]),
                ("refresh", WORK, True),
                ("put", WORK, new["accessToken"]),
                ("usage", WORK, new["accessToken"]),
            ],
        )
        self.assertEqual(seen, [new])
        self.assertFalse(w.lock_held())
        self.assertEqual(w.history.latest(WORK).source, "usage")
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(os.listdir(w.recovery), [])

    def test_store_not_writable_means_no_renewal(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        w.store.put_failures = [True]
        r = w.reader()
        self.assertEqual(
            self.tick_at(r, 0),
            [
                "Lifeline could not write its saved sign-in for Work Account, so it did not renew it. "
                f"Readings for it are paused until {clock_text(later(300))}."
            ],
        )
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(w.store.inner.get(WORK), old)
        self.tick_at(r, 300)
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(len(w.usage.times(WORK)), 1)

    def test_save_after_renewal_retried_twice_with_backoff(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)
        w.store.put_failures = [False, True, True]
        naps = []
        r = w.reader()
        with mock.patch.object(reading, "SAVE_BACKOFF_S", (0.25, 1.0)), mock.patch.object(reading, "_nap", naps.append):
            self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(naps, [0.25, 1.0])
        new = w.refresh.issued[0]
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(len(w.puts(WORK)), 4)
        self.assertFalse(w.recovery_file(WORK).exists())

    def test_unsaved_renewal_kept_in_recovery_folder_and_saved_a_minute_later(self):
        w = self.world
        r, old, new, reports = self.fail_renewal_save()
        self.assertEqual(reports, [KEPT.format(name="Work Account", time=clock_text(later(300)))])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(w.store.inner.get(WORK), old)
        path = w.recovery_file(WORK)
        self.assertEqual(json.loads(path.read_text()), {"claudeAiOauth": new, "spent": switching.fingerprint(old)})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(w.recovery).st_mode), 0o700)
        self.assertEqual(r.status(WORK).last_error, reading.UNSAVED_ERROR)
        puts = len(w.puts(WORK))
        self.assertEqual(self.tick_at(r, 30), [])
        self.assertEqual(self.tick_at(r, 59), [])
        self.assertEqual(len(w.puts(WORK)), puts)
        self.assertEqual(self.tick_at(r, 60), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(path.exists())
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_unsaved_renewal_retried_at_most_once_a_minute(self):
        w = self.world
        r, old, new, _ = self.fail_renewal_save()
        w.store.put_failures = [True] * 30
        for seconds in range(60, 300, 15):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(len(w.puts(WORK)), 4 + 4)
        self.assertTrue(w.recovery_file(WORK).exists())
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(w.usage.times(WORK), [])
        w.store.put_failures = []
        self.assertEqual(self.tick_at(r, 300), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(w.usage.times(WORK), [later(300)])
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])

    def test_recovery_file_kept_until_the_store_really_holds_the_pair(self):
        w = self.world
        r, old, new, _ = self.fail_renewal_save()
        w.store.lost_puts = 1
        self.assertEqual(self.tick_at(r, 60), [])
        self.assertEqual(w.store.inner.get(WORK), old)
        self.assertEqual(json.loads(w.recovery_file(WORK).read_text()), {"claudeAiOauth": new, "spent": switching.fingerprint(old)})
        self.assertEqual(self.tick_at(r, 120), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())

    def crash_then_restart(self, patcher):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        r = w.reader()
        with patcher():
            with self.assertRaises(Crash):
                self.tick_at(r, 0)
        new = w.refresh.issued[0]
        self.assertFalse(w.lock_held())
        restarted = w.reader()
        return old, new, restarted

    def test_crash_after_the_recovery_write_keeps_the_new_pair(self):
        w = self.world

        def write_then_crash(folder, uuid, oauth, **options):
            REAL_WRITE(folder, uuid, oauth, **options)
            raise Crash()

        old, new, restarted = self.crash_then_restart(lambda: mock.patch.object(recovering, "write", write_then_crash))
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(restarted, 15), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_crash_after_the_store_write_keeps_the_new_pair(self):
        w = self.world
        real_put = w.store.put
        seen = []

        def put_then_crash(uuid, oauth):
            real_put(uuid, oauth)
            seen.append(uuid)
            if len(seen) == 2:
                raise Crash()

        old, new, restarted = self.crash_then_restart(lambda: mock.patch.object(w.store, "put", put_then_crash))
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(restarted, 15), [])
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_crash_after_the_file_is_removed_keeps_the_new_pair(self):
        w = self.world

        def remove_then_crash(folder, uuid, expected):
            REAL_REMOVE(folder, uuid, expected)
            raise Crash()

        old, new, restarted = self.crash_then_restart(lambda: mock.patch.object(recovering, "remove", remove_then_crash))
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(restarted, 15), [])
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def refused_long_key(self):
        w = self.world
        w.go_live(ALEX)
        w.remember_live(ALEX)
        old = w.park(WORK, seconds=7200)
        w.usage.plan(WORK, SignInDead(), (10.0, 5.0))
        return old

    def test_renewal_shorter_than_the_refused_key_is_saved(self):
        w = self.world
        old = self.refused_long_key()
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [WORK])
        new = w.refresh.issued[0]
        self.assertLess(new["expiresAt"], old["expiresAt"])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(os.listdir(w.recovery), [])
        with held(w.lock_path):
            choice = recovering.choose(w.store, w.recovery, WORK)
        self.assertEqual((choice.pair, choice.from_file), (new, False))
        self.assertEqual(self.tick_at(r, 60), [])
        self.assertEqual(self.tick_at(r, 180), [])
        self.assertEqual(w.usage.tokens(WORK), [old["accessToken"], new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_crash_before_the_store_write_keeps_a_renewal_shorter_than_the_refused_key(self):
        w = self.world
        old = self.refused_long_key()

        def write_then_crash(folder, uuid, oauth, **options):
            REAL_WRITE(folder, uuid, oauth, **options)
            raise Crash()

        r = w.reader()
        with mock.patch.object(recovering, "write", write_then_crash):
            with self.assertRaises(Crash):
                self.tick_at(r, 0)
        new = w.refresh.issued[0]
        self.assertFalse(w.lock_held())
        self.assertEqual(w.store.inner.get(WORK), old)
        self.assertEqual(
            json.loads(w.recovery_file(WORK).read_text()),
            {"claudeAiOauth": new, "spent": switching.fingerprint(old)},
        )
        with held(w.lock_path):
            choice = recovering.choose(w.store, w.recovery, WORK)
        self.assertEqual((choice.pair, choice.from_file), (new, False))
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        restarted = w.reader()
        self.assertEqual(self.tick_at(restarted, 180), [])
        self.assertEqual(w.usage.tokens(WORK)[-1], new["accessToken"])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_restart_after_a_crash_before_the_store_write_saves_the_shorter_renewal(self):
        w = self.world
        old = self.refused_long_key()

        def write_then_crash(folder, uuid, oauth, **options):
            REAL_WRITE(folder, uuid, oauth, **options)
            raise Crash()

        r = w.reader()
        with mock.patch.object(recovering, "write", write_then_crash):
            with self.assertRaises(Crash):
                self.tick_at(r, 0)
        new = w.refresh.issued[0]
        restarted = w.reader()
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(restarted, 15), [RECOVERED.format(name="Work Account")])
        with held(w.lock_path):
            choice = recovering.choose(w.store, w.recovery, WORK)
        self.assertEqual((choice.pair, choice.from_file), (new, False))
        self.assertEqual(w.refresh.calls, [WORK])

    def test_unsaved_renewal_in_memory_keeps_its_spent_record(self):
        w = self.world
        old = self.refused_long_key()
        w.store.put_failures = [False, True, True, True]
        r = w.reader()
        with mock.patch.object(recovering, "write", failing_writes(1)):
            self.tick_at(r, 0)
            new = w.refresh.issued[0]
            self.assertEqual(w.store.inner.get(WORK), old)
            self.assertFalse(w.recovery_file(WORK).exists())
            w.store.put_failures = [True]
            self.assertEqual(self.tick_at(r, 60), [])
        self.assertEqual(
            json.loads(w.recovery_file(WORK).read_text()),
            {"claudeAiOauth": new, "spent": switching.fingerprint(old)},
        )
        self.assertEqual(self.tick_at(r, 120), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())

    def test_memory_only_renewal_saved_straight_over_the_pair_it_spent(self):
        w = self.world
        old = self.refused_long_key()
        w.store.put_failures = [False, True, True, True]
        r = w.reader()
        with mock.patch.object(recovering, "write", failing_writes(99)):
            self.tick_at(r, 0)
            new = w.refresh.issued[0]
            self.assertEqual(self.tick_at(r, 60), [SAVED_FROM_MEMORY.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)

    def ledger_marks(self, uuid):
        return ledger.spent(self.world.folder, uuid)

    def test_every_renewal_writes_the_pair_it_spent_to_the_ledger(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=30)
        r = w.reader()
        with mock.patch.object(recovering, "write", failing_writes(1)):
            self.assertEqual(self.tick_at(r, 0), [])
        first = w.refresh.issued[0]
        self.assertEqual(w.store.inner.get(WORK), first)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.ledger_marks(WORK), frozenset({switching.fingerprint(old)}))
        self.assertNotIn("SECRET", (w.folder / ledger.NAME).read_text())
        self.tick_at(r, 3600)
        self.assertEqual(w.refresh.calls, [WORK, WORK])
        self.assertEqual(self.ledger_marks(WORK), frozenset({switching.fingerprint(old), switching.fingerprint(first)}))
        self.assertEqual(self.ledger_marks(ALEX), frozenset())

    def test_a_failed_ledger_write_never_blocks_saving_the_renewal(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=30)
        r = w.reader()
        with mock.patch.object(reading.ledger, "add", side_effect=ledger.LedgerError("no")):
            self.assertEqual(self.tick_at(r, 0), [reading.ledger_problem(self.world.folder)])
        new = w.refresh.issued[0]
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(
            json.loads(w.recovery_file(WORK).read_text()), {"claudeAiOauth": new, "spent": switching.fingerprint(old)}
        )
        self.assertEqual(self.ledger_marks(WORK), frozenset())
        self.assertEqual(self.tick_at(r, 60), [])
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.ledger_marks(WORK), frozenset({switching.fingerprint(old)}))

    def test_a_renewal_writes_its_recovery_file_before_its_spent_mark(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=30)
        r = w.reader()
        seen = []
        real_add = reading.ledger.add

        def add(base, uuid, spent, now=None):
            seen.append(recovering.pending(w.recovery, uuid))
            return real_add(base, uuid, spent, now)

        with mock.patch.object(reading.ledger, "add", side_effect=add):
            self.assertEqual(self.tick_at(r, 0), [])
        new = w.refresh.issued[0]
        self.assertTrue(seen)
        self.assertEqual(seen[0], new)
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(self.ledger_marks(WORK), frozenset({switching.fingerprint(old)}))

    def test_the_ledger_problem_names_the_file_and_says_what_to_do(self):
        text = reading.ledger_problem(self.world.folder)
        self.assertIn(str(self.world.folder / ledger.NAME), text)
        self.assertIn("Make that file a plain file you can read and write.", text)
        self.assertIn("If it is damaged, rename it, and Lifeline starts a new one.", text)

    def test_renewal_sets_aside_a_store_copy_another_writer_put_there(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=30)
        other = w.issue(WORK, seconds=600)
        real = w.refresh

        def refresh_then_replace(signin):
            renewed = real(signin)
            w.store.inner.put(WORK, other)
            return renewed

        w.refresh = refresh_then_replace
        events = []
        real_set_aside = recovering.set_aside

        def logged(folder, uuid, oauth):
            events.append(("aside", oauth["accessToken"]))
            return real_set_aside(folder, uuid, oauth)

        w.store.put_hook = lambda uuid, oauth: events.append(("put", oauth["accessToken"]))
        r = w.reader()
        with mock.patch.object(recovering, "set_aside", side_effect=logged):
            self.assertEqual(self.tick_at(r, 0), [])
        new = real.issued[0]
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(events[-2:], [("aside", other["accessToken"]), ("put", new["accessToken"])])

    def test_a_kept_older_copy_never_stops_renewals(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        older = w.issue(WORK, seconds=-3600)
        recovering.rescue(w.recovery, WORK, older)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [WORK])
        new = w.refresh.issued[0]
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertTrue(recovering.holds(w.recovery, WORK, older))
        self.assertIn(switching.fingerprint(old), self.ledger_marks(WORK))

    def test_memory_only_renewal_kept_when_the_store_copy_is_ordered_first(self):
        w = self.world
        with mock.patch.object(recovering, "write", failing_writes(99)):
            r, old, new, reports = self.fail_renewal_save()
            later_copy = w.issue(WORK, seconds=7200)
            w.store.inner.put(WORK, later_copy)
            w.clock.now = later(60)
            self.assertEqual(r.flush(), ([], (WORK,)))
            self.assertEqual(w.store.inner.get(WORK), later_copy)
        w.clock.now = later(120)
        self.assertEqual(r.flush(), ([], ()))
        self.assertEqual(recovering.pending(w.recovery, WORK), new)
        self.assertEqual(w.store.inner.get(WORK), later_copy)

    def test_a_ledger_that_cannot_be_read_is_reported_once(self):
        w = self.world
        w.park(WORK)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        path = w.folder / ledger.NAME
        target = w.folder / "elsewhere.json"
        target.write_text("{}")
        os.symlink(target, path)
        self.assertEqual(self.tick_at(r, 30), [reading.ledger_problem(self.world.folder)])
        self.assertEqual(w.usage.times(WORK), [later(0)])
        self.assertEqual(self.tick_at(r, 300), [])
        path.unlink()
        self.assertEqual(self.tick_at(r, 600), [])
        os.symlink(target, path)
        self.assertEqual(self.tick_at(r, 900), [reading.ledger_problem(self.world.folder)])
        self.assertNotIn("SECRET", reading.ledger_problem(self.world.folder))

    def test_a_damaged_linked_or_unreadable_ledger_is_reported_once(self):
        for state in ("damaged", "linked", "unreadable"):
            with self.subTest(state=state):
                self.setUp()
                w = self.world
                w.park(WORK)
                r = w.reader()
                self.assertEqual(self.tick_at(r, 0), [])
                path = w.folder / ledger.NAME
                path.write_text('{"accounts": {}}')
                if state == "damaged":
                    path.write_text("{broken")
                elif state == "linked":
                    target = w.folder / "elsewhere.json"
                    os.replace(path, target)
                    os.symlink(target, path)
                else:
                    os.chmod(path, 0)
                    self.addCleanup(os.chmod, path, 0o600)
                self.assertEqual(self.tick_at(r, 30), [reading.ledger_problem(self.world.folder)])
                self.assertEqual(self.tick_at(r, 300), [])
                self.assertEqual(self.tick_at(r, 600), [])

    def test_a_renewal_that_keeps_its_renewal_key_marks_nothing_spent(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=30)
        kept = []

        def keep_key(signin):
            fresh = w.issue(WORK)
            renewed = copy.deepcopy(signin)
            renewed["claudeAiOauth"]["accessToken"] = fresh["accessToken"]
            renewed["claudeAiOauth"]["expiresAt"] = fresh["expiresAt"]
            kept.append(copy.deepcopy(renewed["claudeAiOauth"]))
            return renewed

        w.refresh = keep_key
        w.store.put_failures = [False, True, True, True]
        r = w.reader()
        self.tick_at(r, 0)
        new = kept[0]
        self.assertEqual(json.loads(w.recovery_file(WORK).read_text()), {"claudeAiOauth": new})
        self.assertEqual(self.ledger_marks(WORK), frozenset())
        self.tick_at(r, 60)
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(self.ledger_marks(WORK), frozenset())
        self.assertEqual(list(w.recovery.iterdir()), [])

    def ends(self):
        return {account.uuid: account.signin_ends for account in self.world.registry.load()}

    def dated(self, *uuids):
        w = self.world
        for uuid in uuids:
            w.registry.upsert(dataclasses.replace(w.registry.by_uuid(uuid), signin_ends="2026-11-30T00:00:00Z"))

    def test_sign_in_ends_follow_the_live_and_parked_sign_ins_under_the_lock(self):
        w = self.world
        self.dated(ALEX, WORK)
        live = w.go_live(ALEX)
        live["refreshTokenExpiresAt"] = ms(later(20 * 86400))
        parked = w.issue(WORK)
        parked["refreshTokenExpiresAt"] = ms(later(25 * 86400))
        w.store.inner.put(WORK, parked)
        undated = w.issue(PERSONAL)
        undated["refreshTokenExpiresAt"] = ms(later(5 * 86400))
        w.store.inner.put(PERSONAL, undated)
        locked = []
        real = w.registry.save

        def spy(accounts):
            locked.append(w.lock_held())
            return real(accounts)

        with mock.patch.object(w.registry, "save", side_effect=spy):
            r = w.reader()
            self.tick_at(r, 0)
            ends = self.ends()
            self.assertEqual(ends[ALEX], signin_end_text(live, later(0)))
            self.assertEqual(ends[WORK], signin_end_text(parked, later(0)))
            self.assertIsNone(ends[PERSONAL])
            self.assertEqual(locked, [True, True])
            self.tick_at(r, 3600)
            self.assertEqual(locked, [True, True])

    def test_a_sign_in_with_no_known_end_never_clears_a_recorded_one(self):
        w = self.world
        self.dated(ALEX, WORK)
        live = w.go_live(ALEX)
        stale = w.issue(WORK)
        stale["refreshTokenExpiresAt"] = ms(later(-10 * 86400))
        w.store.inner.put(WORK, stale)
        r = w.reader()
        self.tick_at(r, 0)
        self.assertNotIn("refreshTokenExpiresAt", live)
        self.assertEqual(self.ends()[ALEX], "2026-11-30T00:00:00Z")
        self.assertEqual(self.ends()[WORK], "2026-11-30T00:00:00Z")

    def test_a_renewal_records_the_end_of_the_new_pair(self):
        w = self.world
        self.dated(WORK)
        w.go_live(ALEX)
        old = w.issue(WORK, seconds=30)
        old["refreshTokenExpiresAt"] = ms(later(10 * 86400))
        w.store.inner.put(WORK, old)
        real = w.refresh
        ends = [ms(later(30 * 86400)), None]

        def renew(signin):
            renewed = real(signin)
            end = ends.pop(0)
            if end is None:
                renewed["claudeAiOauth"].pop("refreshTokenExpiresAt", None)
            else:
                renewed["claudeAiOauth"]["refreshTokenExpiresAt"] = end
            return renewed

        w.refresh = renew
        r = w.reader()
        self.tick_at(r, 0)
        new = w.store.inner.get(WORK)
        self.assertEqual(new["refreshTokenExpiresAt"], ms(later(30 * 86400)))
        self.assertEqual(self.ends()[WORK], signin_end_text(new, later(0)))
        newer = w.issue(WORK, seconds=30)
        newer["refreshTokenExpiresAt"] = ms(later(30 * 86400))
        w.store.inner.put(WORK, newer)
        self.tick_at(r, 7200)
        self.assertNotIn("refreshTokenExpiresAt", w.store.inner.get(WORK))
        self.assertEqual(self.ends()[WORK], signin_end_text(newer, later(0)))

    def test_a_failed_ledger_write_is_reported_once(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=30)
        r = w.reader()
        with mock.patch.object(reading.ledger, "add", side_effect=ledger.LedgerError("no")):
            self.assertEqual(self.tick_at(r, 0), [reading.ledger_problem(self.world.folder)])
            self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(self.tick_at(r, 3600), [])

    def test_memory_only_renewal_sets_an_undecided_store_copy_aside(self):
        w = self.world
        with mock.patch.object(recovering, "write", failing_writes(99)):
            r, old, new, reports = self.fail_renewal_save()
            self.assertEqual(reports, [HELD.format(name="Work Account", time=clock_text(later(300)))])
            other = w.issue(WORK, seconds=600)
            w.store.inner.put(WORK, other)
            w.clock.now = later(60)
            self.assertEqual(r.flush(), ([SAVED_FROM_MEMORY.format(name="Work Account")], ()))
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual([recovering._load(path)[0] for path in sorted(w.recovery.iterdir())], [other])

    def switch_to(self, target):
        w = self.world
        return switching.switch(
            target,
            slot=w.slot,
            store=w.store,
            registry=w.registry,
            home=w.home,
            profile_fn=w.profile,
            lock_path=w.lock_path,
            backups=w.folder / "backups",
            clock=lambda: w.clock.now.timestamp(),
            settle=0,
        )

    def test_switch_back_keeps_the_shorter_renewal_the_reader_saved(self):
        w = self.world
        slot = w.slot

        def land(item):
            slot.writes.append(copy.deepcopy(item))
            slot.item = copy.deepcopy(item)

        slot.write = land
        (w.home / ".claude").mkdir()
        w.go_live(ALEX)
        w.remember_live(ALEX)
        old = w.park(WORK, seconds=7200)
        w.usage.plan(WORK, SignInDead(), (10.0, 5.0))
        w.store.put_failures = [False, True, True, True]
        r = w.reader()
        self.tick_at(r, 0)
        new = w.refresh.issued[0]
        self.assertLess(new["expiresAt"], old["expiresAt"])
        refusing = [True]

        def refuse_cmd(uuid, oauth):
            if uuid == WORK and refusing[0]:
                raise StoreError("refused")

        real_remove = w.store.remove

        def stuck_remove(uuid):
            if uuid == WORK and refusing[0]:
                raise StoreError("could not remove")
            return real_remove(uuid)

        w.store.put_hook = refuse_cmd
        w.store.remove = stuck_remove
        w.clock.now = later(30)
        loaded = self.switch_to(WORK)
        self.assertTrue(loaded.leftover)
        self.assertEqual(slot.item["claudeAiOauth"], new)
        refusing[0] = False
        self.assertEqual(w.store.inner.get(WORK), old)
        w.remember_live(WORK)
        self.assertEqual(self.tick_at(r, 400), [SPARE.format(name="Work Account")])
        w.clock.now = later(600)
        self.switch_to(ALEX)
        self.assertEqual(w.store.inner.get(WORK), new)
        with held(w.lock_path):
            self.assertEqual(recovering.choose(w.store, w.recovery, WORK).pair, new)
        w.clock.now = later(700)
        self.switch_to(WORK)
        self.assertEqual(slot.item["claudeAiOauth"], new)
        self.assertEqual(w.refresh.calls, [WORK])

    def test_recovery_file_holding_the_live_signin_is_dropped(self):
        w = self.world
        live = w.go_live(ALEX)
        w.remember_live(ALEX)
        recovering.write(w.recovery, ALEX, live)
        r = w.reader()
        self.assertFalse(w.recovery_file(ALEX).exists())
        self.assertIsNone(w.store.inner.get(ALEX))
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.puts(), [])
        self.assertEqual(w.usage.tokens(ALEX), [live["accessToken"]])

    def test_damaged_file_beside_a_good_leftover_is_imported(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)
        renewed = w.issue(WORK)
        w.recovery.mkdir(mode=0o700)
        w.recovery_file(WORK).write_text("{not json")
        (w.recovery / f".{WORK}.abc123_x.tmp").write_text(json.dumps({"claudeAiOauth": renewed}))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), renewed)
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.tokens(WORK), [renewed["accessToken"]])

    def test_flush_saves_a_memory_only_pair_without_any_network_call(self):
        w = self.world
        with mock.patch.object(recovering, "write", failing_writes(100)):
            r, old, new, reports = self.fail_renewal_save()
            self.assertEqual(reports, [HELD.format(name="Work Account", time=clock_text(later(300)))])
            counts = (len(w.usage.calls), len(w.refresh.calls), len(w.profile.calls), w.slot.reads)
            w.store.put_failures = [True]
            w.clock.now = later(5)
            self.assertEqual(r.flush(), ([], (WORK,)))
            w.clock.now = later(10)
            self.assertEqual(r.flush(), ([SAVED_FROM_MEMORY.format(name="Work Account")], ()))
            self.assertEqual((len(w.usage.calls), len(w.refresh.calls), len(w.profile.calls), w.slot.reads), counts)
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(r.flush(), ([], ()))

    def test_flush_moves_a_waiting_recovery_file_into_the_store_without_any_network_call(self):
        w = self.world
        r, old, new, reports = self.fail_renewal_save()
        self.assertEqual(reports, [KEPT.format(name="Work Account", time=clock_text(later(300)))])
        self.assertTrue(w.recovery_file(WORK).exists())
        counts = (len(w.usage.calls), len(w.refresh.calls), len(w.profile.calls), w.slot.reads)
        w.clock.now = later(5)
        self.assertEqual(r.flush(), ([RECOVERED.format(name="Work Account")], ()))
        self.assertEqual((len(w.usage.calls), len(w.refresh.calls), len(w.profile.calls), w.slot.reads), counts)
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertFalse(w.lock_held())

    def test_flush_settles_a_recovery_file_written_by_another_process(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK)
        r = w.reader()
        self.tick_at(r, 0)
        self.assertEqual(w.refresh.calls, [])
        renewed = w.issue(WORK, seconds=7200)
        recovering.write(w.recovery, WORK, renewed)
        reads = w.slot.reads
        self.assertEqual(r.flush(), ([RECOVERED.format(name="Work Account")], ()))
        self.assertEqual(w.slot.reads, reads)
        self.assertEqual(w.store.inner.get(WORK), renewed)

    def test_flush_leaves_a_file_holding_the_live_signin_without_reading_the_slot(self):
        w = self.world
        live = w.go_live(ALEX)
        r = w.reader()
        self.tick_at(r, 0)
        recovering.write(w.recovery, ALEX, live)
        reads = w.slot.reads
        self.assertEqual(r.flush(), ([], ()))
        self.assertEqual(w.slot.reads, reads)
        self.assertTrue(w.recovery_file(ALEX).exists())
        self.assertIsNone(w.store.inner.get(ALEX))
        self.assertEqual(w.puts(), [])
        self.assertEqual(self.tick_at(r, 1), [])
        self.assertEqual(w.slot.reads, reads + 1)
        self.assertFalse(w.recovery_file(ALEX).exists())
        self.assertIsNone(w.store.inner.get(ALEX))
        self.assertEqual(w.puts(), [])

    def test_flush_leaves_a_file_matching_the_last_seen_live_signin(self):
        w = self.world
        live = w.go_live(WORK)
        w.remember_live(WORK)
        spent = w.issue(WORK, -60)
        w.store.inner.put(WORK, spent)
        recovering.write(w.recovery, WORK, live)
        with mock.patch.object(reading, "LOCK_TIMEOUT_S", 0.05):
            with held(w.lock_path):
                r = w.reader()
                self.tick_at(r, 0)
        self.assertEqual(recovering.pending(w.recovery, WORK), live)
        w.go_live(ALEX)
        w.clock.now = later(5)
        reads = w.slot.reads
        self.assertEqual(r.flush(), ([], ()))
        self.assertEqual(w.slot.reads, reads)
        self.assertEqual(recovering.pending(w.recovery, WORK), live)
        self.assertEqual(w.store.inner.get(WORK), spent)
        self.assertEqual(self.tick_at(r, 10), [RECOVERED.format(name="Work Account")])
        self.assertGreater(w.slot.reads, reads)
        self.assertEqual(w.store.inner.get(WORK), live)
        self.assertFalse(w.recovery_file(WORK).exists())

    def test_flush_then_tick_drops_a_file_that_still_holds_the_live_signin(self):
        w = self.world
        live = w.go_live(WORK)
        w.remember_live(WORK)
        r = w.reader()
        self.tick_at(r, 0)
        recovering.write(w.recovery, WORK, live)
        self.assertEqual(r.flush(), ([], ()))
        self.assertTrue(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(r, 1), [])
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertIsNone(w.store.inner.get(WORK))
        self.assertEqual(w.puts(), [])

    def test_rescue_file_is_imported_and_no_renewal_happens_meanwhile(self):
        w = self.world
        w.go_live(ALEX)
        w.remember_live(ALEX)
        old = w.park(WORK, seconds=-30)
        rescued = w.issue(WORK, seconds=1800)
        w.store.put_failures = [True] * 20
        r = w.reader()
        placed = recovering.rescue(w.recovery, WORK, rescued, spent=old)
        for seconds in (0, 60, 120, 300):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertTrue(placed.exists())
        w.store.put_failures = []
        self.assertEqual(self.tick_at(r, 360), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), rescued)
        self.assertEqual(os.listdir(w.recovery), [])
        self.assertEqual(w.usage.tokens(WORK), [rescued["accessToken"]])
        self.assertEqual(w.refresh.calls, [])

    def test_rescue_holding_the_live_signin_is_dropped(self):
        w = self.world
        live = w.go_live(ALEX)
        w.remember_live(ALEX)
        recovering.rescue(w.recovery, ALEX, live)
        recovering.rescue(w.recovery, ALEX, live)
        r = w.reader()
        self.assertEqual(os.listdir(w.recovery), [])
        self.assertIsNone(w.store.inner.get(ALEX))
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.puts(), [])

    def test_flush_waits_for_at_most_the_one_account_in_flight(self):
        w = self.world
        for uuid in ORDER:
            w.park(uuid)
        r = w.reader()
        entered = threading.Event()
        release = threading.Event()

        def slow(uuid, token):
            if not entered.is_set():
                entered.set()
                release.wait(10)

        w.on_usage = slow
        ticked = []
        flushed = []
        ticker = threading.Thread(target=lambda: ticked.append(r.tick()))
        ticker.start()
        self.assertTrue(entered.wait(10))
        flusher = threading.Thread(target=lambda: flushed.append(r.flush()))
        flusher.start()
        deadline = time.monotonic() + 10
        while not r._flush_waiting.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(r._flush_waiting.is_set())
        flusher.join(0.1)
        self.assertTrue(flusher.is_alive())
        release.set()
        ticker.join(10)
        flusher.join(10)
        self.assertFalse(ticker.is_alive())
        self.assertFalse(flusher.is_alive())
        self.assertEqual(ticked, [[]])
        self.assertEqual(flushed, [([], ())])
        self.assertFalse(r._flush_waiting.is_set())
        self.assertEqual([uuid for _, uuid, _ in w.usage.calls], [ALEX])
        w.on_usage = None
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual([uuid for _, uuid, _ in w.usage.calls], list(ORDER))
        self.assertEqual(self.tick_at(r, 60), [])
        self.assertEqual(len(w.usage.calls), 4)

    def test_a_tick_that_starts_while_a_flush_waits_does_nothing(self):
        w = self.world
        w.park(WORK)
        r = w.reader()
        renewed = w.issue(ORG)
        recovering.write(w.recovery, ORG, renewed)
        r._flush_waiting.set()
        try:
            self.assertEqual(self.tick_at(r, 0), [])
        finally:
            r._flush_waiting.clear()
        self.assertEqual(w.usage.calls, [])
        self.assertEqual(w.slot.reads, 0)
        self.assertTrue(w.recovery_file(ORG).exists())
        self.assertEqual(self.tick_at(r, 0), [RECOVERED.format(name="Team Account")])
        self.assertEqual(w.usage.tokens(WORK), [w.store.inner.get(WORK)["accessToken"]])
        self.assertEqual(w.usage.tokens(ORG), [renewed["accessToken"]])

    def test_flush_retries_an_unsaved_state_file(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, RateLimited(3600))
        r = w.reader()
        with mock.patch.object(reading, "_write_private", side_effect=OSError("disk hiccup")):
            self.tick_at(r, 0)
        self.assertFalse(w.state.exists())
        reads = w.slot.reads
        self.assertEqual(r.flush(), ([], ()))
        self.assertTrue(w.state.exists())
        self.assertEqual(w.slot.reads, reads)

    def test_refused_parked_reading_keeps_the_180_second_floor(self):
        w = self.world
        w.go_live(ALEX)
        w.remember_live(ALEX)
        w.park(WORK, seconds=7200)
        w.usage.plan(WORK, SignInDead())
        r = w.reader(guarded_fn=lambda: {WORK})
        for seconds in range(0, 601, 15):
            self.tick_at(r, seconds)
        times = w.usage.times(WORK)
        self.assertEqual(times, [later(s) for s in (0, 180, 360, 540)])
        self.assertEqual(w.refresh.calls, [])

    def test_unsaved_renewal_saved_into_an_empty_store(self):
        w = self.world
        r, old, new, _ = self.fail_renewal_save()
        w.slot.item = {"claudeAiOauth": copy.deepcopy(old)}
        w.store.inner.remove(WORK)
        w.remember_live(WORK, oauth=old)
        self.assertIn(RECOVERED.format(name="Work Account"), self.tick_at(r, 60))
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(w.slot.writes, [])

    def test_unsaved_renewal_saved_over_an_older_signin(self):
        w = self.world
        r, old, new, _ = self.fail_renewal_save()
        w.clock.now = later(-1800)
        w.park(WORK, seconds=1800)
        self.assertEqual(self.tick_at(r, 60), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())

    def test_unsaved_renewal_kept_when_a_later_signin_arrives(self):
        w = self.world
        r, old, new, _ = self.fail_renewal_save()
        newer = w.park(WORK, seconds=7200)
        puts = len(w.puts(WORK))
        self.assertEqual(self.tick_at(r, 60), [])
        self.assertEqual(len(w.puts(WORK)), puts)
        self.assertEqual(w.store.inner.get(WORK), newer)
        self.assertEqual(recovering.pending(w.recovery, WORK), new)
        self.tick_at(r, 300)
        self.assertEqual(w.usage.tokens(WORK), [newer["accessToken"]])

    def test_restart_imports_recovery_before_any_renewal(self):
        w = self.world
        first, old, new, _ = self.fail_renewal_save()
        second = w.reader()
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(second, 15), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(second.status(WORK).paused_until, later(300))
        self.assertEqual(w.usage.times(WORK), [])
        self.tick_at(second, 300)
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_restart_while_store_still_fails_never_renews_the_spent_key(self):
        w = self.world
        first, old, new, _ = self.fail_renewal_save()
        w.store.put_failures = [True] * 9
        second = w.reader()
        self.assertTrue(w.recovery_file(WORK).exists())
        self.tick_at(second, 300)
        gets = w.store.gets.count(WORK)
        self.assertEqual(self.tick_at(second, 330), [])
        self.assertEqual(w.store.gets.count(WORK), gets)
        self.tick_at(second, 600)
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(w.store.inner.get(WORK), old)
        self.assertEqual(json.loads(w.recovery_file(WORK).read_text()), {"claudeAiOauth": new, "spent": switching.fingerprint(old)})

    def test_recovery_held_in_memory_then_written_even_after_the_account_is_removed(self):
        w = self.world
        with mock.patch.object(recovering, "write", failing_writes(1)):
            r, old, new, reports = self.fail_renewal_save()
            self.assertEqual(reports, [HELD.format(name="Work Account", time=clock_text(later(300)))])
            self.assertFalse(w.recovery_file(WORK).exists())
            w.registry.save([w.account(uuid) for uuid in (ALEX, PERSONAL, ORG)])
            w.store.put_failures = [True]
            self.assertEqual(self.tick_at(r, 30), [])
            self.assertFalse(w.recovery_file(WORK).exists())
            self.assertEqual(self.tick_at(r, 60), [])
            self.assertEqual(json.loads(w.recovery_file(WORK).read_text()), {"claudeAiOauth": new, "spent": switching.fingerprint(old)})
            self.assertEqual(stat.S_IMODE(os.stat(w.recovery).st_mode), 0o700)
            self.assertEqual(self.tick_at(r, 120), [RECOVERED.format(name="an account in Lifeline")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())

    def test_pair_only_in_memory_reported_as_saved_without_the_folder(self):
        w = self.world
        with mock.patch.object(recovering, "write", failing_writes(99)):
            r, old, new, reports = self.fail_renewal_save()
            self.assertEqual(reports, [HELD.format(name="Work Account", time=clock_text(later(300)))])
            self.assertEqual(self.tick_at(r, 60), [SAVED_FROM_MEMORY.format(name="Work Account")])
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertFalse(w.recovery_file(WORK).exists())
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.usage.tokens(WORK), [new["accessToken"]])

    def test_the_menu_names_each_renewal_pause_by_what_really_failed(self):
        from lifeline import viewmodel

        w = self.world
        w.recovery.write_text("a file where the folder should be")
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)
        r = w.reader()
        self.tick_at(r, 0)
        status = r.status(WORK)
        said = viewmodel.paused_sentence(status.last_error, status.paused_until, later(0), START.tzinfo)
        self.assertTrue(said.startswith("Lifeline could not write to the folder where it keeps safe copies of sign-ins, so readings are paused until"), said)
        self.assertNotIn("renewed", said)
        w.recovery.unlink()
        locked = World(self)
        locked.go_live(ALEX)
        locked.park(WORK, seconds=-30)
        r = locked.reader()
        locked.clock.now = later(0)
        with mock.patch.object(reading.writelock, "held", side_effect=OSError("cannot open the file")):
            r.tick()
        status = r.status(WORK)
        self.assertEqual(status.last_error, reading.LOCK_ERROR)
        said = viewmodel.paused_sentence(status.last_error, status.paused_until, later(0), START.tzinfo)
        self.assertTrue(said.startswith("Lifeline could not open one of its own files, so readings are paused until"), said)
        self.assertNotIn("busy", said)

    def test_no_renewal_when_the_recovery_folder_cannot_be_written(self):
        w = self.world
        w.recovery.write_text("a file where the folder should be")
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        r = w.reader()
        self.assertEqual(
            self.tick_at(r, 0),
            [
                "Lifeline could not write to its recovery folder, so it did not renew the sign-in for Work Account. "
                f"Readings for it are paused until {clock_text(later(300))}."
            ],
        )
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.store.inner.get(WORK), old)
        self.assertEqual(r.status(WORK).last_error, reading.NO_RECOVERY_ERROR)
        w.recovery.unlink()
        self.tick_at(r, 300)
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(len(w.usage.times(WORK)), 1)

    def test_second_reader_never_renews_while_a_recovery_file_waits(self):
        w = self.world
        w.go_live(ALEX)
        w.remember_live(ALEX)
        w.park(WORK, seconds=-30)
        first = w.reader(accounts=[WORK])
        second = w.reader(accounts=[WORK])
        fired = []

        def first_renews_meanwhile(uuid):
            if uuid == WORK and not fired:
                fired.append(True)
                w.store.put_failures = [False, True, True, True]
                w.store.get_hook = None
                first.tick()

        w.clock.now = later(0)
        w.store.get_hook = first_renews_meanwhile
        second.tick()
        self.assertEqual(fired, [True])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertTrue(w.recovery_file(WORK).exists())
        self.assertFalse(second.status(WORK).needs_signin)

    def test_leftover_from_a_crash_is_imported(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        renewed = w.issue(WORK)
        w.recovery.mkdir(mode=0o700)
        (w.recovery / f".{WORK}.k2j4.tmp").write_text(json.dumps({"claudeAiOauth": renewed}))
        r = w.reader()
        self.assertEqual(w.store.inner.get(WORK), renewed)
        self.assertEqual([recovering._load(path)[0] for path in w.recovery.iterdir()], [old])
        self.assertEqual(self.tick_at(r, 0), [RECOVERED.format(name="Work Account")])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.tokens(WORK), [renewed["accessToken"]])
        self.assertEqual(self.tick_at(r, 60), [])
        self.assertEqual([recovering._load(path)[0] for path in w.recovery.iterdir()], [old])
        self.assertEqual(w.store.inner.get(WORK), renewed)

    def test_unreadable_recovery_file_reported_once_and_left(self):
        w = self.world
        w.park(WORK)
        w.recovery.mkdir(mode=0o700)
        w.recovery_file(WORK).write_text("{broken")
        r = w.reader()
        self.assertEqual(
            self.tick_at(r, 0),
            ["Lifeline could not read the renewed sign-in for Work Account in its recovery folder, so it left the file there."],
        )
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertTrue(w.recovery_file(WORK).exists())
        self.assertEqual(len(w.usage.times(WORK)), 2)

    def test_guarded_account_not_renewed_until_the_guard_lifts(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        fresh = w.park(ORG)
        guarded = {WORK, ORG}
        r = w.reader(guarded_fn=lambda: set(guarded))
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(w.usage.tokens(ORG), [fresh["accessToken"]])
        self.assertEqual(w.puts(), [])
        for seconds in (60, 120, 180, 240):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.store.inner.get(WORK), old)
        guarded.clear()
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(w.usage.tokens(WORK), [w.refresh.issued[0]["accessToken"]])

    def test_guard_checked_again_under_the_lock(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)
        calls = []

        def guard():
            calls.append(w.lock_held())
            return {WORK} if len(calls) > 1 else set()

        r = w.reader(guarded_fn=guard)
        self.tick_at(r, 0)
        self.assertEqual(calls, [False, True])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.puts(), [])

    def test_failing_guard_blocks_renewal_and_shows_a_pause(self):
        w = self.world
        w.park(WORK, seconds=-30)
        fresh = w.park(ORG, seconds=86400)

        def broken():
            raise RuntimeError("watcher gone access-x-SECRET")

        r = w.reader(guarded_fn=broken)
        self.assertEqual(self.tick_at(r, 0), [GUARD.format(name="Work Account", time=clock_text(later(300)))])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(r.status(WORK), AccountStatus(False, False, later(300), reading.GUARD_ERROR))
        self.assertEqual(w.usage.tokens(ORG), [fresh["accessToken"]])
        for seconds in (300, 600, 3600):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(r.status(WORK).paused_until, later(3900))
        self.assertNotIn("SECRET", w.state.read_text())

    def test_parked_key_used_without_renewal_while_fresh(self):
        w = self.world
        fresh = w.park(PERSONAL, seconds=61)
        r = w.reader()
        self.tick_at(r, 0)
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.tokens(PERSONAL), [fresh["accessToken"]])

    def test_dead_signin_marked_once(self):
        w = self.world
        w.park(ORG)
        w.usage.plan(ORG, SignInDead(), (12.0, 3.0))
        w.refresh.errors[ORG] = SignInDead()
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), ["Team Account needs a new sign-in."])
        self.assertTrue(r.status(ORG).needs_signin)
        self.assertFalse(r.status(ORG).cannot_read)
        for seconds in (300, 600, 900, 3600, 86400):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(len(w.usage.times(ORG)), 1)
        self.assertEqual(w.refresh.calls, [ORG])
        self.assertTrue(r.status(ORG).needs_signin)
        new = w.park(ORG)
        self.assertEqual(self.tick_at(r, 86700), [])
        self.assertEqual(w.usage.tokens(ORG)[-1], new["accessToken"])
        self.assertEqual(r.status(ORG), AccountStatus(False, False, None, None))
        self.assertEqual(w.history.latest(ORG).five, 12.0)

    def test_parked_401_tries_one_renewal_before_marking(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK)
        w.usage.plan(WORK, SignInDead(), (20.0, 5.0))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [WORK])
        new = w.refresh.issued[0]
        self.assertEqual(w.store.inner.get(WORK), new)
        self.assertEqual(r.status(WORK), AccountStatus(False, False, None, None))
        self.assertEqual(w.usage.times(WORK), [later(0)])
        self.assertEqual(self.tick_at(r, 179), [])
        self.assertEqual(w.usage.times(WORK), [later(0)])
        self.assertEqual(self.tick_at(r, 180), [])
        self.assertEqual(w.usage.times(WORK), [later(0), later(180)])
        self.assertEqual(w.usage.tokens(WORK), [old["accessToken"], new["accessToken"]])
        self.assertEqual(w.refresh.calls, [WORK])

    def test_parked_401_with_an_unreachable_sign_in_address_is_not_marked(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, SignInDead(), (20.0, 5.0))
        w.refresh.errors[WORK] = Unavailable("Could not reach the server")
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [paused("Work Account", 300)])
        self.assertFalse(r.status(WORK).needs_signin)
        self.assertEqual(r.status(WORK).last_error, reading.RENEW_ERRORS[Unavailable])

    def test_model_scoped_weekly_limits_are_kept_for_the_detail_screen(self):
        from lifeline.anthropic import Limit

        w = self.world
        w.park(WORK)
        reset = datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)
        limits = (
            Limit("weekly_all", "weekly", 57.0, reset, None, None, "normal", True),
            Limit("weekly_scoped", "weekly", 0.0, reset, "Fable", None, "normal", False, {"allowance_share": 50}),
            Limit("weekly_scoped", "weekly", 12.5, None, None, "Design", None, None),
        )
        w.usage.plan(WORK, Usage(five_hour=None, seven_day=Window(57.0, reset), limits=limits), (20.0, 5.0))
        r = w.reader()
        self.assertEqual(r.scoped(WORK), [])
        self.tick_at(r, 0)
        found = r.scoped(WORK)
        self.assertEqual(
            found,
            [
                {"kind": "weekly_scoped", "model": "Fable", "surface": None, "percent": 0.0, "resets_at": reset,
                 "severity": "normal", "extra": {"allowance_share": 50}},
                {"kind": "weekly_scoped", "model": None, "surface": "Design", "percent": 12.5, "resets_at": None,
                 "severity": None, "extra": {}},
            ],
        )
        found[0]["percent"] = 99.0
        self.assertEqual(r.scoped(WORK)[0]["percent"], 0.0)
        latest = w.history.latest(WORK)
        self.assertEqual((latest.five, latest.week, latest.week_reset), (None, 57.0, reset))
        self.tick_at(r, 300)
        self.assertEqual(r.scoped(WORK), [])

    def test_a_certificate_failure_names_the_certificate_never_the_connection(self):
        from lifeline.anthropic import CertificateFailed

        w = self.world
        w.park(WORK)
        w.park(ORG, seconds=-30)
        w.usage.plan(WORK, CertificateFailed())
        w.refresh.errors[ORG] = CertificateFailed()
        r = w.reader()
        self.tick_at(r, 0)
        self.assertEqual(r.status(WORK).last_error, "Lifeline could not check the certificate of the usage address, so it did not connect.")
        self.assertEqual(r.status(ORG).last_error, "Lifeline could not check the certificate of the sign-in address, so it did not connect.")
        for uuid in (WORK, ORG):
            self.assertIn(r.status(uuid).last_error, reading.KNOWN_ERRORS)
            self.assertFalse(r.status(uuid).needs_signin)
            self.assertNotIn("reach", r.status(uuid).last_error)

    def test_401_right_after_a_renewal_is_marked_without_a_second_renewal(self):
        w = self.world
        w.park(WORK, seconds=-30)
        w.usage.plan(WORK, SignInDead())
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), ["Work Account needs a new sign-in."])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertTrue(r.status(WORK).needs_signin)

    def test_parked_401_when_another_writer_renewed_first(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, SignInDead(), (20.0, 5.0))
        fresh = {}

        def other_writer(uuid):
            if uuid == WORK and w.store.gets.count(WORK) == 2:
                fresh.update(w.park(WORK))

        w.store.get_hook = other_writer
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertFalse(r.status(WORK).needs_signin)
        self.tick_at(r, 180)
        self.assertEqual(w.usage.tokens(WORK)[-1], fresh["accessToken"])

    def test_dead_renewal_marked_once(self):
        w = self.world
        w.park(ORG, seconds=-60)
        w.refresh.errors[ORG] = SignInDead()
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), ["Team Account needs a new sign-in."])
        for seconds in (300, 600, 7200):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.refresh.calls, [ORG])
        self.assertEqual(w.usage.times(ORG), [])
        self.assertTrue(r.status(ORG).needs_signin)

    def test_parked_signin_without_renewal_key_needs_a_new_signin(self):
        w = self.world
        oauth = w.issue(ORG, seconds=-60)
        del oauth["refreshToken"]
        w.store.inner.put(ORG, oauth)
        w.refresh.errors[None] = ValueError("The sign-in has no renewal key")
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), ["Team Account needs a new sign-in."])
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertTrue(r.status(ORG).needs_signin)

    def test_interval_180_when_climbing_300_otherwise(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(WORK, (10.0, 5.0), (20.0, 5.0), (20.0, 5.0), (20.0, 6.0), (3.0, 6.0), (3.0, 6.0))
        r = w.reader()
        for seconds in range(0, 1800, 10):
            self.tick_at(r, seconds)
        self.assertEqual(w.usage.times(WORK), [later(s) for s in (0, 300, 480, 780, 960, 1260, 1560)])
        times = w.usage.times(WORK)
        self.assertTrue(all(b - a >= timedelta(seconds=180) for a, b in zip(times, times[1:])))

    def test_identify_live_called_only_when_slot_changes(self):
        w = self.world
        first = w.go_live(ALEX)
        r = w.reader()
        with mock.patch.object(switching, "identify_live", wraps=ORIGINAL_IDENTIFY) as spy:
            for seconds in (0, 300, 600, 900):
                self.tick_at(r, seconds)
            self.assertEqual(spy.call_count, 1)
            item = copy.deepcopy(w.slot.item)
            item["claudeAiOauth"]["accessToken"] = "access-11111111-other-SECRET"
            w.owners["access-11111111-other-SECRET"] = ALEX
            w.slot.item = item
            self.tick_at(r, 1200)
            self.assertEqual(spy.call_count, 1)
            renewed = w.go_live(ALEX)
            self.tick_at(r, 1500)
            self.assertEqual(spy.call_count, 2)
        self.assertEqual(len(w.profile.calls), 2)
        self.assertEqual(
            w.usage.tokens(ALEX),
            [first["accessToken"]] * 4 + ["access-11111111-other-SECRET", renewed["accessToken"]],
        )

    def test_failed_identification_retried_after_300_seconds(self):
        w = self.world
        w.go_live(ALEX)
        w.profile.offline = True
        r = w.reader()
        with mock.patch.object(switching, "identify_live", wraps=ORIGINAL_IDENTIFY) as spy:
            self.assertEqual(self.tick_at(r, 0), [UNCONFIRMED])
            self.assertEqual(self.tick_at(r, 100), [])
            self.assertEqual(spy.call_count, 1)
            w.profile.offline = False
            self.assertEqual(self.tick_at(r, 299), [])
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(self.tick_at(r, 300), [])
            self.assertEqual(spy.call_count, 2)
        self.assertEqual(w.usage.times(ALEX), [later(300)])

    def test_signin_without_renewal_key_is_an_unknown_identity(self):
        w = self.world
        oauth = w.issue(ALEX)
        del oauth["refreshToken"]
        w.slot.item = {"claudeAiOauth": oauth}
        w.park(WORK, seconds=-30)
        r = w.reader()
        with mock.patch.object(switching, "identify_live", wraps=ORIGINAL_IDENTIFY) as spy:
            self.assertEqual(self.tick_at(r, 0), [UNCONFIRMED])
            self.assertEqual(spy.call_count, 0)
        self.assertEqual(w.profile.calls, [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(ALEX), [])

    def test_changed_slot_under_lock_skips_renewal_without_a_profile_call(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)

        def renewed_by_claude_code(uuid):
            if uuid == WORK and w.store.gets.count(WORK) == 1:
                w.go_live(ALEX)

        w.store.get_hook = renewed_by_claude_code
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(len(w.profile.calls), 1)
        self.assertEqual(self.tick_at(r, 15), [])
        self.assertEqual(len(w.profile.calls), 2)
        self.assertEqual(w.profile.under_lock, [False, False])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(w.usage.times(WORK), [later(15)])

    def test_cache_used_when_newer_than_latest_reading(self):
        w = self.world
        w.go_live(ALEX, seconds=-60)
        w.history.add_reading(ALEX, "statusline", 30.0, FIVE_RESET, 11.0, WEEK_RESET, START - timedelta(minutes=10))
        fetched = START - timedelta(minutes=2)
        w.remember_live(ALEX, cache_entry(ALEX, fetched))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        latest = w.history.latest(ALEX)
        self.assertEqual(
            (latest.source, latest.at, latest.five, latest.five_reset, latest.week, latest.week_reset),
            ("cache", fetched, 33.5, FIVE_RESET, 12.0, WEEK_RESET),
        )
        self.assertEqual(w.usage.times(ALEX), [])
        self.assertEqual(w.refresh.calls, [])
        self.tick_at(r, 300)
        self.assertEqual(len(w.history.readings(ALEX, START - timedelta(hours=1))), 2)

    def test_cache_skipped_when_not_newer(self):
        w = self.world
        w.go_live(ALEX, seconds=-60)
        latest = w.history.add_reading(ALEX, "statusline", 35.0, FIVE_RESET, 13.0, WEEK_RESET, START - timedelta(seconds=30))
        w.remember_live(ALEX, cache_entry(ALEX, START - timedelta(minutes=2)))
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.history.latest(ALEX), latest)
        w.remember_live(ALEX, cache_entry(ALEX, latest.at))
        self.tick_at(r, 300)
        self.assertEqual(w.history.readings(ALEX, START - timedelta(hours=1)), [latest])
        self.assertEqual(w.usage.times(ALEX), [])

    def test_cache_from_another_account_or_unreadable_is_ignored(self):
        w = self.world
        w.go_live(ALEX, seconds=-60)
        fetched = START - timedelta(minutes=1)
        broken = cache_entry(ALEX, fetched)
        broken["utilization"]["five_hour"]["utilization"] = "high"
        negative = cache_entry(ALEX, fetched)
        negative["utilization"]["seven_day"]["utilization"] = -4
        empty = cache_entry(ALEX, fetched)
        empty["utilization"] = {"five_hour": None, "seven_day": None}
        cases = [
            cache_entry(WORK, fetched),
            cache_entry(None, fetched),
            cache_entry(ALEX, START + timedelta(hours=1)),
            dict(cache_entry(ALEX, fetched), fetchedAtMs=True),
            dict(cache_entry(ALEX, fetched), fetchedAtMs="soon"),
            dict(cache_entry(ALEX, fetched), utilization=[1, 2]),
            broken,
            negative,
            empty,
            "not an object",
        ]
        r = w.reader()
        for index, case in enumerate(cases):
            w.remember_live(ALEX, case)
            self.assertEqual(self.tick_at(r, index * 300), [], case)
            self.assertIsNone(w.history.latest(ALEX), case)
        (w.home / ".claude.json").write_text("{not json")
        self.assertEqual(self.tick_at(r, len(cases) * 300), [])
        self.assertIsNone(w.history.latest(ALEX))
        self.assertEqual(w.usage.times(ALEX), [])

    def test_cache_with_null_utilization_keeps_the_other_window(self):
        w = self.world
        w.go_live(ALEX, seconds=-60)
        entry = cache_entry(ALEX, START - timedelta(minutes=1))
        entry["utilization"]["five_hour"] = {"utilization": None, "resets_at": None}
        w.remember_live(ALEX, entry)
        r = w.reader()
        self.tick_at(r, 0)
        latest = w.history.latest(ALEX)
        self.assertEqual((latest.five, latest.five_reset, latest.week), (None, None, 12.0))

    def test_leftover_parked_copy_of_live_account_never_renewed(self):
        w = self.world
        w.go_live(ALEX, seconds=-60)
        w.remember_live(ALEX)
        spare = w.park(ALEX, seconds=-120)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [SPARE.format(name="Alex's Account")])
        for seconds in (300, 600, 3600):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(ALEX), [])
        live = w.go_live(ALEX)
        self.assertEqual(self.tick_at(r, 3900), [])
        self.assertEqual(w.usage.tokens(ALEX), [live["accessToken"]])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.slot.writes, [])
        self.assertEqual(w.puts(), [])
        self.assertEqual(w.store.inner.get(ALEX), spare)

    def test_renewal_rereads_store_under_lock(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)
        under_lock = []
        fresh = {}

        def other_writer(uuid):
            if uuid == WORK and w.store.gets.count(WORK) == 2:
                under_lock.append(w.lock_held())
                fresh.update(w.park(WORK))

        w.store.get_hook = other_writer
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(under_lock, [True])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.tokens(WORK), [fresh["accessToken"]])
        self.assertEqual(w.puts(), [])

    def test_renewal_skipped_when_signin_left_the_store(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)

        def switched_away(uuid):
            if uuid == WORK and w.store.gets.count(WORK) == 2:
                w.store.inner.remove(WORK)

        w.store.get_hook = switched_away
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(WORK), [])

    def test_renewal_skipped_when_account_became_live_under_lock(self):
        w = self.world
        w.go_live(ALEX)
        parked = w.park(WORK, seconds=-30)

        def switched_in(uuid):
            if uuid == WORK and w.store.gets.count(WORK) == 1:
                w.slot.item = {"claudeAiOauth": copy.deepcopy(parked)}

        w.store.get_hook = switched_in
        w.remember_live(WORK, oauth=parked)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(self.tick_at(r, 300), [SPARE.format(name="Work Account")])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(w.puts(), [])

    def test_parked_copy_never_renewed_once_its_account_is_signed_in_to_claude_code(self):
        w = self.world
        w.go_live(ALEX)
        old = w.park(WORK, seconds=-30)
        fresh = {}

        def signed_in_directly(uuid):
            if uuid == WORK and w.store.gets.count(WORK) == 1:
                fresh.update(w.go_live(WORK))

        w.store.get_hook = signed_in_directly
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.times(WORK), [])
        self.assertNotEqual(fresh["refreshToken"], old["refreshToken"])
        self.assertEqual(self.tick_at(r, 300), [SPARE.format(name="Work Account")])
        self.assertEqual(w.usage.tokens(WORK), [fresh["accessToken"]])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.store.inner.get(WORK), old)

    def test_not_allowed_stops_reading(self):
        w = self.world
        w.park(PERSONAL)
        w.usage.plan(PERSONAL, NotAllowed(), (5.0, 1.0))
        r = w.reader()
        self.assertEqual(
            self.tick_at(r, 0),
            ["Lifeline is not allowed to read the usage of Personal Account, so it stopped reading that account."],
        )
        self.assertTrue(r.status(PERSONAL).cannot_read)
        self.assertFalse(r.status(PERSONAL).needs_signin)
        for seconds in (300, 600, 7200):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual(len(w.usage.times(PERSONAL)), 1)
        new = w.park(PERSONAL)
        self.assertEqual(self.tick_at(r, 7500), [])
        self.assertEqual(w.usage.tokens(PERSONAL)[-1], new["accessToken"])
        self.assertEqual(r.status(PERSONAL), AccountStatus(False, False, None, None))

    def test_unavailable_backs_off_and_reports_once(self):
        w = self.world
        w.park(WORK)
        w.usage.plan(
            WORK,
            Unavailable("Could not reach the server"),
            Unavailable("Server error 503"),
            UnexpectedReply("The reply was not JSON"),
            (30.0, 5.0),
            Unavailable("Could not reach the server"),
            (31.0, 5.0),
        )
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [paused("Work Account", 300)])
        self.assertEqual(self.tick_at(r, 299), [])
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(self.tick_at(r, 600), [])
        self.assertEqual(r.status(WORK).paused_until, later(900))
        self.assertEqual(self.tick_at(r, 900), [])
        self.assertEqual(r.status(WORK), AccountStatus(False, False, None, None))
        self.assertEqual(self.tick_at(r, 1200), [paused("Work Account", 1500)])
        self.assertEqual(w.usage.times(WORK), [later(s) for s in (0, 300, 600, 900, 1200)])

    def test_accounts_filter(self):
        w = self.world
        for uuid in (WORK, PERSONAL, ORG):
            w.park(uuid)
        r = w.reader(accounts=[WORK, ORG])
        self.tick_at(r, 0)
        self.assertEqual(sorted(owner for _, owner, _ in w.usage.calls), sorted([WORK, ORG]))
        self.assertEqual(r.status(PERSONAL), AccountStatus(False, False, None, None))
        only = w.reader(accounts=PERSONAL)
        self.tick_at(only, 0)
        self.assertEqual(w.usage.times(PERSONAL), [later(0)])

    def test_no_secret_in_any_report_status_or_state_file(self):
        w = self.world
        live = w.go_live(ALEX)
        work = w.park(WORK)
        org = w.park(ORG)
        personal = w.park(PERSONAL, seconds=-5)
        w.usage.plan(ALEX, RuntimeError(f"boom {live['accessToken']} {live['refreshToken']}"))
        w.usage.plan(WORK, Unavailable(f"Could not reach the server with {work['accessToken']}"))
        w.usage.plan(ORG, UnexpectedReply(f"bad reply {org['refreshToken']}"))
        w.refresh.errors[PERSONAL] = UnexpectedReply(f"bad reply {personal['refreshToken']}")
        r = w.reader()
        texts = []
        texts += self.tick_at(r, 0)
        w.usage.plan(ALEX, SignInDead())
        w.usage.plan(WORK, NotAllowed())
        w.usage.plan(ORG, RateLimited(600))
        w.store.broken[PERSONAL] = f"broken {personal['refreshToken']}"
        texts += self.tick_at(r, 300)
        w.slot.broken = True
        texts += self.tick_at(r, 1200)
        w.registry.path.write_text(f"{{{live['refreshToken']}")
        texts += self.tick_at(r, 1500)
        statuses = [repr(r.status(uuid)) for uuid in ORDER]
        self.assertTrue(texts)
        for text in texts + statuses + [w.state.read_text(), repr(r.__dict__.get("_identity"))]:
            self.assertNotIn("SECRET", text)
            self.assertNotIn("access-", text)
            self.assertNotIn("refresh-", text)

    def test_recovery_reports_carry_no_secret(self):
        w = self.world
        with mock.patch.object(recovering, "write", failing_writes(1)):
            r, old, new, reports = self.fail_renewal_save()
            reports += self.tick_at(r, 60)
        w.recovery_file(ORG).write_text(f"{{{new['refreshToken']}")
        reports += self.tick_at(r, 120)
        self.assertEqual(len(reports), 3)
        for text in reports:
            self.assertNotIn("SECRET", text)

    def test_live_repr_hides_the_signin(self):
        live = reading._Live(reading.KNOWN, ALEX, {"accessToken": "access-SECRET", "refreshToken": "refresh-SECRET"})
        self.assertNotIn("SECRET", repr(live))
        self.assertNotIn("SECRET", str(live))

    def test_same_renewal_on_its_own(self):
        same = {"refreshToken": "refresh-1-SECRET"}
        self.assertTrue(reading._same_renewal(same, {"refreshToken": "refresh-1-SECRET", "accessToken": "x"}))
        self.assertFalse(reading._same_renewal(same, {"refreshToken": "refresh-2-SECRET"}))
        self.assertFalse(reading._same_renewal(same, None))
        self.assertFalse(reading._same_renewal(same, "refresh-1-SECRET"))
        self.assertFalse(reading._same_renewal({}, {}))
        self.assertFalse(reading._same_renewal({"refreshToken": ""}, {"refreshToken": ""}))
        self.assertFalse(reading._same_renewal({"refreshToken": None}, {"refreshToken": None}))

    def test_no_renewal_while_live_account_unknown(self):
        w = self.world
        w.go_live(ALEX)
        w.profile.offline = True
        w.park(WORK, seconds=-30)
        fresh = w.park(ORG)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [UNCONFIRMED])
        self.assertEqual(w.refresh.calls, [])
        self.assertEqual(w.usage.tokens(ORG), [fresh["accessToken"]])
        self.assertEqual(w.usage.times(WORK), [])
        gets = w.store.gets.count(WORK)
        self.assertEqual(self.tick_at(r, 15), [])
        self.assertEqual(w.store.gets.count(WORK), gets)
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.refresh.calls, [])
        w.profile.offline = False
        self.assertEqual(self.tick_at(r, 600), [])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(len(w.usage.times(WORK)), 1)

    def test_signed_out_slot_allows_renewal(self):
        w = self.world
        w.park(WORK, seconds=-30)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(len(w.usage.times(WORK)), 1)

    def test_slot_unreadable_still_reads_fresh_parked_keys(self):
        w = self.world
        w.slot.broken = True
        fresh = w.park(WORK)
        w.park(ORG, seconds=-30)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [UNCONFIRMED])
        self.assertEqual(w.usage.tokens(WORK), [fresh["accessToken"]])
        self.assertEqual(w.refresh.calls, [])

    def test_renewal_waits_while_the_write_lock_is_busy(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK, seconds=-30)
        r = w.reader()
        with mock.patch.object(reading, "LOCK_TIMEOUT_S", 0.05):
            with held(w.lock_path):
                self.assertEqual(self.tick_at(r, 0), [])
            self.assertEqual(w.refresh.calls, [])
            self.assertEqual(w.usage.times(WORK), [])
            self.assertEqual(self.tick_at(r, 15), [])
        self.assertEqual(w.refresh.calls, [WORK])
        self.assertEqual(w.usage.times(WORK), [later(15)])

    def test_restart_waits_for_a_recent_reading(self):
        w = self.world
        w.park(WORK)
        w.park(PERSONAL)
        w.history.add_reading(WORK, "usage", 10.0, FIVE_RESET, 5.0, WEEK_RESET, START - timedelta(seconds=100))
        w.history.add_reading(PERSONAL, "statusline", 10.0, FIVE_RESET, 5.0, WEEK_RESET, START - timedelta(seconds=100))
        r = w.reader()
        self.tick_at(r, 0)
        self.tick_at(r, 199)
        self.assertEqual(w.usage.times(WORK), [])
        self.assertEqual(w.usage.times(PERSONAL), [later(0)])
        self.tick_at(r, 200)
        self.assertEqual(w.usage.times(WORK), [later(200)])

    def test_history_failure_reported_once(self):
        w = self.world
        w.park(WORK)
        r = w.reader()
        with mock.patch.object(w.history, "add_reading", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertEqual(
                self.tick_at(r, 0),
                [f"Lifeline could not save a reading for Work Account, so readings for it are paused until {clock_text(later(300))}."],
            )
            self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(self.tick_at(r, 600), [])
        self.assertEqual(r.status(WORK), AccountStatus(False, False, None, None))
        self.assertEqual(len(w.usage.times(WORK)), 3)

    def test_store_failure_reported_once(self):
        w = self.world
        w.park(WORK)
        w.store.broken[WORK] = "The parked sign-in could not be read."
        r = w.reader()
        self.assertEqual(
            self.tick_at(r, 0),
            [
                "Lifeline could not read its saved sign-in for Work Account, "
                f"so readings for it are paused until {clock_text(later(300))}."
            ],
        )
        self.assertEqual(self.tick_at(r, 300), [])
        del w.store.broken[WORK]
        self.tick_at(r, 600)
        self.assertEqual(w.usage.times(WORK), [later(600)])

    def test_broken_registry_reported_once(self):
        w = self.world
        w.registry.path.write_text("not json")
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), ["Lifeline could not read its list of accounts, so it took no readings."])
        self.assertEqual(self.tick_at(r, 15), [])
        self.assertEqual(w.slot.reads, 0)

    def test_nothing_due_reads_nothing(self):
        w = self.world
        w.go_live(ALEX)
        w.park(WORK)
        r = w.reader()
        self.tick_at(r, 0)
        reads, gets = w.slot.reads, len(w.store.gets)
        for seconds in (15, 30, 299):
            self.assertEqual(self.tick_at(r, seconds), [])
        self.assertEqual((w.slot.reads, len(w.store.gets)), (reads, gets))

    def test_concurrent_ticks_read_each_account_once(self):
        w = self.world
        w.park(WORK)
        w.on_usage = lambda uuid, token: time.sleep(0.05)
        r = w.reader()
        threads = [threading.Thread(target=r.tick) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(w.usage.times(WORK)), 1)

    def test_status_of_an_unseen_account_is_clear(self):
        r = self.world.reader()
        self.assertEqual(r.status("99999999-ffff-4fff-8fff-000000000009"), AccountStatus(False, False, None, None))
        with self.assertRaises(Exception):
            r.status(WORK).needs_signin = True


class ReplyStampTests(unittest.TestCase):
    def test_a_usage_row_is_stamped_when_the_reply_arrives(self):
        w = World(self)
        w.go_live(ALEX)
        w.remember_live(ALEX)

        def slow(uuid, token):
            w.clock.now = w.clock.now + timedelta(seconds=7)

        w.on_usage = slow
        r = w.reader(accounts=ALEX)
        w.clock.now = START
        r.tick()
        self.assertEqual(w.history.latest(ALEX).at, START + timedelta(seconds=7))
        self.assertEqual(w.history.latest(ALEX).source, "usage")


class CacheLabelTests(unittest.TestCase):
    def test_the_usage_cache_is_used_only_while_claude_codes_file_names_this_account(self):
        for org, used in (("org-11111111", True), ("org-elsewhere", False)):
            with self.subTest(org=org):
                w = World(self)
                w.go_live(ALEX, seconds=120)
                w.remember_live(ALEX, cache_entry(ALEX, START - timedelta(minutes=2)))
                data = json.loads((w.home / ".claude.json").read_text())
                data["oauthAccount"]["organizationUuid"] = org
                (w.home / ".claude.json").write_text(json.dumps(data))
                w.clock.now = START
                w.reader(accounts=ALEX).tick()
                latest = w.history.latest(ALEX)
                self.assertEqual(latest is not None and latest.source == "cache", used)


class OrganizationReaderTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self, order=SAME_LOGIN)
        backoff = mock.patch.object(reading, "SAVE_BACKOFF_S", (0.0, 0.0))
        backoff.start()
        self.addCleanup(backoff.stop)

    def tick_at(self, reader, seconds):
        self.world.clock.now = later(seconds)
        return reader.tick()

    def test_each_organization_of_one_login_is_read_with_its_own_signin(self):
        w = self.world
        w.usage.plan(PERSONAL, (40.0, 20.0))
        w.usage.plan(TEAM, (7.0, 3.0))
        personal = w.go_live(PERSONAL)
        team = w.park(TEAM)
        w.park(ALEX)
        w.park(WORK)
        w.remember_live(PERSONAL)
        r = w.reader()
        self.assertEqual(self.tick_at(r, 0), [])
        self.assertEqual(w.usage.tokens(PERSONAL), [personal["accessToken"]])
        self.assertEqual(w.usage.tokens(TEAM), [team["accessToken"]])
        self.assertEqual((w.history.latest(PERSONAL).five, w.history.latest(TEAM).five), (40.0, 7.0))
        w.store.inner.remove(TEAM)
        w.store.inner.put(PERSONAL, personal)
        team_live = w.go_live(TEAM)
        w.remember_live(TEAM)
        w.usage.plan(PERSONAL, (41.0, 21.0))
        w.usage.plan(TEAM, (8.0, 4.0))
        self.assertEqual(self.tick_at(r, 300), [])
        self.assertEqual(w.usage.tokens(TEAM), [team["accessToken"], team_live["accessToken"]])
        self.assertEqual(w.usage.tokens(PERSONAL), [personal["accessToken"], personal["accessToken"]])
        self.assertEqual((w.history.latest(PERSONAL).five, w.history.latest(TEAM).five), (41.0, 8.0))

    def test_claude_codes_usage_cache_is_never_filed_under_one_of_two_organizations(self):
        w = self.world
        w.go_live(PERSONAL, seconds=-60)
        w.park(TEAM)
        w.remember_live(PERSONAL, cache_entry(PERSONAL, START - timedelta(minutes=2)))
        r = w.reader()
        self.tick_at(r, 0)
        self.assertIsNone(w.history.latest(PERSONAL))
        self.assertEqual(w.usage.times(PERSONAL), [])


if __name__ == "__main__":
    unittest.main()
