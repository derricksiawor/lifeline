import http.client
import ipaddress
import json
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from lifeline import addtext, anthropic, clock, devbox, enroll, ident, history, server, statusline, viewmodel, writelock
from lifeline import accounts as accounts_module
from lifeline import switch as switching
from lifeline.accountfile import AccountFileError, read_oauth_account
from lifeline.enroll import login_environment
from lifeline.accounts import Registry, RegistryError, identity, plan_label
from lifeline.accounts import stated as stated_pair
from lifeline.slot import FileSlot, MacKeychainSlot, SlotError, run_security
from lifeline.store import FileStore, MacKeychainStore

LOGIN_COMMAND = ("claude", "auth", "login", "--claudeai")
SESSIONS_FOLDER = (".claude", "sessions")
LOGIN_TIMEOUT = 600
WATCH_SECONDS = 600
POLL_SECONDS = 5
NOTE_NAME = "last_switch.json"
SESSION_LIMIT = 1024 * 1024
NUMBER_WORD = re.compile(r"[1-9][0-9]{0,5}", re.ASCII)
HELP_WORDS = ("help", "--help", "-h")
INDENT = "   "
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

UNEXPECTED = "Lifeline stopped because of an unexpected problem. Run lifeline status to see where things stand."
STOPPED = "You stopped Lifeline before it finished. Run lifeline status to see where things stand."
START_PROBLEM = "Lifeline could not start, so nothing was changed."
REGISTRY_PROBLEM = "Lifeline could not read its list of accounts, so it stopped."
REGISTRY_UNSAVED = "Lifeline could not save its list of accounts, so the name was not changed."
NO_ACCOUNTS = (
    "Lifeline has no accounts yet. Add one with lifeline add, "
    "or add the account Claude Code uses now with lifeline adopt."
)
NO_SETTINGS = "Claude Code has no settings file, so no account is in use."
UNREADABLE_SETTINGS = "Lifeline could not read Claude Code's settings file, so it cannot tell which account is in use."
ORG_UNSAID = (
    "Claude Code's settings name {who} but not its organization, "
    "so Lifeline cannot tell which of its accounts is in use."
)
NO_HISTORY = "Lifeline could not read its usage history, so no readings are shown."
NO_READING = "Lifeline has no reading for it yet."
NOTE_DAMAGED = "Lifeline could not read its note of the last switch."
NOTE_DAMAGED_UNDO = "Lifeline could not read its note of the last switch, so nothing was changed."
NOTE_UNSAVED = "Lifeline could not save a note of this switch, so lifeline undo cannot switch back from it."
NO_UNDO = "There is no switch to undo, so nothing was changed."
UNDONE = "Lifeline undid the last switch."
UNCONFIRMED_SIGNIN = "Lifeline could not confirm the new sign-in with Anthropic."
WATCH_START = "Lifeline will watch Claude Code for 10 minutes so the switch holds."
WATCH_HINT = "Press Control-C to stop watching. Stopping the watch does not undo the switch."
WATCH_OVER = "Lifeline has stopped watching Claude Code."
WATCH_PROBLEM = "Lifeline hit an unexpected problem while watching, so it stopped watching."
WATCH_SKIPPED = "You pressed Control-C, so Lifeline will not watch this switch."
WATCH_SKIPPED_SIGNAL = "Lifeline got a request to stop, so it will not watch this switch."
WATCH_SKIPPED_HANGUP = "The terminal closed, so Lifeline will not watch this switch."
STOPPED_SIGNAL = "Lifeline got a request to stop before it finished. Run lifeline status to see where things stand."
STOPPED_HANGUP = "The terminal closed before Lifeline finished. Run lifeline status to see where things stand."
HALT_SIGNAL = "Lifeline got a request to stop, so it stopped the watch."
HALT_HANGUP = "The terminal closed, so Lifeline stopped the watch."
WATCH_STOPPED = "You stopped the watch."
OPENS_IN = "Lifeline opens Claude's sign-in in {browser}, your default browser."
OPENS_FOR = "Lifeline opens Claude's sign-in for {email} in {browser}, your default browser."
OPENS_IN_DEFAULT = "Lifeline opens Claude's sign-in in your default browser."
OPENS_FOR_DEFAULT = "Lifeline opens Claude's sign-in for {email} in your default browser."
SIGN_IN_ADDING = "Sign in there as the account you are adding."
SIGN_IN_AS = "Sign in there as {name}."
CHOOSE_ADDING = "If Claude asks you to choose an organization, choose the one you are adding, then press Authorize."
CHOOSE_ITS_OWN = "If Claude asks you to choose an organization, choose the one this account uses, then press Authorize."
PAGE_OPENED = "Lifeline opened the sign-in page in {browser}."
PAGE_OPENED_DEFAULT = "Lifeline opened the sign-in page in your default browser."
PAGE_NOT_OPENED = "Lifeline could not open the sign-in page by itself. Open this page in your browser: {url}"
PAGE_NOT_TRUSTED = (
    "Claude Code gave a sign-in page Lifeline does not trust, so Lifeline did not open it. Press Control-C to stop the sign-in."
)
PAGE_POLL = 0.1
LOGIN_HINT_PASTE = (
    "Claude Code will show a sign-in address. Open it in a browser and sign in, then paste the code it shows here."
)
LOGIN_STOPPED = "You stopped Claude Code's sign-in."
DEVBOX_REFUSAL = server.DEVBOX_SWITCH_REFUSED
ROUTED = frozenset({"status", "switch", "undo", "add", "adopt", "resign", "rename", "remove", "reset"})
LINUX_ROUTED = ROUTED - {"add", "adopt", "resign"}
REQUEST_SECONDS = 15
CHANGE_SECONDS = 180
PROBE_SECONDS = 1
JOB_POLL_SECONDS = 1
JOB_WAIT_SECONDS = LOGIN_TIMEOUT + 120
REPLY_LIMIT = 8 * 1024 * 1024
SERVICE_SILENT = "Lifeline in the background did not answer in time. Run lifeline status to see whether anything changed."
SERVICE_FALLBACK = "Lifeline in the background did not answer, so this status comes from Lifeline's own files."
SERVICE_REFUSED = "Lifeline in the background could not finish that request."
SERVICE_WATCHES = "Lifeline in the background keeps watching Claude Code, so the switch holds."
SERVICE_ALWAYS_WATCHES = "Lifeline in the background always watches Claude Code, so --no-watch changes nothing here."
SERVICE_LOGIN = "Lifeline in the background will open the sign-in page in your browser. Sign in there, then come back here."
SERVICE_FINISHES = "Lifeline in the background will finish this on its own, so Lifeline is still waiting for its answer."
WAIT_STEP_SECONDS = 0.1
SERVICE_UNKNOWN_ACCOUNT = "Lifeline in the background does not know which account Claude Code is using."
JOB_LOST = "Lifeline in the background no longer lists that request, so Lifeline cannot tell how it ended. Run lifeline status to check."
JOB_TOO_LONG = "Lifeline stopped waiting after 12 minutes. Run lifeline status to see whether the request finished."
WAIT_STOPPED = "You stopped waiting, but Lifeline in the background keeps going on its own. Run lifeline status to see how it ends."
WAIT_STOPPED_SIGNAL = "Lifeline stopped waiting, but Lifeline in the background keeps going on its own. Run lifeline status to see how it ends."
SERVICE_START_PROBLEM = "Lifeline could not start in the background."
NEEDS_SERVICE = "Lifeline is not running in the background, so nothing was changed. Open Lifeline, then try again."
SWITCHING = frozenset({"switch", "undo"})
DEVBOX_ON_MAC = "Run lifeline computer commands on the Mac."
DEVBOX_SILENT = devbox.DEVBOX_SILENT
DEVBOX_REFUSED = devbox.DEVBOX_REFUSED
DEVBOX_LOST = devbox.DEVBOX_LOST
DEVBOX_JOB_LOST = devbox.DEVBOX_JOB_LOST
UNTRUSTED_PAGE = devbox.UNTRUSTED_PAGE
NO_DEVBOX_PAGE = devbox.NO_DEVBOX_PAGE
DEVBOX_TOO_LONG = devbox.DEVBOX_TOO_LONG
DEVBOX_PORT_TAKEN = devbox.DEVBOX_PORT_TAKEN
DEVBOX_STOPPED = "You stopped the sign-in, so the other computer kept the sign-ins it had."
DEVBOX_STARTING = "Lifeline on the other computer is starting the sign-in for {name}."
DEVBOX_PRESS = "Press Authorize on the page that opened for {name}."
DEVBOX_PRESS_EMAIL = "Check that the page shows {email}, then press Authorize."
DEVBOX_STOP_HINT = "Run lifeline computer stop to stop it."
DEVBOX_UNSURE_ALONE = (
    "The other computer did not answer in time. If the sign-in started there, the other computer ends it on its own within 10 minutes."
)
STOPPED_KEPT = "If the sign-in started on the other computer, Lifeline stops it there as soon as the other computer answers."
STOPPED_ALONE = "If the sign-in started on the other computer, the other computer ends it on its own within 10 minutes."
DEVBOX_STILL_STOPPING = "Lifeline asked the other computer to stop the sign-in, and it has not stopped yet. It stops on its own within 10 minutes."
DEVBOX_STOPPED_THERE = "Lifeline stopped the sign-in on the other computer."
BROWSER_NOTE_MARK = "is signed in to claude.ai as that account"
TRY_AGAIN = "then try again."
ADD_AGAIN = "then run lifeline add again."
PRESS_TRY_AGAIN = "then press Try again."
DEVBOX_OPEN_IT = "Your browser did not open, so open this page yourself: {url}"
DEVBOX_SHAPE = (
    "Give a computer command, as in lifeline computer add 3, lifeline computer adopt, "
    "lifeline computer resign 3 or lifeline computer stop."
)
DEVBOX_SHAPES = {
    "add": "Give the number of the account the other computer should sign in to, as in lifeline computer add 3.",
    "resign": "Give the number of the account the other computer should sign in to again, as in lifeline computer resign 3.",
}
DEVBOX_SPECS = {
    "add": (("number",), ("--email",), ()),
    "adopt": ((), (), ()),
    "resign": (("number",), ("--email",), ()),
    "stop": ((), (), ()),
}
NAMING_OPTIONS = ("--name", "--short", "--number")
DEVBOX_SLOT = (".claude", ".credentials.json")
STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))
NO_LOGIN_COMMAND = "Lifeline could not find Claude Code's command on this computer."
LOGIN_TOO_LONG = "The sign-in took longer than 10 minutes, so Lifeline stopped it."
LOGIN_NOT_STARTED = "Lifeline could not start Claude Code's sign-in command."
SESSIONS_UNREADABLE = "Lifeline could not read Claude Code's sessions folder."
NO_SESSIONS = "Lifeline found no Claude Code sessions."
SESSION_ACCOUNTS = "Which account each session is on is not known yet."
BAD_NUMBER = "An account number must be a whole number, 1 or more."
EMPTY_NAME = "A name cannot be empty."
BAD_NAME = "A name must be plain text on one line."
BAD_EMAIL = "An email must look like name@example.com."

HELP = (
    "Lifeline moves Claude Code between your Claude accounts.",
    "Run lifeline status to see each account, its plan, the account in use and its latest use.",
    "Run lifeline add to sign in to a new account in your browser. "
    "Add --email with its email to fill in the sign-in page. "
    "Add --name, --short or --number to choose its name, short name and number.",
    "Run lifeline adopt to add the account Claude Code uses now. It takes --name, --short and --number like add.",
    "Run lifeline resign 2 to sign in to account 2 again in your browser. Add --email to fill in its email.",
    'Run lifeline rename 2 "Work Account" Work to give account 2 a new name and short name.',
    "Run lifeline switch 2 to move Claude Code to account 2. "
    "Lifeline then watches for 10 minutes so the switch holds. Add --no-watch to skip the watch.",
    "Run lifeline undo to switch back to the account Claude Code used before the last switch. "
    "Add --no-watch to skip the watch.",
    "Run lifeline remove 2 to remove account 2, its saved sign-in and its history from Lifeline, here and on the other computer. "
    "Claude Code keeps its own sign-in.",
    "Run lifeline reset to take Lifeline back to its first run. "
    "It saves a copy of its history and settings first, and Claude Code keeps its own sign-in.",
    "Run lifeline sessions to list Claude Code's sessions.",
    "Run lifeline computer add 3 on the Mac to give the other computer its own sign-in for account 3. "
    "The sign-in page opens in your browser, and you press Authorize. Add --email to fill in its email.",
    "Run lifeline computer adopt on the Mac so the other computer's Lifeline takes over the sign-in its Claude Code holds now.",
    "Run lifeline computer resign 3 on the Mac to give the other computer a new sign-in for account 3. Add --email to fill in its email.",
    "Run lifeline computer stop on the Mac to stop a sign-in that is still waiting on the other computer.",
    "Run lifeline serve to run Lifeline in the background. The menu bar app starts it for you. "
    "Add --remote to run it on the other computer the Mac reads.",
    "Run lifeline pair on the other computer to get its address and a new one-time pairing code for the Mac. "
    "Add --reset so the Mac paired before can no longer reach it.",
    "While Lifeline runs in the background, status, switch, undo, add, adopt, resign and rename go through it.",
    "Remove and reset need Lifeline running in the background.",
    "Lifeline numbers the accounts from 1 up.",
)

SPECS = {
    "status": ((), (), ()),
    "sessions": ((), (), ()),
    "serve": ((), (), ("--remote",)),
    "pair": ((), (), ("--reset",)),
    "undo": ((), (), ("--no-watch",)),
    "add": ((), ("--email", "--name", "--short", "--number"), ()),
    "adopt": ((), ("--name", "--short", "--number"), ()),
    "resign": (("number",), ("--email",), ()),
    "rename": (("number", "name", "name"), (), ()),
    "switch": (("number",), (), ("--no-watch",)),
    "remove": (("number",), (), ()),
    "reset": ((), (), ()),
}
SHAPES = {
    "resign": "Give the number of the account to sign in to again, as in lifeline resign 2.",
    "rename": 'Give the account number, the new name and the new short name, as in lifeline rename 2 "Work Account" Work.',
    "switch": "Give the number of the account to switch to, as in lifeline switch 2.",
    "remove": "Give the number of the account to remove, as in lifeline remove 2.",
}


class _Usage(Exception):
    pass


class _Refusal(Exception):
    def __init__(self, lines, code=1):
        super().__init__("")
        self.lines = list(lines)
        self.code = code


class _Damaged(Exception):
    pass


class _LoginFailed(Exception):
    pass


@dataclass
class Command:
    name: str
    words: list = field(default_factory=list)
    options: dict = field(default_factory=dict)
    flags: set = field(default_factory=set)


@dataclass
class Wiring:
    home: Path
    user: str
    slot: object
    store: object
    registry: object
    profile_fn: object
    runner: object
    run_process: object
    lock_path: Path
    backups: Path
    history_path: Path
    temp_root: object
    out: object
    err: object
    now: object
    tz: object
    monotonic: object
    wall: object
    sleep: object
    pid_alive: object
    lock_timeout: float | None = None
    session_timeout: float | None = None
    settle: float | None = None
    watch_lock_timeout: float | None = None
    watch_session_timeout: float | None = None
    changes: bool = True
    service: object = None
    saved_in: str = enroll.SAVED_IN_KEYCHAIN
    routed: frozenset = ROUTED
    system: str = sys.platform
    devbox_link: object = None
    relay: object = None
    open_page: object = None
    open_browser: object = None
    browser_fn: object = None
    computer_name: str | None = None


class _LazyStore:
    def __init__(self, build):
        self._build = build
        self._made = None

    def _store(self):
        if self._made is None:
            self._made = self._build()
        return self._made

    def get(self, uuid):
        return self._store().get(uuid)

    def put(self, uuid, oauth):
        return self._store().put(uuid, oauth)

    def remove(self, uuid):
        return self._store().remove(uuid)


def _utc_now():
    return datetime.now(timezone.utc)


def pid_alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 2**31:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return None
    return True


def _computer_name():
    try:
        found = server._read_private(writelock.lifeline_folder() / server.COMPUTER_NAME)
    except Exception:
        found = None
    return found.get("name") if isinstance(found, dict) and isinstance(found.get("name"), str) else None


def _mac_devbox_link():
    return devbox.paired_link(server.read_computer(writelock.lifeline_folder(), run_security)) or server.NullLink()


def production(system=None):
    home = Path.home()
    user = enroll.login_user()
    system = system or sys.platform
    if system == "darwin":
        slot, store, changes = MacKeychainSlot(account=enroll.keychain_account(user)), MacKeychainStore(), True
        place, routed = enroll.SAVED_IN_KEYCHAIN, ROUTED
        mac = dict(devbox_link=_mac_devbox_link, relay=devbox.mac_relay, open_page=devbox.open_page, computer_name=_computer_name())
    else:
        slot = FileSlot(home.joinpath(*DEVBOX_SLOT))
        store = _LazyStore(lambda: FileStore(ident.signin_folder(home)))
        changes = False
        place, routed = enroll.SAVED_IN_FILE, LINUX_ROUTED
        mac = {}
    return Wiring(
        home=home,
        user=user,
        slot=slot,
        store=store,
        changes=changes,
        saved_in=place,
        routed=routed,
        system=system,
        **mac,
        service=lambda: ServiceClient.find(statusline.socket_path(home, os.environ)),
        registry=Registry(),
        profile_fn=anthropic.profile,
        runner=run_security,
        run_process=subprocess.run,
        lock_path=writelock.default_lock_path(),
        backups=switching.default_backups(),
        history_path=history.default_path(),
        temp_root=None,
        out=sys.stdout,
        err=sys.stderr,
        now=_utc_now,
        tz=None,
        monotonic=time.monotonic,
        wall=time.time,
        sleep=time.sleep,
        pid_alive=pid_alive,
    )


def _given(**values):
    return {key: value for key, value in values.items() if value is not None}


def _write(stream, lines):
    for line in lines:
        try:
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError):
            return


def _say(wiring, lines):
    _write(wiring.out, server.named(list(lines), wiring.computer_name))


def _complain(wiring, lines):
    _write(wiring.err, server.named(list(lines), wiring.computer_name))


def _notes(error):
    return [note for note in getattr(error, "__notes__", ()) if isinstance(note, str) and note]


def _failed(error, login, kind):
    lines = _sentences(error)
    if str(error) != enroll.LOGIN_FAILED:
        return lines + login.reasons()
    reason = server.LOGIN_TIMED_OUT if login.problem == LOGIN_TOO_LONG else login.problem
    return [server.login_result(reason, kind)] + lines[1:]


def _sentences(error):
    text = str(error)
    return ([text] if text else [UNEXPECTED]) + _notes(error)


def _with_hint(lines, wiring=None, already=False):
    if not already:
        return lines
    if any(PRESS_TRY_AGAIN in line for line in lines):
        return [line.replace(PRESS_TRY_AGAIN, ADD_AGAIN) for line in lines]
    if any(BROWSER_NOTE_MARK in line for line in lines):
        return [line.replace(TRY_AGAIN, ADD_AGAIN) if BROWSER_NOTE_MARK in line else line for line in lines]
    return lines + [_signed_in_note(wiring).replace(TRY_AGAIN, ADD_AGAIN)]


def _browser(wiring):
    try:
        return ((wiring.browser_fn if wiring is not None else None) or devbox.default_browser)()
    except Exception:
        return None


def _signed_in_note(wiring):
    return server.BROWSER_SIGNED_IN.format(browser=_browser(wiring) or server.DEFAULT_BROWSER_WORDS)


def _shown(text):
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _text(value):
    if not isinstance(value, str):
        return None
    shown = _shown(value).strip()
    return shown or None


def _number(value):
    if not isinstance(value, str) or NUMBER_WORD.fullmatch(value) is None:
        raise _Usage(BAD_NUMBER)
    return int(value)


def _name(value):
    text = value.strip()
    if not text:
        raise _Usage(EMPTY_NAME)
    if not all(ch.isprintable() for ch in text):
        raise _Usage(BAD_NAME)
    return text


def _email(value):
    local, at, domain = value.rpartition("@")
    plain = all(ch.isprintable() and not ch.isspace() for ch in value)
    if not at or not local or not domain or value.startswith("-") or not plain:
        raise _Usage(BAD_EMAIL)
    return value


CHECKS = {
    "number": _number,
    "name": _name,
    "--number": _number,
    "--name": _name,
    "--short": _name,
    "--email": _email,
}


def parse(argv):
    if not argv or argv[0] in HELP_WORDS:
        return Command("help")
    name = argv[0]
    if name == "computer":
        if len(argv) < 2 or argv[1] not in DEVBOX_SPECS:
            raise _Usage(DEVBOX_SHAPE)
        return _parse_rest(f"computer {argv[1]}", DEVBOX_SPECS[argv[1]], argv[2:], DEVBOX_SHAPES.get(argv[1]))
    if name not in SPECS:
        raise _Usage(f"Lifeline has no command called {_shown(name)}. Run lifeline help to see its commands.")
    return _parse_rest(name, SPECS[name], argv[1:], SHAPES.get(name))


def _parse_rest(name, spec, given, shape):
    places, valued, switches = spec
    rest = list(given)
    words, options, flags = [], {}, set()
    index = 0
    while index < len(rest):
        item = rest[index]
        index += 1
        if not item.startswith("--"):
            words.append(item)
            continue
        key, equals, value = item.partition("=")
        if key in switches:
            if equals:
                raise _Usage(f"The option {key} takes no value.")
            if key in flags:
                raise _Usage(f"The option {key} was given twice.")
            flags.add(key)
            continue
        if key not in valued:
            raise _Usage(f"The {name} command has no option called {_shown(key)}. Run lifeline help to see its options.")
        if key in options:
            raise _Usage(f"The option {key} was given twice.")
        if not equals:
            if index >= len(rest) or rest[index].startswith("--"):
                raise _Usage(f"The option {key} needs a value.")
            value = rest[index]
            index += 1
        options[key] = value
    if len(words) != len(places):
        if not places:
            raise _Usage(f"Lifeline did not expect {_shown(words[0])} after {name}. Run lifeline help to see its commands.")
        raise _Usage(shape)
    words = [CHECKS[place](word) for place, word in zip(places, words)]
    options = {key: CHECKS[key](value) for key, value in options.items()}
    return Command(name, words, options, flags)


class _Stop(KeyboardInterrupt):
    def __init__(self, signum):
        super().__init__()
        self.signum = signum


def _signal_of(stop):
    return getattr(stop, "signum", signal.SIGINT)


class _Interrupts:
    def __init__(self):
        self.count = 0
        self.first = None
        self.sleeping = False
        self.quiet = False
        self._previous = {}

    def __enter__(self):
        for number in STOP_SIGNALS:
            try:
                previous = signal.getsignal(number)
                if previous == signal.SIG_IGN:
                    continue
                signal.signal(number, self._handle)
                self._previous[number] = previous
            except (ValueError, OSError, TypeError):
                continue
        return self

    def __exit__(self, *details):
        for number, previous in self._previous.items():
            if previous is None:
                previous = signal.default_int_handler if number == signal.SIGINT else signal.SIG_DFL
            try:
                signal.signal(number, previous)
            except (ValueError, OSError, TypeError):
                pass
        self._previous = {}
        return False

    @property
    def asked(self):
        return self.count > 0

    @property
    def pressed(self):
        return self.first == signal.SIGINT

    def note(self, signum):
        self.count = max(self.count, 1)
        if self.first is None:
            self.first = signum

    def _handle(self, signum, frame):
        self.count += 1
        if self.first is None:
            self.first = signum
        if self.quiet:
            return
        if self.sleeping or self.count > 1:
            raise _Stop(signum)

    def sleep(self, sleeper, seconds):
        self.sleeping = True
        try:
            if self.asked:
                raise _Stop(self.first if self.first is not None else signal.SIGINT)
            sleeper(seconds)
        finally:
            self.sleeping = False


class _Held:
    def __init__(self, guard):
        self.guard = guard
        self.previous = None

    def __enter__(self):
        self.guard.quiet = True
        try:
            self.previous = signal.pthread_sigmask(signal.SIG_BLOCK, set(STOP_SIGNALS))
        except (AttributeError, OSError, ValueError):
            self.previous = None
        return self

    def __exit__(self, *details):
        if self.previous is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, self.previous)
            except (OSError, ValueError):
                pass
        return False


def _accounts(wiring):
    try:
        return wiring.registry.load()
    except (RegistryError, OSError, ValueError):
        raise _Refusal([REGISTRY_PROBLEM]) from None


def _by_number(wiring, number):
    found = next((account for account in _accounts(wiring) if account.key == number), None)
    if found is None:
        raise _Refusal([f"There is no account number {number} in Lifeline, so nothing was changed."])
    return found


def _account_of(wiring, uuid):
    try:
        return wiring.registry.by_uuid(uuid)
    except Exception:
        return None


def _name_of(wiring, uuid):
    account = _account_of(wiring, uuid)
    return _shown(account.name) if account is not None and account.name else switching.OUTSIDER


def _plan_of(account):
    return plan_label(account.plan) if account.plan else ""


def _plan_sentence(account):
    plan = _plan_of(account)
    return f" It is on the {plan} plan." if plan else ""


def _whole(value):
    return int(math.floor(max(0.0, float(value))))


def _local(moment, tz):
    return clock.nearest_minute(moment, tz)


def _day(local):
    return f"{local.day} {MONTHS[local.month - 1]}"


def _moment(moment, tz):
    local = _local(moment, tz)
    return f"{local:%H:%M} on {_day(local)}"


def _note_path(wiring):
    return Path(wiring.registry.path).parent / NOTE_NAME


def _read_note(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _Damaged() from None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise _Damaged() from None
    if not isinstance(data, dict):
        raise _Damaged()
    for key in ("from_uuid", "to_uuid"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise _Damaged()
    return data


def _save_note(path, from_uuid, to_uuid, at):
    text = json.dumps({"from_uuid": from_uuid, "to_uuid": to_uuid, "at": at.isoformat(timespec="seconds")}) + "\n"
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".last_switch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
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


def _note_time(note, tz):
    value = note.get("at")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _moment(moment, tz)


def _live(wiring, accounts, names):
    record = switching.read_record(switching.record_path_for(wiring.lock_path))
    path = wiring.home / switching.ACCOUNT_FILE_NAME
    if not os.path.lexists(path):
        return None, NO_SETTINGS
    try:
        label = read_oauth_account(path)
    except AccountFileError:
        return None, UNREADABLE_SETTINGS
    login, org = stated_pair(label)
    if login is None:
        head = "Claude Code's settings name no account, so it may be signed out."
        if record is not None:
            head += f" Lifeline's record names {names.get(record, switching.OUTSIDER)}."
        return None, head
    email = _text(label.get("emailAddress"))
    stated = identity(accounts, login, org)
    if stated is None:
        return None, ORG_UNSAID.format(who=email or "an account")
    called = names.get(stated) or email or switching.OUTSIDER
    if record is not None and record != stated:
        recorded = names.get(record, switching.OUTSIDER)
        return None, (
            f"Claude Code's settings name {called}, but Lifeline's record names {recorded}, "
            "so Lifeline cannot tell which one is in use."
        )
    if stated in names:
        return stated, f"Claude Code is using {names[stated]}."
    if email:
        return stated, f"Claude Code is using {email}, which is not in Lifeline. Add it with lifeline adopt."
    return stated, "Claude Code is using an account that is not in Lifeline. Add it with lifeline adopt."


def _readings(wiring, accounts):
    if not os.path.lexists(wiring.history_path):
        return {}, True
    try:
        store = history.History(wiring.history_path)
        return {account.uuid: store.latest(account.uuid) for account in accounts}, True
    except Exception:
        return {}, False


def _reading_lines(reading, now, tz):
    if reading is None:
        return [NO_READING]
    when = _moment(reading.at, tz)
    used = []
    if reading.five is not None:
        used.append(f"{_whole(reading.five)}% of its 5 hours")
    if reading.week is not None:
        used.append(f"{_whole(reading.week)}% of its week")
    if used:
        lines = [f"At {when} it had used {' and '.join(used)}."]
    else:
        lines = [f"The reading at {when} held no usage figures."]
    resets = []
    if reading.five_reset is not None:
        if reading.five_reset > now:
            resets.append(f"Its 5 hours reset {clock.when(reading.five_reset, now, tz)}.")
        else:
            resets.append("Its 5 hours have reset since then.")
    if reading.week_reset is not None:
        if reading.week_reset > now:
            resets.append(f"Its week resets {clock.when(reading.week_reset, now, tz)}.")
        else:
            resets.append("Its week has reset since then.")
    if resets:
        lines.append(" ".join(resets))
    return lines


def _row(account, live):
    plan = _plan_of(account)
    name = _shown(account.name)
    text = f"{account.key}. {name} is on the {plan} plan." if plan else f"{account.key}. {name} is on an unknown plan."
    if live is not None and account.uuid == live:
        text += " It is in use now."
    return text


def _last_switch_lines(wiring, names):
    try:
        note = _read_note(_note_path(wiring))
    except _Damaged:
        return [NOTE_DAMAGED]
    if note is None:
        return []
    origin = names.get(note["from_uuid"], switching.OUTSIDER)
    target = names.get(note["to_uuid"], switching.OUTSIDER)
    when = _note_time(note, wiring.tz)
    if when is None:
        return [f"The last switch moved Claude Code from {origin} to {target}."]
    return [f"The last switch, at {when}, moved Claude Code from {origin} to {target}."]


def _status(wiring, command):
    accounts = sorted(_accounts(wiring), key=lambda account: account.key)
    names = {account.uuid: _shown(account.name) for account in accounts}
    live, head = _live(wiring, accounts, names)
    lines = [head]
    if not accounts:
        lines.append(NO_ACCOUNTS)
        _say(wiring, lines)
        return 0
    readings, readable = _readings(wiring, accounts)
    now = wiring.now()
    for account in accounts:
        lines.append(_row(account, live))
        if readable:
            lines.extend(INDENT + line for line in _reading_lines(readings.get(account.uuid), now, wiring.tz))
    if not readable:
        lines.append(NO_HISTORY)
    lines.extend(_last_switch_lines(wiring, names))
    _say(wiring, lines)
    return 0


class _Login:
    def __init__(self, wiring, email, account=None):
        self.wiring = wiring
        self.email = email
        self.account = account
        self.problem = None
        self.guard = None

    def _stopped(self):
        return self.guard is not None and self.guard.pressed

    def __call__(self, folder):
        command = list(LOGIN_COMMAND)
        if self.email:
            command += ["--email", self.email]
        env = login_environment(folder, self.wiring.user)
        watch = None
        if self.wiring.system != "darwin":
            hint = LOGIN_HINT_PASTE
        else:
            try:
                watch = _PageWatch(self.wiring, server.private_page_env(folder, env))
            except OSError:
                self.problem = LOGIN_NOT_STARTED
                raise
            hint = _hint(self.wiring, self.email, self.account)
        _say(self.wiring, [hint])
        if watch is not None:
            watch.start()
        try:
            done = self.wiring.run_process(command, env=env, timeout=LOGIN_TIMEOUT)
        except FileNotFoundError:
            self.problem = NO_LOGIN_COMMAND
            raise
        except subprocess.TimeoutExpired:
            self.problem = LOGIN_STOPPED if self._stopped() else LOGIN_TOO_LONG
            raise
        except OSError:
            self.problem = LOGIN_NOT_STARTED
            raise
        finally:
            if watch is not None:
                watch.stop()
        code = getattr(done, "returncode", None)
        if code != 0:
            self.problem = LOGIN_STOPPED if self._stopped() else f"Claude Code's sign-in command stopped with code {code}."
            raise _LoginFailed()

    def reasons(self):
        return [self.problem] if self.problem else []


def _hint(wiring, email, account=None):
    browser = _browser(wiring)
    if email:
        opens = OPENS_FOR.format(email=email, browser=browser) if browser else OPENS_FOR_DEFAULT.format(email=email)
    else:
        opens = OPENS_IN.format(browser=browser) if browser else OPENS_IN_DEFAULT
    if account is None:
        parts = [opens] + ([] if email else [SIGN_IN_ADDING]) + [CHOOSE_ADDING]
    else:
        organization = account.organization
        choose = addtext.choose(organization, CHOOSE_ITS_OWN)
        parts = [opens] + ([] if email else [SIGN_IN_AS.format(name=_shown(account.name))]) + [choose]
    return " ".join(parts)


class _PageWatch:
    def __init__(self, wiring, page):
        self.wiring = wiring
        self.page = page
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name="lifeline-page", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.done.set()
        self.thread.join(5)

    def _run(self):
        while not self.done.is_set():
            url = devbox._read_page(self.page)
            if url:
                _say(self.wiring, [self._open(url)])
                return
            self.done.wait(PAGE_POLL)

    def _open(self, url):
        try:
            devbox.signin_port(url)
        except ValueError:
            return PAGE_NOT_TRUSTED
        opener = self.wiring.open_browser or devbox.open_default
        try:
            browser, opened = opener(url)
        except Exception:
            browser, opened = None, False
        if not opened:
            return PAGE_NOT_OPENED.format(url=url)
        return PAGE_OPENED.format(browser=browser) if browser else PAGE_OPENED_DEFAULT


def _signin_options(wiring):
    return dict(
        runner=wiring.runner,
        store=wiring.store,
        registry=wiring.registry,
        profile_fn=wiring.profile_fn,
        user=wiring.user,
        temp_root=wiring.temp_root,
        live_slot=wiring.slot,
        home=wiring.home,
        lock_path=wiring.lock_path,
        saved_in=wiring.saved_in,
        **_given(lock_timeout=wiring.lock_timeout),
    )


def _known_names(wiring):
    path = getattr(wiring.registry, "path", None)
    known = accounts_module.load_known(Path(path).parent / accounts_module.KNOWN_NAME) if path is not None else []

    def names_for(details):
        entry = accounts_module.known_entry(known, details.get("emailAddress"), accounts_module.organization_kind(details))
        return (entry["name"], entry["short"], entry["full"]) if entry is not None else None

    return names_for


def _add(wiring, command):
    login = _Login(wiring, command.options.get("--email"))
    try:
        with _Interrupts() as guard:
            login.guard = guard
            account = enroll.enroll(
                run_login=login,
                name=command.options.get("--name"),
                short=command.options.get("--short"),
                key=command.options.get("--number"),
                names_for=_known_names(wiring),
                **_signin_options(wiring),
            )
    except enroll.EnrollError as error:
        raise _Refusal(_with_hint(_failed(error, login, "add"), wiring, isinstance(error, enroll.AlreadyHas))) from None
    except SlotError as error:
        raise _Refusal(["Lifeline could not save the new sign-in in the Keychain, so nothing was added."] + _notes(error)) from None
    _say(wiring, [f"Lifeline added {_shown(account.name)} as number {account.key}.{_plan_sentence(account)}"])
    return 0


def _resign(wiring, command):
    account = _by_number(wiring, command.words[0])
    login = _Login(wiring, command.options.get("--email"), account)
    try:
        with _Interrupts() as guard:
            login.guard = guard
            updated = enroll.resign(account.uuid, run_login=login, **_signin_options(wiring))
    except enroll.EnrollError as error:
        raise _Refusal(_failed(error, login, "resign")) from None
    except SlotError as error:
        raise _Refusal(["Lifeline could not save the new sign-in in the Keychain, so nothing was changed."] + _notes(error)) from None
    _say(wiring, [f"Lifeline has a new sign-in for {_shown(updated.name)}."])
    return 0


def _already_added(wiring, accounts):
    try:
        item = wiring.slot.read()
        uuid = switching.identify_live(
            item,
            profile_fn=wiring.profile_fn,
            home=wiring.home,
            record_path=switching.record_path_for(wiring.lock_path),
            registry=wiring.registry,
        )
    except Exception:
        return None
    return next((account for account in accounts if account.uuid == uuid), None)


def _naming_refusal(account):
    name = _shown(account.name)
    return (
        f"{name} is already in Lifeline as number {account.key}, so nothing was changed. "
        f"Run lifeline rename {account.key} to change its names."
    )


def _adopt(wiring, command):
    accounts = _accounts(wiring)
    before = {account.uuid for account in accounts}
    naming = any(option in command.options for option in NAMING_OPTIONS)
    if naming:
        existing = _already_added(wiring, accounts)
        if existing is not None:
            raise _Refusal([_naming_refusal(existing)])
    try:
        with _Interrupts():
            account = enroll.adopt_live(
                slot=wiring.slot,
                registry=wiring.registry,
                profile_fn=wiring.profile_fn,
                home=wiring.home,
                lock_path=wiring.lock_path,
                name=command.options.get("--name"),
                short=command.options.get("--short"),
                key=command.options.get("--number"),
                names_for=_known_names(wiring),
                **_given(lock_timeout=wiring.lock_timeout),
            )
    except enroll.EnrollError as error:
        raise _Refusal(_sentences(error)) from None
    name = _shown(account.name)
    if account.uuid not in before:
        message = f"Lifeline added {name} as number {account.key}.{_plan_sentence(account)} Claude Code uses it now."
        _say(wiring, [message])
        return 0
    lines = [f"Lifeline updated {name}, number {account.key}, with the account details Claude Code holds now."]
    if naming:
        lines.append(f"It did not use the names or number you gave. Run lifeline rename {account.key} to change its names.")
    _say(wiring, lines)
    return 0


def _rename(wiring, command):
    number, name, short = command.words
    with _Interrupts(), ExitStack() as stack:
        try:
            stack.enter_context(writelock.held(wiring.lock_path, **_given(timeout=wiring.lock_timeout)))
        except writelock.WriteLockTimeout:
            raise _Refusal([switching.LIFELINE_BUSY]) from None
        except OSError:
            raise _Refusal([switching.NO_LOCK_FILE]) from None
        account = _by_number(wiring, number)
        updated = replace(account, name=name, short=short, full=name)
        try:
            wiring.registry.upsert(updated)
        except (RegistryError, OSError):
            raise _Refusal([REGISTRY_UNSAVED]) from None
    message = f"Account {number} is now called {name}, or {short} for short."
    _say(wiring, [message])
    return 0


def _switch_options(wiring):
    return dict(
        slot=wiring.slot,
        store=wiring.store,
        registry=wiring.registry,
        home=wiring.home,
        profile_fn=wiring.profile_fn,
        lock_path=wiring.lock_path,
        backups=wiring.backups,
        **_given(lock_timeout=wiring.lock_timeout, session_timeout=wiring.session_timeout, settle=wiring.settle),
    )


def _result_lines(wiring, result, opening):
    target = _name_of(wiring, result.to_uuid)
    origin = _name_of(wiring, result.from_uuid)
    lines = [opening] if opening else []
    lines.append(f"Claude Code now uses {target}. It was on {origin}.")
    if result.confirmed is True:
        lines.append(f"Anthropic confirmed that the new sign-in belongs to {target}.")
    elif result.confirmed is False:
        account = _account_of(wiring, result.to_uuid)
        again = f" with lifeline resign {account.key}" if account is not None else ""
        lines.append(f"Anthropic says the new sign-in does not belong to {target}. Sign in to {target} again{again}.")
    else:
        lines.append(UNCONFIRMED_SIGNIN)
    note = _text(getattr(result, "note", None))
    if note:
        lines.append(note)
    if result.leftover:
        lines.append(f"A spare copy of the sign-in for {target} may be left in Lifeline.")
    return target, lines


def _skip_sentence(guard):
    if guard.first == signal.SIGINT:
        return WATCH_SKIPPED
    if guard.first == getattr(signal, "SIGHUP", None):
        return WATCH_SKIPPED_HANGUP
    return WATCH_SKIPPED_SIGNAL


def _run_switch(wiring, action, watch, opening=None):
    with _Interrupts() as guard:
        result = action(_switch_options(wiring))
        target, lines = _result_lines(wiring, result, opening)
        try:
            _save_note(_note_path(wiring), result.from_uuid, result.to_uuid, wiring.now())
        except (OSError, ValueError):
            lines.append(NOTE_UNSAVED)
        _say(wiring, lines)
        if guard.asked:
            _say(wiring, [_skip_sentence(guard)])
            return 0
        if not watch:
            return 0
        return _watch(wiring, result, target, guard)


def _settings_label(wiring):
    try:
        label = read_oauth_account(wiring.home / switching.ACCOUNT_FILE_NAME)
    except AccountFileError:
        return None, False
    stated = label.get("accountUuid") if isinstance(label, dict) else None
    return (label if isinstance(stated, str) and stated else None), True


def _where_now(wiring, result):
    record = switching.read_record(switching.record_path_for(wiring.lock_path))
    label, readable = _settings_label(wiring)
    try:
        accounts = wiring.registry.load()
    except Exception:
        accounts = None
    stated = identity(accounts, *stated_pair(label)) if label is not None and accounts is not None else None
    unclear = label is not None and stated is None
    if record == result.to_uuid and stated == result.to_uuid:
        return True, None
    names = {account.uuid: _shown(account.name) for account in accounts or []}
    email = _text(label.get("emailAddress")) if label is not None else None

    def called(uuid):
        return names.get(uuid) or (email if uuid == stated else None) or switching.OUTSIDER

    if record is not None and stated == record:
        return False, f"Lifeline's record and Claude Code's settings both name {called(record)}."
    recorded = f"Lifeline's record names {called(record)}" if record is not None else "Lifeline's record names no account"
    if not readable:
        settings = "Lifeline could not read Claude Code's settings"
    elif unclear:
        settings = f"Lifeline cannot tell which of its accounts Claude Code's settings name for {email or 'an account'}"
    elif stated is None:
        settings = "Claude Code's settings name no account"
    else:
        settings = f"Claude Code's settings name {called(stated)}"
    return False, f"{recorded}, and {settings}."


def _end_lines(wiring, result, target, stopped):
    stands, files = _where_now(wiring, result)
    if stands:
        return [f"{WATCH_STOPPED} The switch to {target} stands."] if stopped else [f"The switch to {target} stands."]
    return [WATCH_STOPPED, files] if stopped else [files]


def _rescue_line(wiring, result, target, saved):
    if saved:
        return f"Lifeline saved the sign-in for {target} in its recovery folder, so it is not lost."
    account = _account_of(wiring, result.to_uuid)
    command = f" with lifeline resign {account.key}" if account is not None else ""
    return f"Lifeline could not save the sign-in for {target}. If Claude Code is not on {target}, sign in to it again{command}."


def _halt_opening(signum):
    if signum == signal.SIGINT:
        return WATCH_STOPPED
    if signum == getattr(signal, "SIGHUP", None):
        return HALT_HANGUP
    return HALT_SIGNAL


def _rescue(watcher):
    try:
        return watcher.rescue() is True
    except Exception:
        return False


def _halt(wiring, watcher, shift, result, target, guard, signum, last_try):
    with _Held(guard):
        if last_try:
            shift[0] = WATCH_SECONDS
            try:
                _say(wiring, watcher.poll())
            except Exception:
                pass
        saved = _rescue(watcher)
    line = _rescue_line(wiring, result, target, saved)
    if signum is None:
        _complain(wiring, [line])
        return 1
    _say(wiring, [_halt_opening(signum), line])
    return 128 + signum


def _keep_watch(wiring, watcher, guard):
    while watcher.active:
        _say(wiring, watcher.poll())
        if not watcher.active:
            return
        guard.sleep(wiring.sleep, POLL_SECONDS)


def _finish(wiring, watcher, shift, target):
    shift[0] = WATCH_SECONDS
    told = False
    while True:
        _say(wiring, watcher.poll())
        if not watcher.active:
            return
        if not told:
            _say(
                wiring,
                [
                    f"Lifeline is still saving the sign-in for {target}. "
                    "If you press Control-C again, Lifeline will put it in its recovery folder and stop."
                ],
            )
            told = True
        wiring.sleep(POLL_SECONDS)


def _after_press(wiring, watcher, shift, result, target, guard):
    try:
        _finish(wiring, watcher, shift, target)
    except KeyboardInterrupt as stop:
        signum = _signal_of(stop)
        return _halt(wiring, watcher, shift, result, target, guard, signum, signum != signal.SIGINT)
    except Exception:
        _complain(wiring, [WATCH_PROBLEM])
        return _halt(wiring, watcher, shift, result, target, guard, None, False)
    _say(wiring, _end_lines(wiring, result, target, True))
    return 0


def _watch(wiring, result, target, guard):
    shift = [0.0]

    def clock():
        return wiring.monotonic() + shift[0]

    watcher = switching.Watcher(
        wiring.slot,
        wiring.store,
        wiring.registry,
        result.to_uuid,
        wiring.profile_fn,
        clock=clock,
        lifetime=WATCH_SECONDS / 60,
        seed=result,
        home=wiring.home,
        lock_path=wiring.lock_path,
        backups=wiring.backups,
        wall_clock=wiring.wall,
        **_given(lock_timeout=wiring.watch_lock_timeout, session_timeout=wiring.watch_session_timeout, settle=wiring.settle),
    )
    _say(wiring, [WATCH_START, WATCH_HINT])
    try:
        _keep_watch(wiring, watcher, guard)
    except KeyboardInterrupt as stop:
        signum = _signal_of(stop)
        guard.note(signum)
        if guard.count > 1:
            return _halt(wiring, watcher, shift, result, target, guard, signum, signum != signal.SIGINT)
        if signum != signal.SIGINT:
            return _halt(wiring, watcher, shift, result, target, guard, signum, True)
        return _after_press(wiring, watcher, shift, result, target, guard)
    except Exception:
        _complain(wiring, [WATCH_PROBLEM])
        return _halt(wiring, watcher, shift, result, target, guard, None, True)
    _say(wiring, [WATCH_OVER])
    return 0


def _switch(wiring, command):
    account = _by_number(wiring, command.words[0])
    return _run_switch(
        wiring,
        lambda options: switching.switch(account.uuid, **options),
        "--no-watch" not in command.flags,
    )


def _undo(wiring, command):
    try:
        note = _read_note(_note_path(wiring))
    except _Damaged:
        raise _Refusal([NOTE_DAMAGED_UNDO]) from None
    if note is None:
        raise _Refusal([NO_UNDO])
    previous = switching.SwitchResult(note["from_uuid"], note["to_uuid"], None)
    return _run_switch(
        wiring,
        lambda options: switching.undo(previous, **options),
        "--no-watch" not in command.flags,
        UNDONE,
    )


def _plain_file(entry):
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _read_session(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    chunks = []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > SESSION_LIMIT:
            return None
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > SESSION_LIMIT:
                return None
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _started(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _valid_pid(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**31


def _session_order(data):
    started = _started(data.get("startedAt"))
    return (started is None, started.timestamp() if started is not None else 0, _text(data.get("name")) or "")


def _session_lines(index, data, wiring):
    name = _text(data.get("name")) or "A session with no name"
    cwd = _text(data.get("cwd"))
    started = _started(data.get("startedAt"))
    where = f"in {cwd}" if cwd else "in an unknown folder"
    when = f"started at {_moment(started, wiring.tz)}" if started is not None else "started at an unknown time"
    pid = data.get("pid")
    if _valid_pid(pid):
        alive = wiring.pid_alive(pid)
        if alive is True:
            process = f"A process with number {pid} is running."
        elif alive is False:
            process = f"No process with number {pid} is running."
        else:
            process = f"Lifeline could not check for a process with number {pid}."
    else:
        process = "It names no valid process."
    kind = _text(data.get("kind")) or "unknown"
    status = _text(data.get("status")) or "unknown"
    ident = _text(data.get("sessionId"))
    return [
        f"{index}. {name}, {where}, {when}.",
        f"{INDENT}{process} Its kind is {kind}, and its status is {status}.",
        f"{INDENT}Claude Code calls it {ident}." if ident else f"{INDENT}Claude Code gives it no name.",
    ]


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _sessions(wiring, command):
    folder = wiring.home.joinpath(*SESSIONS_FOLDER)
    entries = []
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                if entry.name.endswith(".json"):
                    entries.append((entry.name, entry.path, _plain_file(entry)))
    except FileNotFoundError:
        entries = []
    except OSError:
        raise _Refusal([SESSIONS_UNREADABLE]) from None
    found, unreadable = [], 0
    for _, path, plain in sorted(entries):
        data = _read_session(path) if plain else None
        if data is None:
            unreadable += 1
        else:
            found.append(data)
    found.sort(key=_session_order)
    lines = [f"Lifeline found {_plural(len(found), 'Claude Code session')}." if found else NO_SESSIONS]
    for index, data in enumerate(found, 1):
        lines.extend(_session_lines(index, data, wiring))
    if unreadable:
        lines.append(f"Lifeline could not read {_plural(unreadable, 'session file')}.")
    if found:
        lines.append(SESSION_ACCOUNTS)
    _say(wiring, lines)
    return 0


class _ServiceGone(Exception):
    pass


class _ServiceSilent(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("lifeline", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.unix_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class ServiceClient:
    def __init__(self, path):
        self.path = path
        self.sent = False

    @classmethod
    def find(cls, path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(PROBE_SECONDS)
            probe.connect(path)
        except (OSError, ValueError):
            return None
        finally:
            probe.close()
        return cls(path)

    def call(self, method, route, body=None, timeout=REQUEST_SECONDS):
        connection = _UnixConnection(self.path, timeout)
        try:
            try:
                connection.connect()
            except OSError:
                if self.sent:
                    raise _ServiceSilent() from None
                raise _ServiceGone() from None
            self.sent = True
            payload = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            try:
                connection.request(method, route, body=payload, headers=headers)
                response = connection.getresponse()
                raw = response.read(REPLY_LIMIT + 1)
                status = response.status
            except (OSError, http.client.HTTPException):
                raise _ServiceSilent() from None
        finally:
            connection.close()
        if len(raw) > REPLY_LIMIT:
            raise _ServiceSilent()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _ServiceSilent() from None
        if not isinstance(data, dict):
            raise _ServiceSilent()
        return status, data


def _said(value):
    text = _text(value)
    return text if text and text[-1] in ".!?" else None


def _ask(client, method, route, body=None, timeout=REQUEST_SECONDS):
    status, data = client.call(method, route, body, timeout)
    if 200 <= status < 300:
        return data
    raise _Refusal([_said(data.get("error")) or SERVICE_REFUSED])


def _wait_stopped(stop):
    return WAIT_STOPPED if _signal_of(stop) == signal.SIGINT else WAIT_STOPPED_SIGNAL


def _stopped_waiting(stop):
    return _Refusal([_wait_stopped(stop)], 128 + _signal_of(stop))


def _moment_of(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _figure(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


@dataclass
class _ServiceReading:
    five: float | None
    week: float | None
    five_reset: datetime | None
    week_reset: datetime | None
    at: datetime


def _service_reading(row):
    at = _moment_of(row.get("reading_at"))
    if at is None:
        return None
    return _ServiceReading(
        _figure(row.get("five")),
        _figure(row.get("week")),
        _moment_of(row.get("five_reset")),
        _moment_of(row.get("week_reset")),
        at,
    )


def _service_number(row):
    number = row.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else 99


def _service_rows(data):
    rows = data.get("accounts")
    if not isinstance(rows, list):
        return []
    return sorted((row for row in rows if isinstance(row, dict)), key=_service_number)


def _users(row):
    users = row.get("in_use")
    return users if isinstance(users, list) else []


def _job_sentence(job, names):
    kind = job.get("kind")
    if kind == "add":
        return "Lifeline in the background is still adding an account."
    if kind == "adopt":
        return "Lifeline in the background is still adding the account Claude Code uses."
    if kind == "devbox-adopt":
        return "Lifeline in the background is still taking over the other computer's own sign-in."
    name = names.get(job.get("uuid"), switching.OUTSIDER)
    if kind in ("devbox-add", "devbox-resign"):
        return f"Lifeline in the background is still signing the other computer in to {name}."
    return f"Lifeline in the background is still signing in to {name} again."


def _service_status_lines(wiring, data):
    rows = _service_rows(data)
    names = {row.get("uuid"): _text(row.get("name")) or switching.OUTSIDER for row in rows}
    mac = [row for row in rows if "mac" in _users(row)]
    lines = [f"Claude Code is using {names[mac[0].get('uuid')]}." if mac else SERVICE_UNKNOWN_ACCOUNT]
    view = data.get("mac") if isinstance(data.get("mac"), dict) else {}
    for key in ("head", "sub"):
        text = _said(view.get(key))
        if text and text not in lines:
            lines.append(text)
    devbox = [row for row in rows if "devbox" in _users(row)]
    if devbox:
        lines.append(f"The other computer is using {names[devbox[0].get('uuid')]}.")
    if not rows:
        lines.append(NO_ACCOUNTS)
        return lines
    now = wiring.now()
    for row in rows:
        name = names[row.get("uuid")]
        plan = _text(row.get("plan"))
        number = _service_number(row)
        head = f"{number}. {name} is on the {plan} plan." if plan else f"{number}. {name} is on an unknown plan."
        if "mac" in _users(row):
            head += " It is in use now."
        if "devbox" in _users(row):
            head += " The other computer uses it."
        lines.append(head)
        details = []
        if row.get("needs_signin") is True:
            details.append("It needs a new sign-in.")
        details.extend(_reading_lines(_service_reading(row), now, wiring.tz))
        if row.get("cannot_read") is True:
            details.append("Its usage cannot be read right now.")
        paused = _moment_of(row.get("paused_until"))
        if paused is not None and paused > now:
            details.append(viewmodel.paused_sentence(row.get("reading_error"), paused, now, wiring.tz))
        lines.extend(INDENT + line for line in details)
    countdown = data.get("countdown")
    if isinstance(countdown, dict) and countdown.get("target_uuid") in names:
        lines.append(f"Lifeline is about to switch this Mac to {names[countdown['target_uuid']]}.")
    jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
    for job in jobs:
        if isinstance(job, dict) and job.get("state") == "running":
            lines.append(_job_sentence(job, names))
    lines.extend(_last_switch_lines(wiring, names))
    return lines


def _routed_status(wiring, command, client):
    data = _ask(client, "GET", "/state")
    _say(wiring, _service_status_lines(wiring, data))
    return 0


def _requested(wiring, guard, ask):
    box = {}

    def work():
        try:
            box["value"] = ask()
        except BaseException as error:
            box["error"] = error

    worker = threading.Thread(target=work, name="lifeline-request", daemon=True)
    worker.start()
    told = False
    while worker.is_alive():
        worker.join(WAIT_STEP_SECONDS)
        if guard.asked and not told:
            _say(wiring, [SERVICE_FINISHES])
            told = True
    if "error" in box:
        raise box["error"]
    return box["value"]


def _routed_change(wiring, command, client, route, body, opening):
    try:
        with _Interrupts() as guard:
            data = _requested(wiring, guard, lambda: _ask(client, "POST", route, body, CHANGE_SECONDS))
    except KeyboardInterrupt as stop:
        raise _stopped_waiting(stop) from None
    lines = [opening] if opening else []
    lines.append(_said(data.get("message")) or SERVICE_REFUSED)
    note = _said(data.get("note"))
    if note:
        lines.append(note)
    lines.append(SERVICE_ALWAYS_WATCHES if "--no-watch" in command.flags else SERVICE_WATCHES)
    _say(wiring, lines)
    return 0


def _routed_switch(wiring, command, client):
    return _routed_change(wiring, command, client, "/switch", {"number": command.words[0], "via": "cli"}, None)


def _routed_undo(wiring, command, client):
    return _routed_change(wiring, command, client, "/undo", {"via": "cli"}, UNDONE)


def _follow_job(wiring, client, job_id, guard):
    deadline = wiring.monotonic() + JOB_WAIT_SECONDS
    while True:
        data = _ask(client, "GET", "/state")
        jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
        job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == job_id), None)
        if job is None:
            raise _Refusal([JOB_LOST])
        if job.get("state") != "running":
            return job
        if wiring.monotonic() >= deadline:
            raise _Refusal([JOB_TOO_LONG])
        guard.sleep(wiring.sleep, JOB_POLL_SECONDS)


def _routed_job(wiring, client, route, body, opening, after=None):
    try:
        with _Interrupts() as guard:
            reply = _ask(client, "POST", route, body)
            job_id = reply.get("job")
            if not isinstance(job_id, str) or not job_id:
                raise _Refusal([SERVICE_REFUSED])
            _say(wiring, opening)
            job = _follow_job(wiring, client, job_id, guard)
    except KeyboardInterrupt as stop:
        raise _stopped_waiting(stop) from None
    result = _said(job.get("result")) or SERVICE_REFUSED
    if job.get("state") != "done":
        raise _Refusal(_with_hint([result], wiring, job.get("already_has") is True))
    _say(wiring, [result] + (after(job) if after is not None else []))
    return 0


def _login_opening(email):
    if email:
        return [f"Lifeline in the background will open the sign-in page for {email} in your browser. Sign in there, then come back here."]
    return [SERVICE_LOGIN]


def _routed_add(wiring, command, client):
    body = {key[2:]: command.options[key] for key in ("--email", "--name", "--short", "--number") if key in command.options}
    return _routed_job(wiring, client, "/add", body, _login_opening(command.options.get("--email")))


def _routed_resign(wiring, command, client):
    account = _by_number(wiring, command.words[0])
    email = command.options.get("--email")
    body = {"email": email} if email else {}
    return _routed_job(wiring, client, f"/resign/{account.uuid}", body, _login_opening(email))


def _routed_adopt(wiring, command, client):
    accounts = _accounts(wiring)
    before = {account.uuid for account in accounts}
    naming = any(option in command.options for option in NAMING_OPTIONS)
    if naming:
        existing = _already_added(wiring, accounts)
        if existing is not None:
            raise _Refusal([_naming_refusal(existing)])
    body = {key[2:]: command.options[key] for key in NAMING_OPTIONS if key in command.options}

    def after(job):
        if not naming or job.get("uuid") not in before:
            return []
        account = _account_of(wiring, job.get("uuid"))
        number = account.key if account is not None else "N"
        return [f"It did not use the names or number you gave. Run lifeline rename {number} to change its names."]

    return _routed_job(wiring, client, "/adopt", body, [], after)


def _routed_rename(wiring, command, client):
    number, name, short = command.words
    account = _by_number(wiring, number)
    body = {"name": name, "short": short}
    try:
        with _Interrupts() as guard:
            _requested(wiring, guard, lambda: _ask(client, "POST", f"/rename/{account.uuid}", body, CHANGE_SECONDS))
    except KeyboardInterrupt as stop:
        raise _stopped_waiting(stop) from None
    message = f"Account {number} is now called {name}, or {short} for short."
    _say(wiring, [message])
    return 0


def _routed_said(wiring, client, route):
    try:
        with _Interrupts() as guard:
            data = _requested(wiring, guard, lambda: _ask(client, "POST", route, None, CHANGE_SECONDS))
    except KeyboardInterrupt as stop:
        raise _stopped_waiting(stop) from None
    _say(wiring, [_said(data.get("message")) or SERVICE_REFUSED])
    return 0


def _needs_service(wiring, command):
    raise _Refusal([NEEDS_SERVICE])


ROUTED_HANDLERS = {
    "status": _routed_status,
    "switch": _routed_switch,
    "undo": _routed_undo,
    "add": _routed_add,
    "adopt": _routed_adopt,
    "resign": _routed_resign,
    "rename": _routed_rename,
    "remove": lambda wiring, command, client: _routed_said(wiring, client, f"/remove/{_by_number(wiring, command.words[0]).uuid}"),
    "reset": lambda wiring, command, client: _routed_said(wiring, client, "/reset"),
}


def _pair(reset=False):
    from lifeline import pairing

    code = devbox.keeper(runner=run_security if sys.platform == "darwin" else None).new_code(reset)
    host = pairing.listen_host()
    lines = [
        f"This computer is {pairing.own_name()}, at {pairing.own_address() if host == pairing.EVERY_NETWORK else host}:{pairing.PORT}.",
        f"Its pairing code is {code}. It works once, within an hour.",
        "On the Mac, open Lifeline's Alerts and settings, enter the address and code under Another computer, and press Add a computer.",
        "To pair over another address, such as one on a VPN (Tailscale is one option), install again with --listen and that address.",
    ]
    if reset:
        lines.insert(0, "The Mac paired before can no longer reach this computer.")
    try:
        public = ipaddress.ip_address(host).is_global
    except ValueError:
        public = False
    if host == pairing.EVERY_NETWORK or public:
        where = "every network this computer joins" if host == pairing.EVERY_NETWORK else f"the public address {host}"
        lines.append(f"Lifeline listens on {where}, and the link is signed but not encrypted. An address only your own network or VPN can reach is safer.")
    _write(sys.stdout, lines)
    return 0


def _serve(remote=False):
    try:
        from lifeline import server

        return server.main(remote=remote)
    except KeyboardInterrupt:
        return 130
    except Exception:
        _write(sys.stderr, [SERVICE_START_PROBLEM])
        return 1


def _stopped_sentence(signum):
    if signum == signal.SIGINT:
        return STOPPED
    if signum == getattr(signal, "SIGHUP", None):
        return STOPPED_HANGUP
    return STOPPED_SIGNAL


def _client(wiring, command):
    if command.name not in wiring.routed or wiring.service is None:
        return None
    try:
        return wiring.service()
    except Exception:
        return None


def _dispatch(wiring, command):
    client = _client(wiring, command)
    if client is not None:
        try:
            return ROUTED_HANDLERS[command.name](wiring, command, client)
        except _ServiceGone:
            pass
        except _ServiceSilent:
            if command.name != "status":
                raise _Refusal([SERVICE_SILENT]) from None
            _say(wiring, [SERVICE_FALLBACK])
    return HANDLERS[command.name](wiring, command)


def _devbox_link_for(wiring):
    if wiring.system != "darwin":
        raise _Refusal([DEVBOX_ON_MAC])
    try:
        link = wiring.devbox_link() if wiring.devbox_link is not None else None
    except ValueError:
        link = None
    if link is None or isinstance(link, server.NullLink):
        raise _Refusal([devbox.NO_DEVBOX_LINK])
    return link


def _devbox_ended(wiring, link, job_id, stop):
    devbox.stop_remote(link, job_id)
    outcome, data = devbox.wait_ended(link, job_id, wiring.sleep, wiring.monotonic)
    if outcome == devbox.GONE:
        raise _Refusal([devbox.DEVBOX_JOB_LOST], 128 + _signal_of(stop))
    if data is None:
        raise _Refusal([DEVBOX_STILL_STOPPING], 128 + _signal_of(stop))
    if data.get("state") == "done":
        result = _said(data.get("result"))
        if result:
            _say(wiring, [result])
            return 0
    raise _Refusal([DEVBOX_STOPPED], 128 + _signal_of(stop))


def _hand_to_service(wiring, token):
    try:
        client = wiring.service() if wiring.service is not None else None
        if client is None:
            return False
        status, _ = client.call("POST", "/devbox/unsure", {"request": token})
    except Exception:
        return False
    return status == 202


def _devbox_job(wiring, route, body, account=None):
    link = _devbox_link_for(wiring)
    accounts = _accounts(wiring)
    name = _shown(account.name) if account is not None else None
    email = body.get("email")
    started = {}

    def on_job(job_id):
        started["job"] = job_id
        if account is not None:
            _say(wiring, [DEVBOX_STARTING.format(name=name)])

    def on_page(url, opened):
        lines = [DEVBOX_PRESS_EMAIL.format(email=email) if email else DEVBOX_PRESS.format(name=name)]
        if not opened:
            lines.append(DEVBOX_OPEN_IT.format(url=url))
        _say(wiring, lines)

    def on_unsure(token):
        started["kept"] = _hand_to_service(wiring, token)

    try:
        with _Interrupts() as guard:
            job = devbox.drive_signin(
                link,
                route,
                body,
                accounts=accounts,
                relay=wiring.relay,
                open_page=wiring.open_page,
                sleep=lambda seconds: guard.sleep(wiring.sleep, seconds),
                clock=wiring.monotonic,
                wants_page=account is not None,
                on_job=on_job,
                on_page=on_page,
                on_unsure=on_unsure,
            )
    except devbox.SigninRefused as refused:
        said = refused.sentence
        if said == devbox.DEVBOX_UNSURE and not started.get("kept"):
            said = DEVBOX_UNSURE_ALONE
        raise _Refusal([said] + ([DEVBOX_STOP_HINT] if refused.job else [])) from None
    except KeyboardInterrupt as stop:
        if "job" not in started:
            if "kept" in started:
                stop.add_note(STOPPED_KEPT if started["kept"] else STOPPED_ALONE)
            raise
        return _devbox_ended(wiring, link, started["job"], stop)
    result = _said(job.get("result")) or DEVBOX_REFUSED
    if job.get("state") != "done":
        raise _Refusal([result])
    _say(wiring, [result])
    return 0


def _devbox_email(account, command):
    email = command.options.get("--email") or account.email
    try:
        return _email(email) if email else None
    except _Usage:
        return None


def _devbox_add(wiring, command):
    if wiring.system != "darwin":
        raise _Refusal([DEVBOX_ON_MAC])
    account = _by_number(wiring, command.words[0])
    body = {"uuid": account.uuid}
    email = _devbox_email(account, command)
    if email:
        body["email"] = email
    return _devbox_job(wiring, "/add", body, account)


def _devbox_resign(wiring, command):
    if wiring.system != "darwin":
        raise _Refusal([DEVBOX_ON_MAC])
    account = _by_number(wiring, command.words[0])
    email = _devbox_email(account, command)
    return _devbox_job(wiring, f"/resign/{account.uuid}", {"email": email} if email else {}, account)


def _devbox_adopt(wiring, command):
    return _devbox_job(wiring, "/adopt", {})


def _devbox_stop(wiring, command):
    link = _devbox_link_for(wiring)
    try:
        reply = devbox.stop_running(link)
    except devbox.SigninRefused as refused:
        raise _Refusal([refused.sentence]) from None
    if not reply.get("stopping"):
        _say(wiring, [devbox.NOTHING_RUNNING])
        return 0
    outcome, data = devbox.wait_ended(link, reply.get("job"), wiring.sleep, wiring.monotonic)
    if outcome == devbox.GONE:
        _say(wiring, [devbox.DEVBOX_JOB_LOST])
    elif data is None:
        _say(wiring, [DEVBOX_STILL_STOPPING])
    elif data.get("state") == "done" and _said(data.get("result")):
        _say(wiring, [_said(data.get("result"))])
    else:
        _say(wiring, [DEVBOX_STOPPED_THERE])
    return 0


HANDLERS = {
    "status": _status,
    "sessions": _sessions,
    "add": _add,
    "adopt": _adopt,
    "resign": _resign,
    "rename": _rename,
    "switch": _switch,
    "undo": _undo,
    "computer add": _devbox_add,
    "computer adopt": _devbox_adopt,
    "computer resign": _devbox_resign,
    "computer stop": _devbox_stop,
    "remove": _needs_service,
    "reset": _needs_service,
}


def main(argv=None, wiring=None):
    words = list(sys.argv[1:] if argv is None else argv)
    out = wiring.out if wiring is not None else sys.stdout
    err = wiring.err if wiring is not None else sys.stderr
    try:
        command = parse(words)
    except _Usage as problem:
        _write(err, [str(problem)])
        return 2
    if command.name == "help":
        _write(out, HELP)
        return 0
    if command.name == "serve":
        return _serve("--remote" in command.flags)
    if command.name == "pair":
        return _pair("--reset" in command.flags)
    try:
        if wiring is None:
            wiring = production()
    except Exception:
        _write(err, [START_PROBLEM])
        return 1
    if command.name in SWITCHING and not wiring.changes:
        _complain(wiring, [DEVBOX_REFUSAL])
        return 1
    try:
        return _dispatch(wiring, command)
    except _Refusal as refusal:
        _complain(wiring, refusal.lines)
        return refusal.code
    except (switching.SwitchError, enroll.EnrollError) as error:
        _complain(wiring, _sentences(error))
        return 1
    except RegistryError as error:
        _complain(wiring, [REGISTRY_PROBLEM] + _notes(error))
        return 1
    except KeyboardInterrupt as error:
        signum = _signal_of(error)
        _complain(wiring, [_stopped_sentence(signum)] + _notes(error))
        return 128 + signum
    except Exception as error:
        _complain(wiring, [UNEXPECTED] + _notes(error))
        return 1
