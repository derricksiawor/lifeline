import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from lifeline import writelock

NAME = "spent.json"
RECORD_NAME = "last_loaded.json"
ACCOUNTS_KEY = "accounts"
SEEN_KEY = "seen"
KEEP_SECONDS = 45 * 24 * 3600
LEFTOVER_SECONDS = 300
LEFTOVER = re.compile(r"\.spent-[A-Za-z0-9_]+\.tmp", re.ASCII)
FIRST = "first"
SECOND = "second"
VALID_UUID = re.compile(r"[A-Za-z0-9-]+", re.ASCII)
FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)
BAD_ID = "Lifeline names each account in its files with letters, digits and hyphens only."
BAD_SPENT = "Lifeline takes a spent sign-in only as a sign-in or as its renewal fingerprint."
NOT_WRITTEN = "Lifeline could not write its list of spent sign-ins."
UNSAFE = "Lifeline could not read its list of spent sign-ins, so it did not change it."


class LedgerError(Exception):
    pass


def path_for(lock_path=None):
    base = Path(lock_path).parent if lock_path is not None else writelock.lifeline_folder()
    return base / NAME


def fingerprint(oauth):
    token = oauth.get("refreshToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _usable(oauth):
    return isinstance(oauth, dict) and all(
        isinstance(oauth.get(key), str) and oauth.get(key) for key in ("accessToken", "refreshToken")
    )


def _same(first, second):
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    return (first.get("refreshToken"), first.get("accessToken")) == (second.get("refreshToken"), second.get("accessToken"))


def _ends(oauth):
    return _stamp(oauth.get("expiresAt")) if isinstance(oauth, dict) else None


def _mark(value):
    if isinstance(value, dict):
        found = fingerprint(value)
        if found is not None:
            return found
    elif isinstance(value, str) and FINGERPRINT.fullmatch(value):
        return value
    return None


def _marks(spent):
    if spent is None:
        return set()
    if isinstance(spent, (dict, str)):
        items = [spent]
    else:
        try:
            items = list(spent)
        except TypeError:
            items = None
    if items is None:
        raise LedgerError(BAD_SPENT)
    found = set()
    for item in items:
        mark = _mark(item)
        if mark is None:
            raise LedgerError(BAD_SPENT)
        found.add(mark)
    return found


def _stamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _empty():
    return {ACCOUNTS_KEY: {}, SEEN_KEY: {}}


def _entries(data, key=ACCOUNTS_KEY):
    accounts = data.get(key) if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return {}
    found = {}
    for uuid, marks in accounts.items():
        if not isinstance(uuid, str) or VALID_UUID.fullmatch(uuid) is None or not isinstance(marks, dict):
            continue
        kept = {}
        for mark, at in marks.items():
            moment = _stamp(at)
            if isinstance(mark, str) and FINGERPRINT.fullmatch(mark) and moment is not None:
                kept[mark] = at
        if kept:
            found[uuid] = kept
    return found


def _load(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _empty(), True
    except OSError:
        return _empty(), False
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return _empty(), False
            raw = handle.read()
    except OSError:
        return _empty(), False
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return _empty(), False
    if not isinstance(data, dict):
        return _empty(), False
    return {ACCOUNTS_KEY: _entries(data, ACCOUNTS_KEY), SEEN_KEY: _entries(data, SEEN_KEY)}, True


def _fresh(at, now):
    return _stamp(at) >= now - KEEP_SECONDS


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


def _write(path, sections):
    folder = path.parent
    data = {ACCOUNTS_KEY: sections[ACCOUNTS_KEY]}
    if sections[SEEN_KEY]:
        data[SEEN_KEY] = sections[SEEN_KEY]
    text = json.dumps(data, sort_keys=True) + "\n"
    temp = None
    failed = False
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=".spent-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
        _sync(folder)
    except OSError:
        failed = True
    if temp is not None:
        try:
            os.unlink(temp)
        except OSError:
            pass
    if failed:
        raise LedgerError(NOT_WRITTEN)


def _sweep(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return
    limit = time.time() - LEFTOVER_SECONDS
    for name in names:
        if LEFTOVER.fullmatch(name) is None:
            continue
        path = folder / name
        try:
            found = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(found.st_mode) and found.st_mtime < limit:
            try:
                os.unlink(path)
            except OSError:
                pass


def problem(base):
    _, safe = _load(Path(base) / NAME)
    if not safe:
        return True
    folder = Path(base)
    return folder.exists() and not os.access(folder, os.W_OK)


def _section(base, uuid, key, now):
    if not isinstance(uuid, str) or VALID_UUID.fullmatch(uuid) is None:
        return {}
    moment = time.time() if now is None else now
    sections, _ = _load(Path(base) / NAME)
    return {mark: at for mark, at in sections[key].get(uuid, {}).items() if _fresh(at, moment)}


def spent(base, uuid, now=None):
    return frozenset(_section(base, uuid, ACCOUNTS_KEY, now))


def seen(base, uuid, now=None):
    return dict(_section(base, uuid, SEEN_KEY, now))


def _prune(section, moment):
    kept = {}
    for owner, known in section.items():
        alive = {mark: at for mark, at in known.items() if _fresh(at, moment)}
        if alive:
            kept[owner] = alive
    return kept


def _record(base, uuid, marks, now, key, stamp):
    if not marks:
        return False
    moment = time.time() if now is None else now
    path = Path(base) / NAME
    _sweep(path.parent)
    sections, safe = _load(path)
    if not safe:
        raise LedgerError(UNSAFE)
    kept = {name: _prune(section, moment) for name, section in sections.items()}
    account = kept[key].setdefault(uuid, {})
    new = [mark for mark in sorted(marks) if mark not in account]
    if not new and kept == sections:
        return False
    for mark in new:
        account[mark] = stamp(moment)
    _write(path, kept)
    return True


def add(base, uuid, spent_pairs, now=None):
    if not isinstance(uuid, str) or VALID_UUID.fullmatch(uuid) is None:
        raise LedgerError(BAD_ID)
    return _record(base, uuid, _marks(spent_pairs), now, ACCOUNTS_KEY, int)


def see(base, uuid, pairs, now=None):
    if not isinstance(uuid, str) or VALID_UUID.fullmatch(uuid) is None:
        raise LedgerError(BAD_ID)
    return _record(base, uuid, _marks(pairs), now, SEEN_KEY, lambda moment: round(float(moment), 6))


def record_entry(path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    uuid = data.get("uuid")
    digest = data.get("refresh_sha256")
    digest = digest if isinstance(digest, str) and digest else None
    return {
        "uuid": uuid if isinstance(uuid, str) and uuid else None,
        "refresh_sha256": digest,
        "proven": digest is not None and data.get("proven") is True,
    }


@dataclass(frozen=True)
class Evidence:
    uuid: str | None
    spent: frozenset = field(default=frozenset(), repr=False)
    confirmed: str | None = field(default=None, repr=False)
    seen: dict = field(default_factory=dict, repr=False)
    blind: bool = False

    def is_spent(self, oauth):
        mark = fingerprint(oauth)
        return mark is not None and mark in self.spent

    def adding(self, marks):
        return replace(self, spent=self.spent | frozenset(marks))

    def by_ledger(self, first, second):
        one, two = self.is_spent(first), self.is_spent(second)
        if one == two:
            return None
        return SECOND if one else FIRST

    def by_seen(self, first, second):
        one, two = self.seen.get(fingerprint(first)), self.seen.get(fingerprint(second))
        if one is None or two is None or one == two:
            return None
        return FIRST if one > two else SECOND

    def by_record(self, first, second, live=None):
        if self.blind or not self.confirmed or live is None:
            return None
        one, two = fingerprint(first) == self.confirmed, fingerprint(second) == self.confirmed
        if one == two:
            return None
        winner, loser = (first, second) if one else (second, first)
        if not _same(winner, live):
            return None
        ends, rival = _ends(winner), _ends(loser)
        if ends is not None and rival is not None and rival > ends:
            return None
        after = self.seen.get(fingerprint(loser))
        if after is not None:
            before = self.seen.get(fingerprint(winner))
            if before is None or after > before:
                return None
        return FIRST if one else SECOND

    def judge(self, first, second, live=None):
        one, two = _usable(first), _usable(second)
        if one != two:
            return FIRST if one else SECOND
        if not one or _same(first, second):
            return None
        return self.by_ledger(first, second) or self.by_record(first, second, live)


def gather(base, uuid, now=None, extra=()):
    marks = set(spent(base, uuid, now)) | {mark for mark in extra if isinstance(mark, str) and FINGERPRINT.fullmatch(mark)}
    entry = record_entry(Path(base) / RECORD_NAME)
    proven = entry is not None and entry["uuid"] == uuid and entry["proven"]
    confirmed = entry["refresh_sha256"] if proven else None
    return Evidence(uuid, frozenset(marks), confirmed, seen(base, uuid, now), problem(base))
