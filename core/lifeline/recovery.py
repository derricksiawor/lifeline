import fcntl
import json
import math
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from lifeline import ledger, writelock
from lifeline.accounts import signin_end
from lifeline.store import StoreLeftover

FOLDER_NAME = "recovery"
OAUTH_KEY = "claudeAiOauth"
SPENT_KEY = "spent"
ASIDE_KEY = "set_aside"
SAVED = "saved"
HELD = "held"
SUPERSEDED = "superseded"
ABSENT = "absent"
PENDING = "pending"
VALID_UUID = re.compile(r"[A-Za-z0-9-]+", re.ASCII)
FINAL_NAME = re.compile(r"([A-Za-z0-9-]+)\.json", re.ASCII)
LEFTOVER_NAME = re.compile(r"\.([A-Za-z0-9-]+)\.[A-Za-z0-9_]+\.tmp", re.ASCII)
RESCUE_NAME = re.compile(r"([A-Za-z0-9-]+)\.rescue\.[A-Za-z0-9_]+\.json", re.ASCII)
RESCUE_TEMP = re.compile(r"\.([A-Za-z0-9-]+)\.rescue\.[A-Za-z0-9_]+\.tmp", re.ASCII)
FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)
PROBE_PREFIX = "._probe-"
UNDATED_KEEP_SECONDS = 45 * 86400
RESCUE_TRIES = 5
FINAL = "final"
LEFTOVER = "leftover"
RESCUE = "rescue"
RESCUE_LEFTOVER = "rescue leftover"
BAD_ID = "Lifeline names each account in its files with letters, digits and hyphens only."
INCOMPLETE = "Only a complete sign-in can be kept in the recovery folder."
BAD_SPENT = "The spent sign-in must be given as a sign-in or as its renewal fingerprint."
NOT_WRITTEN = "The renewed sign-in could not be written to the recovery folder."
NOT_RESCUED = "The sign-in could not be written to the recovery folder."
UNREADABLE = "Lifeline could not read its saved sign-in, so it cannot tell which sign-in to use."


class RecoveryError(Exception):
    pass


@dataclass(frozen=True)
class _Entry:
    path: Path
    kind: str
    pair: dict = field(repr=False)
    spent: str | None
    stamp: int
    aside: bool = False


def folder_for(lock_path=None):
    base = Path(lock_path).parent if lock_path is not None else writelock.lifeline_folder()
    return base / FOLDER_NAME


def usable(oauth):
    return isinstance(oauth, dict) and all(
        isinstance(oauth.get(key), str) and oauth.get(key) for key in ("accessToken", "refreshToken")
    )


def same_signin(first, second):
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    return (first.get("refreshToken"), first.get("accessToken")) == (second.get("refreshToken"), second.get("accessToken"))


def fingerprint(oauth):
    return ledger.fingerprint(oauth)


def _mark(spent):
    if spent is None:
        return None
    if isinstance(spent, dict):
        return fingerprint(spent)
    if isinstance(spent, str) and FINGERPRINT.fullmatch(spent):
        return spent
    raise RecoveryError(BAD_SPENT)


def _marks(spent):
    mark = _mark(spent)
    return frozenset() if mark is None else frozenset([mark])


def _expiry(oauth):
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _age(oauth):
    expiry = _expiry(oauth)
    return -math.inf if expiry is None else expiry


def _ended(oauth):
    moment = datetime.now(timezone.utc)
    try:
        end = signin_end(oauth, moment)
    except Exception:
        return False
    return end is not None and end <= moment


def _undated(oauth):
    try:
        return signin_end(oauth, datetime.now(timezone.utc)) is None
    except Exception:
        return False


def _stale(entry):
    written = entry.stamp / 1e9
    return _undated(entry.pair) and datetime.now(timezone.utc).timestamp() - written >= UNDATED_KEEP_SECONDS


def _gone(entry):
    return _ended(entry.pair) or _stale(entry)


def _key(oauth, evidence, gone=None):
    seen = evidence.seen.get(fingerprint(oauth))
    dead = _ended(oauth) if gone is None else gone
    return (not evidence.is_spent(oauth), not dead, _age(oauth), -math.inf if seen is None else seen)


def _keeps(stored, recovered, evidence, aside=False, gone=None):
    if not usable(stored) or same_signin(stored, recovered):
        return False
    dead_stored, dead_recovered = _ended(stored), _ended(recovered) if gone is None else gone
    if dead_stored != dead_recovered:
        return dead_recovered
    verdict = evidence.judge(stored, recovered)
    if verdict is not None:
        return verdict == ledger.FIRST
    first, second = _expiry(stored), _expiry(recovered)
    if first is not None and first == second:
        tie = evidence.by_seen(stored, recovered)
        if tie is not None:
            return tie == ledger.FIRST
    if aside:
        return not (first is not None and second is not None and second > first)
    return first is not None and second is not None and first > second


def _bare(spent):
    return ledger.Evidence(None, _marks(spent))


def newer(stored, recovered, spent=None):
    return _keeps(stored, recovered, _bare(spent))


def _base(folder):
    return Path(folder).parent


def _evidence(folder, uuid, entries=(), spent=None):
    marks = {entry.spent for entry in entries if entry.spent is not None} | set(_marks(spent))
    return ledger.gather(_base(folder), uuid, extra=marks)


def _check(uuid):
    if not isinstance(uuid, str) or VALID_UUID.fullmatch(uuid) is None:
        raise RecoveryError(BAD_ID)


def _valid(uuid):
    return isinstance(uuid, str) and VALID_UUID.fullmatch(uuid) is not None


def _final(folder, uuid):
    return Path(folder) / f"{uuid}.json"


def _sync(folder):
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


def _prepare(folder):
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)


def _load(path, in_use=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                return None
            if in_use:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    return None
            data = json.loads(handle.read())
    except (OSError, ValueError, RecursionError):
        return None
    pair = data.get(OAUTH_KEY) if isinstance(data, dict) else None
    if not usable(pair):
        return None
    mark = data.get(SPENT_KEY)
    if not isinstance(mark, str) or FINGERPRINT.fullmatch(mark) is None or mark == fingerprint(pair):
        mark = None
    return pair, mark, facts.st_mtime_ns, data.get(ASIDE_KEY) is True


def _read_pair(path):
    found = _load(path)
    return None if found is None else found[0]


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _payload(oauth, mark, aside=False):
    data = {OAUTH_KEY: oauth}
    if mark is not None:
        data[SPENT_KEY] = mark
    if aside:
        data[ASIDE_KEY] = True
    return json.dumps(data, allow_nan=False)


def _encode(oauth, spent, failure, aside=False):
    _check_pair(oauth)
    mark = _mark(spent)
    try:
        return _payload(oauth, mark, aside)
    except (TypeError, ValueError, RecursionError):
        pass
    raise RecoveryError(failure)


def _check_pair(oauth):
    if not usable(oauth):
        raise RecoveryError(INCOMPLETE)


def write(folder, uuid, oauth, spent=None):
    _check(uuid)
    text = _encode(oauth, spent, NOT_WRITTEN)
    failed = False
    try:
        _prepare(folder)
    except OSError:
        failed = True
    if failed:
        raise RecoveryError(NOT_WRITTEN)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(dir=folder, prefix=f".{uuid}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, _final(folder, uuid))
        temp = None
        _sync(folder)
    except OSError:
        failed = True
        if temp is not None:
            try:
                _unlink(temp)
            except OSError:
                pass
    if failed:
        raise RecoveryError(NOT_WRITTEN)


def rescue(folder, uuid, oauth, spent=None):
    return _rescue(folder, uuid, oauth, spent, False)


def set_aside(folder, uuid, oauth):
    if usable(oauth) and _ended(oauth):
        return None
    if _valid(uuid):
        for entry in _entries(folder, uuid):
            if same_signin(entry.pair, oauth):
                return entry.path
    return _rescue(folder, uuid, oauth, None, True)


def _rescue(folder, uuid, oauth, spent, aside):
    _check(uuid)
    data = _encode(oauth, spent, NOT_RESCUED, aside).encode("utf-8")
    failed = False
    try:
        _prepare(folder)
    except OSError:
        failed = True
    if failed:
        raise RecoveryError(NOT_RESCUED)
    fd = None
    temp = None
    complete = False
    placed = None
    try:
        fd, temp = tempfile.mkstemp(dir=folder, prefix=f".{uuid}.rescue.", suffix=".tmp")
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        complete = True
        for _ in range(RESCUE_TRIES):
            name = Path(folder) / f"{uuid}.rescue.{secrets.token_hex(8)}.json"
            try:
                os.link(temp, name)
            except FileExistsError:
                continue
            placed = name
            break
        if placed is None:
            raise FileExistsError()
        _sync(folder)
        try:
            _unlink(temp)
        except OSError:
            pass
        _sync(folder)
    except OSError:
        failed = True
        if temp is not None and not complete:
            try:
                _unlink(temp)
            except OSError:
                pass
    finally:
        if fd is not None:
            os.close(fd)
    if failed:
        raise RecoveryError(NOT_RESCUED)
    return placed


def writable(folder):
    temp = None
    try:
        _prepare(folder)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=PROBE_PREFIX, suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(b"lifeline")
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(temp)
        temp = None
        _sync(folder)
        return True
    except OSError:
        return False
    finally:
        if temp is not None:
            try:
                _unlink(temp)
            except OSError:
                pass


def _owner(name):
    for pattern in (FINAL_NAME, LEFTOVER_NAME, RESCUE_NAME, RESCUE_TEMP):
        match = pattern.fullmatch(name)
        if match:
            return match.group(1)
    return None


def accounts(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted({owner for owner in map(_owner, names) if owner is not None})


def forget(folder, uuid=None):
    """Removes every file kept for one account, or for every account when none is named (Remove and Reset)."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return
    for name in names:
        owner = _owner(name)
        if owner is not None and uuid in (None, owner):
            _unlink(Path(folder) / name)


def _kind(name, uuid):
    if name == f"{uuid}.json":
        return FINAL
    for pattern, kind in ((LEFTOVER_NAME, LEFTOVER), (RESCUE_NAME, RESCUE), (RESCUE_TEMP, RESCUE_LEFTOVER)):
        match = pattern.fullmatch(name)
        if match and match.group(1) == uuid:
            return kind
    return None


def _entries(folder, uuid):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in sorted(names):
        kind = _kind(name, uuid)
        if kind is None:
            continue
        path = Path(folder) / name
        loaded = _load(path, in_use=kind == RESCUE_LEFTOVER)
        if loaded is not None:
            found.append(_Entry(path, kind, loaded[0], loaded[1], loaded[2], loaded[3]))
    return found


def _rank(entry, evidence):
    return (_key(entry.pair, evidence, _gone(entry)), entry.stamp, entry.path.name)


def _best(entries, evidence):
    return max(entries, key=lambda entry: _rank(entry, evidence), default=None)


def damaged(folder, uuid):
    if not _valid(uuid):
        return False
    path = _final(folder, uuid)
    if os.path.lexists(path) and _read_pair(path) is None:
        return True
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(_kind(name, uuid) == RESCUE and _read_pair(Path(folder) / name) is None for name in names)


def _complete(folder, uuid, entries):
    if any(entry.kind == FINAL for entry in entries):
        return entries
    pick = _best([entry for entry in entries if entry.kind == LEFTOVER], _evidence(folder, uuid, entries))
    if pick is None:
        return entries
    final = _final(folder, uuid)
    try:
        os.replace(pick.path, final)
    except OSError:
        return entries
    _sync(folder)
    return [replace(entry, path=final, kind=FINAL) if entry is pick else entry for entry in entries]


def _gather(folder, uuid):
    return _complete(folder, uuid, _entries(folder, uuid))


def pending(folder, uuid):
    if not _valid(uuid):
        return None
    entries = _gather(folder, uuid)
    best = _best(entries, _evidence(folder, uuid, entries))
    return None if best is None else best.pair


def _put(store, uuid, pair, evidence, folder=None, aside=False):
    try:
        current = store.get(uuid)
    except Exception:
        return PENDING, None
    return _place(store, uuid, pair, current, evidence, folder, aside)


def _place(store, uuid, pair, current, evidence, folder, aside, gone=None):
    if same_signin(current, pair):
        return HELD, None
    if _keeps(current, pair, evidence, aside, gone):
        return SUPERSEDED, None
    displaced = None
    placed = None
    if (
        folder is not None
        and usable(current)
        and fingerprint(current) != fingerprint(pair)
        and evidence.judge(current, pair) is None
    ):
        displaced = current
        existed = holds(folder, uuid, current)
        try:
            path = set_aside(folder, uuid, current)
        except RecoveryError:
            return PENDING, None
        placed = None if existed else path
    try:
        store.put(uuid, pair)
    except StoreLeftover:
        pass
    except Exception:
        _undo_aside(store, uuid, current, placed)
        return PENDING, None
    try:
        back = store.get(uuid)
    except Exception:
        return PENDING, None
    if same_signin(back, pair):
        return SAVED, displaced
    _undo_aside(store, uuid, current, placed)
    return PENDING, None


def _undo_aside(store, uuid, current, placed):
    if placed is None:
        return
    try:
        back = store.get(uuid)
    except Exception:
        return
    if not same_signin(back, current):
        return
    loaded = _load(placed)
    if loaded is not None and same_signin(loaded[0], current):
        try:
            _unlink(placed)
        except OSError:
            pass


def store_pair(store, uuid, pair, spent=None, *, folder=None):
    if folder is None:
        return _put(store, uuid, pair, _bare(spent))[0]
    return _put(store, uuid, pair, _evidence(folder, uuid, spent=spent), folder)[0]


def _others(entries, current):
    return [entry for entry in entries if not same_signin(entry.pair, current)]


def _drop(entry):
    loaded = _load(entry.path, in_use=entry.kind == RESCUE_LEFTOVER)
    if loaded is None or not same_signin(loaded[0], entry.pair):
        return False
    try:
        _unlink(entry.path)
    except OSError:
        return False
    return True


def _clear(folder, doomed, entries):
    left = list(doomed)
    remaining = list(entries)
    progress = True
    while left and progress:
        progress = False
        for entry in list(left):
            if entry.spent is not None and any(
                other is not entry and fingerprint(other.pair) == entry.spent for other in remaining
            ):
                continue
            left.remove(entry)
            if _drop(entry):
                remaining.remove(entry)
                progress = True
    _sync(folder)
    return not any(entry in remaining for entry in doomed)


def _recorded(folder, uuid, entries):
    marks = {entry.spent for entry in entries if entry.spent is not None}
    if not marks:
        return True
    try:
        ledger.add(_base(folder), uuid, marks)
    except ledger.LedgerError:
        return False
    return True


def _doomed(entries, kept, evidence):
    return [
        entry
        for entry in entries
        if same_signin(entry.pair, kept) or evidence.is_spent(entry.pair) or _gone(entry)
    ]


def remove(folder, uuid, expected):
    if not _valid(uuid):
        return False
    entries = _entries(folder, uuid)
    evidence = _evidence(folder, uuid, entries)
    doomed = _doomed(entries, expected, evidence)
    if not _recorded(folder, uuid, entries):
        doomed = [entry for entry in doomed if entry.spent is None]
    _clear(folder, doomed, entries)
    return not _entries(folder, uuid)


def settle(store, folder, uuid):
    if not _valid(uuid):
        return ABSENT
    entries = _gather(folder, uuid)
    if not entries:
        return ABSENT
    evidence = _evidence(folder, uuid, entries)
    try:
        current = store.get(uuid)
    except Exception:
        return PENDING
    best = _best(_others(entries, current) or entries, evidence)
    outcome, _ = _place(store, uuid, best.pair, current, evidence, folder, best.aside, _gone(best))
    if outcome == PENDING or not _recorded(folder, uuid, entries):
        return PENDING
    stored = best.pair if outcome == SAVED else current
    doomed = _doomed(entries, stored, evidence)
    if not _clear(folder, doomed, entries):
        return PENDING
    return outcome


def waiting(store, folder, uuid):
    if not _valid(uuid):
        return False
    entries = _entries(folder, uuid)
    if not entries:
        return False
    try:
        current = store.get(uuid)
    except Exception:
        return True
    evidence = _evidence(folder, uuid, entries)
    best = _best(_others(entries, current), evidence)
    return best is not None and not _keeps(current, best.pair, evidence, best.aside, _gone(best))


def holds(folder, uuid, oauth):
    if not _valid(uuid):
        return False
    return any(same_signin(entry.pair, oauth) for entry in _entries(folder, uuid))


def import_into(store, folder, uuid):
    return settle(store, folder, uuid) != PENDING


@dataclass(frozen=True)
class Choice:
    pair: dict | None = field(default=None, repr=False)
    from_file: bool = False
    damaged: bool = False
    proven: bool = False


def _proven(pick, rivals, evidence):
    if not usable(pick):
        return False
    return all(
        not usable(rival) or same_signin(rival, pick) or _ended(rival) or evidence.judge(pick, rival) == ledger.FIRST
        for rival in rivals
    )


def choose(store, folder, uuid):
    outcome = settle(store, folder, uuid)
    unreadable = False
    current = None
    try:
        current = store.get(uuid)
    except Exception:
        unreadable = True
    if unreadable:
        raise RecoveryError(UNREADABLE)
    broken = damaged(folder, uuid)
    entries = _entries(folder, uuid) if _valid(uuid) else []
    evidence = _evidence(folder, uuid, entries) if _valid(uuid) else _bare(None)
    rivals = [entry.pair for entry in entries if not _gone(entry)]
    if outcome == PENDING:
        best = _best(_others(entries, current), evidence)
        if best is not None and not _keeps(current, best.pair, evidence, best.aside, _gone(best)):
            return Choice(best.pair, True, broken, not broken and _proven(best.pair, [*rivals, current], evidence))
    return Choice(current, False, broken, not broken and _proven(current, rivals, evidence))
