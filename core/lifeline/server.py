import fcntl
import http.server
import json
import logging
import logging.handlers
import math
import os
import re
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
import uuid as uuidlib
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from lifeline import alerts as alerting
from lifeline import addtext, anthropic, clock, ident, recovery, statusline, viewmodel, writelock
from lifeline import enroll as enrolling
from lifeline import forecast as forecasting
from lifeline import switch as switching
from lifeline.accounts import (
    KNOWN_NAME,
    Registry,
    identity,
    known_entry,
    known_of,
    load_known,
    organization_kind,
    plan_label,
    resolve,
    stated,
)
from lifeline.history import SOURCES, History
from lifeline.reader import AccountStatus, Reader
from lifeline.slot import MacKeychainSlot, run_security
from lifeline.store import MacKeychainStore

log = logging.getLogger("lifeline.server")

VERSION = 1
TICK_SECONDS = 15.0
COUNTDOWN_SECONDS = 5.0
AUTO_SWITCH_AT = forecasting.SWITCH_AT
BODY_LIMIT = 64 * 1024
REQUEST_TIMEOUT = 15
SOCKET_LIMIT = 104
PRESENT_IDLE_SECONDS = 600
PRESENCE_FRESH = timedelta(minutes=3)
MATCH_SECONDS = 120
SIGNIN_LIFETIME = timedelta(days=30)
JUST_SWITCHED_FOR = timedelta(minutes=switching.WATCH_MINUTES)
UNDO_SHOWN_FOR = timedelta(seconds=5)
AUTO_SWITCH_TITLE = "Switch this Mac for me before an account runs out"
AUTO_SWITCH_DESCRIPTION = (
    f"At {AUTO_SWITCH_AT:g}%, this Mac moves to the account with the most room "
    f"after {COUNTDOWN_SECONDS:g} seconds, unless you cancel. Claude Code sessions follow."
)
AUTO_NO_ROOM = "Lifeline did not switch this Mac, because no other account has room now."
AUTO_REASON = "{name} reached {at:g}% of its {window}"
LEARN_EVERY = timedelta(hours=1)
LEARN_SPAN = timedelta(days=28)
WEEK_SPAN = timedelta(days=7)
KEEP_FORECAST = timedelta(hours=24)
KEEP_FOR = timedelta(hours=24)
KEYS_KEEP_FOR = timedelta(days=8)
SESSION_RECENT = timedelta(hours=5)
SESSION_FORGET = timedelta(days=1)
IDENTIFY_RETRY = timedelta(minutes=5)
MATCH_LOOKBACK = forecasting.WINDOW + timedelta(hours=1)
STARTUP_QUIET_SECONDS = 120
SLEEP_GAP_SECONDS = 60
DEVBOX_READ_GAP = timedelta(seconds=300)
SERVICE_LOCK_NAME = "service.lock"
HISTORY_DAYS = 7
MAX_HISTORY_DAYS = 366
LOGIN_TIMEOUT = 600
LOGIN_PAGE_SECONDS = 60
LOGIN_POLL = 0.2
OPEN_SHIM = ".lifeline-open"
SESSION_FILE_LIMIT = 64 * 1024
STATUSLINE_SOURCE = "statusline"
DEVBOX_SOURCE = "usage"
USAGE_SOURCES = frozenset({"usage", "cache", DEVBOX_SOURCE})
SETTINGS_NAME = "settings.json"
SETTINGS_PARTS = (".claude", "settings.json")
SETTINGS_BACKUP = ".lifeline-backup-"
STATUSLINE_KEY = "statusLine"
STATUSLINE_PREVIOUS = "statusline-previous.json"
STATUSLINE_REFRESH_KEY = "refreshInterval"
STATUSLINE_REFRESH_SECONDS = 5
EPOCH_START = datetime(1970, 1, 1, tzinfo=timezone.utc)
STATUSLINE_LOCK = threading.Lock()
PINNED_FOLDER = "app"
PINNED_LINK = "current"
STATUSLINE_NAME = "lifeline-statusline"
LOG_NAME = "lifeline.log"
CODE_ROOT = Path(__file__).resolve().parents[2]
BUILD_NAME = "build.txt"
BUILD_LIMIT = 200
LOG_BYTES = 1024 * 1024
LOG_FILES = 3
LOG_FORMAT = "%(asctime)s %(message)s"
BUNDLE_PARTS = ("Lifeline.app", "Contents", "Resources", "core", "bin", STATUSLINE_NAME)
LAST_SWITCH_NAME = "last_switch.json"
COMPUTER_NAME = "computer.json"
COMPUTER_ACCOUNT = "paired-computer"
PAIRING_ACCOUNT = "pairing"
LOGIN_COMMAND = "claude"
CHANNEL_NAMES = alerting.CHANNELS
READING_KEYS = frozenset({"source", "session_id", "account_uuid", "org_uuid", "at", "five_hour", "seven_day"})
ADD_KEYS = frozenset({"email", "name", "short", "number", "known", "step"})
RENAME_KEYS = ("name", "short")
PLAIN_WORD = re.compile(r"[a-z_]{1,40}")
REPORT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
REPORT_LIMIT = 500
REPORTS_KEEP_FOR = timedelta(hours=48)
REPORTS_NAME = "devbox_reports.json"
FORGET_NAME = "devbox_forget.json"
FORGET_ROUTE = re.compile(r"/reset|/remove/[A-Za-z0-9-]{1,128}")
FORGET_WAIT = 8.0
RESET_ROUTE = "/reset"
RESET_FILES = ("accounts.json", SETTINGS_NAME, LAST_SWITCH_NAME, "spent.json", "reader_state.json", REPORTS_NAME)
SWITCH_BUSY = "Lifeline is switching this Mac now, so nothing was changed. Try again when the switch ends."
SIGNIN_BUSY = "A sign-in is still running, so nothing was changed. Finish or cancel it first."
REMOVED = {
    None: "Lifeline removed {name}.",
    True: "Lifeline removed {name} here and on the other computer.",
    False: "Lifeline removed {name} here, and removes it on the other computer when the other computer next answers.",
}
RESET_DONE = {
    None: "Lifeline is back to its first run.",
    True: "Lifeline is back to its first run here and on the other computer.",
    False: "Lifeline is back to its first run here, and resets the other computer when the other computer next answers.",
}
RESET_SAVED = "It saved a copy of its history and settings in {path}."
NO_RESET_COPY = "Lifeline could not save a copy of its history and settings, so nothing was changed."
REMOVE_UNFINISHED = "Lifeline could not finish removing {name}, so it is still listed. Try again."
RESET_UNFINISHED = (
    "Lifeline could not finish the reset, and some saved sign-ins may already be gone. Try again. The copy it saved is in {path}."
)
NO_RESET_SIGNINS = "Lifeline could not list its saved sign-ins, so nothing was changed."
NO_DEVBOX_NOTE = "Lifeline could not save its note for the other computer, so nothing was changed."
KEEP_HISTORY = timedelta(days=90)
PRUNE_EVERY = timedelta(days=1)
PUSH_LIMIT = 200
SIGNIN_KEY = "signin-ends:"
ADOPT_KEYS = frozenset({"name", "short", "number", "step", "expected"})
RESIGN_KEYS = frozenset({"email", "step"})
MACHINES = {"mac": "this Mac", "devbox": "the other computer"}
PLACEMENTS = ("mac", "devbox", "both")
DEFAULT_PLACEMENT = "both"
PLACEMENT_ID = re.compile(r"[A-Za-z0-9-]{1,128}")
NESTED_ROUTE = "devbox"
JOBS_ROUTE = "jobs"
CANCEL_WORD = "cancel"
OPEN_WORD = "open"
PAGE_KINDS = frozenset({"add", "resign", "devbox-add", "devbox-resign"})
CANCEL_WAIT_SECONDS = 1.5
JOB_STOP_SECONDS = 30
PROXY_SLICE_SECONDS = 0.2
SIGNIN_KINDS = frozenset({"add", "resign"})
DEVBOX_JOB_KINDS = {"add": "devbox-add", "adopt": "devbox-adopt", "resign": "devbox-resign"}
DEVBOX_ADD_KEYS = frozenset({"uuid", "number", "email", "step"})
PROXY_JOIN_SECONDS = 5
FOLLOW_JOIN_SECONDS = 30

ALREADY_RUNNING = "Lifeline is already running."
NOT_A_SOCKET = "Another file sits where Lifeline opens its connection for the menu, so Lifeline did not start."
OPEN_FOLDER = "The folder for Lifeline's connection must belong to you and be closed to others, so Lifeline did not start."
LONG_PATH = "The path of Lifeline's connection is too long, so Lifeline did not start."
NO_SOCKET = "Lifeline could not open its connection for the menu, so it did not start."
MAC_ONLY = "This part of Lifeline runs only on the Mac."
FROM_DEVBOX = "This came from Lifeline on the other computer."
DEVBOX_REFUSED_CHANGES = "The other computer no longer accepts this Mac's key, so pair it again in Alerts and settings."
DEVBOX_SWITCH_REFUSED = "Lifeline switches only this Mac, so nothing was changed."
BAD_COMPUTER = "Adding a computer takes the address and the pairing code it showed."
BAD_CODE = "The pairing code is the 20 letters and digits that lifeline pair showed on the other computer."
PAIR_SILENT = "Lifeline could not reach {address}, so nothing was paired. Check the address and that Lifeline runs there."
PAIR_REFUSED = "{address} did not accept that code, so nothing was paired. Run lifeline pair there for a new code."
PAIR_UNSAVED = "Lifeline could not save the pairing, so nothing was paired."
PAIRED = "This Mac is paired with {name} now."
FORGOTTEN = "This Mac no longer reads {name}. Pair it again whenever you like."
NOTHING_PAIRED = "This Mac is not paired with another computer."
UNKNOWN_KNOWN = "Lifeline does not know an account called {name}."
FULL_RESTORED = "Lifeline restored the full name of {name} from your list of known accounts."
STOPPING_SIGNIN = "Lifeline is stopping, so it did not start the sign-in."
NO_LIVE_TO_ADOPT = "Claude Code on this Mac has no current sign-in Lifeline can read, so nothing was added."
MAC_NOT_EXPECTED = "Claude Code on this Mac uses {live}, not {expected}, so nothing was changed."
MAC_LIVE_GONE = "Claude Code on this Mac no longer uses {expected}, so nothing was changed."
NOT_ADDED_ACCOUNT = "an account Lifeline has not added"
KEEPING_LIVE = "The other computer already uses {name}, so Lifeline is keeping the sign-in the other computer has."
SIGN_IN_ONCE = "The other computer already uses {name}, so you sign in once, here on this Mac. The other computer keeps its own sign-in."
ALREADY_ON_DEVBOX = "The other computer already has its own sign-in for {name}."
ADOPT_DEVBOX = "Add {name} from the other computer"
ADOPT_DEVBOX_PLAIN = "Add the other computer's own sign-in"
ADOPT_MAC = "Add {name} from this Mac"
ADOPT_MAC_PLAIN = "Add this Mac's own sign-in"
ADOPT_BOTH = "Add both machines' own sign-ins"
ADDING_LIVE = "Lifeline is adding the account Claude Code uses on this Mac."
TAKING_OVER = "Lifeline on the other computer is taking over the sign-in its Claude Code holds for {name}."
TAKING_OVER_PLAIN = "Lifeline on the other computer is taking over the sign-in its Claude Code holds."
STEP = "Step {index} of {total}: {action}"
BROWSER_SIGNED_IN = (
    "If {browser} is signed in to claude.ai as that account, sign out there or choose the other account, then try again."
)
DEFAULT_BROWSER_WORDS = "your default browser"
ADD_DIFFERENT = "To add a different account, sign out of claude.ai in {browser} or choose the other account, then press Try again."
LATER_WORD = "later"
BAD_LATER = "Later needs the alert it is for."
LATER_SAID = "Lifeline reminds you again {when}."
BAD_ADOPT = "Adding the account Claude Code uses takes a name, short name and number, each optional."
BAD_RESIGN = "Signing in again takes only an email, and the email is optional."
RESCUED_AT_STOP = "Lifeline saved the sign-in for {name} in its recovery folder before it stopped."
NOT_RESCUED_AT_STOP = (
    "Lifeline could not save the sign-in for {name} before it stopped. "
    "If Claude Code is not on {name}, sign in to {name} again."
)
ADOPT_RUNNING = "Lifeline is already adding the account Claude Code uses."
NO_START = "Lifeline could not start."
BAD_STATUSLINE = "The status line setting takes on, as true or false."
OTHER_STATUSLINE = "Claude Code shows your own status line, and Lifeline never replaces or removes it."
STATUSLINE_ON = "Lifeline's status line is on. Claude Code shows it from its next refresh."
STATUSLINE_OFF = "Lifeline's status line is off."
NO_SETTINGS_FOLDER = "Lifeline could not find Claude Code's settings folder, so it changed nothing."
SETTINGS_UNREADABLE = "Lifeline could not read Claude Code's settings file, so it changed nothing."
SETTINGS_LINKED = "Claude Code's settings file is a link, so Lifeline left it as it is."
SETTINGS_MOVED = "Claude Code's settings file changed while Lifeline was updating it, so Lifeline wrote nothing."
SETTINGS_UNSAVED = "Lifeline could not save Claude Code's settings file, so it changed nothing."
SETTINGS_UNCHECKED = "Lifeline changed the status line but could not confirm the rest of Claude Code's settings."
NOT_FOUND = "Lifeline does not know that address."
WRONG_METHOD = "That address does not answer this type of request."
TOO_LARGE = "The request is larger than 64 KB, so Lifeline did not read it."
NO_LENGTH = "Lifeline needs to know the length of the request before it reads it."
BAD_LENGTH = "The request gives a length Lifeline cannot read."
SHORT_BODY = "The request ended before all of it arrived."
BAD_JSON = "The request body is not valid JSON."
NOT_OBJECT = "The request body must be a JSON object."
BAD_REQUEST = "Lifeline could not read that request."
INTERNAL = "Lifeline hit a problem and could not finish that request."
UNKNOWN_ACCOUNT = "That account is not in Lifeline."
NO_ACCOUNTS = "Lifeline could not read its list of accounts."
BAD_APPEARANCE = "The appearance must be light or dark."
BAD_DAYS = "The days value must be a whole number from 1 to 366."
BAD_TARGET = "Give the account's number, as in lifeline switch 2."
NO_COUNTDOWN = "There is no switch to cancel."
ALREADY_SWITCHING = "The switch has already started, so you cannot cancel it."
NO_UNDO = "There is no switch to undo."
NOTE_DAMAGED = "Lifeline could not read its note of the last switch, so nothing was changed."
UNSAVED_TARGET = "Lifeline renewed the sign-in for {name} but has not saved it yet, so it did not switch. Try again in a moment."
NO_FLUSH = "Lifeline could not save its renewed sign-ins, so it did not switch. Try again in a moment."
LOST_AT_STOP = "Lifeline stopped before it could save the renewed sign-in for {names}, so sign in to {them} again."
STOPPING = "Lifeline is stopping, so it did not switch."
ADD_RUNNING = "Lifeline is already adding an account. Finish that sign-in first."
RESIGN_RUNNING = "Lifeline is already signing in to that account again. Finish that sign-in first."
BAD_READING = "The reading is not in the status line's format."
BAD_PRESENCE = "Presence needs idle_seconds in seconds, and locked and asleep as true or false."
BAD_ADD = "Adding an account takes an email, name, short name and number, each optional."
BAD_NAMES = "A rename takes a name and a short name, each plain text on one line."
BAD_SETTING = "Each setting must be true or false."
UNCHECKED = "Lifeline could not check the switch with Anthropic yet."
DISAGREES = "Anthropic reports a different account in Claude Code, so check the menu."
SPARE_LEFT = "A spare copy of its saved sign-in is still in Lifeline."
NO_UNDO_SAVE = "Lifeline switched, but it could not save the switch for Undo."
BAD_SETTINGS_FILE = "Lifeline could not read its settings file, so it uses the defaults."
NO_SETTINGS_SAVE = "Lifeline could not save the settings."
JOB_FAILED = {
    "adopt": "Lifeline could not finish adding the account Claude Code uses.",
    "add": "Lifeline could not finish adding the account.",
    "resign": "Lifeline could not finish signing in again.",
    "devbox-add": "Lifeline could not finish the other computer sign-in.",
    "devbox-adopt": "Lifeline could not finish taking over the other computer's own sign-in.",
    "devbox-resign": "Lifeline could not finish the other computer sign-in.",
}
VIA_CHOICES = ("row", "footer", "key", "notification", "menu-undo", "cli")
BAD_VIA = "The via field must be row, footer, key, notification, menu-undo or cli."
BAD_UNDO = "An undo request takes only the via field."
PERSONAL_ORGANIZATION = "'s Organization"
JOB_LOG = {
    "adopt": "Adding the account Claude Code uses did not finish.",
    "add": "Adding an account did not finish.",
    "resign": "Signing in to {name} again did not finish.",
    "devbox-add": "Adding an account on the other computer did not finish.",
    "devbox-adopt": "Taking over the other computer's own sign-in did not finish.",
    "devbox-resign": "Signing in again on the other computer did not finish.",
}
NO_LOGIN_COMMAND = "Lifeline could not find Claude Code's command, so it could not open the browser sign-in."
NO_SIGNIN_PAGE = "Claude Code did not give a sign-in page within a minute, so Lifeline stopped the sign-in."
STRANGE_SIGNIN_PAGE = "Claude Code gave a sign-in page Lifeline does not trust, so Lifeline did not open it and stopped the sign-in."
LOGIN_TIMED_OUT = "The browser sign-in took longer than 10 minutes, so Lifeline stopped waiting."
CLIMB_SPAN = timedelta(minutes=30)
LIVE_ON = {"mac": "Claude Code on this Mac", "devbox": "The other computer's Claude Code"}
UNADDED_LIVE = "{who} uses {name}, which Lifeline has not added yet, so Lifeline will not read it or renew its sign-in."
NO_DAYS = "Lifeline has no readings for this account from the last 7 days."
STARTED_READING = "Lifeline started reading it {day}, so the last 7 days fill in from here."
WAITED_TOO_LONG = "Lifeline stopped waiting for {whose} sign-in after 10 minutes, so {nothing}."
STRANGE_SIGNIN_CAUSE = "Claude Code gave a sign-in page Lifeline does not trust, so Lifeline did not open it"
LOGIN_REFUSED = "The browser sign-in did not finish."
BAD_PLACEMENT = "Each account's place must be this Mac, the other computer or both."
NOT_ON_MAC = "This Mac did not switch, because {name} is set to be used on the other computer only."
BAD_DEVBOX_ADD = "Adding an account on the other computer takes the account and, if you like, an email."
DEVBOX_PROXY_BUSY = "Lifeline is already signing the other computer in to an account. Finish that sign-in first."
DEVBOX_PROXY_STOPPED = "Lifeline stopped, so it stopped the other computer sign-in too."
STOP_UNSURE = (
    "Lifeline stopped before the other computer answered. If the sign-in started there, the other computer ends it on its own within 10 minutes."
)
CANCEL_UNSURE = "You cancelled the sign-in. If it started on the other computer, Lifeline stops it there as soon as the other computer answers."
BAD_UNSURE = (
    "Lifeline could not keep that sign-in on the other computer to stop later. If it started, the other computer ends it on its own within 10 minutes."
)
ORPHAN_KEEP_SECONDS = 15 * 60
STILL_ON = "This Mac is still on {name}, which has used {used}% of its {window}."
NOTHING_CHANGED = "Nothing changed."
JOB_CANCELLED = "You cancelled the sign-in. Nothing changed."
STOP_CANCELLED = "Lifeline stopped, so it cancelled the sign-in. Nothing changed."
STOPPED_SIGNIN = "Lifeline stopped, so it cancelled the sign-in."
SIGNIN_CANCELLED = "You cancelled the sign-in."
CANCELLED_WHAT = {
    "add": "adding an account",
    "resign": "signing in to {name} again",
    "devbox-add": "adding an account on the other computer",
    "devbox-adopt": "taking over the other computer's own sign-in",
    "devbox-resign": "signing the other computer in to {name} again",
}
NO_SUCH_JOB = "Lifeline cannot find that sign-in."
JOB_ENDED = "That sign-in has already finished, so there is nothing to cancel."
JOB_NOT_CANCELLABLE = "That sign-in has nothing to cancel."
JOB_SAVING = "The sign-in has finished and Lifeline is saving it now, so you cannot cancel it."
PAGE_ENDED = "That sign-in has already finished, so there is no page to open."
NO_PAGE_TO_OPEN = "That sign-in has no page to open."
PAGE_NOT_READY = "The sign-in page is not ready yet. Try again in a moment."
PAGE_UNTRUSTED = "Lifeline does not trust that sign-in page, so it did not open it."


class ServiceError(Exception):
    pass


class AlreadyRunning(ServiceError):
    pass


class LoginFailed(enrolling.LoginFailure):
    pass


def login_result(reason, kind, devbox=False):
    nothing = "nothing changed" if kind == "resign" else "nothing was added"
    if devbox:
        nothing += " on the other computer" if kind == "resign" else " to the other computer"
    if reason == LOGIN_TIMED_OUT:
        return WAITED_TOO_LONG.format(whose="the other computer's" if devbox else "the", nothing=nothing)
    if reason == STRANGE_SIGNIN_PAGE:
        text = STRANGE_SIGNIN_CAUSE
    else:
        text = (reason or LOGIN_REFUSED).rstrip(".")
    return f"{text} and {nothing}." if ", so " in text else f"{text}, so {nothing}."


def says_nothing_changed(result):
    words = result.lower() if isinstance(result, str) else ""
    return "nothing was added" in words or "nothing changed" in words


def _control(thread):
    return {"stop": threading.Event(), "phase": "login", "cancelled": False, "stopped": False, "thread": thread}


class _ProxyStopped(Exception):
    pass


class Refusal(Exception):
    def __init__(self, status, sentence, extra=None):
        super().__init__(sentence)
        self.status = status
        self.sentence = sentence
        self.extra = dict(extra or {})


@dataclass
class Parts:
    home: Path
    folder: Path
    slot: object
    store: object
    registry: object
    history: object
    profile_fn: object
    usage_fn: object
    refresh_fn: object
    run_login: object
    runner: object
    user: str
    link: object
    clock: object
    tz: tzinfo
    monotonic: object = time.monotonic
    sessions_fn: object = None
    timer_factory: object = threading.Timer
    settle: float = switching.SETTLE_SECONDS
    lock_timeout: float = writelock.DEFAULT_TIMEOUT
    session_timeout: float = switching.SESSION_TIMEOUT
    temp_root: Path | None = None
    backups: Path | None = None
    session_details_fn: object = None
    relay_fn: object = None
    open_page: object = None
    callback_fn: object = None
    open_browser: object = None
    link_in_background: bool = False
    browser_fn: object = None


class NullLink:
    def reachable(self):
        return False

    def fetch_readings(self):
        return []

    def devbox_account(self):
        return None

    def devbox_status(self):
        return None

    def push_registry(self, accounts):
        return False

    def push_readings(self, readings):
        return False


class _Scoped:
    def __init__(self, registry, skipped):
        self._registry = registry
        self._skipped = skipped

    def load(self):
        skip = self._skipped()
        return [account for account in self._registry.load() if account.uuid not in skip]

    def by_uuid(self, uuid):
        return self._registry.by_uuid(uuid)

    def __getattr__(self, name):
        return getattr(self._registry, name)


@dataclass(frozen=True)
class _Row:
    account: object
    five: float | None
    five_reset: datetime | None
    week: float | None
    week_reset: datetime | None
    reading_at: datetime | None
    forecast: forecasting.Forecast | None
    status: object
    signin_ends: datetime | None
    binding: str = "five"
    other_forecast: forecasting.Forecast | None = None

    @property
    def main(self):
        return self.week if self.binding == "week" else self.five

    @property
    def main_reset(self):
        return self.week_reset if self.binding == "week" else self.five_reset


@dataclass(frozen=True)
class _Snapshot:
    now: datetime
    rows: tuple
    mac: str | None
    devbox: str | None


def _segment(segments, reading, value, reset):
    if value is None or reset is None:
        return None
    key = clock.nearest_minute(reset, timezone.utc)
    usage_top, count = segments.get(key, (None, 0))
    if reading.source == STATUSLINE_SOURCE:
        return key, count
    if usage_top is not None and value < usage_top - forecasting.STALE_SLACK:
        usage_top, count = value, count + 1
    segments[key] = (value if usage_top is None else max(usage_top, value), count)
    return key, count


def _failed_line(lead, result):
    return result if says_nothing_changed(result) else f"{lead} {result}"


def _and(items):
    items = [item for item in items if item]
    if len(items) < 2:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


STEP_ONE_LEFT = {
    "devbox-add": ("{name} is on this Mac.", "the other computer has no sign-in for it yet"),
    "devbox-adopt": ("{name} is on this Mac.", "Lifeline does not manage the other computer's own sign-in for it yet"),
    "devbox-resign": ("{name} is signed in again on this Mac.", "the other computer keeps the sign-in it had"),
}


def second_step_result(kind, name, state, result):
    if kind not in STEP_ONE_LEFT:
        return None
    done, left = STEP_ONE_LEFT[kind]
    done = done.format(name=name)
    if state == "cancelled" and result == JOB_CANCELLED:
        return f"{done} You cancelled the other computer step, so {left}."
    if state == "cancelled" and result == CANCEL_UNSURE:
        return f"{done} You cancelled the other computer step. If it started on the other computer, Lifeline stops it there as soon as the other computer answers."
    if result == DEVBOX_PROXY_STOPPED:
        return f"{done} Lifeline stopped, so it cancelled the other computer step. {left[:1].upper()}{left[1:]}."
    if result == STOP_UNSURE:
        return (
            f"{done} Lifeline stopped before the other computer answered. "
            "If the other computer step started there, the other computer ends it on its own within 10 minutes."
        )
    return None


def _cancel_log(kind, name, result):
    what = CANCELLED_WHAT.get(kind, "the sign-in").format(name=name)
    for lead, said in ((STOPPED_SIGNIN, "Lifeline stopped, so it cancelled {}."), (SIGNIN_CANCELLED, "You cancelled {}.")):
        if result.startswith(lead):
            return " ".join(part for part in (said.format(what), result[len(lead):].strip()) if part)
    return result


def suggested_names(profile):
    organization = profile.get("organizationName") if isinstance(profile.get("organizationName"), str) else ""
    organization = organization.strip()
    if organization and not organization.endswith(PERSONAL_ORGANIZATION):
        return f"{organization} Account", organization
    display = profile.get("displayName") if isinstance(profile.get("displayName"), str) else ""
    first = display.strip().split()[0] if display.strip() else ""
    first = first[:-2] if first.endswith("'s") else first
    if not first:
        email = profile.get("emailAddress") if isinstance(profile.get("emailAddress"), str) else ""
        local = re.split(r"[^A-Za-z]+", email.partition("@")[0])
        first = next((part.capitalize() for part in local if part), "")
    return (f"{first}'s Account", first) if first else ("", "")


def _forecast_fields(fc, now):
    if fc is None:
        return None
    runs_out = fc.runs_out_at if fc.runs_out_at is not None and fc.runs_out_at > now else None
    return {"at_reset_pct": fc.at_reset_pct, "runs_out_at": runs_out, "pace_per_min": fc.pace_per_min, "basis": fc.basis}


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _clean(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _used(value):
    return 0.0 if value is None else max(0.0, float(value))


def named(value, name):
    """Puts the paired computer's name where a sentence says the other computer."""
    if not name:
        return value
    if isinstance(value, str):
        return value.replace("The other computer", name).replace("the other computer", name)
    if isinstance(value, dict):
        return {key: named(item, name) for key, item in value.items()}
    if isinstance(value, list):
        return [named(item, name) for item in value]
    return value


def machine_key(key, machine):
    if isinstance(key, str) and key.startswith(SIGNIN_KEY):
        return SIGNIN_KEY + machine + ":" + key[len(SIGNIN_KEY):]
    return key


class _MachineEvents:
    def __init__(self, history, machine):
        self._history = history
        self._machine = machine

    def add_event(self, key, at):
        return self._history.add_event(machine_key(key, self._machine), at)

    def event_at(self, key):
        return self._history.event_at(machine_key(key, self._machine))

    def __getattr__(self, name):
        return getattr(self._history, name)


def has_reading_at(history, uuid, at):
    stamp = at.astimezone(timezone.utc)
    stamp = stamp.replace(microsecond=stamp.microsecond // 1000 * 1000)
    return any(reading.at == stamp for reading in history.readings(uuid, stamp, limit=8, fields=()))


def _plain(text):
    return isinstance(text, str) and 0 < len(text) <= REPORT_LIMIT and not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def plan_text(plan):
    return plan_label(plan) if isinstance(plan, str) and plan else ""


def _write_private(path, data):
    path = Path(path)
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".lifeline-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


class _Unreadable(Exception):
    pass


def computer_key(runner):
    """Where the paired computer's key is kept: the keychain. Its name and address sit in computer.json."""
    return MacKeychainSlot(ident.bundle_id(), COMPUTER_ACCOUNT, runner)


def read_computer(folder, runner):
    """The paired computer's name, address and key, or None. The keychain is read only when computer.json exists."""
    try:
        record = _read_private(Path(folder) / COMPUTER_NAME)
        found = computer_key(runner).read() if isinstance(record, dict) else None
    except Exception:
        return None
    return dict(record, key=found["key"]) if isinstance(found, dict) and isinstance(found.get("key"), str) else None


def _read_private(path, limit=BODY_LIMIT):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Unreadable() from None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        raise _Unreadable() from None
    if len(raw) > limit:
        raise _Unreadable()
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Unreadable() from None


def last_switch_path(folder=None):
    return Path(folder if folder is not None else writelock.lifeline_folder()) / LAST_SWITCH_NAME


def write_last_switch(path, from_uuid, to_uuid, at):
    _write_private(path, {"from_uuid": from_uuid, "to_uuid": to_uuid, "at": _iso(at)})


def read_last_switch(path):
    try:
        data = _read_private(path)
    except _Unreadable:
        return None
    if not isinstance(data, dict):
        return None
    from_uuid, to_uuid = data.get("from_uuid"), data.get("to_uuid")
    if not isinstance(from_uuid, str) or not from_uuid or not isinstance(to_uuid, str) or not to_uuid:
        return None
    return {"from_uuid": from_uuid, "to_uuid": to_uuid, "at": _parse_time(data.get("at"))}


def default_settings():
    return {
        "auto_switch": True,
        "alerts": {kind: True for kind in alerting.KINDS},
        "channels": {name: True for name in CHANNEL_NAMES},
        "hide_name": True,
        "placement": {},
    }


def _merge_settings(settings, data):
    for key, value in data.items():
        if key in ("auto_switch", "hide_name"):
            if not isinstance(value, bool):
                raise Refusal(400, BAD_SETTING)
            settings[key] = value
        elif key in ("alerts", "channels"):
            if not isinstance(value, dict):
                raise Refusal(400, BAD_SETTING)
            names = alerting.KINDS if key == "alerts" else CHANNEL_NAMES
            word = "alert" if key == "alerts" else "alert channel"
            for name, flag in value.items():
                if name not in names:
                    raise Refusal(400, _unknown(name, word))
                if not isinstance(flag, bool):
                    raise Refusal(400, BAD_SETTING)
                settings[key][name] = flag
        elif key == "placement":
            _placement_check(value)
            settings[key] = {**settings.get(key, {}), **value}
        else:
            raise Refusal(400, _unknown(key, "setting"))
    return settings


def _placement_check(value):
    if not isinstance(value, dict):
        raise Refusal(400, BAD_PLACEMENT)
    for uuid, place in value.items():
        if not isinstance(uuid, str) or not PLACEMENT_ID.fullmatch(uuid) or place not in PLACEMENTS:
            raise Refusal(400, BAD_PLACEMENT)


def _unknown(name, word):
    if isinstance(name, str) and PLAIN_WORD.fullmatch(name):
        return f"Lifeline has no {word} called {name}."
    return f"Lifeline has no such {word}."


def load_settings(path):
    settings = default_settings()
    try:
        data = _read_private(path)
    except _Unreadable:
        return settings, False
    if data is None:
        return settings, True
    if not isinstance(data, dict):
        return settings, False
    try:
        return _merge_settings(settings, data), True
    except Refusal:
        return default_settings(), False


def live_sessions(home):
    folder = Path(home) / ".claude" / "sessions"
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    found = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            data = _read_private(folder / name, SESSION_FILE_LIMIT)
        except _Unreadable:
            continue
        if not isinstance(data, dict):
            continue
        session, pid = data.get("sessionId"), data.get("pid")
        if isinstance(session, str) and session and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            if _alive(pid):
                found.add(session)
    return found


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def statusline_script(source=None):
    return os.path.realpath(Path(source or __file__).resolve().parents[2] / "bin" / STATUSLINE_NAME)


def statusline_command(source=None):
    script = Path(statusline_script(source))
    root = script.parents[1]
    current = root.parent / PINNED_LINK
    if root.parent.name == PINNED_FOLDER and current.is_symlink() and os.path.realpath(current) == str(root):
        return shlex.quote(str(current / "bin" / script.name))
    return shlex.quote(str(script))


def _lifeline_script(path):
    path = Path(path)
    if path.name != STATUSLINE_NAME or path.parent.name != "bin":
        return False
    if path.parts[-len(BUNDLE_PARTS):] == BUNDLE_PARTS:
        return True
    return (path.parents[1] / "core" / "lifeline" / "statusline.py").is_file()


def _settings_file(home):
    return Path(home).joinpath(*SETTINGS_PARTS)


def _is_ours(entry, script):
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) != 1:
        return False
    found = os.path.realpath(os.path.expanduser(words[0]))
    return found == script or _lifeline_script(found)


def _command_of(entry):
    try:
        words = shlex.split(entry.get("command"))
    except (ValueError, TypeError, AttributeError):
        return None
    return words[0] if len(words) == 1 else None


def _parse_settings(raw):
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def statusline_state(home, source=None):
    unknown = {"on": None, "ours": None, "other_present": None}
    try:
        raw = _settings_file(home).read_bytes()
    except FileNotFoundError:
        return {"on": False, "ours": False, "other_present": False}
    except OSError:
        return unknown
    data = _parse_settings(raw)
    if data is None:
        return unknown
    if STATUSLINE_KEY not in data:
        return {"on": False, "ours": False, "other_present": False}
    ours = _is_ours(data[STATUSLINE_KEY], statusline_script(source))
    return {"on": True, "ours": ours, "other_present": not ours}


def _indent_of(text):
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) < len(line):
            return line[: len(line) - len(stripped)]
    return None


def _settings_bytes(data, raw):
    if raw is None:
        text, newline = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False), True
    else:
        original = raw.decode("utf-8")
        indent = _indent_of(original)
        if indent is None and "\n" not in original.strip():
            text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        else:
            text = json.dumps(data, indent=indent if indent is not None else 2, ensure_ascii=False, allow_nan=False)
        newline = original.endswith("\n")
    return (text + ("\n" if newline else "")).encode("utf-8")


def _peek(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _backup_settings(path, raw, mode, moment):
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    count = 0
    while True:
        suffix = stamp if count == 0 else f"{stamp}-{count}"
        target = path.with_name(f"{path.name}{SETTINGS_BACKUP}{suffix}")
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        except FileExistsError:
            count += 1
            continue
        break
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return target


def _write_settings(path, content, expected, mode):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _peek(path) != expected:
            raise Refusal(409, SETTINGS_MOVED)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _same_apart_from_statusline(path, before, intended):
    try:
        after = _parse_settings(path.read_bytes())
    except OSError:
        return False
    if after is None or list(after) != list(intended):
        return False
    others = {key: value for key, value in before.items() if key != STATUSLINE_KEY}
    found = {key: value for key, value in after.items() if key != STATUSLINE_KEY}
    return found == others and after.get(STATUSLINE_KEY) == intended.get(STATUSLINE_KEY)


def _refresh_kept(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 1


def _kept(path):
    try:
        found = _read_private(path)
    except _Unreadable:
        return None
    return found if isinstance(found, dict) else None


def change_statusline(home, on, moment, source=None, keep=None):
    """Turns the status line on or off. The first time it turns on, the entry that was there is kept in the keep
    folder, and turning it off puts that entry back exactly, or removes the entry if there was none."""
    saved = Path(keep if keep is not None else ident.data_folder(home)) / STATUSLINE_PREVIOUS
    folder = Path(home).joinpath(*SETTINGS_PARTS[:-1])
    path = _settings_file(home)
    if not folder.is_dir():
        raise Refusal(409, NO_SETTINGS_FOLDER)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError:
        raise Refusal(409, SETTINGS_UNREADABLE) from None
    if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise Refusal(409, SETTINGS_LINKED)
    raw = None
    data = {}
    if info is not None:
        try:
            raw = path.read_bytes()
        except OSError:
            raise Refusal(409, SETTINGS_UNREADABLE) from None
        data = _parse_settings(raw)
        if data is None:
            raise Refusal(409, SETTINGS_UNREADABLE)
    script = statusline_script(source)
    wanted = statusline_command(source)
    present = STATUSLINE_KEY in data
    if present and not _is_ours(data[STATUSLINE_KEY], script):
        raise Refusal(409, OTHER_STATUSLINE)
    sentence = STATUSLINE_ON if on else STATUSLINE_OFF
    named = _command_of(data[STATUSLINE_KEY]) if present else None
    target = os.path.realpath(shlex.split(wanted)[0])
    current = named is not None and os.path.realpath(os.path.expanduser(named)) == target
    refreshing = present and _refresh_kept(data[STATUSLINE_KEY].get(STATUSLINE_REFRESH_KEY))
    before = _kept(saved) if not on else None
    if (on and current and refreshing) or (not on and not present and (before is None or raw is None)):
        return sentence
    intended = dict(data)
    if on and present:
        entry = dict(data[STATUSLINE_KEY], command=wanted)
        if not _refresh_kept(entry.get(STATUSLINE_REFRESH_KEY)):
            entry[STATUSLINE_REFRESH_KEY] = STATUSLINE_REFRESH_SECONDS
        intended[STATUSLINE_KEY] = entry
    elif on:
        intended[STATUSLINE_KEY] = {"type": "command", "command": wanted, STATUSLINE_REFRESH_KEY: STATUSLINE_REFRESH_SECONDS}
    elif before is not None and "previous" in before:
        intended[STATUSLINE_KEY] = json.loads(before["previous"])
    else:
        intended.pop(STATUSLINE_KEY, None)
    mode = stat.S_IMODE(info.st_mode) if info is not None else 0o600
    backup = None
    try:
        content = _settings_bytes(intended, raw)
        if raw is not None:
            backup = _backup_settings(path, raw, mode, moment)
        if on and not current and _kept(saved) is None:
            _write_private(saved, {"previous": json.dumps(data[STATUSLINE_KEY])} if present else {})
        _write_settings(path, content, raw, mode)
        if not on:
            saved.unlink(missing_ok=True)
    except Refusal:
        raise
    except (OSError, ValueError, TypeError):
        raise Refusal(500, SETTINGS_UNSAVED) from None
    if not _same_apart_from_statusline(path, data, intended):
        where = f" A copy from before the change is at {backup}." if backup is not None else ""
        raise Refusal(500, SETTINGS_UNCHECKED + where)
    return sentence


def build_label(root):
    try:
        with open(Path(root) / BUILD_NAME, "rb") as handle:
            raw = handle.read(BUILD_LIMIT + 2)
    except OSError:
        return None
    try:
        line = raw.decode("utf-8").splitlines()[0].strip() if raw.strip() else ""
    except UnicodeDecodeError:
        return None
    if not line or len(line) > BUILD_LIMIT or not line.isprintable():
        return None
    return line


def _plain_field(value):
    if not isinstance(value, str):
        return None
    text = "".join("?" if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value).strip()
    return text or None


def _started_from_ms(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def live_session_details(home):
    folder = Path(home) / ".claude" / "sessions"
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = folder / name
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            data = _read_private(path, SESSION_FILE_LIMIT)
        except (OSError, _Unreadable):
            continue
        if not isinstance(data, dict):
            continue
        session, pid = data.get("sessionId"), data.get("pid")
        if not (isinstance(session, str) and session and isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**31):
            continue
        if not _alive(pid):
            continue
        found.append(
            {
                "session_id": session,
                "cwd": _plain_field(data.get("cwd")),
                "name": _plain_field(data.get("name")),
                "started_at": _started_from_ms(data.get("startedAt")),
            }
        )
    return found


def _project_order(entry):
    started = entry["started_at"]
    return (started is None, started or EPOCH_START)


def _search_path():
    extra = [Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    return os.pathsep.join([os.environ.get("PATH", "")] + [str(p) for p in extra])


def _stopped(stop):
    return stop is not None and stop.is_set()


def private_page_env(folder, env):
    from lifeline import devbox

    folder = Path(folder)
    hook, page, shim = devbox.hook_path(), folder / devbox.PAGE_NAME, folder / OPEN_SHIM
    env.update({"BROWSER": str(hook), devbox.PAGE_VARIABLE: str(page), "DISABLE_AUTOUPDATER": "1"})
    env["PATH"] = os.pathsep.join(part for part in (str(shim), env.get("PATH", "")) if part)
    shim.mkdir(mode=0o700, exist_ok=True)
    if not os.path.lexists(shim / "open"):
        (shim / "open").symlink_to(hook)
    return page


def run_login(
    folder, email=None, user=None, on_page=None, popen=None, open_page=None, clock=time.monotonic, poll=LOGIN_POLL, stop=None
):
    from lifeline import devbox

    if _stopped(stop):
        raise LoginFailed(SIGNIN_CANCELLED)
    command = shutil.which(LOGIN_COMMAND, path=_search_path())
    if command is None:
        raise LoginFailed(NO_LOGIN_COMMAND)
    args = [command, "auth", "login", "--claudeai"]
    if email:
        args += ["--email", email]
    folder = Path(folder)
    env = enrolling.login_environment(folder, user if user is not None else enrolling.login_user())
    try:
        page = private_page_env(folder, env)
        process = (popen or subprocess.Popen)(
            args,
            env=env,
            cwd=str(folder),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        raise LoginFailed(LOGIN_REFUSED) from None
    started, shown, failure, code = clock(), False, None, None
    try:
        while True:
            code = process.poll()
            if code is not None:
                break
            if _stopped(stop):
                failure = SIGNIN_CANCELLED
                break
            waited = clock() - started
            if waited >= LOGIN_TIMEOUT:
                failure = LOGIN_TIMED_OUT
                break
            if not shown:
                url = devbox._read_page(page)
                if url:
                    try:
                        devbox.signin_port(url)
                    except ValueError:
                        failure = STRANGE_SIGNIN_PAGE
                        break
                    browser, opened = (open_page or devbox.open_default)(url)
                    shown = True
                    if on_page is not None:
                        on_page(url, browser, opened)
                elif waited >= LOGIN_PAGE_SECONDS:
                    failure = NO_SIGNIN_PAGE
                    break
            if stop is not None:
                stop.wait(poll)
            else:
                time.sleep(poll)
    finally:
        if failure is not None:
            devbox._end(process)
    if failure is not None:
        raise LoginFailed(failure)
    if code != 0:
        raise LoginFailed(LOGIN_REFUSED)


def _email_shape(value):
    local, at, domain = value.rpartition("@")
    plain = all(ch.isprintable() and not ch.isspace() for ch in value)
    return bool(at and local and domain and plain and not value.startswith("-"))


def _local_zone():
    try:
        from zoneinfo import ZoneInfo

        target = os.path.realpath("/etc/localtime")
        marker = "zoneinfo" + os.sep
        if marker in target:
            return ZoneInfo(target.split(marker, 1)[1])
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo


def _near(first, second):
    return abs((first - second).total_seconds()) <= MATCH_SECONDS


def _same_event(first, second):
    if first[0] != second[0]:
        return False
    if first[1] is None or second[1] is None:
        return first[1] is None and second[1] is None
    return abs((first[1] - second[1]).total_seconds()) <= MATCH_SECONDS


class Service:
    machine = "mac"

    def __init__(self, parts, *, tick_seconds=TICK_SECONDS, countdown_seconds=COUNTDOWN_SECONDS):
        self.parts = parts
        self.tick_seconds = tick_seconds
        self.countdown_seconds = countdown_seconds
        self.folder = Path(parts.folder)
        self.lock_path = self.folder / writelock.LOCK_NAME
        self.record_path = switching.record_path_for(self.lock_path)
        self.settings_path = self.folder / SETTINGS_NAME
        self.last_switch_path = last_switch_path(self.folder)
        self.computer_path = self.folder / COMPUTER_NAME
        self.backups = Path(parts.backups) if parts.backups is not None else switching.default_backups(parts.home)
        self.ticks = 0
        self.build = build_label(CODE_ROOT)
        self._state = threading.RLock()
        self._switching = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._watcher = None
        self._retiring = []
        self._switch_target = None
        self._notifications = []
        self._jobs = {}
        self._proxies = {}
        self._controls = {}
        self._presence = None
        self._asleep_seen = None
        self._quiet_until = parts.monotonic() + STARTUP_QUIET_SECONDS
        self._reported = False
        self._clocks = None
        self._sessions = {}
        self._learned = {}
        self._forecasts = {}
        self._devbox = {"reachable": False, "account_uuid": None, "live_registered": None, "outsider": None}
        self._devbox_seen = {}
        self._devbox_saved = {}
        self._covered = frozenset()
        self._devbox_statuses = {}
        self._devbox_sessions = 0
        self._devbox_reports = {}
        self._devbox_signin_ends = {}
        self._devbox_held = None
        self._left_use = {}
        self._link_busy = False
        self._link_thread = None
        self._first_wait_from = None
        self._exchanged = False
        self._orphans = set()
        self._orphan_since = {}
        self._own_lock = threading.Lock()
        self._mac_live_ready = None
        self._devbox_scoped = {}
        self._readings_pushed = {}
        self._pruned = None
        self.reports_path = self.folder / REPORTS_NAME
        self._reports_newest = self._load_reports_mark()
        self._pushed = None
        self._skip = frozenset()
        self._usage_rows = {}
        self._countdown = None
        self._handled = {}
        self._just = None
        self._switch_note = None
        self._follow_ups = []
        self._identity = None
        self._mac_refused = False
        self._settings, ok = load_settings(self.settings_path)
        if not ok:
            self.report(BAD_SETTINGS_FILE)
        options = dict(
            registry=_Scoped(parts.registry, lambda: self._skip),
            store=parts.store,
            slot=parts.slot,
            history=parts.history,
            clock=parts.clock,
            home=parts.home,
            record_path=self.record_path,
            lock_path=self.lock_path,
            usage_fn=parts.usage_fn,
            refresh_fn=parts.refresh_fn,
            profile_fn=parts.profile_fn,
            guarded_fn=self.guarded,
            full_registry=parts.registry,
            live_note_fn=self._live_note,
        )
        self.reader = Reader(**options)
        self._computer = read_computer(self.folder, parts.runner)
        if self._computer is not None:
            from lifeline import devbox

            self.parts.link = devbox.paired_link(self._computer) or parts.link
        self._link_lock = threading.Lock()
        self._forgetting = False
        self._holding = None
        self._devbox_todo = self._load_todo() if self._linked() else []
        if not self._linked():
            (self.folder / FORGET_NAME).unlink(missing_ok=True)

    def computer_name(self):
        found = self._computer
        return found.get("name") if isinstance(found, dict) and isinstance(found.get("name"), str) else None

    def pair_computer(self, body):
        from lifeline import devbox, pairing

        address, code = body.get("address"), body.get("code")
        if set(body) != {"address", "code"} or not isinstance(address, str) or not isinstance(code, str):
            raise Refusal(400, BAD_COMPUTER)
        try:
            host, port = pairing.parse_address(address)
            pairing.code_key(code)
        except ValueError:
            raise Refusal(400, BAD_CODE if address.strip() else BAD_COMPUTER) from None
        shown = f"{host}:{port}"
        try:
            status, name, key = devbox.pair(shown, code)
        except (devbox.LinkError, OSError):
            raise Refusal(502, PAIR_SILENT.format(address=shown)) from None
        if status != 200 or name is None:
            raise Refusal(403, PAIR_REFUSED.format(address=shown))
        record = {"name": name, "address": shown, "key": key.hex()}
        try:
            computer_key(self.parts.runner).write({"key": record["key"]})
            _write_private(self.computer_path, {"name": name, "address": shown})
        except Exception:
            raise Refusal(500, PAIR_UNSAVED) from None
        self._use_computer(record, devbox.paired_link(record))
        log.info("This Mac is paired with %s at %s.", name, shown)
        return {"message": PAIRED.format(name=name), "name": name}

    def forget_computer(self, rest):
        if rest != "forget":
            raise Refusal(404, NOT_FOUND)
        name = self.computer_name()
        if name is None:
            return {"message": NOTHING_PAIRED}
        unpair = getattr(self.parts.link, "unpair", None)
        if unpair is not None:
            unpair()
        try:
            self.computer_path.unlink(missing_ok=True)
            computer_key(self.parts.runner).delete()
        except Exception:
            raise Refusal(500, PAIR_UNSAVED) from None
        self._use_computer(None, NullLink())
        return {"message": FORGOTTEN.format(name=name)}

    def _use_computer(self, record, link):
        with self._state:
            self._computer = record
            self.parts.link = link
            self._devbox["reachable"] = False
            self._pushed = None
            self._readings_pushed = {}
            self._mac_refused = False
            self._devbox_todo = []
        (self.folder / FORGET_NAME).unlink(missing_ok=True)
        self._wake.set()

    @property
    def watcher(self):
        return self._watcher

    def _now(self):
        return self.parts.clock()

    def _wall(self):
        return self._now().timestamp()

    def guarded(self):
        found = {w.chosen_uuid for w in [self._watcher, *self._retiring] if w is not None and w.active}
        target = self._switch_target
        if target is not None:
            found.add(target)
        return found

    def _known_accounts(self):
        return load_known(self.folder / KNOWN_NAME)

    def _restore_full_names(self):
        known = self._known_accounts()
        if not known:
            return
        try:
            with writelock.held(self.lock_path, timeout=self.parts.lock_timeout):
                for account in self.parts.registry.load():
                    entry = known_of(account, known)
                    if entry is not None and account.full == account.name and account.full != entry["full"]:
                        self.parts.registry.upsert(replace(account, full=entry["full"]))
                        log.info(FULL_RESTORED.format(name=account.name))
        except Exception as error:
            log.warning("Lifeline could not restore full names from the known accounts (%s).", type(error).__name__)

    def _devbox_live(self, snap, devbox_state, outsider):
        from lifeline import devbox

        uuid = devbox_state["account_uuid"]
        if not devbox_state["answers"] or (uuid is None and not isinstance(outsider, dict)):
            return None
        accounts = {row.account.uuid: row.account for row in snap.rows}
        known = self._known_accounts()
        added = accounts.get(uuid)
        if added is not None:
            entry = known_of(added, known)
            email, organization = added.email, organization_kind(added.oauth_account)
            name, short = added.name, added.short
        else:
            email = outsider.get("email") if isinstance(outsider, dict) else None
            organization = outsider.get("organization") if isinstance(outsider, dict) else None
            entry = known_entry(known, email, organization) if email else None
            name, short = (entry["name"], entry["short"]) if entry is not None else (None, None)
        return {
            "uuid": uuid,
            "added": added is not None,
            "known": entry is not None,
            "name": name,
            "short": short,
            "email": email,
            "organization": organization,
            "adopt_ready": devbox.adopt_ready(uuid, set(accounts), self._devbox.get("live_ready")),
            "held": self._devbox.get("live_held") is True,
            "note": SIGN_IN_ONCE.format(name=name) if name and added is None else None,
            "adopt_title": ADOPT_DEVBOX.format(name=name) if name else ADOPT_DEVBOX_PLAIN,
            "both_title": ADOPT_BOTH,
        }

    def _live_step(self, now):
        ready = self._mac_live_check(now)
        with self._state:
            self._mac_live_ready = ready

    def _mac_live_check(self, now):
        try:
            item = self.parts.slot.read()
        except Exception:
            return False
        oauth = item.get(switching.OAUTH_KEY) if isinstance(item, dict) else None
        if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
            return False
        if switching._expired(oauth, now.timestamp()):
            entry = switching.read_record_entry(self.record_path)
            if entry is None or entry.get("refresh_sha256") != switching.fingerprint(oauth):
                return False
        details = statusline.read_oauth_account(self.parts.home)
        return isinstance(details, dict) and stated(details)[0] is not None

    def _mac_adopt_ready(self):
        with self._state:
            ready = self._mac_live_ready
        if ready is None:
            ready = self._mac_live_check(self._now())
            with self._state:
                self._mac_live_ready = ready
        return ready

    def _mac_live(self, snap):
        uuid = snap.mac
        accounts = {row.account.uuid: row.account for row in snap.rows}
        known = self._known_accounts()
        added = accounts.get(uuid)
        if added is not None:
            entry = known_of(added, known)
            email, organization = added.email, organization_kind(added.oauth_account)
            name, short = added.name, added.short
        else:
            details = statusline.read_oauth_account(self.parts.home)
            if uuid is None and not isinstance(details, dict):
                return None
            details = details if isinstance(details, dict) else {}
            email = details.get("emailAddress") if isinstance(details.get("emailAddress"), str) else None
            organization = organization_kind(details)
            entry = known_entry(known, email, organization) if email else None
            name, short = (entry["name"], entry["short"]) if entry is not None else (None, None)
        return {
            "uuid": uuid,
            "added": added is not None,
            "known": entry is not None,
            "name": name,
            "short": short,
            "email": email,
            "organization": organization,
            "adopt_ready": self._mac_adopt_ready(),
            "adopt_title": ADOPT_MAC.format(name=name) if name else ADOPT_MAC_PLAIN,
        }

    def _card(self, job, snap):
        names = {row.account.uuid: row.account.name for row in snap.rows}
        name = names.get(job.get("uuid"))
        with self._state:
            devbox_live = self._devbox["account_uuid"]
        place = job.get("browser") or self._signin_browser() or "your default browser"
        kind = job.get("kind")
        if kind == "add":
            action = f"press Authorize in {place} to add {name or job.get('known') or 'a new account'} to this Mac."
        elif kind == "resign":
            action = f"press Authorize in {place} to sign this Mac in to {name or 'the account'} again."
        elif kind == "adopt":
            action = ADDING_LIVE
        elif kind == "devbox-add" and job.get("uuid") is not None and job.get("uuid") == devbox_live:
            action = KEEPING_LIVE.format(name=name or "the account")
        elif kind == "devbox-add":
            action = f"press Authorize in {place} to give the other computer {name or 'the new account'}."
        elif kind == "devbox-resign":
            action = f"press Authorize in {place} to sign the other computer in to {name or 'the account'} again."
        elif kind == "devbox-adopt":
            live_name = names.get(devbox_live)
            action = TAKING_OVER.format(name=live_name) if live_name else TAKING_OVER_PLAIN
        else:
            return None
        step = job.get("step")
        if step and step[1] > 1:
            return STEP.format(index=step[0], total=step[1], action=action)
        return action[:1].upper() + action[1:]

    def _live_note(self):
        details = statusline.read_oauth_account(self.parts.home)
        if not isinstance(details, dict):
            return None
        login, org = stated(details)
        if login is None:
            return None
        try:
            accounts = self.parts.registry.load()
        except Exception:
            return None
        if org is None and any(account.login == login for account in accounts):
            return None
        if org is not None and resolve(accounts, login, org) is not None:
            return None
        email = details.get("emailAddress") if isinstance(details.get("emailAddress"), str) else ""
        entry = known_entry(self._known_accounts(), email, organization_kind(details))
        name = entry["name"] if entry is not None else (email.strip() or "an account")
        who = LIVE_ON[self.machine]
        return UNADDED_LIVE.format(who=who, name=name)

    def _known_missing(self):
        known = self._known_accounts()
        try:
            accounts = self.parts.registry.load()
        except Exception:
            return []
        return [entry for entry in known if not any(known_of(account, [entry]) is not None for account in accounts)]

    def _signin_browser(self):
        from lifeline import devbox

        if self.parts.browser_fn is None and sys.platform != "darwin":
            return None
        try:
            return (self.parts.browser_fn or devbox.default_browser)()
        except Exception:
            return None

    def start(self, ticking=True):
        self._stop.clear()
        self._restore_full_names()
        if ticking and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, name="lifeline-scheduler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._state:
            proxies = list(self._proxies.values())
        deadline = time.monotonic() + PROXY_JOIN_SECONDS
        for proxy in proxies:
            if proxy is not threading.current_thread():
                proxy.join(max(0.0, deadline - time.monotonic()))
        for job in self._stop_signins():
            if job is not threading.current_thread() and job.ident is not None:
                job.join(JOB_STOP_SECONDS)
        exchange = self._link_thread
        if exchange is not None and exchange is not threading.current_thread():
            exchange.join(PROXY_JOIN_SECONDS)
        self.finish_checks()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(60)
        with self._state:
            countdown = self._countdown
            self._countdown = None
        if countdown is not None:
            countdown["timer"].cancel()
        with self._switching:
            watchers = [watcher for watcher in [self._watcher, *self._retiring] if watcher is not None]
            for watcher in watchers:
                if watcher.active:
                    for sentence in self._ask(watcher.poll) or []:
                        log.info("%s", sentence)
            for watcher in watchers:
                if watcher.active and (watcher is not self._watcher or not self._held_in_slot(watcher)):
                    self._rescue_at_stop(watcher)
        try:
            reports, unsaved = self._flush_reader()
        except Exception as error:
            log.warning("Lifeline could not save its renewed sign-ins before it stopped (%s).", type(error).__name__)
            return
        for sentence in reports:
            log.info("%s", sentence)
        if unsaved:
            names = self._names()
            listed = sorted(names.get(uuid, "an account in Lifeline") for uuid in unsaved)
            joined = listed[0] if len(listed) == 1 else ", ".join(listed[:-1]) + " and " + listed[-1]
            log.warning(LOST_AT_STOP.format(names=joined, them="it" if len(listed) == 1 else "them"))

    def _held_in_slot(self, watcher):
        entry = switching.read_record_entry(self.record_path)
        if entry is None or entry["uuid"] != watcher.chosen_uuid or entry["refresh_sha256"] is None:
            return False
        try:
            item = self.parts.slot.read()
        except Exception:
            return False
        oauth = item.get("claudeAiOauth") if isinstance(item, dict) else None
        return switching.fingerprint(oauth) == entry["refresh_sha256"]

    def _rescue_at_stop(self, watcher):
        name = self._names().get(watcher.chosen_uuid, "an account in Lifeline")
        try:
            saved = watcher.rescue() is True
        except Exception:
            saved = False
        if saved:
            log.info(RESCUED_AT_STOP.format(name=name))
        else:
            log.warning(NOT_RESCUED_AT_STOP.format(name=name))

    def _flush_reader(self):
        reports, unsaved = self.reader.flush()
        return list(reports or []), set(unsaved or ())

    def _before_switch(self, uuid):
        try:
            reports, unsaved = self._flush_reader()
        except Exception as error:
            log.warning("Lifeline could not save its renewed sign-ins before a switch (%s).", type(error).__name__)
            raise Refusal(409, NO_FLUSH) from None
        for sentence in reports:
            self.report(sentence)
        if uuid in unsaved:
            name = self._names().get(uuid, "that account")
            raise Refusal(409, UNSAVED_TARGET.format(name=name))

    def _loop(self):
        due = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() >= due:
                try:
                    self.tick()
                except Exception as error:
                    log.warning("A round of Lifeline's work failed (%s).", type(error).__name__)
                due = time.monotonic() + self.tick_seconds
            woke = self._wake.wait(max(0.0, due - time.monotonic()))
            if woke:
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._guarded_step("auto-switch", self._auto_step, self._now())

    def tick(self):
        now = self._now()
        self._note_clocks(now)
        for name, step in (
            ("watch", self._watch_step),
            ("live", self._live_step),
            ("reading", self._read_step),
            ("forecast", self._learn_step),
            ("alerts", self._alert_step),
            ("auto-switch", self._auto_step),
            ("tidy", self._tidy_step),
            ("prune", self._prune_step),
        ):
            self._guarded_step(name, step, now)
        self.ticks += 1

    def _note_clocks(self, now):
        wall, mono = now.timestamp(), self.parts.monotonic()
        last = self._clocks
        self._clocks = (wall, mono)
        with self._state:
            report = self._presence
            slept = last is not None and (wall - last[0]) - (mono - last[1]) > SLEEP_GAP_SECONDS
            if slept and (report is None or report["at"].timestamp() <= last[0]):
                self._quiet(mono)
            if report is not None and report["asleep"] and report is not self._asleep_seen:
                self._asleep_seen = report
                self._quiet(mono)

    def _quiet(self, mono):
        self._quiet_until = mono + STARTUP_QUIET_SECONDS
        self._reported = False

    def _alerts_allowed(self):
        with self._state:
            return self._reported or self.parts.monotonic() >= self._quiet_until

    def _guarded_step(self, name, step, now):
        try:
            step(now)
        except Exception as error:
            log.warning(
                "Lifeline could not finish its %s step (%s). It will try again on the next round.",
                name,
                type(error).__name__,
            )

    def report(self, sentence, kind="report", key=None, title=None, body="", uuid=None, log_it=True, keep=False):
        with self._state:
            ident = key or "report-" + uuidlib.uuid4().hex[:16]
            if any(n["id"] == ident for n in self._notifications):
                return
            self._notifications.append(
                {
                    "id": ident,
                    "kind": kind,
                    "title": title if title is not None else sentence,
                    "body": body,
                    "uuid": uuid,
                    "at": self._now(),
                    "keep": bool(keep),
                }
            )
        if log_it:
            log.info("%s", sentence)

    def _watcher_for(self, uuid, seed):
        return switching.Watcher(
            self.parts.slot,
            self.parts.store,
            self.parts.registry,
            uuid,
            self.parts.profile_fn,
            clock=self.parts.monotonic,
            lifetime=None,
            seed=seed,
            home=self.parts.home,
            lock_path=self.lock_path,
            backups=self.backups,
            wall_clock=self._wall,
            settle=self.parts.settle,
        )

    def _recorded(self):
        uuid = switching.read_record(self.record_path)
        if uuid is None:
            return None
        try:
            known = self.parts.registry.by_uuid(uuid)
        except Exception:
            return None
        return uuid if known is not None else None

    def _watch_step(self, now):
        with self._switching:
            watcher = self._watcher
            if watcher is None or not watcher.active:
                recorded = self._recorded()
                watcher = self._watcher_for(recorded, None) if recorded is not None else None
                self._watcher = watcher
            if watcher is not None:
                for sentence in self._stand_down(watcher):
                    self.report(sentence)
            for retiring in list(self._retiring):
                for sentence in self._stand_down(retiring):
                    self.report(sentence)
                if not retiring.active:
                    self._retiring.remove(retiring)
            if self._watcher is not None and self._watcher.active:
                self._identity = None
                return
        self._identify(now)

    def _identify(self, now):
        with self._switching:
            looked = self._identify_start(now)
        if looked is None:
            return
        item, mark, known = looked
        found = self._identify_found(item)
        with self._switching:
            if self._recorded() is not None or self._identity is not known or self._slot_mark() != mark:
                return
            self._identify_finish(now, mark, known, found)

    def _slot_mark(self):
        try:
            item = self.parts.slot.read()
        except Exception:
            return None
        oauth = item.get(switching.OAUTH_KEY) if isinstance(item, dict) else None
        return switching.fingerprint(oauth) if isinstance(oauth, dict) else None

    def _identify_start(self, now):
        if self._recorded() is not None:
            self._identity = None
            return None
        try:
            item = self.parts.slot.read()
        except Exception:
            return None
        oauth = item.get(switching.OAUTH_KEY) if isinstance(item, dict) else None
        mark = switching.fingerprint(oauth) if isinstance(oauth, dict) else None
        if mark is None:
            self._identity = None
            return None
        known = self._identity
        if known is not None and known[0] == mark and (known[1] is not None or now < known[2]):
            return None
        return item, mark, known

    def _identify_found(self, item):
        try:
            found = switching.identify_live(
                item,
                profile_fn=self.parts.profile_fn,
                home=self.parts.home,
                record_path=self.record_path,
                registry=self.parts.registry,
                clock=self._wall,
            )
        except Exception:
            return None
        return found

    def _identify_finish(self, now, mark, known, found):
        if known is not None and known[1] is not None and found is not None and found != known[1]:
            names = self._names()
            log.info(
                "Claude Code on %s changed from %s to %s outside Lifeline.",
                MACHINES[self.machine],
                names.get(known[1], "an account Lifeline does not know"),
                names.get(found, "an account Lifeline does not know"),
            )
        self._identity = (mark, found, now + IDENTIFY_RETRY)

    def mac_uuid(self):
        watcher = self._watcher
        if watcher is not None and watcher.active:
            return watcher.chosen_uuid
        recorded = self._recorded()
        if recorded is not None:
            return recorded
        known = self._identity
        return known[1] if known is not None else None

    def _read_step(self, now):
        if self.parts.link_in_background:
            self._start_link_exchange(now)
            moment = self.parts.monotonic()
            with self._state:
                if self._first_wait_from is None:
                    self._first_wait_from = moment
                waiting = not self._exchanged and moment - self._first_wait_from < self.tick_seconds
            if not waiting:
                self._own_reads(now)
            return
        if self._link_exchange(now):
            self._own_reads(now)
            self._after_exchange(now)
        else:
            self._own_reads(now)

    def _after_exchange(self, now):
        link = self.parts.link
        self._push_readings(link, now)
        self._note_refusal(link)
        self._stop_orphans(link)

    def _start_link_exchange(self, now):
        with self._state:
            if self._link_busy:
                return
            self._link_busy = True

        def run():
            try:
                reached = self._link_exchange(self._now())
                with self._state:
                    first = not self._exchanged
                    self._exchanged = True
                if first:
                    self._own_reads(self._now())
                if reached:
                    self._after_exchange(self._now())
            except Exception as error:
                log.warning("Lifeline could not finish its exchange with the other computer (%s).", type(error).__name__)
            finally:
                with self._state:
                    self._link_busy = False
                    self._exchanged = True

        thread = threading.Thread(target=run, name="lifeline-devbox-link", daemon=True)
        try:
            thread.start()
        except Exception as error:
            log.warning("Lifeline could not start its exchange with the other computer (%s).", type(error).__name__)
            with self._state:
                self._link_busy = False
                self._exchanged = True
            return
        self._link_thread = thread

    def _prune_orphans(self, moment):
        for token in list(self._orphans):
            since = self._orphan_since.setdefault(token, moment)
            if moment - since > ORPHAN_KEEP_SECONDS:
                self._orphans.discard(token)
                self._orphan_since.pop(token, None)

    def _stop_orphans(self, link):
        with self._state:
            self._prune_orphans(self.parts.monotonic())
            tokens = sorted(self._orphans)
        for token in tokens:
            try:
                status, data = link.find_request(token)
            except Exception:
                continue
            if status == 200 and isinstance(data, dict):
                if data.get("state") == "running":
                    if not isinstance(data.get("job"), str):
                        continue
                    try:
                        stopped, _ = link.stop_signin(data["job"])
                    except Exception:
                        continue
                    if stopped != 200:
                        continue
            elif status != 404:
                continue
            with self._state:
                self._orphans.discard(token)
                self._orphan_since.pop(token, None)

    def _link_exchange(self, now):
        link = self.parts.link
        reachable = self._ask(link.reachable) is True
        readings = None
        if reachable:
            try:
                readings = list(link.fetch_readings())
            except Exception:
                log.warning("The other computer answered but its readings could not be read, so this Mac read instead.")
                reachable = False
        now = self._now()
        if not reachable:
            with self._state:
                self._devbox["reachable"] = False
                self._devbox["live_registered"] = None
                self._devbox["outsider"] = None
                self._devbox_scoped = {}
                self._covered = frozenset()
                self._devbox_statuses = {}
                self._devbox_held = None
            self._pushed = None
            return False
        self._save_devbox(readings, now)
        devbox = self._ask(link.devbox_account)
        with self._state:
            before = self._devbox["account_uuid"]
            if before is not None and before != devbox:
                self._left_use[before] = now
        asked = getattr(link, "live_registered", None)
        registered = self._ask(asked) if callable(asked) else None
        asked = getattr(link, "live_outsider", None)
        outsider = self._ask(asked) if callable(asked) else None
        asked = getattr(link, "live_ready", None)
        live_ready = self._ask(asked) if callable(asked) else None
        asked = getattr(link, "can_sign_in", None)
        can_sign_in = self._ask(asked) if callable(asked) else None
        asked = getattr(link, "live_held", None)
        live_held = self._ask(asked) if callable(asked) else None
        covered, statuses, reports, sessions, ends, held = self._devbox_facts(link)
        asked = getattr(link, "devbox_status", None)
        told = self._ask(asked) if asked is not None else None
        given = told.get("model_limits") if isinstance(told, dict) else None
        scoped = {uuid: list(rows) for uuid, rows in given.items() if isinstance(rows, list)} if isinstance(given, dict) else {}
        with self._state:
            self._devbox = {
                "reachable": True,
                "account_uuid": devbox if isinstance(devbox, str) and devbox else None,
                "live_registered": registered if isinstance(registered, bool) else None,
                "outsider": outsider if isinstance(outsider, dict) else None,
                "live_ready": live_ready if isinstance(live_ready, bool) else None,
                "can_sign_in": can_sign_in if isinstance(can_sign_in, bool) else None,
                "live_held": live_held if isinstance(live_held, bool) else None,
            }
            self._covered = covered
            self._devbox_statuses = statuses
            self._devbox_sessions = sessions
            self._devbox_signin_ends = ends
            self._devbox_held = held
            self._devbox_scoped = scoped
        self._import_reports(reports, now)
        with self._link_lock:
            self._send_devbox(link)
            self._push_registry(link)
        return True

    def _note_refusal(self, link):
        asked = getattr(link, "refused", None)
        refused = self._ask(asked) if callable(asked) else None
        if refused is True and not self._mac_refused:
            self._mac_refused = True
            self.report(DEVBOX_REFUSED_CHANGES)
        elif refused is False:
            self._mac_refused = False

    def _push_readings(self, link, now):
        push = getattr(link, "push_readings", None)
        if push is None:
            return
        with self._state:
            covered = set(self._covered)
        batch = []
        for account in self.parts.registry.load():
            if account.uuid in covered:
                continue
            mark = self._readings_pushed.get(account.uuid, now - MATCH_LOOKBACK)
            rows = self.parts.history.readings(account.uuid, mark, limit=PUSH_LIMIT + 1)
            batch.extend(reading for reading in rows if reading.at > mark)
        batch = sorted(batch, key=lambda reading: reading.at)[:PUSH_LIMIT]
        if not batch or self._ask(push, batch) is not True:
            return
        for reading in batch:
            self._readings_pushed[reading.uuid] = max(self._readings_pushed.get(reading.uuid, reading.at), reading.at)

    def _own_reads(self, now):
        if not self._own_lock.acquire(blocking=False):
            return
        try:
            self._own_reads_now(now)
        finally:
            self._own_lock.release()

    def _own_reads_now(self, now):
        with self._state:
            covered = set(self._covered) if self._devbox["reachable"] else set()
            saved = dict(self._devbox_saved)
        recent = {uuid for uuid, at in saved.items() if now - at < DEVBOX_READ_GAP}
        self._skip = frozenset(covered | recent)
        try:
            wanted = [account for account in self.parts.registry.load() if account.uuid not in self._skip]
        except Exception:
            wanted = None
        if self._switch_target is not None or wanted == []:
            reports = self._flush_reader()[0]
        else:
            reports = self.reader.tick()
        for sentence in reports:
            self.report(sentence)

    def _devbox_facts(self, link):
        try:
            known = frozenset(account.uuid for account in self.parts.registry.load())
        except Exception:
            known = frozenset()
        asked = getattr(link, "devbox_status", None)
        status = self._ask(asked) if asked is not None else None
        if not isinstance(status, dict):
            return known, {}, [], 0, {}, None
        covers = status.get("covers")
        if isinstance(covers, (list, tuple, set, frozenset)):
            covered = frozenset(uuid for uuid in covers if isinstance(uuid, str))
        else:
            covered = known
        given = status.get("statuses")
        statuses = {}
        if isinstance(given, dict):
            statuses = {uuid: item for uuid, item in given.items() if isinstance(uuid, str) and isinstance(item, AccountStatus)}
        reports = status.get("reports")
        sessions = status.get("sessions")
        if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 0:
            sessions = 0
        given = status.get("signin_ends")
        ends = {}
        if isinstance(given, dict):
            for uuid, stamp in given.items():
                moment = _parse_time(stamp)
                if isinstance(uuid, str) and moment is not None:
                    ends[uuid] = moment
        given = status.get("held")
        held = frozenset(uuid for uuid in given if isinstance(uuid, str)) if isinstance(given, (list, tuple, set, frozenset)) else None
        return covered, statuses, reports if isinstance(reports, list) else [], sessions, ends, held

    def _load_reports_mark(self):
        try:
            data = _read_private(self.reports_path)
        except _Unreadable:
            return None
        return _parse_time(data.get("newest")) if isinstance(data, dict) else None

    def _import_reports(self, reports, now):
        fresh = []
        for item in reports:
            if not isinstance(item, dict):
                continue
            ident, title = item.get("id"), item.get("title")
            moment = _parse_time(item.get("at"))
            if not isinstance(ident, str) or not REPORT_ID.fullmatch(ident) or not _plain(title) or moment is None:
                continue
            if self._reports_newest is not None and moment <= self._reports_newest:
                continue
            fresh.append((moment, ident, title))
        if not fresh:
            return
        for moment, ident, title in sorted(fresh):
            key = "devbox-" + ident
            with self._state:
                if key in self._devbox_reports:
                    continue
                self._devbox_reports[key] = now
            self.report(title, key=key, title=title, body=FROM_DEVBOX)
        self._reports_newest = max(moment for moment, _, _ in fresh)
        try:
            _write_private(self.reports_path, {"newest": self._reports_newest.isoformat()})
        except Exception:
            log.warning("Lifeline could not save which notes from the other computer it has shown.")

    def _registry_payload(self):
        return [
            {
                "uuid": account.uuid,
                "number": account.key,
                "name": account.name,
                "short": account.short,
                "full": account.full,
                "email": account.email,
                "plan": account.plan,
                "login_uuid": account.login,
                "org_uuid": account.org_uuid,
            }
            for account in sorted(self.parts.registry.load(), key=lambda a: a.key)
        ]

    def _push_registry(self, link):
        push = getattr(link, "push_registry", None)
        if push is None:
            return
        try:
            payload = self._registry_payload()
        except Exception:
            return
        known = self._known_accounts()
        if (payload, known) == self._pushed:
            return
        if self._ask(push, payload, known) is True:
            self._pushed = (payload, known)

    @staticmethod
    def _ask(action, *args):
        try:
            return action(*args)
        except Exception:
            return None

    def _save_devbox(self, readings, now):
        history = self.parts.history
        known = {account.uuid for account in self.parts.registry.load()}
        for reading in readings:
            uuid = getattr(reading, "uuid", None)
            original = getattr(reading, "at", None)
            if uuid not in known or not isinstance(original, datetime) or original.tzinfo is None:
                continue
            with self._state:
                known_seen = uuid in self._devbox_seen
            if not known_seen:
                earlier = [r.at for r in history.readings(uuid, now - timedelta(days=1), fields=()) if r.source == DEVBOX_SOURCE]
                with self._state:
                    self._devbox_seen.setdefault(uuid, max(earlier) if earlier else None)
            with self._state:
                seen = self._devbox_seen[uuid]
            if seen is not None and original <= seen:
                continue
            if has_reading_at(history, uuid, original):
                with self._state:
                    self._devbox_seen[uuid] = original
                continue
            source = getattr(reading, "source", None)
            try:
                saved = history.add_reading(
                    uuid,
                    source if source in SOURCES else DEVBOX_SOURCE,
                    getattr(reading, "five", None),
                    getattr(reading, "five_reset", None),
                    getattr(reading, "week", None),
                    getattr(reading, "week_reset", None),
                    min(original, now),
                )
            except ValueError:
                continue
            with self._state:
                self._devbox_seen[uuid] = original
                last = self._devbox_saved.get(uuid)
                if saved is not None and (last is None or saved.at > last):
                    self._devbox_saved[uuid] = saved.at

    def _learn_step(self, now):
        for account in self.parts.registry.load():
            stamp = self._learned.get(account.uuid)
            if stamp is not None and now - stamp[0] < LEARN_EVERY:
                continue
            readings = self.parts.history.readings(account.uuid, now - LEARN_SPAN, fields=("five",))
            self._learned[account.uuid] = (now, forecasting.learn(readings, self.parts.tz, now))

    def _kind(self, uuid, now):
        history = self.parts.history
        told = history.newest(uuid, now - WEEK_SPAN, sources=USAGE_SOURCES) or history.latest(uuid)
        return "week" if told is not None and told.five is None and told.week is not None else "five"

    def _figures(self, uuid, now):
        history = self.parts.history
        latest = history.latest(uuid)
        if latest is None:
            return None
        since = now - WEEK_SPAN
        told = history.newest(uuid, since, sources=USAGE_SOURCES) or latest
        five, five_reset, week, week_reset = latest.five, latest.five_reset, latest.week, latest.week_reset
        recent = max(since, told.at)
        if told.five is None and told.week is not None:
            five, five_reset = None, None
        elif five is None:
            found = history.newest(uuid, recent, field="five")
            if found is not None:
                five, five_reset = found.five, found.five_reset
        if week is None:
            found = history.newest(uuid, recent, field="week")
            if found is not None:
                week, week_reset = found.week, found.week_reset
        if five_reset is not None and five_reset <= now:
            five, five_reset = 0.0, None
        if week_reset is not None and week_reset <= now:
            week, week_reset = 0.0, None
        return five, five_reset, week, week_reset, latest.at, forecasting.binding(five, five_reset, week, week_reset, now)

    def _forecast(self, uuid, five_reset, now, binding="five", week_reset=None):
        reset = week_reset if binding == "week" else five_reset
        if reset is None:
            return None
        newest = self.parts.history.latest(uuid)
        hour = now.astimezone(self.parts.tz).hour
        if binding == "week":
            key = (newest, reset)
            fresh = newest is not None and now - newest.at <= KEEP_FORECAST
        else:
            recent = len(self.parts.history.readings(uuid, now - forecasting.RECENT, fields=()))
            key = (newest, reset, hour, recent)
            fresh = newest is not None and now - newest.at < forecasting.RECENT
        kept = self._forecasts.get((uuid, binding))
        if fresh and kept is not None and kept[0] == key:
            found = kept[1]
        elif binding == "week":
            readings = self.parts.history.readings(uuid, min(now, reset) - forecasting.WEEK_WINDOW, fields=("week",))
            found = forecasting.forecast(
                readings,
                reset,
                now,
                value="week",
                window=forecasting.WEEK_WINDOW,
                recent=forecasting.WEEK_RECENT,
                min_span=forecasting.WEEK_MIN_SPAN,
            )
        else:
            readings = self.parts.history.readings(uuid, min(now, reset) - forecasting.WINDOW, fields=("five",))
            stamp = self._learned.get(uuid)
            learned = stamp[1] if stamp is not None else {}
            found = forecasting.forecast(readings, reset, now, learned, hour)
        if fresh:
            self._forecasts[(uuid, binding)] = (key, found)
        else:
            self._forecasts.pop((uuid, binding), None)
        return found

    def _snapshot(self, now):
        try:
            accounts = sorted(self.parts.registry.load(), key=lambda a: a.key)
        except Exception:
            raise Refusal(500, NO_ACCOUNTS) from None
        rows = []
        for account in accounts:
            figures = self._figures(account.uuid, now)
            if figures is None:
                five = five_reset = week = week_reset = reading_at = None
                binding = "five"
            else:
                five, five_reset, week, week_reset, reading_at, binding = figures
            other = None
            if five is not None and week is not None:
                other = self._forecast(account.uuid, five_reset, now, "five" if binding == "week" else "week", week_reset)
            rows.append(
                _Row(
                    account,
                    five,
                    five_reset,
                    week,
                    week_reset,
                    reading_at,
                    self._forecast(account.uuid, five_reset, now, binding, week_reset),
                    self._status(account.uuid),
                    self._signin_end(account),
                    binding,
                    other,
                )
            )
        with self._state:
            devbox = self._devbox["account_uuid"]
        return _Snapshot(now, tuple(rows), self.mac_uuid(), devbox)

    def _status(self, uuid):
        with self._state:
            theirs = self._devbox_statuses.get(uuid) if self._devbox["reachable"] and uuid in self._covered else None
        return theirs if theirs is not None else self.reader.status(uuid)

    def _signin_end(self, account):
        stated = _parse_time(getattr(account, "signin_ends", None))
        if stated is not None:
            return stated
        started = _parse_time(account.signed_in_at)
        return started + SIGNIN_LIFETIME if started is not None else None

    def _devbox_side(self):
        with self._state:
            return dict(self._devbox_statuses), dict(self._devbox_signin_ends)

    def _usable_on_devbox(self):
        with self._state:
            return frozenset(self._covered) if self._devbox["reachable"] is True else frozenset()

    def _left_times(self):
        with self._state:
            left = dict(self._left_use)
        last = read_last_switch(self.last_switch_path)
        if last is not None and last["from_uuid"] and last["at"] is not None:
            before = left.get(last["from_uuid"])
            left[last["from_uuid"]] = last["at"] if before is None else max(before, last["at"])
        return left

    def _rising(self, uuid, now, left_at):
        since = now - CLIMB_SPAN if left_at is None else max(now - CLIMB_SPAN, left_at)
        try:
            found = [r for r in self.parts.history.readings(uuid, since) if r.at > since]
        except Exception:
            return False
        if len(found) < 2:
            return False
        first, last = found[0], found[-1]
        week = first.week is not None and last.week is not None and last.week > first.week
        five = (
            first.five is not None and last.five is not None and last.five > first.five
            and (first.five_reset is None or last.five_reset is None or _near(first.five_reset, last.five_reset))
        )
        return week or five

    def _views(self, snap, view="mac"):
        statuses, ends = self._devbox_side() if view == "dev" else ({}, {})
        devbox_usable = self._usable_on_devbox()
        left = self._left_times()
        views = []
        for row in snap.rows:
            uuid = row.account.uuid
            needs, barred, end = row.status.needs_signin, row.status.cannot_read, row.signin_ends
            if view == "dev":
                theirs = statuses.get(uuid)
                needs = theirs.needs_signin if theirs is not None else False
                barred = theirs.cannot_read if theirs is not None else False
                end = ends.get(uuid)
            views.append(
                viewmodel.AccountView(
                    uuid,
                    row.account.name,
                    row.account.short,
                    plan_text(row.account.plan),
                    row.account.key,
                    row.five,
                    row.week,
                    row.five_reset,
                    row.week_reset,
                    row.forecast,
                    needs,
                    barred,
                    row.status.paused_until,
                    row.reading_at,
                    signin_ends=end,
                    on_mac=self.on_mac(uuid),
                    binding=row.binding,
                    other_forecast=row.other_forecast,
                    on_devbox=uuid in devbox_usable,
                    climbing=None if uuid in (snap.mac, snap.devbox) else self._rising(uuid, snap.now, left.get(uuid)),
                    pause_reason=row.status.last_error,
                )
            )
        return tuple(views)

    def _switch_sessions(self, just):
        live = self._ask(self.parts.sessions_fn) if self.parts.sessions_fn is not None else None
        with self._state:
            sessions = dict(self._sessions)
        moved = behind = 0
        for session, (uuid, at) in sessions.items():
            if isinstance(live, (set, frozenset)) and session not in live:
                continue
            if at < just[2]:
                continue
            if uuid == just[1]:
                moved += 1
            elif uuid == just[0]:
                behind += 1
        return moved, behind

    def _menu(self, snap, view, sessions_mac, back, sessions_dev=0):
        now = snap.now
        with self._state:
            countdown = self._countdown
            just = self._just
        target = countdown["target"] if countdown is not None else None
        back, back_window = back if back is not None else (None, "")
        moment = None
        moved = behind = 0
        if just is not None and now - just[2] < UNDO_SHOWN_FOR:
            moment = ("mac", just[0], just[1])
            moved, behind = self._switch_sessions(just)
        known = self._identity
        with self._state:
            answers = self._devbox["reachable"] is True
            outsider = self._devbox.get("outsider")
        accounts = [row.account for row in snap.rows]
        added = {account.uuid for account in accounts}
        mac_unadded = devbox_unadded = None
        if snap.mac is not None and snap.mac not in added:
            mac_unadded = self._unadded_known(self._mac_outsider(accounts, snap.mac))
        if answers and snap.devbox is not None and snap.devbox not in added:
            devbox_unadded = self._unadded_known(outsider)
        return viewmodel.MenuState(
            self._views(snap, view),
            snap.mac,
            snap.devbox,
            view,
            moment,
            target,
            back,
            sessions_mac,
            sessions_dev,
            now,
            self.parts.tz,
            sessions_moved=moved,
            sessions_behind=behind,
            live_signed_in=known is not None and known[0] is not None,
            back_window=back_window,
            countdown_left=max(1, math.ceil((countdown["ends_at"] - now).total_seconds())) if countdown is not None else 5,
            devbox_answers=answers,
            mac_unadded_known=mac_unadded,
            mac_adopt_ready=view == "mac" and snap.mac is not None and snap.mac not in added and self._mac_adopt_ready(),
            devbox_unadded_known=devbox_unadded,
        )

    def _mac_outsider(self, accounts, uuid):
        details = statusline.read_oauth_account(self.parts.home)
        if not isinstance(details, dict):
            return None
        login, org = stated(details)
        if login is None or identity(accounts, login, org) != uuid:
            return None
        return {"email": details.get("emailAddress"), "organization": organization_kind(details)}

    def _unadded_known(self, outsider):
        if not isinstance(outsider, dict) or not isinstance(outsider.get("email"), str):
            return None
        entry = known_entry(self._known_accounts(), outsider["email"], outsider.get("organization"))
        return {key: entry[key] for key in ("name", "short", "email")} if entry is not None else None

    def _back(self, snap):
        last = read_last_switch(self.last_switch_path)
        if last is None or last["from_uuid"] == snap.mac:
            return None
        uuid = last["from_uuid"]
        row = next((r for r in snap.rows if r.account.uuid == uuid), None)
        if row is None or row.main is None or row.main >= forecasting.LIMIT or not self.on_mac(uuid):
            return None
        now = snap.now
        fields = ["five"]
        week_forecast = row.forecast if row.binding == "week" else row.other_forecast
        if alerting.week_may_have_ended(row.week_reset, now) or alerting.restarted_lately(week_forecast, now):
            fields.append("week")
        ended = [(end, field) for field in fields if (end := alerting.ended_at_limit(self.parts.history, uuid, now, field)) is not None]
        return (uuid, max(ended)[1]) if ended else None

    def _session_counts(self, mac, now):
        live = None
        if self.parts.sessions_fn is not None:
            live = self._ask(self.parts.sessions_fn)
        with self._state:
            sessions = dict(self._sessions)
        if isinstance(live, (set, frozenset)):
            behind = sum(1 for session in live if session in sessions and mac is not None and sessions[session][0] != mac)
            return len(live), behind
        recent = {key: value for key, value in sessions.items() if now - value[1] <= SESSION_RECENT}
        behind = sum(1 for uuid, _ in recent.values() if mac is not None and uuid != mac)
        return len(recent), behind

    def _session_pair(self, snap, now):
        with self._state:
            dev = self._devbox_sessions if self._devbox["reachable"] else 0
        return self._session_counts(snap.mac, now), (dev, 0)

    def _projects(self, snap):
        fn = self.parts.session_details_fn
        details = self._ask(fn) if fn is not None else self._ask(live_session_details, self.parts.home)
        with self._state:
            sessions = dict(self._sessions)
        projects = {row.account.uuid: [] for row in snap.rows}
        unknown = []
        for item in details or []:
            if not isinstance(item, dict):
                continue
            owner = sessions.get(item.get("session_id"))
            uuid = owner[0] if owner is not None else None
            entry = {"cwd": item.get("cwd"), "name": item.get("name"), "started_at": item.get("started_at")}
            (projects[uuid] if uuid in projects else unknown).append(entry)
        for listed in [*projects.values(), unknown]:
            listed.sort(key=_project_order)
        return projects, unknown

    def later(self, rest, body):
        if rest != LATER_WORD:
            raise Refusal(404, NOT_FOUND)
        if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str) or not body["id"].strip():
            raise Refusal(400, BAD_LATER)
        ident = body["id"]
        parts = ident.split(":")
        nothing = {"reminder": None, "message": None}
        if len(parts) != 4 or parts[0] + ":" != SIGNIN_KEY or parts[1] not in ("mac", "devbox"):
            return nothing
        now = self._now()
        if parts[1] == "devbox":
            with self._state:
                ends = self._devbox_signin_ends.get(parts[2])
        else:
            ends = next((row.signin_ends for row in self._snapshot(now).rows if row.account.uuid == parts[2]), None)
        if ends is None:
            return nothing
        if machine_key(alerting.signin_key(parts[2], ends, self.parts.tz), parts[1]) != ident or self.parts.history.event_at(ident) is None:
            return nothing
        lasting = machine_key(alerting.later_key(parts[2], ends) + alerting.LATER_SUFFIX, parts[1])
        asked = self.parts.history.event_at(lasting) or now
        due = max(ends - alerting.REMIND_BEFORE, asked + alerting.REMIND_AFTER)
        if due >= ends or due <= now:
            return nothing
        self.parts.history.add_event(lasting, now)
        return {"reminder": _iso(due), "message": LATER_SAID.format(when=clock.when(due, now, self.parts.tz))}

    def set_statusline(self, body):
        if set(body) != {"on"} or not isinstance(body.get("on"), bool):
            raise Refusal(400, BAD_STATUSLINE)
        with STATUSLINE_LOCK:
            sentence = change_statusline(self.parts.home, body["on"], self._now().astimezone(self.parts.tz), keep=self.parts.folder)
        return {"statusline": statusline_state(self.parts.home), "message": sentence}

    def state(self, appearance="dark"):
        if appearance not in ("light", "dark"):
            raise Refusal(400, BAD_APPEARANCE)
        now = self._now()
        snap = self._snapshot(now)
        (total, behind), (dev_total, dev_behind) = self._session_pair(snap, now)
        back = self._back(snap)
        mac = viewmodel.build(self._menu(snap, "mac", total, back, dev_total), appearance)
        dev = viewmodel.build(self._menu(snap, "dev", total, back, dev_total), appearance)
        with self._state:
            just = self._just
        showing = just is not None and now - just[2] < UNDO_SHOWN_FOR and mac.get("action_kind") == "undo"
        mac["undo_until"] = just[2] + UNDO_SHOWN_FOR if showing else None
        dev["undo_until"] = None
        if "icon_dot" in dev:
            mac["icon_dot"] = dev["icon_dot"]
        statuses, ends = self._devbox_side()
        projects, projects_unknown = self._projects(snap)
        with self._state:
            settings = self._shown_settings(self._settings)
            notifications = [dict(n) for n in self._notifications]
            jobs = [dict(j) for j in self._jobs.values()]
            computer = self._computer if isinstance(self._computer, dict) else {}
            devbox = {
                "name": computer.get("name"),
                "address": computer.get("address"),
                "reachable": self._devbox["reachable"],
                "answers": self._devbox["reachable"] is True,
                "account_uuid": self._devbox["account_uuid"],
                "live_registered": self._devbox.get("live_registered"),
                "set_up": self._linked(),
            }
            live_outsider = self._devbox.get("outsider")
            countdown = self._countdown
            switch_note = self._switch_note
        devbox["live"] = self._devbox_live(snap, devbox, live_outsider)
        with self._state:
            devbox["can_sign_in"] = self._devbox.get("can_sign_in") if devbox["answers"] else None
        mac["live"] = self._mac_live(snap)
        if settings["hide_name"]:
            mac["icon_name"] = ""
            dev["icon_name"] = ""
        accounts = []
        with self._state:
            answering = self._devbox["reachable"] is True
            held = self._devbox_held
        views = {view.uuid: view for view in self._views(snap)}
        covered = self._usable_on_devbox()
        emails = [(row.account.email or "").strip().lower() for row in snap.rows]
        for row in snap.rows:
            uuid = row.account.uuid
            where = [name for name, holder in (("mac", snap.mac), ("devbox", snap.devbox)) if holder == uuid]
            fc = row.forecast
            theirs = statuses.get(uuid)
            if not answering:
                signed = None
            elif held is not None:
                signed = uuid in held
            else:
                signed = uuid in ends or uuid in covered or (theirs is not None and (theirs.needs_signin or theirs.cannot_read))
            if signed is False:
                theirs = None
            devbox_side = None if signed is None else {
                "held": signed,
                "needs": bool(theirs.needs_signin) if theirs is not None else False,
                "ends": ends.get(uuid) if signed else None,
            }
            signin = viewmodel.signin_text(now, self.parts.tz, row.status.needs_signin, row.signin_ends, devbox_side)
            email = (row.account.email or "").strip().lower()
            limits = [
                dict(item, week_reset=_parse_time(item["week_reset"]) if isinstance(item.get("week_reset"), str) else item.get("week_reset"))
                for item in self._model_limits(uuid)
            ]
            accounts.append(
                {
                    "uuid": uuid,
                    "number": row.account.key,
                    "name": row.account.name,
                    "short": row.account.short,
                    "email": row.account.email,
                    "org_uuid": row.account.org_uuid,
                    "organization": row.account.organization,
                    "plan": plan_text(row.account.plan),
                    "signed_in_at": row.account.signed_in_at,
                    "signin_ends": row.signin_ends,
                    "five": row.five,
                    "week": row.week,
                    "five_reset": row.five_reset,
                    "week_reset": row.week_reset,
                    "forecast": _forecast_fields(fc, now),
                    "other_forecast": _forecast_fields(row.other_forecast, now),
                    "needs_signin": row.status.needs_signin,
                    "cannot_read": row.status.cannot_read,
                    "paused_until": row.status.paused_until,
                    "reading_error": row.status.last_error,
                    "reading_at": row.reading_at,
                    "in_use": where or ["idle"],
                    "devbox_needs_signin": None if theirs is None else theirs.needs_signin,
                    "devbox_cannot_read": None if theirs is None else theirs.cannot_read,
                    "devbox_signin_ends": ends.get(uuid) if signed else None,
                    "devbox_signed_in": signed,
                    "devbox_already": ALREADY_ON_DEVBOX.format(name=row.account.name) if signed else None,
                    "signin_text": signin,
                    "detail": {
                        "usage": viewmodel.usage_line(views[uuid], now, self.parts.tz) if uuid in views else "",
                        "models": viewmodel.models_line(limits, now, self.parts.tz),
                        "account": viewmodel.account_line(
                            row.account.name, row.account.full, row.account.short, row.account.organization, row.account.email,
                            bool(email) and emails.count(email) > 1, plan_text(row.account.plan), row.status.needs_signin,
                            row.signin_ends, now, self.parts.tz,
                        ),
                        "projects": viewmodel.projects_line(projects.get(uuid, [])),
                    },
                    "projects": projects.get(uuid, []),
                    "binding": row.binding,
                    "model_limits": self._model_limits(uuid),
                    "full": row.account.full,
                    "organization_kind": organization_kind(row.account.oauth_account),
                }
            )
        return _clean(
            addtext.decorate({
                "version": VERSION,
                "build": self.build,
                "at": now,
                "appearance": appearance,
                "mac": mac,
                "dev": dev,
                "accounts": accounts,
                "notifications": sorted(notifications, key=lambda n: n["at"]),
                "jobs": sorted([dict(job, card=self._card(job, snap)) for job in jobs], key=lambda j: j["started_at"]),
                "settings": settings,
                "settings_text": {"auto_switch": {"title": AUTO_SWITCH_TITLE, "description": AUTO_SWITCH_DESCRIPTION}},
                "devbox": devbox,
                "sessions": {"mac": {"open": total, "behind": behind}, "dev": {"open": dev_total, "behind": dev_behind}},
                "switch_note": switch_note,
                "statusline": statusline_state(self.parts.home),
                "projects_unknown": projects_unknown,
                "known_accounts": self._known_missing(),
                "signin_browser": self._signin_browser(),
                "countdown": None
                if countdown is None
                else {
                    "target_uuid": countdown["target"],
                    "ends_at": countdown["ends_at"],
                    "seconds": self.countdown_seconds,
                },
            }, now, self.parts.tz)
        )

    def _model_limits(self, uuid):
        try:
            found = self.reader.scoped(uuid)
        except Exception:
            found = []
        if not found:
            with self._state:
                found = [dict(item) for item in self._devbox_scoped.get(uuid, [])]
        return [
            {"model": item["model"], "week": item["percent"], "week_reset": item["resets_at"]}
            for item in found
            if isinstance(item.get("model"), str) and item["model"]
        ]

    def _alert_states(self, snap):
        states = []
        devbox_usable = self._usable_on_devbox()
        for row in snap.rows:
            uuid = row.account.uuid
            used_by = tuple(
                MACHINES[name] for name, holder in (("mac", snap.mac), ("devbox", snap.devbox)) if holder == uuid
            )
            states.append(
                alerting.AccountState(
                    uuid,
                    row.account.name,
                    row.five,
                    row.five_reset,
                    row.week,
                    row.week_reset,
                    row.forecast,
                    used_by,
                    row.signin_ends,
                    MACHINES[self.machine],
                    self.on_mac(uuid),
                    row.binding,
                    bool(getattr(row.status, "needs_signin", False)),
                    row.other_forecast,
                    uuid in devbox_usable,
                    cannot_read=bool(getattr(row.status, "cannot_read", False)),
                )
            )
        return states

    def _alert_settings(self):
        with self._state:
            settings = dict(self._settings["alerts"])
            settings.update(self._settings["channels"])
        return settings

    def _evaluate(self, states, settings, now, machine):
        engine = alerting.Alerts(_MachineEvents(self.parts.history, machine), settings, self.parts.tz, lambda: now)
        return [replace(alert, key=machine_key(alert.key, machine)) for alert in engine.evaluate(states)]

    def _devbox_signin_alerts(self, snap, settings, now):
        with self._state:
            ends = dict(self._devbox_signin_ends)
        states = [
            alerting.AccountState(
                row.account.uuid, row.account.name, None, None, None, None, None, (), ends[row.account.uuid], MACHINES["devbox"]
            )
            for row in snap.rows
            if row.account.uuid in ends
        ]
        if not states:
            return []
        only = dict(settings)
        only.update({kind: False for kind in alerting.KINDS if kind != "signin_ends"})
        return self._evaluate(states, only, now, "devbox")

    def _alert_step(self, now):
        if not self._alerts_allowed():
            return
        snap = self._snapshot(now)
        settings = self._alert_settings()
        found = self._evaluate(self._alert_states(snap), settings, now, self.machine)
        found += self._devbox_signin_alerts(snap, settings, now)
        for alert in found:
            self.report(
                alert.title, kind=alert.kind, key=alert.key, title=alert.title, body=alert.body, uuid=alert.uuid,
                keep=not self.present(),
            )

    def _best_target(self, snap):
        candidates = [
            r
            for r in snap.rows
            if r.account.uuid != snap.mac
            and not r.status.needs_signin
            and not r.status.cannot_read
            and self.on_mac(r.account.uuid)
            and r.main is not None
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda r: (_used(r.main), _used(r.week)))
        current = next((r for r in snap.rows if r.account.uuid == snap.mac), None)
        if current is not None and current.main is not None and _used(best.main) >= _used(current.main):
            return None
        return best.account.uuid if _used(best.main) < AUTO_SWITCH_AT else None

    def _auto_step(self, now):
        with self._switching:
            self._auto_locked(now)

    def _seen_event(self, event):
        return any(_same_event(event, known) for known in self._handled)

    def _auto_locked(self, now):
        with self._state:
            if not self._settings["auto_switch"] or self._countdown is not None:
                return
        snap = self._snapshot(now)
        below = {r.account.uuid for r in snap.rows if r.main is not None and r.main < AUTO_SWITCH_AT}
        with self._state:
            self._handled = {event: at for event, at in self._handled.items() if event[0] not in below}
        row = next((r for r in snap.rows if r.account.uuid == snap.mac), None)
        if row is None or row.main is None or row.main < AUTO_SWITCH_AT:
            return
        event = (snap.mac, row.main_reset)
        target = self._best_target(snap)
        if target is None:
            return
        with self._state:
            if not self._settings["auto_switch"] or self._countdown is not None or self._seen_event(event):
                return
            token = uuidlib.uuid4().hex
            timer = self.parts.timer_factory(self.countdown_seconds, self._countdown_due, (token,))
            timer.daemon = True
            self._countdown = {
                "token": token,
                "event": event,
                "target": target,
                "from": snap.mac,
                "ends_at": now + timedelta(seconds=self.countdown_seconds),
                "timer": timer,
                "firing": False,
            }
            self._handled[event] = now
        timer.start()

    def _at_switch_point(self, uuid):
        recorded = self._recorded()
        if recorded is not None and recorded != uuid:
            return None
        snap = self._snapshot(self._now())
        row = next((r for r in snap.rows if r.account.uuid == snap.mac), None)
        if snap.mac == uuid and row is not None and row.main is not None and row.main >= AUTO_SWITCH_AT:
            return row
        return None

    def _fire_target(self, chosen):
        snap = self._snapshot(self._now())
        row = next((r for r in snap.rows if r.account.uuid == chosen), None)
        if row is None or row.main is None or _used(row.main) < AUTO_SWITCH_AT:
            return chosen
        return self._best_target(snap)

    def _still_at_limit(self, uuid):
        return self._at_switch_point(uuid) is not None

    def _countdown_due(self, token):
        try:
            with self._switching:
                with self._state:
                    countdown = self._countdown
                    if countdown is None or countdown["token"] != token:
                        return
                    countdown["firing"] = True
                still = None
                try:
                    row = None if self._stop.is_set() else self._at_switch_point(countdown["from"])
                    if row is None:
                        return
                    still = STILL_ON.format(
                        name=self._names().get(countdown["from"], "the account it was on"),
                        used=max(0, min(100, math.floor(row.main))),
                        window=viewmodel.WINDOW_WORDS.get(row.binding, "5 hours"),
                    )
                    target = self._fire_target(countdown["target"])
                    if target is None:
                        self.report(AUTO_NO_ROOM)
                        return
                    why = AUTO_REASON.format(
                        name=self._names().get(countdown["from"], "the account it was on"),
                        at=AUTO_SWITCH_AT,
                        window=viewmodel.WINDOW_WORDS.get(row.binding, "5 hours"),
                    )
                    result = self._run_switch(target, "auto", "auto", why=why)
                except Refusal as refusal:
                    self.report(f"{refusal.sentence} {still}" if still else refusal.sentence)
                    return
                except switching.SwitchError as error:
                    self.report(str(error))
                    return
                finally:
                    with self._state:
                        if self._countdown is countdown:
                            self._countdown = None
            names = self._names()
            said = f"Lifeline switched this Mac to {names.get(result.to_uuid, 'another account')} because {why}."
            self.report(f"{said} {result.note}" if result.note else said, log_it=False)
        except Exception as error:
            log.warning("The automatic switch failed (%s).", type(error).__name__)

    def _names(self):
        try:
            return {a.uuid: a.name for a in self.parts.registry.load()}
        except Exception:
            return {}

    def cancel(self):
        with self._state:
            countdown = self._countdown
            if countdown is None:
                raise Refusal(409, NO_COUNTDOWN)
            if countdown["firing"]:
                raise Refusal(409, ALREADY_SWITCHING)
            self._countdown = None
        countdown["timer"].cancel()
        return {"cancelled": True}

    def _drop_countdown(self):
        with self._state:
            countdown = self._countdown
            if countdown is None or countdown["firing"]:
                return
            self._countdown = None
        countdown["timer"].cancel()

    def _run_switch(self, uuid, who="you", via="unknown", why=None):
        if self._stop.is_set():
            raise Refusal(503, STOPPING)
        if not self.on_mac(uuid):
            raise Refusal(409, NOT_ON_MAC.format(name=self._names().get(uuid, "that account")))
        self._switch_target = uuid
        try:
            self._before_switch(uuid)
            result = switching.switch(
                uuid,
                slot=self.parts.slot,
                store=self.parts.store,
                registry=self.parts.registry,
                home=self.parts.home,
                profile_fn=self.parts.profile_fn,
                lock_path=self.lock_path,
                backups=self.backups,
                lock_timeout=self.parts.lock_timeout,
                session_timeout=self.parts.session_timeout,
                clock=self._wall,
                settle=self.parts.settle,
                confirm=False,
            )
            outgoing = self._watcher
            if outgoing is not None and outgoing.active and outgoing not in self._retiring:
                self._retiring.append(outgoing)
            now = self._now()
            self._watcher = self._watcher_for(result.to_uuid, result)
            self._identity = None
            with self._state:
                self._just = (result.from_uuid, result.to_uuid, now)
                self._switch_note = result.note
                if result.from_uuid is not None:
                    self._left_use[result.from_uuid] = now
            try:
                write_last_switch(self.last_switch_path, result.from_uuid, result.to_uuid, now)
            except Exception:
                self.report(NO_UNDO_SAVE)
            self._follow(result, outgoing, self._watcher)
            log.info("%s", self._switch_line(who, result, via, why))
            return result
        finally:
            self._switch_target = None

    def _switch_line(self, who, result, via, why=None):
        names = self._names()
        place = MACHINES[self.machine]
        before = names.get(result.from_uuid) if result.from_uuid else None
        after = names.get(result.to_uuid, "an account Lifeline does not know")
        if before is None and who == "undo":
            said = f"You undid the last switch, so {place} went back to {after}"
        elif before is None:
            said = f"You switched {place} to {after}" if who != "auto" else f"Auto-switch moved {place} to {after}"
        elif who == "undo":
            said = f"You undid the last switch, so {place} went from {before} back to {after}"
        elif who == "auto":
            said = f"Auto-switch moved {place} from {before} to {after} because {why or before + ' reached its limit'}"
        else:
            said = f"You switched {place} from {before} to {after}"
        return f"{said} (via {via})."

    @staticmethod
    def via(body):
        if not isinstance(body, dict) or "via" not in body:
            return "unknown"
        if body["via"] not in VIA_CHOICES:
            raise Refusal(400, BAD_VIA)
        return body["via"]

    def _follow(self, result, outgoing, incoming):
        thread = threading.Thread(
            target=self._after_switch, args=(result, outgoing, incoming), name="lifeline-after-switch", daemon=True
        )
        with self._state:
            self._follow_ups = [each for each in self._follow_ups if each.is_alive()]
            self._follow_ups.append(thread)
        thread.start()

    def _follow_ups_done(self):
        with self._state:
            return not any(each.is_alive() for each in self._follow_ups)

    def finish_checks(self, seconds=FOLLOW_JOIN_SECONDS):
        with self._state:
            follow_ups = list(self._follow_ups)
        deadline = time.monotonic() + seconds
        for follow in follow_ups:
            if follow is not threading.current_thread():
                follow.join(max(0.0, deadline - time.monotonic()))
        return self._follow_ups_done()

    def _after_switch(self, result, outgoing, incoming):
        try:
            if outgoing is not None:
                with self._switching:
                    if outgoing in self._retiring:
                        for sentence in self._stand_down(outgoing):
                            self.report(sentence)
                        if not outgoing.active:
                            self._retiring.remove(outgoing)
            checked = switching.confirm_switch(
                result,
                slot=self.parts.slot,
                profile_fn=self.parts.profile_fn,
                registry=self.parts.registry,
                lock_path=self.lock_path,
                lock_timeout=self.parts.lock_timeout,
                clock=self._wall,
            )
            if checked.confirmed is False:
                with self._switching:
                    if self._watcher is incoming:
                        incoming.distrust(result.signin.open())
                self.report(DISAGREES)
            elif checked.confirmed is None and self._still_loaded(result):
                self.report(UNCHECKED)
        except Exception as error:
            log.warning("Lifeline could not finish checking a switch (%s).", type(error).__name__)

    def _still_loaded(self, result):
        try:
            item = self.parts.slot.read()
        except Exception:
            return False
        oauth = item.get(switching.OAUTH_KEY) if isinstance(item, dict) else None
        return isinstance(result.signin, switching.SealedSignin) and oauth == result.signin.open()

    @staticmethod
    def _stand_down(watcher):
        try:
            return watcher.poll()
        except Exception as error:
            log.warning("Lifeline could not check Claude Code's sign-in this round (%s).", type(error).__name__)
            return []

    def switch_to(self, uuid, who="you", via="unknown"):
        if self._stop.is_set():
            raise Refusal(503, STOPPING)
        self._drop_countdown()
        with self._switching:
            return self._run_switch(uuid, who, via)

    def undo(self, body=None):
        body = body or {}
        if not isinstance(body, dict) or set(body) - {"via"}:
            raise Refusal(400, BAD_UNDO)
        via = self.via(body)
        last = read_last_switch(self.last_switch_path)
        if last is None:
            if os.path.lexists(self.last_switch_path):
                raise Refusal(409, NOTE_DAMAGED)
            raise Refusal(409, NO_UNDO)
        return self.switch_to(last["from_uuid"], "undo", via)

    def switch_message(self, result):
        name = self._names().get(result.to_uuid, "the chosen account")
        parts = [f"This Mac now uses {name}."]
        if result.confirmed is False:
            parts.append(DISAGREES)
        if result.leftover:
            parts.append(SPARE_LEFT)
        return " ".join(parts)

    def resolve(self, body):
        machine = body.get("machine", "mac")
        if machine != "mac":
            raise Refusal(400, DEVBOX_SWITCH_REFUSED)
        if set(body) - {"uuid", "number", "machine", "via"}:
            raise Refusal(400, BAD_TARGET)
        try:
            accounts = self.parts.registry.load()
        except Exception:
            raise Refusal(500, NO_ACCOUNTS) from None
        if "uuid" in body:
            uuid = body["uuid"]
            if not isinstance(uuid, str) or not uuid:
                raise Refusal(400, BAD_TARGET)
            match = next((a for a in accounts if a.uuid == uuid), None)
        elif "number" in body:
            number = body["number"]
            if isinstance(number, bool) or not isinstance(number, int):
                raise Refusal(400, BAD_TARGET)
            match = next((a for a in accounts if a.key == number), None)
        else:
            raise Refusal(400, BAD_TARGET)
        if match is None:
            raise Refusal(404, UNKNOWN_ACCOUNT)
        return match.uuid

    def account(self, uuid):
        try:
            found = self.parts.registry.by_uuid(uuid)
        except Exception:
            raise Refusal(500, NO_ACCOUNTS) from None
        if found is None:
            raise Refusal(404, UNKNOWN_ACCOUNT)
        return found

    def set_presence(self, body):
        idle = body.get("idle_seconds")
        locked, asleep = body.get("locked"), body.get("asleep")
        number = _finite(idle)
        if number is None or number < 0 or not isinstance(locked, bool) or not isinstance(asleep, bool):
            raise Refusal(400, BAD_PRESENCE)
        if set(body) - {"idle_seconds", "locked", "asleep"}:
            raise Refusal(400, BAD_PRESENCE)
        with self._state:
            self._presence = {"idle_seconds": number, "locked": locked, "asleep": asleep, "at": self._now()}
            self._reported = True
        return {"present": self.present()}

    def present(self):
        with self._state:
            report = self._presence
        if report is None:
            return False
        return (
            not report["locked"]
            and not report["asleep"]
            and report["idle_seconds"] < PRESENT_IDLE_SECONDS
            and self._now() - report["at"] < PRESENCE_FRESH
        )

    def take_reading(self, body):
        return self._take_reading(body)[0]

    def reading_reply(self, body):
        uuid, saved, own = self._take_reading(body)
        try:
            found = self.parts.registry.by_uuid(own) if own is not None else None
        except Exception:
            found = None
        stated_login = isinstance(body, dict) and isinstance(body.get("account_uuid"), str) and bool(body["account_uuid"])
        reply = {
            "saved": saved,
            "account_uuid": found.login if found is not None else None,
            "org_uuid": found.org_uuid if found is not None else None,
            "reading_named": stated_login and own is not None and uuid == own,
        }
        figures = self._figures(own, self._now()) if found is not None else None
        if figures is not None:
            reply.update(zip(("five", "five_reset", "week", "week_reset"), figures[:4]))
        return reply

    def _take_reading(self, body):
        session, file_login, file_org, at, five, five_reset, week, week_reset = self._parse_reading(body)
        if five is None and week is None:
            try:
                named = resolve(self.parts.registry.load(), file_login, file_org)
            except Exception:
                named = None
            return None, False, named.uuid if named is not None else None
        now = self._now()
        if at is None or at > now:
            at = now
        try:
            accounts = self.parts.registry.load()
        except Exception:
            raise Refusal(500, NO_ACCOUNTS) from None
        registered = {a.uuid for a in accounts}
        named = resolve(accounts, file_login, file_org)
        own = named.uuid if named is not None else None
        kin = {a.uuid for a in accounts if a.uuid != own and file_login is not None and a.login == file_login}
        uuid, matched = self._attribute(five_reset, week_reset, own, registered, at, now, kin)
        if uuid is None:
            return None, False, own
        if matched is not None:
            if five_reset is not None and matched.five_reset is not None:
                five_reset = matched.five_reset
            if week_reset is not None and matched.week_reset is not None and _near(week_reset, matched.week_reset):
                week_reset = matched.week_reset
        saved = self.parts.history.add_reading(uuid, STATUSLINE_SOURCE, five, five_reset, week, week_reset, at, per_minute=True)
        if session is not None:
            with self._state:
                self._sessions[session] = (uuid, now)
        reached = forecasting.fullest(five, five_reset, week, week_reset, now)
        with self._state:
            wake_at = AUTO_SWITCH_AT if self._settings["auto_switch"] else forecasting.LIMIT
        if saved is not None and uuid == self.mac_uuid() and reached is not None and reached >= wake_at:
            self._wake.set()
        return uuid, saved is not None, own

    def _parse_reading(self, body):
        if body.get("source") != STATUSLINE_SOURCE or set(body) - READING_KEYS:
            raise Refusal(400, BAD_READING)
        session = body.get("session_id")
        file_uuid = body.get("account_uuid")
        file_org = body.get("org_uuid")
        if session is not None and (not isinstance(session, str) or not session):
            raise Refusal(400, BAD_READING)
        if file_uuid is not None and not isinstance(file_uuid, str):
            raise Refusal(400, BAD_READING)
        if file_org is not None and (not isinstance(file_org, str) or not file_org):
            raise Refusal(400, BAD_READING)
        at = None
        if body.get("at") is not None:
            at = _parse_time(body["at"])
            if at is None:
                raise Refusal(400, BAD_READING)
        five, five_reset = self._window(body.get("five_hour"))
        week, week_reset = self._window(body.get("seven_day"))
        return session, file_uuid, file_org, at, five, five_reset, week, week_reset

    @staticmethod
    def _window(value):
        if value is None:
            return None, None
        if not isinstance(value, dict) or set(value) - {"used_percentage", "resets_at"}:
            raise Refusal(400, BAD_READING)
        used = value.get("used_percentage")
        if used is not None:
            used = _finite(used)
            if used is None or used < 0:
                raise Refusal(400, BAD_READING)
        raw = value.get("resets_at")
        if raw is None:
            reset = None
        elif isinstance(raw, str):
            reset = _parse_time(raw)
            if reset is None:
                raise Refusal(400, BAD_READING)
        else:
            seconds = _finite(raw)
            if seconds is None:
                raise Refusal(400, BAD_READING)
            try:
                reset = datetime.fromtimestamp(seconds, timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise Refusal(400, BAD_READING) from None
        return used, reset

    def _latest_usage(self, uuid, now):
        floor = now - MATCH_LOOKBACK
        cached = self._usage_rows.get(uuid)
        if cached is not None and cached.at < floor:
            cached = None
        since = floor if cached is None else cached.at
        newest = cached
        for reading in self.parts.history.readings(uuid, since, fields=()):
            if reading.source in USAGE_SOURCES and (newest is None or reading.at >= newest.at):
                newest = reading
        self._usage_rows[uuid] = newest
        return newest

    def _attribute(self, five_reset, week_reset, own, registered, at, now, kin=frozenset()):
        if five_reset is not None:
            field, mine = "five_reset", five_reset
        elif week_reset is not None:
            field, mine = "week_reset", week_reset
        else:
            return None, None
        rows = {uuid: self._latest_usage(uuid, now) for uuid in sorted(registered)}
        resets = {uuid: getattr(row, field) if row is not None else None for uuid, row in rows.items()}
        named = own in registered
        if named and resets[own] is not None and _near(resets[own], mine):
            return own, rows[own]
        others = [
            uuid for uuid, reset in resets.items() if uuid != own and reset is not None and reset > at and _near(reset, mine)
        ]
        if named and resets[own] is not None and resets[own] > at:
            return (others[0], rows[others[0]]) if len(others) == 1 else (None, None)
        if len(others) == 1 and others[0] not in kin:
            return others[0], rows[others[0]]
        if others or not named:
            return None, None
        return own, None

    def history_for(self, uuid, query):
        self.account(uuid)
        days = HISTORY_DAYS
        if "days" in query:
            raw = query["days"][-1]
            if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_HISTORY_DAYS:
                raise Refusal(400, BAD_DAYS)
            days = int(raw)
        now = self._now()
        readings = self.parts.history.readings(uuid, now - timedelta(days=days))
        kind = self._kind(uuid, now)
        daily = {}
        hits = {}
        ran_out = {}
        segments = {}
        for reading in readings:
            day = reading.at.astimezone(self.parts.tz).date().isoformat()
            peak = daily.setdefault(day, 0.0)
            week_key = _segment(segments, reading, reading.week, reading.week_reset)
            value = reading.week if kind == "week" else reading.five
            if value is not None:
                daily[day] = max(peak, value)
                if value >= forecasting.LIMIT:
                    hits.setdefault(day if kind != "week" or week_key is None else week_key, day)
            if reading.week is not None and reading.week >= forecasting.LIMIT:
                ran_out.setdefault(day if week_key is None else week_key, day)
        first = set(hits.values())
        summary = self._seven_days(sorted(daily), sorted(first), sorted(set(ran_out.values())), kind, now)
        return _clean(
            {
                "uuid": uuid,
                "days": days,
                "peaks_of": kind,
                "summary": summary,
                "week_ran_out": sorted(set(ran_out.values())),
                "readings": [
                    {
                        "source": r.source,
                        "five": r.five,
                        "five_reset": r.five_reset,
                        "week": r.week,
                        "week_reset": r.week_reset,
                        "at": r.at,
                    }
                    for r in readings
                ],
                "daily": [
                    {"date": day, "peak_five": peak, "hit_limit": day in first}
                    for day, peak in sorted(daily.items())
                ],
            }
        )

    def _seven_days(self, read_days, limit_days, week_days, kind, now):
        today = now.astimezone(self.parts.tz).date()

        def named(days):
            return _and([clock.past_day(date.fromisoformat(day), today) for day in days])

        def when(days):
            return f"on {len(days)} of the last 7 days" if len(days) >= 4 else named(days)

        if len(read_days) < 2:
            return STARTED_READING.format(day=named(read_days[:1])) if read_days else NO_DAYS
        five_days, week_days = ([], limit_days) if kind == "week" else (limit_days, week_days)
        parts = [f"of its 5 hours {when(five_days)}" if five_days else "", f"of its week {when(week_days)}" if week_days else ""]
        found = [part for part in parts if part]
        return f"It ran out {', and '.join(found)}." if found else "It did not run out in the last 7 days."

    def rename(self, uuid, body):
        if not isinstance(body, dict) or set(body) != set(RENAME_KEYS):
            raise Refusal(400, BAD_NAMES)
        changes = {}
        for key in RENAME_KEYS:
            value = body[key]
            if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise Refusal(400, BAD_NAMES)
            changes[key] = value.strip()
        self.account(uuid)
        try:
            with writelock.held(self.lock_path, timeout=self.parts.lock_timeout):
                current = self.parts.registry.by_uuid(uuid)
                if current is None:
                    raise Refusal(404, UNKNOWN_ACCOUNT)
                updated = replace(current, **changes)
                self.parts.registry.upsert(updated)
        except writelock.WriteLockTimeout:
            raise Refusal(409, switching.LIFELINE_BUSY) from None
        except Refusal:
            raise
        except Exception:
            raise Refusal(500, "Lifeline could not save the new names.") from None
        return {"uuid": updated.uuid, "name": updated.name, "short": updated.short}

    def update_settings(self, body):
        if "placement" in body:
            _placement_check(body["placement"])
            try:
                known = {account.uuid for account in self.parts.registry.load()}
            except Exception:
                raise Refusal(500, NO_ACCOUNTS) from None
            if any(uuid not in known for uuid in body["placement"]):
                raise Refusal(404, UNKNOWN_ACCOUNT)
        with self._state:
            merged = _merge_settings(json.loads(json.dumps(self._settings)), body)
            try:
                _write_private(self.settings_path, merged)
            except Exception:
                raise Refusal(500, NO_SETTINGS_SAVE) from None
            self._settings = merged
        if not merged["auto_switch"]:
            self._drop_countdown()
        return {"settings": self._shown_settings(merged)}

    def remove(self, uuid):
        name = self.account(uuid).name or "the account"

        def work():
            recovery.forget(recovery.folder_for(self.lock_path), uuid)
            self.parts.store.remove(uuid)
            self.parts.history.forget(uuid)
            self.parts.registry.save([account for account in self.parts.registry.load() if account.uuid != uuid])

        told = self._forget({uuid}, work, f"/remove/{uuid}", REMOVE_UNFINISHED.format(name=name))
        return {"message": REMOVED[told].format(name=name)}

    def reset(self):
        saved = self.folder.with_name(f"{self.folder.name}-before-reset-{self._now().astimezone(self.parts.tz):%Y-%m-%d-%H%M%S}")
        home = Path(self.parts.home)
        shown = f"~/{saved.relative_to(home)}" if saved.is_relative_to(home) else str(saved)

        def work():
            listed, accounts = self._ask(self.parts.store.names), self._ask(self.parts.registry.load)
            if listed is None and accounts is None:
                raise Refusal(500, NO_RESET_SIGNINS)
            try:
                saved.mkdir(mode=0o700)
            except OSError:
                raise Refusal(500, NO_RESET_COPY) from None
            try:
                for name in RESET_FILES:
                    if (self.folder / name).is_file():
                        shutil.copy2(self.folder / name, saved / name)
                self.parts.history.copy_to(saved / "history.db")
            except Exception:
                shutil.rmtree(saved, ignore_errors=True)
                raise Refusal(500, NO_RESET_COPY) from None
            # The pairing keys share the store's keychain service, and a reset keeps the pairing.
            for uuid in sorted((set(listed or ()) - {COMPUTER_ACCOUNT, PAIRING_ACCOUNT}) | {account.uuid for account in accounts or ()}):
                self.parts.store.remove(uuid)
            for name in RESET_FILES:
                (self.folder / name).unlink(missing_ok=True)
            self.parts.history.forget()
            recovery.forget(recovery.folder_for(self.lock_path))

        told = self._forget(None, work, RESET_ROUTE, RESET_UNFINISHED.format(path=shown))
        return {"message": f"{RESET_DONE[told]} {RESET_SAVED.format(path=shown)}"}

    def _forget(self, uuids, work, route, unfinished):
        """Remove and Reset: claims the right to run in the same locked step that checks for a sign-in, refuses during a
        switch, saves the other computer's change before deleting anything, never touches Claude Code's own sign-in, then
        tells the other computer. A failure puts the other computer's list back as it was."""
        with self._state:
            if self._forgetting or any(job["state"] == "running" for job in self._jobs.values()):
                raise Refusal(409, switching.LIFELINE_BUSY if self._forgetting else SIGNIN_BUSY)
            self._forgetting = True
        try:
            if self._switch_target is not None or not self._switching.acquire(timeout=FORGET_WAIT):
                raise Refusal(409, SWITCH_BUSY)
            try:
                linked = self._linked()
                before = self._queue(route) if linked else None

                def locked():
                    with writelock.held(self.lock_path, timeout=self.parts.lock_timeout):
                        work()

                try:
                    self.reader.forget(uuids, locked)
                    self._drop_memory(uuids)
                except Exception as error:
                    if linked:
                        with self._state:
                            self._set_todo(before if route == RESET_ROUTE else [each for each in self._devbox_todo if each != route])
                    if isinstance(error, writelock.WriteLockTimeout):
                        raise Refusal(409, switching.LIFELINE_BUSY) from None
                    if isinstance(error, Refusal):
                        raise
                    log.warning("Lifeline could not finish a remove or reset (%s).", type(error).__name__)
                    raise Refusal(500, unfinished) from None
            finally:
                self._switching.release()
        finally:
            with self._state:
                self._forgetting, self._holding = False, None
        return self._send_now(route) if linked else None

    def _drop_memory(self, uuids):
        def gone(key):
            return uuids is None or any(uuid in str(key) for uuid in uuids)

        self._watcher = None if self._watcher is None or gone(self._watcher.chosen_uuid) else self._watcher
        self._retiring = [watcher for watcher in self._retiring if not gone(watcher.chosen_uuid)]
        with self._state:
            self._notifications = [n for n in self._notifications if not gone(n["id"]) and not gone(n.get("uuid"))]
            for table in (self._learned, self._forecasts, self._left_use, self._devbox_seen, self._devbox_saved,
                          self._readings_pushed, self._handled, self._devbox_reports):
                for key in [key for key in table if gone(key)]:
                    del table[key]
            placed = [self._settings["placement"].pop(uuid, None) for uuid in uuids or ()]
            if uuids is None:
                self._settings, self._just, self._switch_note = default_settings(), None, None
            elif any(placed):
                try:
                    _write_private(self.settings_path, self._settings)
                except Exception:
                    log.warning("%s", NO_SETTINGS_SAVE)
            self._identity = None
            self._pushed = None

    def _linked(self):
        return callable(getattr(self.parts.link, "request", None))

    def _load_todo(self):
        try:
            todo = _read_private(self.folder / FORGET_NAME)
        except _Unreadable:
            todo = None
        return [route for route in todo if isinstance(route, str) and FORGET_ROUTE.fullmatch(route)] if isinstance(todo, list) else []

    def _set_todo(self, todo, strict=False):
        with self._state:
            self._devbox_todo = list(todo)
            try:
                _write_private(self.folder / FORGET_NAME, self._devbox_todo)
            except Exception:
                if strict:
                    raise
                log.warning("Lifeline could not save the changes it still owes the other computer.")

    def _queue(self, route):
        """Saves the other computer's change on disk before the local delete, so a crash between the two never loses it.
        Until the delete is done, the exchange holds it back."""
        with self._state:
            before = list(self._devbox_todo)
            try:
                self._set_todo([route] if route == RESET_ROUTE else [*before, route], strict=True)
            except Exception:
                self._set_todo(before)
                raise Refusal(500, NO_DEVBOX_NOTE) from None
            self._holding = route
        return before

    def _send_now(self, route):
        with self._state:
            reachable = self._devbox["reachable"]
        if reachable and self._link_lock.acquire(timeout=FORGET_WAIT):
            try:
                self._send_devbox(self.parts.link)
            finally:
                self._link_lock.release()
        with self._state:
            return route not in self._devbox_todo

    def _send_devbox(self, link):
        """Sends each remove or reset the other computer has not taken yet, in order, before the registry goes out."""
        ask = getattr(link, "request", None)
        while callable(ask):
            with self._state:
                if not self._devbox_todo or self._devbox_todo[0] == self._holding:
                    return
                route = self._devbox_todo[0]
            try:
                status, data = ask("POST", route, {})
            except Exception:
                return
            if not (200 <= status < 300 or (isinstance(data, dict) and data.get("error") == UNKNOWN_ACCOUNT)):
                return
            with self._state:
                if self._devbox_todo[:1] == [route]:
                    self._set_todo(self._devbox_todo[1:])
                self._pushed = None

    def _place(self, uuid):
        with self._state:
            place = self._settings.get("placement", {}).get(uuid)
        return place if place in PLACEMENTS else DEFAULT_PLACEMENT

    def on_mac(self, uuid):
        return self._place(uuid) != "devbox"

    def _shown_settings(self, settings):
        shown = json.loads(json.dumps(settings))
        try:
            uuids = [account.uuid for account in sorted(self.parts.registry.load(), key=lambda a: a.key)]
        except Exception:
            uuids = []
        stored = settings.get("placement", {})
        shown["placement"] = {uuid: stored.get(uuid, DEFAULT_PLACEMENT) for uuid in uuids}
        return shown

    def start_job(self, kind, uuid=None, body=None):
        options = self._job_options(kind, body or {})
        step = self._step(body or {}, {"resign": BAD_RESIGN, "adopt": BAD_ADOPT}.get(kind, BAD_ADD))
        if kind == "adopt" and not self._mac_adopt_ready():
            raise Refusal(409, NO_LIVE_TO_ADOPT)
        if kind == "adopt" and "expected" in (body or {}):
            options["expected"] = self._proxy_account(body["expected"], None, BAD_ADOPT).uuid
            if self.mac_uuid() != options["expected"]:
                raise Refusal(409, self._mac_not_expected(options["expected"]))
        choose = None
        if kind == "resign":
            choose = self.account(uuid).organization
        entry = None
        if "known" in options:
            wanted = options.pop("known")
            entry = next((item for item in self._known_accounts() if item["name"] == wanted), None)
            if entry is None:
                raise Refusal(404, UNKNOWN_KNOWN.format(name=wanted))
            options.setdefault("email", entry["email"])
            choose = entry["organization"] if entry["organization"] != "personal" else entry["email"] + PERSONAL_ORGANIZATION
        job_id = uuidlib.uuid4().hex
        with self._state:
            if self._stop.is_set():
                raise Refusal(503, STOPPING_SIGNIN)
            if self._forgetting:
                raise Refusal(409, switching.LIFELINE_BUSY)
            for job in self._jobs.values():
                if job["state"] != "running" or job["kind"] != kind:
                    continue
                if kind == "add":
                    raise Refusal(409, ADD_RUNNING)
                if kind == "adopt":
                    raise Refusal(409, ADOPT_RUNNING)
                if job["uuid"] == uuid:
                    raise Refusal(409, RESIGN_RUNNING)
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "uuid": uuid,
                "state": "running",
                "result": "",
                "started_at": self._now(),
                "finished_at": None,
                "url": None,
                "opened": None,
                "browser": None,
                "suggested_name": None,
                "suggested_short": None,
                "known": entry["name"] if entry is not None else None,
                "organization": entry["organization"] if entry is not None else None,
                "guidance": addtext.choose(choose) if choose else None,
                "step": step,
            }
            thread = threading.Thread(target=self._run_job, args=(job_id, kind, uuid, options), name="lifeline-job", daemon=True)
            if kind in SIGNIN_KINDS:
                self._controls[job_id] = _control(thread)
        thread.start()
        return {"job": job_id}

    def job_action(self, rest):
        word = (rest or "").partition("/")[2]
        if word == OPEN_WORD:
            return self.open_job(rest)
        return self.cancel_job(rest)

    def open_job(self, rest):
        from lifeline import devbox

        job_id, _, word = (rest or "").partition("/")
        if not job_id or word != OPEN_WORD:
            raise Refusal(404, NOT_FOUND)
        with self._state:
            job = self._jobs.get(job_id)
            if job is None:
                raise Refusal(404, NO_SUCH_JOB)
            if job["state"] != "running":
                raise Refusal(409, PAGE_ENDED)
            if job["kind"] not in PAGE_KINDS:
                raise Refusal(409, NO_PAGE_TO_OPEN)
            url = job.get("url")
        if not url:
            raise Refusal(409, PAGE_NOT_READY)
        try:
            devbox.signin_port(url)
        except ValueError:
            raise Refusal(409, PAGE_UNTRUSTED) from None
        opener = self.parts.open_browser or devbox.open_default
        try:
            browser, opened = opener(url)
        except Exception:
            browser, opened = None, False
        with self._state:
            job = self._jobs.get(job_id)
            if job is not None and job["state"] == "running":
                job.update(browser=browser, opened=bool(opened))
        return {"browser": browser, "opened": bool(opened)}

    def cancel_job(self, rest):
        job_id, _, word = (rest or "").partition("/")
        if not job_id or word != CANCEL_WORD:
            raise Refusal(404, NOT_FOUND)
        with self._state:
            job = self._jobs.get(job_id)
            control = self._controls.get(job_id)
            if job is None:
                raise Refusal(404, NO_SUCH_JOB)
            if job["state"] != "running":
                raise Refusal(409, JOB_ENDED)
            if control is None:
                raise Refusal(409, JOB_NOT_CANCELLABLE)
            if control["phase"] != "login":
                raise Refusal(409, JOB_SAVING)
            control["cancelled"] = True
            control["stop"].set()
            thread = control["thread"]
        if thread is not threading.current_thread() and thread.ident is not None:
            thread.join(CANCEL_WAIT_SECONDS)
        return {"cancelled": True}

    def _cancelled(self, job_id):
        with self._state:
            control = self._controls.get(job_id)
            return control is not None and control["cancelled"]

    def _cancel_sentence(self, job_id):
        with self._state:
            control = self._controls.get(job_id)
            if control is None:
                return None
            if control["cancelled"]:
                return SIGNIN_CANCELLED
            return STOPPED_SIGNIN if control["stopped"] else None

    def _stop_signins(self):
        threads = []
        with self._state:
            for job_id, control in self._controls.items():
                job = self._jobs.get(job_id)
                if job_id in self._proxies or job is None or job["state"] != "running":
                    continue
                if control["phase"] == "login":
                    control["stopped"] = True
                    control["stop"].set()
                threads.append(control["thread"])
        return threads

    @staticmethod
    def _step(body, sentence):
        if "step" not in body:
            return None
        value = body["step"]
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise Refusal(400, sentence)
        index, total = value
        if any(isinstance(n, bool) or not isinstance(n, int) for n in (index, total)) or not 1 <= index <= total <= 9:
            raise Refusal(400, sentence)
        return [index, total]

    @staticmethod
    def _job_options(kind, body):
        if kind == "resign":
            allowed, sentence = RESIGN_KEYS, BAD_RESIGN
        elif kind == "adopt":
            allowed, sentence = ADOPT_KEYS, BAD_ADOPT
        else:
            allowed, sentence = ADD_KEYS, BAD_ADD
        if set(body) - allowed:
            raise Refusal(400, sentence)
        options = {}
        for key in ("email", "name", "short", "known"):
            if key in body:
                value = body[key]
                if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
                    raise Refusal(400, sentence)
                if key == "email" and not _email_shape(value.strip()):
                    raise Refusal(400, sentence)
                options[key] = value.strip()
        if "number" in body:
            number = body["number"]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise Refusal(400, sentence)
            options["key"] = number
        return options

    def _mac_not_expected(self, expected):
        def name_of(uuid):
            try:
                found = self.parts.registry.by_uuid(uuid) if uuid is not None else None
            except Exception:
                found = None
            return found.name if found is not None else None

        live = self.mac_uuid()
        if live == expected:
            return MAC_LIVE_GONE.format(expected=name_of(expected) or NOT_ADDED_ACCOUNT)
        shown = name_of(live)
        if shown is None:
            try:
                block = self._mac_live(self._snapshot(self._now())) or {}
            except Exception:
                block = {}
            shown = block.get("name") or block.get("email")
        return MAC_NOT_EXPECTED.format(live=shown or NOT_ADDED_ACCOUNT, expected=name_of(expected) or NOT_ADDED_ACCOUNT)

    def _adopt(self, options):
        try:
            before = {account.uuid for account in self.parts.registry.load()}
        except Exception:
            before = set()
        adopted = enrolling.adopt_live(
            slot=self.parts.slot,
            registry=self.parts.registry,
            profile_fn=self.parts.profile_fn,
            home=self.parts.home,
            record_path=self.record_path,
            lock_path=self.lock_path,
            lock_timeout=self.parts.lock_timeout,
            names_for=self._known_names,
            **options,
        )
        if adopted.uuid in before:
            return f"Lifeline updated {adopted.name} with the account details Claude Code holds now.", adopted.uuid
        return f"Lifeline added {adopted.name} as number {adopted.key}. Claude Code uses it now.", adopted.uuid

    def _run_job(self, job_id, kind, uuid, options):
        state, result, found = "failed", JOB_FAILED[kind], uuid
        before = set(self._names())
        notes, already = [], False
        try:
            result, found = self._adopt(options) if kind == "adopt" else self._enrol_job(kind, uuid, options, job_id)
            state = "done"
        except enrolling.EnrollError as error:
            notes = [n for n in getattr(error, "__notes__", []) if isinstance(n, str)]
            said = login_result(error.reason, kind) if str(error) == enrolling.LOGIN_FAILED else str(error)
            if kind == "adopt" and options.get("expected") and str(error) == enrolling.LIVE_NOT_ASKED:
                said = self._mac_not_expected(options["expected"])
            result = " ".join([said, *notes])
            if isinstance(error, enrolling.AlreadyHas):
                result, already = " ".join([self._signed_in_note(job_id, error), *notes]), True
        except Exception as error:
            log.warning("A background job failed (%s).", type(error).__name__)
        sentence = self._cancel_sentence(job_id) if state != "done" else None
        if sentence is not None:
            state, result = "cancelled", " ".join([sentence, *notes] if notes else [sentence, NOTHING_CHANGED])
        added = self._added(found) if state == "done" and kind in ("add", "adopt") and found not in before else None
        name, short = self._suggested(added) if added is not None else ("", "")
        with self._state:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(state=state, result=result, uuid=found, finished_at=self._now())
                job.update(suggested_name=name or None, suggested_short=short or None)
                if already and state == "failed":
                    job["already_has"] = True
            self._controls.pop(job_id, None)
        self._log_job(kind, state, result, found, added, stopped=sentence == STOPPED_SIGNIN)

    def _known_names(self, details):
        email = details.get("emailAddress") if isinstance(details, dict) else None
        entry = known_entry(self._known_accounts(), email, organization_kind(details))
        return (entry["name"], entry["short"], entry["full"]) if entry is not None else None

    def _suggested(self, added):
        entry = known_of(added, self._known_accounts())
        if entry is None:
            return suggested_names(added.oauth_account or {})
        if added.full != entry["full"]:
            try:
                with writelock.held(self.lock_path, timeout=self.parts.lock_timeout):
                    current = self.parts.registry.by_uuid(added.uuid)
                    if current is not None:
                        self.parts.registry.upsert(replace(current, full=entry["full"]))
            except Exception as error:
                log.warning("Lifeline could not save the full name of %s (%s).", added.name, type(error).__name__)
        return entry["name"], entry["short"]

    def _signed_in_note(self, job_id, error):
        with self._state:
            job = self._jobs.get(job_id)
            browser = job.get("browser") if job is not None else None
        browser = browser or self._signin_browser()
        said = enrolling.already_has(error, browser or enrolling.YOUR_BROWSER)
        return f"{said} {ADD_DIFFERENT.format(browser=browser or DEFAULT_BROWSER_WORDS)}"

    def _added(self, uuid):
        try:
            return self.parts.registry.by_uuid(uuid) if uuid is not None else None
        except Exception:
            return None

    def _log_job(self, kind, state, result, uuid, added=None, stopped=False):
        name = self._names().get(uuid, "an account") if uuid is not None else "an account"
        if state == "cancelled":
            log.info("%s", _cancel_log(kind, name, result))
            return
        if state == "failed":
            log.warning("%s", _failed_line(JOB_LOG[kind].format(name=name), result))
            return
        if added is not None:
            log.info("You added %s to Lifeline as number %s.", added.name, added.key)
        elif kind == "resign":
            log.info("You signed in to %s again.", name)
        else:
            log.info("%s", result)

    def _job_page(self, job_id, url, browser, opened):
        with self._state:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(url=url, browser=browser, opened=opened)

    def _enrol_job(self, kind, uuid, options, job_id=None):
        email = options.pop("email", None)

        def on_page(url, browser, opened):
            self._job_page(job_id, url, browser, opened)

        with self._state:
            control = self._controls.get(job_id)

        def login(folder):
            if control is None:
                return self.parts.run_login(folder, email, user=self.parts.user, on_page=on_page)
            if control["stop"].is_set():
                raise LoginFailed(SIGNIN_CANCELLED)
            try:
                self.parts.run_login(folder, email, user=self.parts.user, on_page=on_page, stop=control["stop"])
            finally:
                with self._state:
                    control["phase"] = "saving"
            if control["stop"].is_set():
                raise LoginFailed(SIGNIN_CANCELLED)

        common = dict(
            run_login=login,
            runner=self.parts.runner,
            store=self.parts.store,
            registry=self.parts.registry,
            profile_fn=self.parts.profile_fn,
            user=self.parts.user,
            temp_root=self.parts.temp_root,
            live_slot=self.parts.slot,
            home=self.parts.home,
            lock_path=self.lock_path,
            lock_timeout=self.parts.lock_timeout,
        )
        if kind == "add":
            added = enrolling.enroll(**common, **options, names_for=self._known_names)
            return f"Lifeline added {added.name}.", added.uuid
        updated = enrolling.resign(uuid, **common)
        return f"{updated.name} has a new sign-in.", uuid

    def start_devbox_job(self, rest, body):
        from lifeline import devbox

        if self._stop.is_set():
            raise Refusal(503, STOPPING_SIGNIN)
        words = (rest or "").split("/")
        kind = words[0]
        if words == ["unsure"]:
            return self.keep_unsure(body)
        if kind not in DEVBOX_JOB_KINDS or len(words) != (2 if kind == "resign" else 1):
            raise Refusal(404, NOT_FOUND)
        body = body or {}
        account = None
        if kind == "add":
            if set(body) - DEVBOX_ADD_KEYS or len({"uuid", "number"} & set(body)) != 1:
                raise Refusal(400, BAD_DEVBOX_ADD)
            email = self._proxy_email(body, BAD_DEVBOX_ADD)
            account = self._proxy_account(body.get("uuid"), body.get("number"), BAD_DEVBOX_ADD)
            route, sent = "/add", {"uuid": account.uuid}
        elif kind == "resign":
            if set(body) - RESIGN_KEYS:
                raise Refusal(400, BAD_RESIGN)
            email = self._proxy_email(body, BAD_RESIGN)
            account = self._proxy_account(words[1], None, BAD_RESIGN)
            route, sent = f"/resign/{account.uuid}", {}
        else:
            if set(body) - {"step", "expected"}:
                raise Refusal(400, devbox.BAD_DEVBOX_ADOPT)
            email, route, sent = None, "/adopt", {}
            if "expected" in body:
                expected = self._proxy_account(body["expected"], None, devbox.BAD_DEVBOX_ADOPT)
                sent["expected"] = expected.uuid
        step = self._step(body, {"add": BAD_DEVBOX_ADD, "resign": BAD_RESIGN}.get(kind, devbox.BAD_DEVBOX_ADOPT))
        if account is not None:
            email = email or (account.email if _email_shape(account.email or "") else None)
            if email:
                sent["email"] = email
        link = self.parts.link
        if not callable(getattr(link, "start_signin", None)):
            raise Refusal(409, devbox.NO_DEVBOX_LINK)
        job_kind = DEVBOX_JOB_KINDS[kind]
        job_id = uuidlib.uuid4().hex
        thread = threading.Thread(
            target=self._devbox_proxy, args=(job_id, job_kind, route, sent, link), name="lifeline-devbox-job", daemon=True
        )
        with self._state:
            if self._stop.is_set():
                raise Refusal(503, STOPPING_SIGNIN)
            if self._forgetting:
                raise Refusal(409, switching.LIFELINE_BUSY)
            if any(job["state"] == "running" and job["kind"] in DEVBOX_JOB_KINDS.values() for job in self._jobs.values()):
                raise Refusal(409, DEVBOX_PROXY_BUSY)
            self._jobs[job_id] = {
                "id": job_id,
                "kind": job_kind,
                "uuid": account.uuid if account is not None else sent.get("expected"),
                "state": "running",
                "result": "",
                "started_at": self._now(),
                "finished_at": None,
                "url": None,
                "opened": None,
                "guidance": addtext.choose(account.organization)
                if account is not None and account.organization
                else None,
                "step": step,
            }
            self._proxies[job_id] = thread
            self._controls[job_id] = _control(thread)
        thread.start()
        return {"job": job_id}

    @staticmethod
    def _proxy_email(body, sentence):
        if "email" not in body:
            return None
        value = body["email"]
        if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
            raise Refusal(400, sentence)
        if not _email_shape(value.strip()):
            raise Refusal(400, sentence)
        return value.strip()

    def _proxy_account(self, uuid, number, sentence):
        if uuid is not None and (not isinstance(uuid, str) or not uuid):
            raise Refusal(400, sentence)
        if number is not None and (isinstance(number, bool) or not isinstance(number, int)):
            raise Refusal(400, sentence)
        try:
            accounts = self.parts.registry.load()
        except Exception:
            raise Refusal(500, NO_ACCOUNTS) from None
        found = next((a for a in accounts if (a.uuid == uuid if uuid is not None else a.key == number)), None)
        if found is None:
            raise Refusal(404, UNKNOWN_ACCOUNT)
        return found

    def _proxy_pause(self, seconds, stop=None):
        if stop is None:
            if self._stop.wait(seconds):
                raise _ProxyStopped()
            return
        deadline = time.monotonic() + seconds
        while True:
            if self._stop.is_set() or stop.is_set():
                raise _ProxyStopped()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            stop.wait(min(left, PROXY_SLICE_SECONDS))

    def _remote_end(self, link, remote_job):
        if remote_job is None:
            return None
        deadline = time.monotonic() + CANCEL_WAIT_SECONDS * 4
        while time.monotonic() < deadline:
            try:
                status, data = link.job(remote_job)
            except Exception:
                return None
            if status != 200 or not isinstance(data, dict):
                return None
            if data.get("state") != "running":
                return data
            if self._stop.wait(PROXY_SLICE_SECONDS):
                return None
        return None

    def _proxy_started(self, link, remote, found):
        from lifeline import devbox

        remote.update(job=found)
        if self._stop.is_set():
            devbox.stop_remote(link, found)
            raise _ProxyStopped()

    def _keep_orphan(self, token):
        with self._state:
            moment = self.parts.monotonic()
            self._prune_orphans(moment)
            self._orphans.add(token)
            self._orphan_since[token] = moment

    def keep_unsure(self, body):
        from lifeline import devbox

        token = body.get("request") if isinstance(body, dict) and set(body) == {"request"} else None
        if not isinstance(token, str) or not devbox.REQUEST_ID.fullmatch(token):
            raise Refusal(400, BAD_UNSURE)
        self._keep_orphan(token)
        return {"kept": True}

    def _proxy_page(self, job_id, url, opened):
        browser = self._signin_browser()
        with self._state:
            job = self._jobs.get(job_id)
            if job is not None:
                job["url"] = url
                job["opened"] = opened
                job["browser"] = browser

    def _devbox_proxy(self, job_id, kind, route, body, link):
        from lifeline import devbox

        state, result = "failed", JOB_FAILED[kind]
        remote = {}
        with self._state:
            control = self._controls.get(job_id)
        stop = control["stop"] if control is not None else None
        try:
            job = devbox.drive_signin(
                link,
                route,
                body,
                accounts=self.parts.registry.load(),
                relay=self.parts.relay_fn,
                open_page=self.parts.open_page,
                sleep=lambda seconds: self._proxy_pause(seconds, stop),
                clock=time.monotonic,
                wants_page=kind != "devbox-adopt",
                on_job=lambda found: self._proxy_started(link, remote, found),
                on_page=lambda url, opened: self._proxy_page(job_id, url, opened),
                on_unsure=lambda token: (self._keep_orphan(token), remote.update(unsure=token)),
            )
            said = devbox._said(job.get("result"))
            result = said or devbox.DEVBOX_REFUSED
            state = "done" if job.get("state") == "done" else "failed"
        except devbox.SigninRefused as refused:
            result = refused.sentence
        except _ProxyStopped:
            if "job" in remote:
                devbox.stop_remote(link, remote["job"])
                result = DEVBOX_PROXY_STOPPED
            else:
                result = STOP_UNSURE
            if self._cancelled(job_id) and "job" not in remote:
                state, result = "cancelled", CANCEL_UNSURE
            elif self._cancelled(job_id):
                ended = self._remote_end(link, remote["job"])
                if isinstance(ended, dict) and ended.get("state") == "done":
                    state, result = "done", devbox._said(ended.get("result")) or devbox.DEVBOX_REFUSED
                else:
                    state, result = "cancelled", JOB_CANCELLED
        except Exception as error:
            log.warning("A sign-in on the other computer from this Mac failed (%s).", type(error).__name__)
        with self._state:
            ran = dict(self._jobs.get(job_id) or {})
        if ran.get("step") == [2, 2] and ran.get("uuid"):
            result = second_step_result(kind, self._names().get(ran["uuid"], "The account"), state, result) or result
        with self._state:
            found = self._jobs.get(job_id)
            if found is not None:
                found.update(state=state, result=result, finished_at=self._now())
            self._proxies.pop(job_id, None)
            self._controls.pop(job_id, None)
        if state == "cancelled":
            whose = found.get("uuid") if found is not None else None
            log.info("%s", _cancel_log(kind, self._names().get(whose, "an account") if whose else "an account", result))
        elif state == "failed":
            log.warning("%s", _failed_line(JOB_LOG[kind].format(name="an account"), result))
        else:
            log.info("%s", result)

    def _tidy_step(self, now):
        with self._state:
            self._notifications = [n for n in self._notifications if now - n["at"] < KEEP_FOR]
            self._jobs = {
                k: j for k, j in self._jobs.items() if j["finished_at"] is None or now - j["finished_at"] < KEEP_FOR
            }
            self._handled = {k: at for k, at in self._handled.items() if now - at < KEYS_KEEP_FOR}
            self._sessions = {k: v for k, v in self._sessions.items() if now - v[1] < SESSION_FORGET}
            self._devbox_reports = {k: at for k, at in self._devbox_reports.items() if now - at < REPORTS_KEEP_FOR}

    def _prune_step(self, now):
        if self._pruned is not None and now - self._pruned < PRUNE_EVERY:
            return
        self._pruned = now
        removed, events = self.parts.history.prune(now - KEEP_HISTORY)
        if removed or events:
            log.info("Lifeline removed %d readings and %d alert records older than 90 days.", removed, events)
            if self._just is not None and now - self._just[2] >= JUST_SWITCHED_FOR:
                self._just = None


ERROR_SENTENCES = {
    400: BAD_REQUEST,
    404: NOT_FOUND,
    405: WRONG_METHOD,
    408: "The request took too long to arrive.",
    411: NO_LENGTH,
    413: TOO_LARGE,
    414: "The request address is too long.",
    431: "The request headers are too large.",
    500: INTERNAL,
    501: WRONG_METHOD,
    505: "Lifeline speaks HTTP/1.1 only.",
}


def _switched(service, result):
    return {
        "from_uuid": result.from_uuid,
        "to_uuid": result.to_uuid,
        "confirmed": result.confirmed,
        "message": service.switch_message(result),
        "note": getattr(result, "note", None),
    }


def _appearance(query):
    return query.get("appearance", ["dark"])[-1]


MAC_ROUTES = {
    ("state", False): {"GET": lambda s, query, body, rest: (200, s.state(_appearance(query)))},
    ("history", True): {"GET": lambda s, query, body, rest: (200, s.history_for(rest, query))},
    ("switch", False): {"POST": lambda s, query, body, rest: (200, _switched(s, s.switch_to(s.resolve(body), "you", s.via(body))))},
    ("undo", False): {"POST": lambda s, query, body, rest: (200, _switched(s, s.undo(body)))},
    ("cancel", False): {"POST": lambda s, query, body, rest: (200, s.cancel())},
    ("reading", False): {"POST": lambda s, query, body, rest: (200, s.reading_reply(body))},
    ("presence", False): {"POST": lambda s, query, body, rest: (200, s.set_presence(body))},
    ("notifications", True): {"POST": lambda s, query, body, rest: (200, s.later(rest, body))},
    ("add", False): {"POST": lambda s, query, body, rest: (202, s.start_job("add", None, body))},
    ("settings", False): {"POST": lambda s, query, body, rest: (200, s.update_settings(body))},
    ("resign", True): {"POST": lambda s, query, body, rest: (202, s.start_job("resign", rest, body))},
    ("rename", True): {"POST": lambda s, query, body, rest: (200, s.rename(rest, body))},
    ("adopt", False): {"POST": lambda s, query, body, rest: (202, s.start_job("adopt", None, body))},
    (NESTED_ROUTE, True): {"POST": lambda s, query, body, rest: (202, s.start_devbox_job(rest, body))},
    (JOBS_ROUTE, True): {"POST": lambda s, query, body, rest: (200, s.job_action(rest))},
    ("computer", False): {"POST": lambda s, query, body, rest: (200, s.pair_computer(body))},
    ("computer", True): {"POST": lambda s, query, body, rest: (200, s.forget_computer(rest))},
}
FORGET_ROUTES = {
    ("remove", True): {"POST": lambda s, query, body, rest: (200, s.remove(rest))},
    ("reset", False): {"POST": lambda s, query, body, rest: (200, s.reset())},
}
MAC_ROUTES.update(FORGET_ROUTES)


def _reject_constant(name):
    raise ValueError(name)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Lifeline"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    def version_string(self):
        return "Lifeline"

    def address_string(self):
        return "local"

    def log_message(self, format, *args):
        return

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._reply(code, {"error": ERROR_SENTENCES.get(code, BAD_REQUEST)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method):
        self._body_read = False
        try:
            status, payload = self._route(method)
        except Refusal as refusal:
            status, payload = refusal.status, {**refusal.extra, "error": refusal.sentence}
        except switching.SwitchRefused as error:
            status, payload = 409, {"error": str(error)}
        except switching.SwitchFailed as error:
            status, payload = 500, {"error": str(error)}
        except Exception as error:
            log.warning("A request to Lifeline failed (%s).", type(error).__name__)
            status, payload = 500, {"error": INTERNAL}
        if status >= 400 and not self._body_read:
            self.close_connection = True
            self._drain()
        self._reply(status, payload)

    def _drain(self):
        raw = (self.headers.get("Content-Length") or "").strip()
        if self.headers.get("Transfer-Encoding") or not (raw.isascii() and raw.isdigit()):
            return
        length = int(raw)
        if 0 < length <= BODY_LIMIT:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _route(self, method):
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        if not segments or len(segments) > 3 or (len(segments) == 3 and segments[0] not in (NESTED_ROUTE, JOBS_ROUTE)):
            raise Refusal(404, NOT_FOUND)
        name = segments[0]
        rest = "/".join(segments[1:]) if len(segments) > 1 else None
        methods = self.server.routes.get((name, rest is not None))
        if methods is None:
            raise Refusal(404, NOT_FOUND)
        handler = methods.get(method)
        if handler is None:
            self.close_connection = True
            raise Refusal(405, WRONG_METHOD)
        service = self.server.service
        if method == "GET":
            if self.headers.get("Content-Length") not in (None, "0") or self.headers.get("Transfer-Encoding"):
                self.close_connection = True
            return handler(service, parse_qs(parts.query, keep_blank_values=True), None, rest)
        return handler(service, None, self._body(), rest)

    def _body(self):
        data = self._raw_body()
        if not data.strip():
            return {}
        try:
            value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise Refusal(400, BAD_JSON) from None
        if not isinstance(value, dict):
            raise Refusal(400, NOT_OBJECT)
        return value

    def _raw_body(self):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise Refusal(411, NO_LENGTH)
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()):
            self.close_connection = True
            raise Refusal(400, BAD_LENGTH)
        length = int(raw)
        if length > BODY_LIMIT:
            self.close_connection = True
            raise Refusal(413, TOO_LARGE)
        self._body_read = True
        data = self.rfile.read(length) if length else b""
        if len(data) < length:
            self.close_connection = True
            raise Refusal(400, SHORT_BODY)
        return data

    def _signed(self, status, data):
        return []

    def _reply(self, status, payload):
        service = getattr(self.server, "service", None)
        name = service.computer_name() if service is not None else None
        try:
            data = json.dumps(named(_clean(payload), name), allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            data = json.dumps({"error": INTERNAL}).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for header, value in self._signed(status, data):
                self.send_header(header, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError:
            self.close_connection = True


LOCAL_ONLY_ROUTES = {
    ("statusline", False): {"POST": lambda s, query, body, rest: (200, s.set_statusline(body))},
}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path, routes=None):
        self.service = None
        self.routes = {**(MAC_ROUTES if routes is None else routes), **LOCAL_ONLY_ROUTES}
        super().__init__(path, _Handler, bind_and_activate=False)

    def handle_error(self, request, client_address):
        log.warning("Lifeline closed a connection that failed.")


def _secure_folder(folder):
    default = Path(statusline.socket_path(Path.home(), {})).parent
    if not folder.exists():
        folder.mkdir(mode=0o700, parents=True)
        os.chmod(folder, 0o700)
    elif Path(os.path.realpath(folder)) == Path(os.path.realpath(default)):
        os.chmod(folder, 0o700)
    info = os.stat(folder)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ServiceError(OPEN_FOLDER)


def _answers(path):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _start_lock(path):
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(ALREADY_RUNNING) from None
    except OSError:
        os.close(fd)
        raise ServiceError(NO_SOCKET) from None
    return fd


def _open(path, routes=None):
    path = str(path)
    if len(os.fsencode(path)) >= SOCKET_LIMIT:
        raise ServiceError(LONG_PATH)
    _secure_folder(Path(path).parent)
    lock_fd = _start_lock(path)
    server = None
    try:
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise ServiceError(NOT_A_SOCKET)
            if _answers(path):
                raise AlreadyRunning(ALREADY_RUNNING)
            os.unlink(path)
        server = _Server(path, routes)
        try:
            server.server_bind()
            os.chmod(path, 0o600)
            server.server_activate()
        except OSError:
            raise ServiceError(NO_SOCKET) from None
        info = os.lstat(path)
        return server, lock_fd, (info.st_dev, info.st_ino)
    except BaseException:
        if server is not None:
            server.server_close()
        os.close(lock_fd)
        raise


def _close(server, lock_fd, path, identity):
    try:
        server.server_close()
    except OSError:
        pass
    try:
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) == identity:
            os.unlink(path)
    except OSError:
        pass
    os.close(lock_fd)


def _service_lock(path):
    path = Path(path) if path is not None else writelock.lifeline_folder() / SERVICE_LOCK_NAME
    folder = path.parent
    if not folder.exists():
        folder.mkdir(mode=0o700, parents=True)
    if path.parent == writelock.lifeline_folder():
        os.chmod(folder, 0o700)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise ServiceError(NO_SOCKET) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(ALREADY_RUNNING) from None
    except OSError:
        os.close(fd)
        raise ServiceError(NO_SOCKET) from None
    return fd


def serve(factory, path, *, install_signals=True, ready=None, ticking=True, service_lock=None, routes=None, extra=None):
    try:
        held = _service_lock(service_lock)
    except ServiceError as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        return _serve(factory, path, install_signals, ready, ticking, routes, extra)
    finally:
        os.close(held)


def _serve(factory, path, install_signals, ready, ticking, routes=None, extra=None):
    try:
        server, lock_fd, identity = _open(path, routes)
    except ServiceError as error:
        print(str(error), file=sys.stderr)
        return 1
    service = None
    closer = None
    previous = {}
    stopping = threading.Event()

    def stop(signum=None, frame=None):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, name="lifeline-stop", daemon=True).start()

    code = 0
    try:
        try:
            service = factory()
            server.service = service
            service.start(ticking=ticking)
        except Exception as error:
            print(str(error) if isinstance(error, ServiceError) else NO_START, file=sys.stderr)
            log.warning("Lifeline could not start its service (%s).", type(error).__name__)
            return 1
        if extra is not None:
            try:
                closer = extra(service)
            except Exception as error:
                log.warning("Lifeline could not open its listener for the Mac (%s).", type(error).__name__)
        if install_signals and threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGINT):
                previous[number] = signal.signal(number, stop)
        log.info("Lifeline is running.")
        if ready is not None:
            ready(stop)
        server.serve_forever(poll_interval=0.25)
    finally:
        if closer is not None:
            try:
                closer()
            except Exception as error:
                log.warning("Lifeline could not close its listener for the Mac (%s).", type(error).__name__)
        if service is not None:
            service.stop()
        _close(server, lock_fd, str(path), identity)
        for number, handler in previous.items():
            signal.signal(number, handler)
        log.info("Lifeline stopped.")
    return code


class _PlainFormatter(logging.Formatter):
    def format(self, record):
        plain = logging.makeLogRecord(record.__dict__)
        plain.exc_info = None
        plain.exc_text = None
        plain.stack_info = None
        text = super().format(plain)
        return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


class _PrivateLog(logging.handlers.RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)


def log_folder(home=None):
    return ident.log_folder(home)


def private_log(folder):
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    limit = LOG_BYTES - 1024
    handler = _PrivateLog(folder / LOG_NAME, maxBytes=limit, backupCount=LOG_FILES - 1, encoding="utf-8")
    handler.setFormatter(_PlainFormatter(LOG_FORMAT))
    return handler


def production_parts():
    if sys.platform != "darwin":
        raise ServiceError(MAC_ONLY)
    home = Path.home()
    folder = writelock.lifeline_folder()
    user = enrolling.login_user()
    return Parts(
        home=home,
        folder=folder,
        slot=MacKeychainSlot(account=enrolling.keychain_account(user)),
        store=MacKeychainStore(),
        registry=Registry(),
        history=History(),
        profile_fn=anthropic.profile,
        usage_fn=anthropic.usage,
        refresh_fn=anthropic.refresh,
        run_login=run_login,
        runner=run_security,
        user=user,
        link=NullLink(),
        clock=_utc_now,
        tz=_local_zone(),
        sessions_fn=lambda: live_sessions(home),
        temp_root=enrolling.login_root(home),
        backups=switching.default_backups(),
        relay_fn=_mac_relay,
        open_page=_open_page(),
        link_in_background=True,
    )


def _mac_relay(port, forward):
    from lifeline import devbox

    return devbox.mac_relay(port, forward)


def _open_page():
    from lifeline import devbox

    return devbox.open_page


def main(argv=None, remote=False):
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    if remote or not sys.platform == "darwin":
        from lifeline import devbox

        return devbox.run()
    try:
        logging.getLogger().addHandler(private_log(log_folder()))
    except Exception as error:
        log.warning("Lifeline could not open its log file (%s).", type(error).__name__)
    try:
        path = statusline.socket_path(Path.home(), os.environ)
        return serve(lambda: Service(production_parts()), path)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(NO_START, file=sys.stderr)
        log.warning("Lifeline stopped because of a problem (%s).", type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
