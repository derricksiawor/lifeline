import copy
import hashlib
import json
import os
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from lifeline import accountfile, ident, ledger, recovery, writelock
from lifeline.anthropic import CertificateFailed, SignInDead, Unavailable
from lifeline.accounts import agrees, identity, labels, signin_end_text
from lifeline.locks import STORAGE_TIMEOUT, LockTimeout, SessionLocks, StorageLock, nudge_sessions
from lifeline.locks import claude_folder as find_claude_folder
from lifeline.writelock import WriteLockTimeout

OAUTH_KEY = "claudeAiOauth"
RECORD_NAME = "last_loaded.json"
BACKUPS_NAME = "backups"
ACCOUNT_FILE_NAME = ".claude.json"
WATCH_MINUTES = 10
UNKNOWN_HOLD_SECONDS = 120
LOCK_TIMEOUT = writelock.DEFAULT_TIMEOUT
SESSION_TIMEOUT = 30.0
SETTLE_SECONDS = 1.0
OUTSIDER = "an account that is not in Lifeline"

NOT_SIGNED_IN = "Lifeline found no sign-in in Claude Code, so nothing was changed."
UNCONFIRMED = (
    "Lifeline could not confirm which account Claude Code is signed in to, so nothing was changed. "
    "Check the internet connection and try again."
)
CERTIFICATE_UNCHECKED = (
    "Lifeline could not check Anthropic's certificate, so it could not confirm which account Claude Code is signed in to. "
    "Nothing was changed."
)
LIVE_REFUSED = "Anthropic refused the sign-in in Claude Code, so Lifeline could not confirm its account. Nothing was changed."
LIVE_UNCHECKED = "Anthropic did not confirm which account Claude Code is signed in to, so nothing was changed. Try again in a moment."
LIVE_NOT_RENEWED = (
    "Claude Code has not renewed its sign-in lately, and Lifeline has no record of it, "
    "so Lifeline cannot tell which account it holds. Nothing was changed. Use Claude Code once, then try again."
)
LIVE_MISMATCHED = (
    "Claude Code's account details do not match Lifeline's record of its sign-in, "
    "so Lifeline cannot tell which account it holds. Nothing was changed."
)
ORG_UNKNOWN = (
    "Anthropic did not say which organization the sign-in in Claude Code belongs to, "
    "so Lifeline cannot tell which account it holds. Nothing was changed."
)
LIFELINE_BUSY = "Lifeline is busy with another change, so nothing was changed. Try again in a moment."
NO_LOCK_FILE = "Lifeline could not open its own lock file, so nothing was changed."
SESSION_BUSY = (
    "Claude Code is busy renewing its sign-in or saving its settings, so nothing was changed. "
    "Try again in a moment."
)
NO_SETTINGS_FOLDER = "Lifeline could not find Claude Code's settings folder, so nothing was changed."
NO_SESSION_LOCKS = "Lifeline could not take Claude Code's locks, so nothing was changed."
SESSIONS_STAY = "Open Claude Code sessions may stay on {name} until you restart them."
LOCK_LOST = "Another program took over one of Claude Code's locks during the switch. Check that Claude Code uses {name}."
UNKNOWN_SIGNIN = (
    "The sign-in in Claude Code changed, and Lifeline could not check whose it is, "
    "so it left it alone for now."
)
WRONG_NAME = "Claude Code may show the wrong account name until the next switch."
RECORD_RETRY = "Lifeline could not update its own record. It will try again."
REGISTRY_UNREADABLE = "Lifeline could not read its list of accounts, so nothing was changed."
NO_RENEWAL_KEY = "The sign-in has no renewal key, so Lifeline cannot record it."
STALE_HELD = (
    "Claude Code holds an older sign-in for {name} that may no longer work, "
    "and Lifeline could not put the newer one back yet. It will keep trying."
)
STALE_OTHER = (
    "Claude Code now holds a different sign-in, and Lifeline could not put back "
    "the newer sign-in for {name} yet. It will keep trying."
)
STALE_UNREAD = (
    "Lifeline could not read the sign-in in Claude Code, so it could not put back "
    "the newer sign-in for {name} yet. It will keep trying."
)
STALE_NOTES = {"foreign": STALE_HELD, "other": STALE_OTHER, "unreadable": STALE_UNREAD}
LEDGER_STOP = (
    "Lifeline stopped watching {name} because it could not write its list of spent sign-ins at {path}. "
    "Every sign-in is kept. Make that file a plain file you can read and write. "
    "If it is damaged, rename it, and Lifeline starts a new one."
)
STORED = "stored"
GOOD = "good"
PROOF = "proof"
PARKED = "parked"
ORDER = "order"

_UNSET = object()


class SwitchError(Exception):
    pass


class SwitchRefused(SwitchError):
    pass


class SwitchFailed(SwitchError):
    pass


class LifelineBusy(SwitchRefused):
    pass


class IdentityUnknown(SwitchRefused):
    pass


class SealedSignin:
    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = copy.deepcopy(value)

    def open(self):
        return copy.deepcopy(self._value)

    def __repr__(self):
        return "SealedSignin()"

    __str__ = __repr__

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __getstate__(self):
        raise TypeError("A sign-in cannot be pickled.")

    def __reduce__(self):
        raise TypeError("A sign-in cannot be pickled.")

    def __reduce_ex__(self, protocol):
        raise TypeError("A sign-in cannot be pickled.")


@dataclass(frozen=True)
class SwitchResult:
    from_uuid: str
    to_uuid: str
    backup: Path | None
    leftover: bool = False
    confirmed: bool | None = None
    signin: SealedSignin | None = field(default=None, repr=False, compare=False)
    note: str | None = None
    proven: bool = False


def default_backups(home=None):
    return ident.data_folder(home) / BACKUPS_NAME


def record_path_for(lock_path):
    return Path(lock_path).parent / RECORD_NAME


def default_record_path():
    return record_path_for(writelock.default_lock_path())


def fingerprint(oauth):
    token = oauth.get("refreshToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_record_entry(path):
    return ledger.record_entry(path)


def read_record(path):
    entry = read_record_entry(path)
    return entry["uuid"] if entry is not None else None


def _write_record(path, uuid, stamp, digest, proven=False):
    path = Path(path)
    entry = {"uuid": uuid, "at": stamp.isoformat(timespec="seconds"), "refresh_sha256": digest}
    if digest is not None:
        entry["proven"] = bool(proven)
    text = json.dumps(entry) + "\n"
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".last_loaded-", suffix=".tmp")
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


def write_record(path, uuid, now=None):
    stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    _write_record(path, uuid, stamp, None)


def record_confirmed(record_path, uuid, item, at, proven=False):
    digest = fingerprint(_oauth_of(item))
    if not isinstance(uuid, str) or not uuid or digest is None:
        raise ValueError(NO_RENEWAL_KEY)
    _write_record(record_path, uuid, at, digest, proven)


def note_signin_end(registry, uuid, oauth, now=None):
    text = signin_end_text(oauth, now)
    if text is None:
        return False
    account = registry.by_uuid(uuid)
    if account is None or account.signin_ends is None or account.signin_ends == text:
        return False
    registry.upsert(replace(account, signin_ends=text))
    return True


def _attempt(action):
    try:
        value = action()
    except Exception:
        return False, None
    return True, value


def _done(action):
    return _attempt(action)[0]


def _oauth_of(item):
    value = item.get(OAUTH_KEY) if isinstance(item, dict) else None
    return value if isinstance(value, dict) else None


def _refresh_of(oauth):
    token = oauth.get("refreshToken") if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token else None


def _usable(oauth):
    if not isinstance(oauth, dict):
        return False
    return all(isinstance(oauth.get(key), str) and oauth.get(key) for key in ("accessToken", "refreshToken"))


def _expires(oauth):
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _expired(oauth, now):
    expires = _expires(oauth)
    return expires is not None and expires <= now * 1000


def _not_older(candidate, existing):
    if existing is None:
        return True
    new, old = _expires(candidate), _expires(existing)
    if new is None or old is None:
        return True
    return new >= old


def _older(candidate, reference):
    new, old = _expires(candidate), _expires(reference)
    return new is not None and old is not None and new < old


def _later(candidate, reference):
    return _older(reference, candidate)


def _with_oauth(item, oauth):
    updated = dict(item) if isinstance(item, dict) else {}
    updated[OAUTH_KEY] = oauth
    return updated


def _stamp(clock):
    return datetime.fromtimestamp(clock(), timezone.utc)


def _text(value):
    return value if isinstance(value, str) and value else None


def _profile_check(oauth, profile_fn, now, registry):
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token or _expired(oauth, now):
        return None, LIVE_UNCHECKED
    try:
        found = profile_fn(token)
    except CertificateFailed:
        return None, CERTIFICATE_UNCHECKED
    except SignInDead:
        return None, LIVE_REFUSED
    except Unavailable:
        return None, UNCONFIRMED
    except Exception:
        return None, LIVE_UNCHECKED
    login = _text(getattr(found, "account_uuid", None))
    if login is None:
        return None, LIVE_UNCHECKED
    ok, accounts = _attempt(registry.load)
    if not ok:
        return None, REGISTRY_UNREADABLE
    uuid = identity(accounts, login, _text(getattr(found, "org_uuid", None)))
    return (uuid, None) if uuid is not None else (None, ORG_UNKNOWN)


def _profile_uuid(oauth, profile_fn, now, registry):
    return _profile_check(oauth, profile_fn, now, registry)[0]


def _recorded_as(entry, account, registry):
    ok, known = _attempt(lambda: registry.by_uuid(entry["uuid"]))
    if not ok:
        return None, REGISTRY_UNREADABLE
    if known is not None:
        return (entry["uuid"], None) if agrees(account, known) else (None, LIVE_MISMATCHED)
    stated = account.get("accountUuid") if isinstance(account, dict) else None
    return (entry["uuid"], None) if stated == entry["uuid"] else (None, LIVE_MISMATCHED)


def _identify_why(live, account, profile_fn, record_path, clock, registry):
    now = clock()
    if not _expired(live, now):
        return _profile_check(live, profile_fn, now, registry)
    entry = read_record_entry(record_path)
    if entry is None or entry["uuid"] is None or entry["refresh_sha256"] is None:
        return None, LIVE_NOT_RENEWED
    if entry["refresh_sha256"] != fingerprint(live):
        return None, LIVE_NOT_RENEWED
    return _recorded_as(entry, account, registry)


def identify_live(item, *, profile_fn, home, record_path, registry, clock=time.time):
    live = _oauth_of(item)
    if not live:
        raise IdentityUnknown(NOT_SIGNED_IN)
    ok, account = _attempt(lambda: accountfile.read_oauth_account(Path(home) / ACCOUNT_FILE_NAME))
    found, why = _identify_why(live, account if ok else None, profile_fn, record_path, clock, registry)
    if found is None:
        raise IdentityUnknown(why)
    return found


def _enter(stack, manager):
    kind = None
    try:
        stack.enter_context(manager)
    except (WriteLockTimeout, LockTimeout):
        kind = "busy"
    except FileNotFoundError:
        kind = "missing"
    except OSError:
        kind = "broken"
    return kind


def switch(
    target_uuid,
    *,
    slot,
    store,
    registry,
    home,
    profile_fn,
    lock_path=None,
    backups=None,
    lock_timeout=LOCK_TIMEOUT,
    session_timeout=SESSION_TIMEOUT,
    clock=time.time,
    settle=SETTLE_SECONDS,
    confirm=True,
    storage_timeout=STORAGE_TIMEOUT,
):
    lock_path = Path(lock_path) if lock_path is not None else writelock.default_lock_path()
    job = _Switch(
        target_uuid,
        slot,
        store,
        registry,
        Path(home),
        profile_fn,
        record_path_for(lock_path),
        Path(backups) if backups is not None else default_backups(),
        clock,
        settle,
        recovery.folder_for(lock_path),
        claude_folder=find_claude_folder(home),
        storage_timeout=storage_timeout,
    )
    with ExitStack() as stack:
        kind = _enter(stack, writelock.held(lock_path, timeout=lock_timeout))
        if kind == "busy":
            raise LifelineBusy(LIFELINE_BUSY)
        if kind is not None:
            raise SwitchRefused(NO_LOCK_FILE)
        session = SessionLocks(home, timeout=session_timeout)
        kind = _enter(stack, session)
        if kind == "busy":
            raise SwitchRefused(SESSION_BUSY)
        if kind == "missing":
            raise SwitchRefused(NO_SETTINGS_FOLDER)
        if kind is not None:
            raise SwitchRefused(NO_SESSION_LOCKS)
        result = job.run(session)
    result = replace(result, signin=SealedSignin(job.loaded), proven=job.proven)
    if not confirm:
        return result
    return confirm_switch(
        result,
        slot=slot,
        profile_fn=profile_fn,
        registry=registry,
        lock_path=lock_path,
        lock_timeout=lock_timeout,
        clock=clock,
    )


def confirm_switch(result, *, slot, profile_fn, registry, lock_path=None, lock_timeout=LOCK_TIMEOUT, clock=time.time):
    lock_path = Path(lock_path) if lock_path is not None else writelock.default_lock_path()
    loaded = result.signin.open() if isinstance(result.signin, SealedSignin) else None
    ok, item = _attempt(slot.read)
    value = _oauth_of(item) if ok else None
    confirmed = None
    if loaded is not None and value is not None and value == loaded:
        found = _profile_uuid(value, profile_fn, clock(), registry)
        if found is not None:
            confirmed = found == result.to_uuid
    if confirmed is not None:
        _done(lambda: _mark(slot, record_path_for(lock_path), result, confirmed, value, clock, lock_path, lock_timeout))
    signin = None if confirmed is False else result.signin
    return replace(result, confirmed=confirmed, signin=signin)


def _mark(slot, record_path, result, confirmed, value, clock, lock_path, lock_timeout):
    with writelock.held(lock_path, timeout=lock_timeout):
        if read_record(record_path) != result.to_uuid:
            return
        ok, item = _attempt(slot.read)
        if not ok or _oauth_of(item) != value:
            return
        if confirmed:
            record_confirmed(record_path, result.to_uuid, item, _stamp(clock), proven=result.proven)
        else:
            write_record(record_path, None, clock())


def undo(result, **options):
    previous = getattr(result, "from_uuid", None)
    if not previous:
        raise SwitchRefused("There is no earlier account to switch back to, so nothing was changed.")
    return switch(previous, **options)


class _Switch:
    def __init__(
        self, target_uuid, slot, store, registry, home, profile_fn, record_path, backups, clock, settle, folder,
        *, claude_folder, storage_timeout,
    ):
        self.claude_folder = claude_folder
        self.storage_timeout = storage_timeout
        self.target_uuid = target_uuid
        self.slot = slot
        self.store = store
        self.registry = registry
        self.account_file = home / ACCOUNT_FILE_NAME
        self.profile_fn = profile_fn
        self.record_path = record_path
        self.backups = backups
        self.clock = clock
        self.settle = settle
        self.folder = folder
        self.base = folder.parent
        self.source_uuid = None
        self.live = None
        self.loaded = None
        self.parked_new = False
        self.names = {}
        self.note = None
        self.proven = False
        self.prior = None

    def _source_name(self):
        return self.names[self.source_uuid]

    def _target_name(self):
        return self.names[self.target_uuid]

    def _evidence(self, uuid):
        return ledger.gather(self.base, uuid)

    def _set_aside(self, uuid, oauth):
        return _done(lambda: recovery.set_aside(self.folder, uuid, oauth))

    def _note_seen(self, uuid, oauth):
        _attempt(lambda: ledger.see(self.base, uuid, [oauth]))

    def _note_end(self, uuid, oauth):
        _attempt(lambda: note_signin_end(self.registry, uuid, oauth, _stamp(self.clock)))

    def _record(self, uuid):
        return _done(lambda: write_record(self.record_path, uuid, self.clock()))

    def _record_source(self):
        prior = self.prior
        proven = (
            prior is not None
            and prior["uuid"] == self.source_uuid
            and prior["refresh_sha256"] == fingerprint(self.live)
            and prior["proven"]
        )
        return _done(
            lambda: record_confirmed(
                self.record_path, self.source_uuid, {OAUTH_KEY: self.live}, _stamp(self.clock), proven=proven
            )
        ) or self._record(self.source_uuid)

    def _remove(self, uuid):
        return _done(lambda: self.store.remove(uuid)) or _done(lambda: self.store.remove(uuid))

    def _clear_files(self, pair):
        _attempt(lambda: recovery.remove(self.folder, self.target_uuid, pair))

    def run(self, session):
        target = self._target_account()
        self.prior = read_record_entry(self.record_path)
        item = self._read_slot()
        live = _oauth_of(item)
        if not live:
            raise SwitchRefused(NOT_SIGNED_IN)
        original = self._read_account_file()
        source, why = _identify_why(live, original, self.profile_fn, self.record_path, self.clock, self.registry)
        if source is None:
            raise IdentityUnknown(why)
        self.source_uuid = source
        if source == self.target_uuid:
            raise SwitchRefused(f"Claude Code is already using {target.name}, so nothing was changed.")
        self._check_known(source)
        parked = self._parked_target(target)
        self.live = live
        self.loaded = parked
        self._park(live)
        self._note_seen(source, live)
        self._note_end(source, live)
        if not self._record(None):
            self._abandon("Lifeline could not update its own record.")
        with ExitStack() as stack:
            storage = StorageLock(self.claude_folder, timeout=self.storage_timeout)
            kind = _enter(stack, storage)
            if kind == "busy":
                self._abandon("Claude Code was saving its sign-in.")
            if kind == "missing":
                self._abandon("Lifeline could not find Claude Code's settings folder.")
            if kind is not None:
                self._abandon("Lifeline could not take Claude Code's locks.")
            if session.compromised or storage.compromised:
                self._abandon("Another program took over one of Claude Code's locks during the switch.")
            fresh = self._fresh_item(live, parked)
            self._load(_with_oauth(fresh, parked), live, parked)
            backup = self._relabel(target, live, parked, original)
        if session.compromised or storage.compromised:
            lost = LOCK_LOST.format(name=self._target_name())
            self.note = f"{self.note} {lost}" if self.note else lost
        self._note_seen(self.target_uuid, parked)
        self._note_end(self.target_uuid, parked)
        self._clear_files(parked)
        leftover = not self._release_target(parked)
        self._record(self.target_uuid)
        return SwitchResult(source, self.target_uuid, backup, leftover, note=self.note)

    def _target_account(self):
        ok, account = _attempt(lambda: self.registry.by_uuid(self.target_uuid))
        if not ok:
            raise SwitchRefused(REGISTRY_UNREADABLE)
        if account is None:
            raise SwitchRefused("That account is not in Lifeline, so nothing was changed.")
        if not agrees(account.oauth_account, account):
            raise SwitchRefused(
                f"Lifeline does not have the account details for {account.name}, so nothing was changed. "
                "Sign in to it again in Lifeline."
            )
        self.names[self.target_uuid] = account.name
        return account

    def _read_slot(self):
        ok, item = _attempt(self.slot.read)
        if not ok:
            raise SwitchRefused("Lifeline could not read the sign-in in Claude Code, so nothing was changed.")
        return item

    def _read_account_file(self):
        ok, account = _attempt(lambda: accountfile.read_oauth_account(self.account_file))
        if not ok:
            raise SwitchRefused("Lifeline could not read Claude Code's settings file, so nothing was changed.")
        return account

    def _check_known(self, source):
        ok, account = _attempt(lambda: self.registry.by_uuid(source))
        if not ok:
            raise SwitchRefused(REGISTRY_UNREADABLE)
        if account is None:
            raise SwitchRefused(
                "Claude Code is signed in to an account that is not in Lifeline, so nothing was changed. "
                "Add that account to Lifeline first."
            )
        self.names[source] = account.name

    def _parked_target(self, target):
        ok, choice = _attempt(lambda: recovery.choose(self.store, self.folder, self.target_uuid))
        if not ok:
            raise SwitchRefused(f"Lifeline could not read its saved sign-in for {target.name}, so nothing was changed.")
        parked = choice.pair
        self.proven = choice.proven
        if parked is None and choice.damaged:
            raise SwitchRefused(
                f"Lifeline has no saved sign-in for {target.name} that it can use, so nothing was changed. "
                "A damaged copy in its recovery folder may have held a newer one. Sign in to it again in Lifeline."
            )
        if parked is None:
            raise SwitchRefused(
                f"Lifeline has no saved sign-in for {target.name}, so nothing was changed. "
                "Sign in to it again in Lifeline."
            )
        if not _usable(parked):
            raise SwitchRefused(
                f"The saved sign-in for {target.name} is damaged, so nothing was changed. "
                "Sign in to it again in Lifeline."
            )
        if choice.damaged:
            self.note = (
                f"A copy of the sign-in for {target.name} in Lifeline's recovery folder is damaged, "
                "so the sign-in Lifeline loaded may no longer work. "
                f"If Claude Code asks you to sign in, sign in to {target.name} again in Lifeline."
            )
        return parked

    def _park(self, live):
        name = self._source_name()
        ok, existing = _attempt(lambda: self.store.get(self.source_uuid))
        if not ok:
            raise SwitchRefused(f"Lifeline could not read its saved sign-in for {name}, so nothing was changed.")
        if recovery.same_signin(existing, live):
            return
        if recovery.usable(existing):
            verdict = self._evidence(self.source_uuid).judge(existing, live, live=live)
            if verdict == ledger.FIRST:
                return
            if verdict is None and not self._set_aside(self.source_uuid, existing):
                raise SwitchRefused(f"Lifeline could not keep its saved sign-in for {name}, so nothing was changed.")
        if _done(lambda: self.store.put(self.source_uuid, live)):
            self.parked_new = True
            return
        ok, now = _attempt(lambda: self.store.get(self.source_uuid))
        self.parked_new = ok and now == live
        self._abandon(f"Lifeline could not save the sign-in for {name}.")

    def _release_target(self, parked):
        ok, stored = _attempt(lambda: self.store.get(self.target_uuid))
        if not ok:
            return False
        if recovery.usable(stored) and not recovery.same_signin(stored, parked):
            verdict = self._evidence(self.target_uuid).judge(stored, parked, live=parked)
            if verdict == ledger.FIRST:
                return False
            if verdict is None and not self._set_aside(self.target_uuid, stored):
                return False
        return self._remove(self.target_uuid)

    def _abandon(self, reason):
        name = self._source_name()
        note = ""
        if self.parked_new and not self._remove(self.source_uuid):
            note = f" A spare copy of the sign-in for {name} may be left in Lifeline."
        self._record_source()
        raise SwitchFailed(f"{reason} Lifeline stopped before changing Claude Code, which is still on {name}.{note}")

    def _classify(self, live, parked):
        ok, item = _attempt(self.slot.read)
        if not ok:
            return "unreadable", None
        current = _oauth_of(item)
        if current == parked:
            return "target", item
        if current == live:
            return "source", item
        if not current:
            return "empty", item
        return "other", item

    def _holder(self, live, parked, recheck=("source", "unreadable")):
        state, item = self._classify(live, parked)
        if state in recheck and self.settle > 0:
            time.sleep(self.settle)
            state, item = self._classify(live, parked)
        return state, item

    def _lost_track(self, opening, state):
        source, target = self._source_name(), self._target_name()
        holding = {
            "target": f"the sign-in for {target}",
            "empty": "no sign-in",
            "other": f"a sign-in that is neither {source} nor {target}",
            "unreadable": "a sign-in Lifeline could not read",
        }[state]
        return (
            f"{opening}, and Claude Code now holds {holding}. "
            f"Lifeline stopped and kept its saved sign-ins for {source} and {target}."
        )

    def _fresh_item(self, live, parked):
        ok, item = _attempt(self.slot.read)
        if ok and _oauth_of(item) == live:
            return item
        state, _ = self._holder(live, parked)
        if state == "source":
            self._abandon("Lifeline could not read the sign-in in Claude Code a second time.")
        raise SwitchFailed(self._lost_track("The sign-in in Claude Code changed during the switch", state))

    def _nudge(self, stay_on=None):
        if nudge_sessions(self.claude_folder) != "failed" or stay_on is None:
            return
        stays = SESSIONS_STAY.format(name=self.names[stay_on])
        self.note = f"{self.note} {stays}" if self.note else stays

    def _load(self, new_item, live, parked):
        written = _done(lambda: self.slot.write(new_item))
        self._nudge(self.source_uuid)
        if written:
            return
        state, _ = self._holder(live, parked)
        if state == "target":
            return
        if state == "source":
            self._abandon("Lifeline could not write the new sign-in into Claude Code.")
        raise SwitchFailed(self._lost_track("Lifeline could not write the new sign-in", state))

    def _relabel(self, target, live, parked, original):
        notes = []
        ok, backup = _attempt(
            lambda: accountfile.replace_oauth_account(self.account_file, target.oauth_account, self.backups, notes=notes)
        )
        if ok:
            if notes:
                self.note = " ".join([*notes, self.note] if self.note else notes)
            return backup
        self._unload(target, live, parked, original)

    def _unload(self, target, live, parked, original):
        source_name, target_name = self._source_name(), target.name
        opening = "Lifeline could not update the account details in Claude Code's settings"
        state, item = self._holder(live, parked)
        if state == "target":
            written = _done(lambda: self.slot.write(_with_oauth(item, live)))
            self._nudge()
            if written:
                state = "source"
            else:
                state, _ = self._holder(live, parked, recheck=("target", "unreadable"))
        if state == "target":
            notes = [WRONG_NAME]
            if self.note is not None:
                notes.append(self.note)
            self._clear_files(parked)
            if not self._release_target(parked):
                notes.append(f"A spare copy of the sign-in for {target_name} may be left in Lifeline.")
            self._record(self.target_uuid)
            message = (
                f"{opening}, and it could not switch back. Claude Code now uses {target_name}, "
                f"and the sign-in for {source_name} is kept in Lifeline."
            )
            raise SwitchFailed(" ".join([message, *notes]))
        if state != "source":
            raise SwitchFailed(self._lost_track(f"{opening} or switch back", state))
        notes = []
        if self.parked_new and not self._remove(self.source_uuid):
            notes.append(f"A spare copy of the sign-in for {source_name} may be left in Lifeline.")
        ok, now = _attempt(lambda: accountfile.read_oauth_account(self.account_file))
        if ok and now == target.oauth_account and now != original:
            restored = isinstance(original, dict) and _done(
                lambda: accountfile.replace_oauth_account(self.account_file, original, self.backups)
            )
            if not restored:
                notes.append(WRONG_NAME)
        elif not ok:
            notes.append(WRONG_NAME)
        self._record_source()
        message = f"{opening}, so it switched back to {source_name}. Nothing was lost."
        raise SwitchFailed(" ".join([message, *notes]))


def _seed_from(seed, chosen_uuid):
    if not isinstance(seed, SwitchResult) or seed.to_uuid != chosen_uuid:
        return None
    if seed.confirmed is False or not isinstance(seed.signin, SealedSignin):
        return None
    value = seed.signin.open()
    return value if _usable(value) else None


class Watcher:
    def __init__(
        self,
        slot,
        store,
        registry,
        chosen_uuid,
        profile_fn,
        minutes=WATCH_MINUTES,
        clock=time.monotonic,
        *,
        lifetime=_UNSET,
        seed=None,
        home=None,
        lock_path=None,
        backups=None,
        wall_clock=time.time,
        lock_timeout=5.0,
        session_timeout=10.0,
        settle=SETTLE_SECONDS,
        storage_timeout=STORAGE_TIMEOUT,
    ):
        self.slot = slot
        self.store = store
        self.registry = registry
        self.chosen_uuid = chosen_uuid
        self.profile_fn = profile_fn
        self.home = Path(home) if home is not None else Path.home()
        self.lock_path = Path(lock_path) if lock_path is not None else writelock.default_lock_path()
        self.record_path = record_path_for(self.lock_path)
        self.folder = recovery.folder_for(self.lock_path)
        self.base = self.lock_path.parent
        self.backups = Path(backups) if backups is not None else default_backups()
        self.account_file = self.home / ACCOUNT_FILE_NAME
        self.claude_folder = find_claude_folder(self.home)
        self._unmoved = False
        self.storage_timeout = storage_timeout
        self.lock_timeout = lock_timeout
        self.session_timeout = session_timeout
        self.settle = settle
        self._clock = clock
        self._wall = wall_clock
        self._window = minutes * 60
        span = minutes if lifetime is _UNSET else lifetime
        self._lifetime = None if span is None else span * 60
        self._start = clock()
        self._good = _seed_from(seed, chosen_uuid)
        self._proof = None
        self._confirmed = None
        self._settled = None
        self._spent = set()
        self._unwritten = set()
        self._spares = set()
        self._previous = None
        self._safe = None
        self._parked = None
        self._known = None
        self._failed = None
        self._noted = _UNSET
        self._stuck = None
        self._stale_seen = None
        self._live = None
        self._unkept = []
        self._marked = []
        self._unseen = []
        self._seen_marks = set()
        self._fought = {}
        self._notes = set()
        self._names = {}
        self._ends = {}
        self._deferred = []
        self._blocked_since = None
        self._done = False

    @property
    def active(self):
        return not self._done

    def poll(self):
        reports = self._release()
        if self._done:
            return reports
        ending = self._lifetime is not None and self._clock() - self._start >= self._lifetime
        ok, item = self._read()
        if not ok:
            return reports
        current = _oauth_of(item)
        if self._is_settled(current):
            if ending:
                self._done = True
            return reports
        if not current:
            self._locked(lambda session: self._away(None, current, reports, "signed_out", session))
            return reports
        owner, confirmed = self._whose(current)
        if owner is not None:
            self._note_end(owner, current)
        if owner == self.chosen_uuid:
            self._home(current, confirmed, reports)
            if ending and not self._done and self._is_settled(current):
                self._done = True
            if not self._done and self._give_up_on_ledger(current, ending):
                self._done = True
                path = self.base / ledger.NAME
                self._say(reports, lambda name: LEDGER_STOP.format(name=name, path=path), self.chosen_uuid)
            return reports
        if ending:
            self._locked(lambda session: self._away(owner, current, reports, "end", session))
            return reports
        if owner is None:
            self._unknown(current, reports, None)
            return reports
        if self._good is None:
            reason = "no_copy"
        elif self._recently_fought(owner):
            reason = "again"
        else:
            known = self._registered(owner)
            if known is None:
                return reports
            if not known:
                self._unknown(current, reports, owner)
                return reports
            reason = "fight"
        self._locked(lambda session: self._away(owner, current, reports, reason, session))
        return reports

    def _read(self):
        ok, item = _attempt(self.slot.read)
        if ok:
            self._live = _oauth_of(item)
        return ok, item

    def _give_up_on_ledger(self, current, ending):
        blocked = (
            bool(self._unwritten)
            and not self._unkept
            and not self._marked
            and self._parked is None
            and current is not None
            and current == self._good
        )
        if not blocked:
            self._blocked_since = None
            return False
        if self._blocked_since is None:
            self._blocked_since = self._clock()
        if ending:
            return True
        return self._lifetime is None and self._clock() - self._blocked_since >= self._window

    def _is_settled(self, current):
        return (
            current is not None
            and self._settled is not None
            and current == self._settled
            and current == self._good
            and self._parked is None
        )

    def _name(self, uuid):
        if uuid in self._names:
            return self._names[uuid]
        ok, account = _attempt(lambda: self.registry.by_uuid(uuid))
        if not ok:
            return None
        if account is not None and account.name:
            self._names[uuid] = account.name
            return account.name
        return OUTSIDER

    def _registered(self, uuid):
        ok, account = _attempt(lambda: self.registry.by_uuid(uuid))
        if not ok:
            return None
        if account is not None and account.name:
            self._names[uuid] = account.name
        return account is not None

    def _say(self, reports, build, *uuids):
        names = [self._name(uuid) for uuid in uuids]
        if any(name is None for name in names):
            self._deferred.append((build, uuids))
            return
        reports.append(build(*names))

    def _release(self):
        waiting, self._deferred = self._deferred, []
        reports = []
        for build, uuids in waiting:
            self._say(reports, build, *uuids)
        return reports

    def distrust(self, value):
        if self._good is not None and value == self._good:
            self._good = None
            self._known = None
            self._settled = None
            self._safe = None

    def rescue(self):
        good = self._good
        if not _usable(good):
            return False
        spent = self._previous
        ok, _ = _attempt(lambda: recovery.rescue(self.folder, self.chosen_uuid, good, spent=spent))
        return ok

    def _note(self, reports, key, build, *uuids):
        if key not in self._notes:
            self._notes.add(key)
            self._say(reports, build, *uuids)

    def _recently_fought(self, owner):
        when = self._fought.get(owner)
        return when is not None and self._clock() - when < self._window

    def _ledger(self):
        return ledger.spent(self.base, self.chosen_uuid)

    def _note_end(self, owner, value):
        at = _stamp(self._wall)
        ok, text = _attempt(lambda: signin_end_text(value, at))
        if not ok or text is None or self._ends.get(owner, _UNSET) == text:
            return
        ok, account = _attempt(lambda: self.registry.by_uuid(owner))
        if not ok or account is None:
            return
        if account.signin_ends is not None and account.signin_ends != text:
            with ExitStack() as stack:
                if _enter(stack, writelock.held(self.lock_path, timeout=self.lock_timeout)) is not None:
                    return
                if not _done(lambda: note_signin_end(self.registry, owner, value, at)):
                    return
        self._ends[owner] = text

    def _evidence(self):
        return ledger.gather(self.base, self.chosen_uuid).adding(self._spent)

    def _adopt(self, value):
        old = self._good
        if old is None:
            self._proof = False
        elif value != old:
            mark = fingerprint(old)
            if mark is None:
                self._proof = False
            elif mark != fingerprint(value):
                if _later(value, old):
                    self._spent.add(mark)
                    self._unwritten.add(mark)
                    self._marked.append(old)
                    self._previous = mark
                    self._proof = True
                elif self._evidence().by_ledger(value, old) == ledger.FIRST:
                    self._proof = True
                else:
                    self._unkept.append(old)
                    self._parked = None
                    self._proof = False
        self._see(value)
        self._good = value

    def _see(self, value):
        mark = fingerprint(value)
        if mark is not None and mark not in self._seen_marks:
            self._seen_marks.add(mark)
            self._unseen.append(value)

    def _take_stored(self, stored, proven):
        old = self._good
        if old is not None and not recovery.same_signin(old, stored):
            if not proven and not self._set_aside(self.chosen_uuid, old):
                return False
            mark = fingerprint(old)
            self._previous = mark if self._evidence().is_spent(old) else None
        if not recovery.same_signin(old, stored):
            self._proof = bool(proven)
        self._good = stored
        return True

    def _set_aside(self, uuid, oauth):
        return _done(lambda: recovery.set_aside(self.folder, uuid, oauth))

    def _stale(self, value):
        return self._known_spent(value)

    def _known_spent(self, value):
        digest = fingerprint(value)
        return digest is not None and (digest in self._spent or digest in self._ledger())

    def _decide(self, stored):
        good = self._good
        if not recovery.usable(stored):
            return GOOD, PROOF
        if good is None:
            return STORED, PROOF
        if recovery.same_signin(stored, good):
            return None, PROOF
        evidence = self._evidence()
        verdict = evidence.by_ledger(stored, good)
        if verdict is None and self._parked is not None and not recovery.same_signin(stored, self._parked):
            return STORED, PARKED
        if verdict is None and self._proof is not False:
            verdict = evidence.by_record(stored, good, self._live)
        if verdict is None and fingerprint(stored) in self._spares:
            verdict = ledger.SECOND
        if verdict is not None:
            return (STORED if verdict == ledger.FIRST else GOOD), PROOF
        if _older(good, stored):
            return STORED, ORDER
        if _older(stored, good):
            return GOOD, ORDER
        tie = evidence.by_seen(stored, good)
        if tie is not None:
            return (STORED if tie == ledger.FIRST else GOOD), ORDER
        return None, ORDER

    def _observe_spare(self, current):
        if self._parked is not None:
            return
        ok, stored = _attempt(lambda: self.store.get(self.chosen_uuid))
        if not ok or not recovery.usable(stored) or recovery.same_signin(stored, current):
            return
        winner, kind = self._decide(stored)
        if winner == GOOD and kind == PROOF:
            mark = fingerprint(stored)
            if mark is not None:
                self._spares.add(mark)

    def _stored(self):
        ok, choice = _attempt(lambda: recovery.choose(self.store, self.folder, self.chosen_uuid))
        return ok, (choice.pair if ok else None)

    def _prefer_stored(self, stored):
        winner, kind = self._decide(stored)
        return winner == STORED and kind in (PROOF, PARKED)

    def _whose(self, value):
        if self._known is not None and self._known[0] == value:
            return self._known[1], self._known[2]
        held = self._failed
        if held is not None and held[0] == value and self._clock() - held[1] < UNKNOWN_HOLD_SECONDS:
            return None, False
        refresh = _refresh_of(value)
        digest = fingerprint(value)
        owner, confirmed = None, False
        if refresh is not None and self._good is not None and refresh == _refresh_of(self._good):
            owner = self.chosen_uuid
        if owner is None and digest is not None and (digest in self._spent or digest in self._ledger()):
            owner = self.chosen_uuid
        if owner is None:
            owner = _profile_uuid(value, self.profile_fn, self._wall(), self.registry)
            confirmed = owner is not None
        if owner is None:
            entry = read_record_entry(self.record_path)
            if entry is not None and entry["uuid"] is not None and digest is not None and entry["refresh_sha256"] == digest:
                owner = entry["uuid"]
        if owner is None:
            owner = self._match_store(refresh)
        if owner is None:
            self._failed = (value, self._clock())
            return None, False
        self._known = (value, owner, confirmed)
        return owner, confirmed

    def _match_store(self, refresh):
        if refresh is None:
            return None
        ok, accounts = _attempt(self.registry.load)
        if not ok:
            return None
        for account in accounts:
            ok, parked = _attempt(lambda uuid=account.uuid: self.store.get(uuid))
            if ok and isinstance(parked, dict) and parked.get("refreshToken") == refresh:
                return account.uuid
        return None

    def _locked(self, action):
        with ExitStack() as stack:
            if _enter(stack, writelock.held(self.lock_path, timeout=self.lock_timeout)) is not None:
                return False
            session = SessionLocks(self.home, timeout=self.session_timeout)
            if _enter(stack, session) is not None:
                return False
            action(session)
            return True

    def _record_plan(self):
        entry = read_record_entry(self.record_path)
        if entry is None or entry["uuid"] != self.chosen_uuid:
            return None
        mark = fingerprint(self._good)
        proven = self._proof is True
        if self._confirmed is not None and self._confirmed == self._good and entry["refresh_sha256"] != mark:
            return mark, proven, True
        if self._proof is False and entry["proven"]:
            return entry["refresh_sha256"], False, False
        return None

    def _record_due(self):
        return self._record_plan() is not None

    def _may_replace_record(self):
        entry = read_record_entry(self.record_path)
        mark = entry["refresh_sha256"] if entry is not None else None
        if mark is None or mark == fingerprint(self._good):
            return True
        ok, stored = _attempt(lambda: self.store.get(self.chosen_uuid))
        if not ok:
            return False
        if fingerprint(stored) != mark or recovery.same_signin(stored, self._good):
            return True
        return not self._prefer_stored(stored)

    def _flush_spent(self, reports):
        ok = True
        if self._unwritten:
            marks = set(self._unwritten)
            if _done(lambda: ledger.add(self.base, self.chosen_uuid, marks)):
                self._unwritten -= marks
                self._marked = []
            else:
                self._unkept.extend(self._marked)
                self._marked = []
                ok = False
        while self._unkept and self._set_aside(self.chosen_uuid, self._unkept[0]):
            self._unkept.pop(0)
        if self._unkept:
            ok = False
        if self._unseen:
            pairs = list(self._unseen)
            self._unseen = []
            _attempt(lambda: ledger.see(self.base, self.chosen_uuid, pairs))
        if ok:
            self._notes.discard("record")
            return True
        self._note(reports, "record", lambda: RECORD_RETRY)
        return False

    def _home(self, current, confirmed, reports):
        if self._good is not None and current != self._good and self._stale(current):
            self._locked(lambda session: self._restore(current, reports, session))
            return
        self._adopt(current)
        if confirmed:
            self._confirmed = current
        self._observe_spare(current)
        check = self._known_spent(current)
        quiet = not (self._unwritten or self._unkept or self._unseen)
        if self._parked is None and quiet and not check and not self._record_due():
            self._safe = None
            self._settled = current
            return
        self._locked(lambda session: self._settle(reports, session))

    def _settle(self, reports, session, restore=True):
        if not self._flush_spent(reports):
            return False
        ok, item = self._read()
        if not ok:
            return False
        now = _oauth_of(item)
        if now != self._good:
            if restore and now and self._known_spent(now):
                self._restore(now, reports, session)
            return False
        if restore:
            found, stored = self._stored()
            if found and self._prefer_stored(stored):
                self._restore(now, reports, session)
                return False
        plan = self._record_plan()
        if plan is not None:
            digest, proven, guarded = plan
            if guarded and not self._may_replace_record():
                return False
            at = _stamp(self._wall)
            if digest == fingerprint(now):
                written = _done(lambda: record_confirmed(self.record_path, self.chosen_uuid, item, at, proven=proven))
            else:
                written = _done(lambda: _write_record(self.record_path, self.chosen_uuid, at, digest, proven))
            if not written:
                self._note(reports, "record", lambda: RECORD_RETRY)
                return False
            self._notes.discard("record")
        if not self._unpark():
            return False
        self._settled = self._good
        return True

    def _restore(self, current, reports, session):
        if not self._still_chosen(reports):
            return
        ok, item = self._read()
        if not ok or _oauth_of(item) != current:
            return
        if not self._secure(reports):
            return
        outcome = self._write_back(current, session)
        if outcome == "stuck":
            self._note_stuck(reports)
        if outcome != "done":
            return
        self._stale_seen = None
        self._known = (self._good, self.chosen_uuid, False)
        self._settle(reports, session, restore=False)

    def _note_stuck(self, reports):
        state, seen = self._stuck
        if self._stale_seen == (state, seen):
            return
        self._stale_seen = (state, seen)
        text = STALE_NOTES[state]
        self._say(reports, lambda name: text.format(name=name), self.chosen_uuid)

    def _unknown(self, current, reports, outsider):
        if self._good is not None and self._safe != self._good:
            self._locked(lambda session: self._away(outsider, current, reports, "unknown", session))
            if self._done:
                return
        if self._noted is not _UNSET and self._noted == current:
            return
        self._noted = current
        secured = self._good is not None and self._safe == self._good
        if outsider is not None:
            opening = "Another app signed Claude Code in to an account that is not in Lifeline."
            if secured:
                self._say(reports, lambda chosen: f"{opening} Lifeline saved {chosen} and left the other sign-in in place.", self.chosen_uuid)
            else:
                reports.append(f"{opening} Lifeline left it in place.")
        elif secured:
            self._say(
                reports,
                lambda chosen: (
                    "The sign-in in Claude Code changed, and Lifeline could not check whose it is. "
                    f"Lifeline saved {chosen} and left the new sign-in alone for now."
                ),
                self.chosen_uuid,
            )
        else:
            reports.append(UNKNOWN_SIGNIN)

    def _still_chosen(self, reports):
        recorded = read_record(self.record_path)
        if recorded == self.chosen_uuid:
            return True
        if self._good is not None:
            ok, item = self._read()
            current = _oauth_of(item) if ok else None
            holds = ok and _refresh_of(current) == _refresh_of(self._good)
            if not holds:
                ok_store, stored = self._stored()
                wrote = False
                if ok_store and not (recovery.usable(stored) and recovery.same_signin(stored, self._good)):
                    winner, kind = self._decide(stored)
                    proven = kind == PROOF
                    if winner == STORED:
                        if not proven:
                            ok_store = self._set_aside(self.chosen_uuid, self._good)
                    else:
                        if recovery.usable(stored) and not proven:
                            ok_store = self._set_aside(self.chosen_uuid, stored)
                        if ok_store:
                            ok_store = wrote = _done(lambda: self.store.put(self.chosen_uuid, self._good))
                if not ok_store:
                    self._note_save(reports)
                    return False
                if wrote:
                    self._say_stood_down(reports, ok and not current, recorded)
        self._done = True
        return False

    def _say_stood_down(self, reports, signed_out, recorded):
        def build(chosen, *others):
            if signed_out:
                cause = "Claude Code was signed out"
            elif others:
                cause = f"Claude Code was switched to {others[0]}"
            else:
                cause = "Lifeline's record of the last switch was cleared"
            return f"Lifeline stopped watching {chosen} because {cause}. {chosen} is saved in Lifeline, ready to switch back to."

        uuids = (self.chosen_uuid,) if signed_out or recorded is None else (self.chosen_uuid, recorded)
        self._say(reports, build, *uuids)

    def _note_save(self, reports):
        self._note(
            reports,
            "save",
            lambda chosen: f"Lifeline could not save the sign-in for {chosen} yet. It will try again.",
            self.chosen_uuid,
        )

    def _secure(self, reports):
        if self._good is None or self._safe == self._good:
            return True
        ok, stored = self._stored()
        if ok:
            winner, kind = self._decide(stored)
            proven = kind == PROOF
            if winner == STORED:
                ok = self._take_stored(stored, proven)
                if ok:
                    self._safe = self._good
                    self._notes.discard("save")
                    return True
            elif not (recovery.usable(stored) and recovery.same_signin(stored, self._good)):
                if recovery.usable(stored) and not proven:
                    ok = self._set_aside(self.chosen_uuid, stored)
                if ok:
                    ok = _done(lambda: self.store.put(self.chosen_uuid, self._good))
                    if ok:
                        self._parked = self._good
        if not ok:
            self._note_save(reports)
            return False
        self._notes.discard("save")
        self._safe = self._good
        return True

    def _unpark(self):
        ok, parked = _attempt(lambda: self.store.get(self.chosen_uuid))
        if not ok:
            return False
        ours = parked is not None and (
            recovery.same_signin(parked, self._good)
            or recovery.same_signin(parked, self._parked)
            or self._known_spent(parked)
        )
        if ours and not _done(lambda: self.store.remove(self.chosen_uuid)):
            return False
        _attempt(lambda: recovery.remove(self.folder, self.chosen_uuid, self._good))
        self._parked = None
        self._safe = None
        return True

    def _away(self, owner, value, reports, reason, session):
        if not self._still_chosen(reports):
            return
        ok, item = self._read()
        if not ok or _oauth_of(item) != value:
            return
        if not self._secure(reports):
            return
        if reason == "unknown":
            return
        if reason == "fight":
            outcome = self._fight(owner, value, reports, session)
            if outcome != "stuck":
                return
            reason = "stuck"
        self._hand_over(owner, value, reports, reason)

    def _classify(self, value):
        ok, item = self._read()
        if not ok:
            return "unreadable", None
        current = _oauth_of(item)
        if current == self._good:
            return "chosen", current
        if current == value:
            return "foreign", current
        return "other", current

    def _holder(self, value):
        state, seen = self._classify(value)
        if state in ("foreign", "unreadable") and self.settle > 0:
            time.sleep(self.settle)
            state, seen = self._classify(value)
        return state, seen

    def _write_back(self, value, session):
        if session.compromised:
            return "compromised"
        ok, stored = self._stored()
        if ok:
            winner, kind = self._decide(stored)
            if winner == STORED:
                self._take_stored(stored, kind == PROOF)
        with ExitStack() as stack:
            storage = StorageLock(self.claude_folder, timeout=self.storage_timeout)
            if _enter(stack, storage) is not None:
                return "retry"
            ok, item = self._read()
            if not ok or _oauth_of(item) != value:
                return "retry"
            _done(lambda: self.slot.write(_with_oauth(item, self._good)))
            self._unmoved = nudge_sessions(self.claude_folder) == "failed"
        state, seen = self._holder(value)
        if state != "chosen":
            self._stuck = (state, seen)
            return "stuck"
        self._see(self._good)
        return "done"

    def _keep_foreign(self, owner, value):
        ok, existing = _attempt(lambda: self.store.get(owner))
        if not ok:
            return False, False
        if recovery.same_signin(existing, value):
            return True, False
        if not recovery.usable(existing):
            wrote = _done(lambda: self.store.put(owner, value))
            return wrote, wrote
        verdict = ledger.gather(self.base, owner).judge(existing, value, live=value)
        if verdict == ledger.FIRST:
            return True, False
        if verdict is None and _older(value, existing):
            return self._set_aside(owner, value), False
        if verdict is None and not self._set_aside(owner, existing):
            return False, False
        wrote = _done(lambda: self.store.put(owner, value))
        return wrote, wrote

    def _fight(self, owner, value, reports, session):
        ok, wrote = self._keep_foreign(owner, value)
        if not ok:
            return "stuck"
        outcome = self._write_back(value, session)
        if outcome == "compromised":
            if wrote:
                _done(lambda: self.store.remove(owner))
            return "retry"
        if outcome != "done":
            return outcome
        self._fought[owner] = self._clock()
        self._known = (self._good, self.chosen_uuid, False)
        self._settle(reports, session, restore=False)
        named = self._relabel(reports)

        unmoved = self._unmoved

        def build(foreign, chosen):
            sentence = (
                f"Another app switched Claude Code to {foreign}. "
                f"Lifeline kept the newest sign-in for {foreign} and switched back to {chosen}."
            )
            if not named:
                sentence = f"{sentence} {WRONG_NAME}"
            return f"{sentence} {SESSIONS_STAY.format(name=foreign)}" if unmoved else sentence

        self._say(reports, build, owner, self.chosen_uuid)
        return "done"

    def _release_owner(self, owner, value, parked):
        if recovery.usable(parked) and not recovery.same_signin(parked, value):
            verdict = ledger.gather(self.base, owner).judge(parked, value, live=value)
            if verdict == ledger.FIRST:
                return
            if verdict is None and (_older(value, parked) or not self._set_aside(owner, parked)):
                return
        elif not _not_older(value, parked):
            return
        _done(lambda: self.store.remove(owner))

    def _hand_over(self, owner, value, reports, reason):
        if owner is not None and owner != self.chosen_uuid:
            ok, item = self._read()
            if ok and _oauth_of(item) == value:
                ok, parked = _attempt(lambda: self.store.get(owner))
                if ok and parked is not None:
                    self._release_owner(owner, value, parked)
        if not _done(lambda: write_record(self.record_path, None, self._wall())):
            self._note(reports, "record", lambda: RECORD_RETRY)
            return
        self._done = True
        saved = self._good is not None
        uuids = (self.chosen_uuid,) if owner is None else (self.chosen_uuid, owner)
        self._say(reports, lambda *names: _handover_sentence(reason, saved, *names), *uuids)

    def _relabel(self, reports):
        ok, current = _attempt(lambda: accountfile.read_oauth_account(self.account_file))
        if not ok:
            return False
        ok, account = _attempt(lambda: self.registry.by_uuid(self.chosen_uuid))
        if not ok or account is None:
            return False
        if labels(current, account):
            return True
        details = account.oauth_account
        if agrees(details, account):
            notes = []
            named = _done(lambda: accountfile.replace_oauth_account(self.account_file, details, self.backups, notes=notes))
            reports.extend(notes)
            return named
        return False


def _handover_sentence(reason, saved, chosen, foreign=None):
    kept = f" {chosen} is saved in Lifeline, ready to switch back to." if saved else ""
    if reason == "signed_out":
        return f"Claude Code was signed out.{kept}"
    if reason == "no_copy":
        return (
            f"Another app switched Claude Code to {foreign}. Lifeline had no checked copy of {chosen} "
            f"to put back, so it left Claude Code on {foreign}."
        )
    if reason == "again":
        return f"Another app switched Claude Code to {foreign} again, so Lifeline left it there this time.{kept}"
    if reason == "stuck":
        return f"Another app switched Claude Code to {foreign}, and Lifeline could not switch back.{kept}"
    if foreign is not None:
        return f"Claude Code is on {foreign} at the end of the watch.{kept}"
    return f"Claude Code holds a sign-in Lifeline could not check at the end of the watch.{kept}"
