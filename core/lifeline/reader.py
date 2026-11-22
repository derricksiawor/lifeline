import copy
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lifeline import anthropic, clock, ledger, recovery, switch, writelock
from lifeline.accounts import labels, signin_end_text, stated
from lifeline.anthropic import CertificateFailed, NotAllowed, RateLimited, SignInDead, Unavailable, UnexpectedReply
from lifeline.store import StoreLeftover

OAUTH_KEY = "claudeAiOauth"
CACHE_KEY = "cachedUsageUtilization"
ACCOUNT_FILE_NAME = switch.ACCOUNT_FILE_NAME
STATE_NAME = "reader_state.json"
NORMAL_S = 300
CLIMBING_S = 180
MIN_GAP_S = 180
FAILURE_S = 300
MAX_WAIT_S = 86400
RENEW_MARGIN_S = 60
LIVE_MARGIN_S = 300
IDENTIFY_RETRY_S = 300
RECOVERY_RETRY_S = 60
LOCK_TIMEOUT_S = 5.0
SAVE_BACKOFF_S = (0.25, 1.0)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MILLISECOND = timedelta(milliseconds=1)

_UNSET = object()
NONE = "none"
KNOWN = "known"
UNKNOWN = "unknown"
SIGNIN = "signin"
BARRED = "barred"

NAMELESS = "an account in Lifeline"
UNCONFIRMED = (
    "Lifeline could not confirm which account Claude Code is using. "
    "Until it can, it will not read that account or renew any saved sign-in."
)
NO_ACCOUNTS = "Lifeline could not read its list of accounts, so it took no readings."
DEAD = "{name} needs a new sign-in."
NOT_ALLOWED = "Lifeline is not allowed to read the usage of {name}, so it stopped reading that account."
SPARE = (
    "Claude Code is using {name}, and Lifeline also holds a spare saved sign-in for it. "
    "Lifeline will not use or renew the spare."
)
PAUSED = "Readings for {name} are paused until {time}."
STORE_PAUSED = "Lifeline could not read its saved sign-in for {name}, so readings for it are paused until {time}."
UNWRITABLE_PAUSED = (
    "Lifeline could not write its saved sign-in for {name}, so it did not renew it. "
    "Readings for it are paused until {time}."
)
NO_RECOVERY_PAUSED = (
    "Lifeline could not write to its recovery folder, so it did not renew the sign-in for {name}. "
    "Readings for it are paused until {time}."
)
GUARD_PAUSED = (
    "Lifeline could not check whether it may renew the sign-in for {name}, so it did not renew it. "
    "Readings for it are paused until {time}."
)
LOCK_PAUSED = (
    "Lifeline could not take its own lock to renew the sign-in for {name}, "
    "so readings for it are paused until {time}."
)
HISTORY_PAUSED = "Lifeline could not save a reading for {name}, so readings for it are paused until {time}."
KEPT_PAUSED = (
    "Lifeline renewed the sign-in for {name} but could not save it. "
    "The new sign-in is kept in Lifeline's recovery folder until it can be saved. "
    "Readings for it are paused until {time}."
)
HELD_PAUSED = (
    "Lifeline renewed the sign-in for {name} but could not save it or write it to its recovery folder. "
    "The new sign-in is only in memory, so it is lost if Lifeline stops before it is saved. "
    "Lifeline tries again every minute. Readings for it are paused until {time}."
)
RECOVERED = "Lifeline saved the renewed sign-in for {name} from its recovery folder."
SAVED_FROM_MEMORY = "Lifeline saved the renewed sign-in for {name}."
BAD_RECOVERY = "Lifeline could not read the renewed sign-in for {name} in its recovery folder, so it left the file there."
LEDGER_PROBLEM = (
    "Lifeline could not read or update its list of spent sign-ins at {path}, so it may stop renewing some accounts. "
    "Make that file a plain file you can read and write. If it is damaged, rename it, and Lifeline starts a new one."
)

DEAD_ERROR = "The sign-in was refused and needs to be replaced."
NOT_ALLOWED_ERROR = "The sign-in is not allowed to read the usage."
FAILED_ERROR = "Lifeline could not take a reading."
STORE_ERROR = "Lifeline could not read its saved sign-in."
UNWRITABLE_ERROR = "Lifeline could not write its saved sign-in."
NO_RECOVERY_ERROR = "Lifeline could not write to its recovery folder."
GUARD_ERROR = "Lifeline could not check whether it may renew the sign-in."
LOCK_ERROR = "Lifeline could not take its own lock."
HISTORY_ERROR = "Lifeline could not save the reading."
UNSAVED_ERROR = "Lifeline renewed the sign-in but could not save it yet."
READ_ERRORS = {
    RateLimited: "The usage address asked Lifeline to wait before it reads again.",
    Unavailable: "Lifeline could not reach the usage address.",
    UnexpectedReply: "The usage address sent a reply Lifeline could not read.",
    CertificateFailed: "Lifeline could not check the certificate of the usage address, so it did not connect.",
}
RENEW_ERRORS = {
    RateLimited: "The sign-in address asked Lifeline to wait before it renews again.",
    Unavailable: "Lifeline could not reach the sign-in address.",
    UnexpectedReply: "The sign-in address sent a reply Lifeline could not read.",
    CertificateFailed: "Lifeline could not check the certificate of the sign-in address, so it did not connect.",
}
KNOWN_ERRORS = frozenset(
    [
        DEAD_ERROR,
        NOT_ALLOWED_ERROR,
        FAILED_ERROR,
        STORE_ERROR,
        UNWRITABLE_ERROR,
        NO_RECOVERY_ERROR,
        GUARD_ERROR,
        LOCK_ERROR,
        HISTORY_ERROR,
        UNSAVED_ERROR,
        *READ_ERRORS.values(),
        *RENEW_ERRORS.values(),
    ]
)


@dataclass(frozen=True)
class AccountStatus:
    needs_signin: bool = False
    cannot_read: bool = False
    paused_until: datetime | None = None
    last_error: str | None = None


@dataclass
class _Track:
    next_due: datetime | None = None
    previous: tuple | None = None
    failing: bool = False
    paused_until: datetime | None = None
    blocked: str | None = None
    blocked_on: str | None = None
    last_error: str | None = None
    spare_noted: bool = False
    read_at: datetime | None = None


@dataclass(frozen=True)
class _Live:
    state: str
    uuid: str | None = None
    oauth: dict | None = field(default=None, repr=False)


def ledger_problem(base):
    return LEDGER_PROBLEM.format(path=Path(base) / ledger.NAME)


class _BadCache(Exception):
    pass


def _oauth(item):
    value = item.get(OAUTH_KEY) if isinstance(item, dict) else None
    return value if isinstance(value, dict) and value else None


def _digest(*parts):
    text = json.dumps([part if isinstance(part, str) else None for part in parts])
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _renewal_print(oauth):
    return _digest(oauth.get("refreshToken"))


def _signin_print(oauth):
    return _digest(oauth.get("refreshToken"), oauth.get("accessToken"))


def _same_renewal(parked, live):
    token = parked.get("refreshToken")
    return isinstance(token, str) and bool(token) and isinstance(live, dict) and live.get("refreshToken") == token


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _fresh(oauth, now, margin):
    token = oauth.get("accessToken")
    expires = _number(oauth.get("expiresAt"))
    if not isinstance(token, str) or not token or expires is None:
        return False
    return expires - now.timestamp() * 1000 > margin * 1000


def _wait(retry_after):
    if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
        seconds = anthropic.DEFAULT_RETRY_AFTER_S
    elif retry_after != retry_after or retry_after < 0:
        seconds = anthropic.DEFAULT_RETRY_AFTER_S
    else:
        seconds = min(retry_after, MAX_WAIT_S)
    return max(float(seconds), float(MIN_GAP_S))


def _figures(window):
    if window is None:
        return None, None
    return getattr(window, "utilization", None), getattr(window, "resets_at", None)


def _rose(before, after):
    if before is None:
        return False
    return any(old is not None and new is not None and new > old for old, new in zip(before, after))


def _clock_text(moment, now):
    return clock.when(moment, now, on=False)


def _name(account):
    name = getattr(account, "name", None)
    return name if isinstance(name, str) and name else NAMELESS


def _nap(seconds):
    if seconds > 0:
        time.sleep(seconds)


def _sync_folder(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_private(path, data):
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = json.dumps(data, allow_nan=False)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".reader_state-", suffix=".tmp")
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
    _sync_folder(folder)


def _read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            return json.loads(handle.read())
    except (OSError, ValueError, RecursionError):
        return None


def _cache_window(value):
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise _BadCache()
    raw_used = value.get("utilization")
    used = None if raw_used is None else _number(raw_used)
    if raw_used is not None and (used is None or used < 0):
        raise _BadCache()
    raw_reset = value.get("resets_at")
    if raw_reset is None:
        return used, None
    if not isinstance(raw_reset, str):
        raise _BadCache()
    try:
        resets = datetime.fromisoformat(raw_reset)
    except ValueError:
        raise _BadCache() from None
    if resets.tzinfo is None:
        resets = resets.replace(tzinfo=timezone.utc)
    return used, resets


class Reader:
    def __init__(
        self,
        *,
        registry,
        store,
        slot,
        history,
        clock,
        home,
        record_path,
        lock_path=None,
        usage_fn=anthropic.usage,
        refresh_fn=anthropic.refresh,
        profile_fn=anthropic.profile,
        accounts=None,
        guarded_fn=lambda: set(),
        full_registry=None,
        live_note_fn=None,
    ):
        self.registry = registry
        self.full_registry = full_registry if full_registry is not None else registry
        self.store = store
        self.slot = slot
        self.history = history
        self.clock = clock
        self.home = Path(home)
        self.record_path = Path(record_path)
        self.lock_path = Path(lock_path) if lock_path is not None else None
        self.usage_fn = usage_fn
        self.refresh_fn = refresh_fn
        self.profile_fn = profile_fn
        self.guarded_fn = guarded_fn
        self.live_note_fn = live_note_fn
        self.on_identified = None
        if isinstance(accounts, str):
            accounts = [accounts]
        self._wanted = None if accounts is None else frozenset(accounts)
        folder = self.lock_path.parent if self.lock_path is not None else writelock.lifeline_folder()
        self._base = folder
        self._recovery_dir = recovery.folder_for(self.lock_path)
        self._state_path = folder / STATE_NAME
        self._tracks = {}
        self._statuses = {}
        self._scoped = {}
        self._unsaved = {}
        self._spent = {}
        self._pending = set()
        self._retry_at = {}
        self._bad_noted = set()
        self._identity = None
        self._last_live = None
        self._unknown_noted = False
        self._registry_noted = False
        self._ledger_failed = False
        self._ledger_noted = False
        self._state_dirty = False
        self._ticking = threading.Lock()
        self._flush_waiting = threading.Event()
        self._asking = threading.Lock()
        self._askers = 0
        try:
            self._state = self._load_state()
        except Exception:
            self._state = {}
        self._carried = []
        try:
            self._recover(self.clock(), self._carried)
        except Exception:
            pass

    def status(self, uuid):
        return self._statuses.get(uuid, AccountStatus())

    def scoped(self, uuid):
        return [dict(item, extra=dict(item["extra"])) for item in self._scoped.get(uuid, [])]

    def read_at(self, uuid):
        track = self._tracks.get(uuid)
        return None if track is None else track.read_at

    def tick(self):
        with self._ticking:
            now = self.clock()
            reports, self._carried = self._carried, []
            if self._flush_waiting.is_set():
                return reports
            self._check_ledger(reports)
            if self._state_dirty:
                self._write_state(now)
            accounts = self._accounts(reports)
            self._recover(now, reports)
            due = [account for account in accounts if self._due(self._track(account.uuid, now), now)]
            if not due or self._flush_waiting.is_set():
                return reports
            live = self._observe_live(now)
            self._note_live(live, reports)
            for account in due:
                if self._flush_waiting.is_set():
                    break
                self._visit(account, live, now, reports)
            self._check_ledger(reports)
            return reports

    def _check_ledger(self, reports):
        try:
            trouble = self._ledger_failed or ledger.problem(self._base)
        except Exception:
            trouble = True
        if not trouble:
            self._ledger_noted = False
        elif not self._ledger_noted:
            self._ledger_noted = True
            reports.append(ledger_problem(self._base))

    def _ask(self, step):
        with self._asking:
            self._askers += step
            if self._askers > 0:
                self._flush_waiting.set()
            else:
                self._flush_waiting.clear()

    def _accounts(self, reports):
        try:
            loaded = list(self.registry.load())
        except Exception:
            if not self._registry_noted:
                self._registry_noted = True
                reports.append(NO_ACCOUNTS)
            return []
        self._registry_noted = False
        chosen = []
        for account in loaded:
            uuid = getattr(account, "uuid", None)
            if isinstance(uuid, str) and uuid and (self._wanted is None or uuid in self._wanted):
                chosen.append(account)
        return chosen

    def _name_of(self, uuid):
        try:
            account = self.registry.by_uuid(uuid)
        except Exception:
            account = None
        return _name(account)

    def _track(self, uuid, now):
        track = self._tracks.get(uuid)
        if track is None:
            track = _Track()
            self._seed(uuid, track, now)
            self._tracks[uuid] = track
            self._statuses[uuid] = self._snapshot(track)
        return track

    def _seed(self, uuid, track, now):
        try:
            recent = self.history.readings(uuid, now - timedelta(seconds=NORMAL_S), fields=())
        except Exception:
            recent = []
        spent = [reading for reading in recent if reading.source == "usage" and reading.at <= now]
        if spent:
            last = spent[-1]
            track.next_due = last.at + timedelta(seconds=NORMAL_S)
            track.previous = (last.five, last.week)
        saved = self._state.get(uuid)
        if saved is not None and saved[0] > now:
            until = min(saved[0], now + timedelta(seconds=MAX_WAIT_S))
            if track.next_due is None or track.next_due < until:
                track.next_due = until
            track.paused_until = until
            track.last_error = saved[1]
            track.failing = True

    def _load_state(self):
        data = _read_private(self._state_path)
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, dict):
            return {}
        state = {}
        for uuid, entry in accounts.items():
            if not isinstance(uuid, str) or not isinstance(entry, dict):
                continue
            stamp = _number(entry.get("paused_until_ms"))
            if stamp is None:
                continue
            try:
                until = EPOCH + timedelta(milliseconds=math.floor(stamp))
            except (OverflowError, ValueError):
                continue
            error = entry.get("last_error")
            state[uuid] = (until, error if isinstance(error, str) and error in KNOWN_ERRORS else None)
        return state

    def _remember(self, uuid, track, now):
        entry = None if track.paused_until is None else (track.paused_until, track.last_error)
        if self._state.get(uuid) != entry:
            if entry is None:
                self._state.pop(uuid, None)
            else:
                self._state[uuid] = entry
            self._state_dirty = True
        if self._state_dirty:
            self._write_state(now)

    def _write_state(self, now):
        self._state = {key: value for key, value in self._state.items() if value[0] > now}
        data = {
            "accounts": {
                key: {"paused_until_ms": (until - EPOCH) // MILLISECOND, "last_error": error}
                for key, (until, error) in self._state.items()
            }
        }
        try:
            _write_private(self._state_path, data)
        except Exception:
            self._state_dirty = True
            return
        self._state_dirty = False

    @staticmethod
    def _due(track, now):
        return track.next_due is None or now >= track.next_due

    def _observe_live(self, now, identify=True):
        try:
            item = self.slot.read()
        except Exception:
            return _Live(UNKNOWN)
        oauth = _oauth(item)
        self._last_live = oauth
        if oauth is None:
            self._identity = None
            return _Live(NONE)
        renewal = oauth.get("refreshToken")
        if not isinstance(renewal, str) or not renewal:
            return _Live(UNKNOWN, None, oauth)
        mark = _renewal_print(oauth)
        known = self._identity
        changed = known is None or known[0] != mark
        if changed and not identify:
            return _Live(UNKNOWN, None, oauth)
        if changed or (known[1] is None and now >= known[2]):
            known = (mark, self._identify(item, now), now + timedelta(seconds=IDENTIFY_RETRY_S))
            self._identity = known
            if known[1] is not None and self.on_identified is not None:
                try:
                    self.on_identified(item, known[1])
                except Exception:
                    pass
        if known[1] is None:
            return _Live(UNKNOWN, None, oauth)
        return _Live(KNOWN, known[1], oauth)

    def _identify(self, item, now):
        try:
            found = switch.identify_live(
                item,
                profile_fn=self.profile_fn,
                home=self.home,
                record_path=self.record_path,
                registry=self.full_registry,
                clock=now.timestamp,
            )
        except Exception:
            return None
        return found if isinstance(found, str) and found else None

    def _note_live(self, live, reports):
        if live.state != UNKNOWN:
            self._unknown_noted = False
        elif not self._unknown_noted:
            self._unknown_noted = True
            try:
                said = self.live_note_fn() if self.live_note_fn is not None else None
            except Exception:
                said = None
            reports.append(said or UNCONFIRMED)

    def _visit(self, account, live, now, reports):
        track = self._track(account.uuid, now)
        try:
            self._attend(account, track, live, now, reports)
        except Exception:
            self._pause(account, track, now, FAILURE_S, PAUSED, FAILED_ERROR, reports)
        self._statuses[account.uuid] = self._snapshot(track)
        self._remember(account.uuid, track, now)

    def _attend(self, account, track, live, now, reports):
        uuid = account.uuid
        is_live = live.state == KNOWN and live.uuid == uuid
        if track.blocked is not None and not self._unblocked(uuid, track, live, is_live, now):
            return
        if is_live:
            self._read_live(account, track, live, now, reports)
        else:
            self._read_parked(account, track, live, now, reports)

    def _unblocked(self, uuid, track, live, is_live, now):
        current = live.oauth if is_live else self._quiet_get(uuid)
        if not isinstance(current, dict) or _signin_print(current) == track.blocked_on:
            track.next_due = now + timedelta(seconds=NORMAL_S)
            return False
        track.blocked = None
        track.blocked_on = None
        track.last_error = None
        return True

    def _quiet_get(self, uuid):
        try:
            return self.store.get(uuid)
        except Exception:
            return None

    def _note_end(self, account, oauth, now, locked=False):
        try:
            text = signin_end_text(oauth, now)
            if text is None or account.signin_ends is None or account.signin_ends == text:
                return
            if locked:
                switch.note_signin_end(self.registry, account.uuid, oauth, now)
                return
            with writelock.held(self.lock_path, timeout=LOCK_TIMEOUT_S):
                switch.note_signin_end(self.registry, account.uuid, oauth, now)
        except Exception:
            return

    def _read_live(self, account, track, live, now, reports):
        self._note_end(account, live.oauth, now)
        self._check_spare(account, track, reports)
        if _fresh(live.oauth, now, LIVE_MARGIN_S):
            oauth = live.oauth
            self._read_with(account, track, oauth, now, reports, lambda: self._slot_moved(track, oauth, now))
        else:
            self._read_cache(account, track, now, reports)

    def _check_spare(self, account, track, reports):
        try:
            spare = self.store.get(account.uuid)
        except Exception:
            return
        if spare is None:
            track.spare_noted = False
        elif not track.spare_noted:
            track.spare_noted = True
            reports.append(SPARE.format(name=_name(account)))

    def _read_parked(self, account, track, live, now, reports):
        uuid = account.uuid
        try:
            oauth = self.store.get(uuid)
        except Exception:
            self._pause(account, track, now, FAILURE_S, STORE_PAUSED, STORE_ERROR, reports)
            return
        if not isinstance(oauth, dict):
            track.next_due = now + timedelta(seconds=NORMAL_S)
            return
        self._note_end(account, oauth, now)
        if _fresh(oauth, now, RENEW_MARGIN_S):
            used = oauth
            self._read_with(account, track, used, now, reports, lambda: self._renew_after_refusal(account, track, used, now, reports))
            return
        if live.state == UNKNOWN:
            track.next_due = now + timedelta(seconds=NORMAL_S)
            return
        if self._waiting(uuid):
            track.next_due = now + timedelta(seconds=RECOVERY_RETRY_S)
            return
        if not self._may_renew(account, track, now, reports):
            return
        renewed = self._renew(account, track, now, reports)
        if renewed is not None:
            self._read_with(account, track, renewed, now, reports)

    def _waiting(self, uuid):
        return uuid in self._pending or uuid in self._unsaved

    def _may_renew(self, account, track, now, reports):
        try:
            guarded = frozenset(self.guarded_fn())
        except Exception:
            self._pause(account, track, now, FAILURE_S, GUARD_PAUSED, GUARD_ERROR, reports)
            return False
        if account.uuid in guarded:
            track.next_due = now + timedelta(seconds=RECOVERY_RETRY_S)
            return False
        return True

    def _renew(self, account, track, now, reports, refused=None):
        uuid = account.uuid
        with ExitStack() as stack:
            try:
                stack.enter_context(writelock.held(self.lock_path, timeout=LOCK_TIMEOUT_S))
            except writelock.WriteLockTimeout:
                return None
            except OSError:
                self._pause(account, track, now, FAILURE_S, LOCK_PAUSED, LOCK_ERROR, reports)
                return None
            live = self._observe_live(now, identify=False)
            if live.state == UNKNOWN or live.uuid == uuid or self._waiting(uuid):
                return None
            if not self._may_renew(account, track, now, reports):
                return None
            if recovery.waiting(self.store, self._recovery_dir, uuid):
                self._pending.add(uuid)
                track.next_due = now + timedelta(seconds=RECOVERY_RETRY_S)
                return None
            try:
                current = self.store.get(uuid)
            except Exception:
                self._pause(account, track, now, FAILURE_S, STORE_PAUSED, STORE_ERROR, reports)
                return None
            if not isinstance(current, dict):
                return None
            if _same_renewal(current, live.oauth):
                track.next_due = now + timedelta(seconds=NORMAL_S)
                return None
            if _fresh(current, now, RENEW_MARGIN_S) and _signin_print(current) != refused:
                return current
            if not self._writable(uuid, current):
                self._pause(account, track, now, FAILURE_S, UNWRITABLE_PAUSED, UNWRITABLE_ERROR, reports)
                return None
            if not recovery.writable(self._recovery_dir):
                self._pause(account, track, now, FAILURE_S, NO_RECOVERY_PAUSED, NO_RECOVERY_ERROR, reports)
                return None
            return self._renew_locked(account, track, current, now, reports)

    def _renew_after_refusal(self, account, track, oauth, now, reports):
        before = (track.next_due, track.blocked, track.paused_until)
        renewed = self._renew(account, track, now, reports, refused=_signin_print(oauth))
        if renewed is None and (track.next_due, track.blocked, track.paused_until) == before:
            track.next_due = now + timedelta(seconds=NORMAL_S)
        floor = now + timedelta(seconds=MIN_GAP_S)
        if track.next_due is None or track.next_due < floor:
            track.next_due = floor
        return True

    def _writable(self, uuid, current):
        try:
            self.store.put(uuid, current)
        except StoreLeftover:
            return True
        except Exception:
            return False
        return True

    def _renew_locked(self, account, track, current, now, reports):
        uuid = account.uuid
        marker = _signin_print(current)
        try:
            renewed = self.refresh_fn({OAUTH_KEY: copy.deepcopy(current)})
        except Exception as error:
            self._refused(account, track, error, marker, now, reports, RENEW_ERRORS)
            return None
        new = _oauth(renewed)
        if not recovery.usable(new):
            self._pause(account, track, now, FAILURE_S, PAUSED, RENEW_ERRORS[UnexpectedReply], reports)
            return None
        spent = current if recovery.fingerprint(new) != recovery.fingerprint(current) else None
        on_disk = self._write_first(uuid, new, spent)
        if spent is not None:
            self._note_spent(uuid, spent)
        if not self._save(uuid, new, spent):
            self._keep_unsaved(account, track, new, spent, on_disk, now, reports)
            return None
        self._note_end(account, new, now, locked=True)
        if on_disk:
            recovery.remove(self._recovery_dir, uuid, new)
        if not _fresh(new, now, RENEW_MARGIN_S):
            self._pause(account, track, now, FAILURE_S, PAUSED, RENEW_ERRORS[UnexpectedReply], reports)
            return None
        return new

    def _note_spent(self, uuid, spent):
        try:
            ledger.add(self._base, uuid, spent)
        except Exception:
            self._ledger_failed = True
            return
        self._ledger_failed = False

    def _write_first(self, uuid, new, spent):
        try:
            recovery.write(self._recovery_dir, uuid, new, spent=spent)
        except recovery.RecoveryError:
            return False
        return True

    def _save(self, uuid, oauth, spent):
        for attempt in range(len(SAVE_BACKOFF_S) + 1):
            if attempt:
                _nap(SAVE_BACKOFF_S[attempt - 1])
            if recovery.store_pair(self.store, uuid, oauth, spent=spent, folder=self._recovery_dir) in (
                recovery.SAVED,
                recovery.HELD,
            ):
                return True
        return False

    def _keep_unsaved(self, account, track, new, spent, on_disk, now, reports):
        uuid = account.uuid
        self._pending.add(uuid)
        self._retry_at[uuid] = now + timedelta(seconds=RECOVERY_RETRY_S)
        if on_disk:
            self._pause(account, track, now, FAILURE_S, KEPT_PAUSED, UNSAVED_ERROR, reports, always=True)
            return
        self._unsaved[uuid] = copy.deepcopy(new)
        self._spent[uuid] = recovery.fingerprint(spent)
        self._pause(account, track, now, FAILURE_S, HELD_PAUSED, UNSAVED_ERROR, reports, always=True)

    def _gate_open(self, uuid, now):
        at = self._retry_at.get(uuid)
        return at is None or now >= at

    def _recover(self, now, reports, live_oauth=_UNSET, stale=False):
        waiting = set(recovery.accounts(self._recovery_dir)) | set(self._unsaved)
        ready = sorted(uuid for uuid in waiting if self._gate_open(uuid, now))
        pending = {uuid for uuid in waiting if uuid not in ready and self._waiting(uuid)}
        if ready:
            with ExitStack() as stack:
                try:
                    stack.enter_context(writelock.held(self.lock_path, timeout=LOCK_TIMEOUT_S))
                    locked = True
                except Exception:
                    locked = False
                if locked and live_oauth is _UNSET:
                    live_oauth = self._read_live_oauth()
                for uuid in ready:
                    self._retry_at[uuid] = now + timedelta(seconds=RECOVERY_RETRY_S)
                    if not locked or not self._settle(uuid, reports, live_oauth, stale):
                        pending.add(uuid)
        self._pending = pending

    def _read_live_oauth(self):
        try:
            oauth = _oauth(self.slot.read())
        except Exception:
            return None
        self._last_live = oauth
        return oauth

    def flush(self):
        self._ask(1)
        holding = False
        try:
            with self._ticking:
                holding = True
                self._ask(-1)
                now = self.clock()
                reports, self._carried = self._carried, []
                self._retry_at.clear()
                self._recover(now, reports, self._last_live, stale=True)
                if self._state_dirty:
                    self._write_state(now)
                return reports, tuple(sorted(self._unsaved))
        finally:
            if not holding:
                self._ask(-1)

    def forget(self, uuids, work):
        """Runs work while no reading round runs, then drops what it holds for those accounts, or for every account when none are named."""
        self._ask(1)
        holding = False
        try:
            with self._ticking:
                holding = True
                self._ask(-1)
                done = work()
                for table in (self._tracks, self._statuses, self._scoped, self._unsaved, self._spent, self._retry_at, self._state):
                    for uuid in [key for key in table if uuids is None or key in uuids]:
                        del table[uuid]
                self._pending = {uuid for uuid in self._pending if uuids is not None and uuid not in uuids}
                self._state_dirty = True
                return done
        finally:
            if not holding:
                self._ask(-1)

    def _forget(self, uuid):
        del self._unsaved[uuid]
        self._spent.pop(uuid, None)

    def _settle(self, uuid, reports, live_oauth=None, stale=False):
        memory = self._unsaved.get(uuid)
        if memory is not None:
            mark = self._spent.get(uuid)
            try:
                recovery.write(self._recovery_dir, uuid, memory, spent=mark)
            except recovery.RecoveryError:
                outcome = recovery.store_pair(self.store, uuid, memory, spent=mark, folder=self._recovery_dir)
                if outcome == recovery.PENDING:
                    return False
                if outcome == recovery.SUPERSEDED and recovery.fingerprint(memory) not in ledger.spent(self._base, uuid):
                    return False
                self._forget(uuid)
                if outcome == recovery.SAVED:
                    reports.append(SAVED_FROM_MEMORY.format(name=self._name_of(uuid)))
                return recovery.pending(self._recovery_dir, uuid) is None
            self._forget(uuid)
        waiting = recovery.pending(self._recovery_dir, uuid)
        if waiting is not None and recovery.same_signin(waiting, live_oauth):
            if stale:
                self._retry_at.pop(uuid, None)
                return False
            return recovery.remove(self._recovery_dir, uuid, waiting)
        outcome = recovery.settle(self.store, self._recovery_dir, uuid)
        if outcome == recovery.SAVED:
            reports.append(RECOVERED.format(name=self._name_of(uuid)))
        elif outcome == recovery.ABSENT and recovery.damaged(self._recovery_dir, uuid) and uuid not in self._bad_noted:
            self._bad_noted.add(uuid)
            reports.append(BAD_RECOVERY.format(name=self._name_of(uuid)))
        return outcome != recovery.PENDING

    def _read_with(self, account, track, oauth, now, reports, on_refused=None):
        try:
            found = self.usage_fn(oauth["accessToken"])
        except Exception as error:
            if on_refused is not None and isinstance(error, SignInDead) and on_refused():
                return
            self._refused(account, track, error, _signin_print(oauth), now, reports, READ_ERRORS)
            return
        five_window = getattr(found, "five_hour", None)
        week_window = getattr(found, "seven_day", None)
        if five_window is None and week_window is None:
            self._pause(account, track, now, FAILURE_S, PAUSED, READ_ERRORS[UnexpectedReply], reports)
            return
        five, five_reset = _figures(five_window)
        week, week_reset = _figures(week_window)
        arrived = max(now, self.clock())
        self._record(account, track, "usage", (five, five_reset, week, week_reset), arrived, now, reports)
        self._scoped[account.uuid] = [
            {
                "kind": limit.kind,
                "model": limit.model,
                "surface": limit.surface,
                "percent": limit.percent,
                "resets_at": limit.resets_at,
                "severity": limit.severity,
                "extra": dict(limit.extra),
            }
            for limit in getattr(found, "limits", ()) or ()
            if getattr(limit, "kind", None) == "weekly_scoped"
        ]

    def _slot_moved(self, track, used, now):
        try:
            moved = not recovery.same_signin(_oauth(self.slot.read()), used)
        except Exception:
            moved = True
        if moved:
            track.next_due = now + timedelta(seconds=NORMAL_S)
        return moved

    def _refused(self, account, track, error, marker, now, reports, errors):
        renewing = errors is RENEW_ERRORS
        if isinstance(error, SignInDead) or (renewing and isinstance(error, ValueError)):
            self._block(account, track, SIGNIN, marker, now, reports)
            return
        if isinstance(error, NotAllowed):
            self._block(account, track, BARRED, marker, now, reports)
            return
        seconds = FAILURE_S
        text = FAILED_ERROR
        for kind, sentence in errors.items():
            if isinstance(error, kind):
                text = sentence
        if isinstance(error, RateLimited):
            seconds = _wait(getattr(error, "retry_after_s", None))
        self._pause(account, track, now, seconds, PAUSED, text, reports)

    def _read_cache(self, account, track, now, reports):
        track.next_due = now + timedelta(seconds=NORMAL_S)
        found = self._cache(account, now)
        if found is None:
            return
        fetched, figures = found
        try:
            latest = self.history.latest(account.uuid)
        except Exception:
            return
        if latest is not None and fetched <= latest.at:
            return
        self._record(account, track, "cache", figures, fetched, now, reports)

    def _cache_is(self, entry, account, label):
        if not labels(label, account):
            return False
        login, org = stated(entry)
        if login is None or login != account.login:
            return False
        if org is not None:
            return account.org_uuid is None or org == account.org_uuid
        try:
            everyone = self.full_registry.load()
        except Exception:
            return False
        return not any(other.uuid != account.uuid and other.login == login for other in everyone)

    def _cache(self, account, now):
        try:
            data = json.loads((self.home / ACCOUNT_FILE_NAME).read_bytes())
        except (OSError, ValueError, RecursionError):
            return None
        entry = data.get(CACHE_KEY) if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not self._cache_is(entry, account, data.get("oauthAccount")):
            return None
        stamp = _number(entry.get("fetchedAtMs"))
        if stamp is None:
            return None
        try:
            fetched = EPOCH + timedelta(milliseconds=math.floor(stamp))
        except OverflowError:
            return None
        if fetched > now:
            return None
        usage = entry.get("utilization")
        if not isinstance(usage, dict):
            return None
        try:
            five, five_reset = _cache_window(usage.get("five_hour"))
            week, week_reset = _cache_window(usage.get("seven_day"))
        except _BadCache:
            return None
        if five is None and five_reset is None and week is None and week_reset is None:
            return None
        return fetched, (five, five_reset, week, week_reset)

    def _record(self, account, track, source, figures, at, now, reports):
        five, five_reset, week, week_reset = figures
        try:
            self.history.add_reading(account.uuid, source, five, five_reset, week, week_reset, at)
        except Exception:
            self._pause(account, track, now, FAILURE_S, HISTORY_PAUSED, HISTORY_ERROR, reports)
            return
        climbing = _rose(track.previous, (five, week))
        track.previous = (five, week)
        track.next_due = now + timedelta(seconds=CLIMBING_S if climbing else NORMAL_S)
        track.failing = False
        track.paused_until = None
        track.last_error = None
        track.read_at = now

    def _pause(self, account, track, now, seconds, sentence, error_text, reports, always=False):
        until = now + timedelta(seconds=seconds)
        track.next_due = until
        track.paused_until = until
        track.last_error = error_text
        if always or not track.failing:
            reports.append(sentence.format(name=_name(account), time=_clock_text(until, now)))
        track.failing = True

    def _block(self, account, track, kind, marker, now, reports):
        track.blocked = kind
        track.blocked_on = marker
        track.next_due = now + timedelta(seconds=NORMAL_S)
        track.failing = False
        track.paused_until = None
        track.last_error = DEAD_ERROR if kind == SIGNIN else NOT_ALLOWED_ERROR
        sentence = DEAD if kind == SIGNIN else NOT_ALLOWED
        reports.append(sentence.format(name=_name(account)))

    @staticmethod
    def _snapshot(track):
        return AccountStatus(
            needs_signin=track.blocked == SIGNIN,
            cannot_read=track.blocked == BARRED,
            paused_until=track.paused_until,
            last_error=track.last_error,
        )
