import fcntl
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lifeline import ident

SOURCES = ("usage", "statusline", "cache")
BUSY_SECONDS = 5.0
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MILLISECOND = timedelta(milliseconds=1)
MINUTE_MS = 60000
SAME_RESET_MS = 60000
STALE_SLACK = 1.0
COLUMNS = "uuid, source, five, five_reset, week, week_reset, at"
STATUSLINE = "statusline"
FIELDS = ("five", "week")
SPANS_MS = {"five": 5 * 3600 * 1000, "week": 7 * 86400 * 1000}
LAST_ID = 2**62

SCHEMA = (
    "create table if not exists readings ("
    "id integer primary key autoincrement,"
    "uuid text not null,"
    "source text not null,"
    "five real,"
    "five_reset integer,"
    "week real,"
    "week_reset integer,"
    "at integer not null)",
    "create index if not exists readings_uuid_at on readings (uuid, at)",
    "create index if not exists readings_uuid_five_reset on readings (uuid, five_reset, at, five)",
    "create index if not exists readings_uuid_week_reset on readings (uuid, week_reset, at, week)",
    "create table if not exists events ("
    "key text primary key,"
    "at integer not null)",
)


def default_path():
    return ident.data_folder() / "history.db"


def _aware(value, name):
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone aware")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError(f"{name} is out of range") from None


def _floor_ms(value, name):
    return (_aware(value, name) - EPOCH) // MILLISECOND


def _ceil_ms(value, name):
    return -((EPOCH - _aware(value, name)) // MILLISECOND)


def _from_ms(ms):
    if ms is None:
        return None
    try:
        return EPOCH + timedelta(milliseconds=ms)
    except OverflowError:
        raise ValueError("stored time is out of range") from None


def _number(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or None")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and not negative")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Reading:
    uuid: str
    source: str
    five: float | None
    five_reset: datetime | None
    week: float | None
    week_reset: datetime | None
    at: datetime

    def __post_init__(self):
        _text(self.uuid, "uuid")
        _number(self.five, "five")
        _number(self.week, "week")
        if self.source not in SOURCES:
            raise ValueError(f"unknown source: {self.source!r}")
        _aware(self.at, "at")
        for name in ("five_reset", "week_reset"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)


def _row_to_reading(row):
    uuid, source, five, five_reset, week, week_reset, at = row
    return Reading(uuid, source, five, _from_ms(five_reset), week, _from_ms(week_reset), _from_ms(at))


def _judged(row, fields):
    return [
        (field, value, reset)
        for field, value, reset in (("five", row[2], row[3]), ("week", row[4], row[5]))
        if field in fields and value is not None and reset is not None
    ]


def _newer_window(conn, uuid, field, reset, at_ms):
    newest = conn.execute(
        f"select max({field}_reset) from readings where uuid = ? and at >= ? and at <= ? and {field} is not null",
        (uuid, reset - SPANS_MS[field], at_ms),
    ).fetchone()[0]
    return newest is not None and newest - reset > SAME_RESET_MS


def _start(conn, uuid, field, reset, limit):
    since = reset - SPANS_MS[field]
    group = (reset - SAME_RESET_MS, reset + SAME_RESET_MS)
    anchor = conn.execute(
        f"select id, at, {field}_reset from readings where uuid = ? and at >= ? and at <= ? and source != ? "
        f"and {field} is not null and {field}_reset >= ? and {field}_reset <= ? and (at < ? or id < ?) "
        "order by at desc, id desc limit 1",
        (uuid, since, limit[0], STATUSLINE) + group + limit,
    ).fetchone()
    if anchor is None:
        return (since, -1), None
    anchor_id, anchor_at, anchor_reset = anchor
    top = conn.execute(
        f"select max({field}) from readings where uuid = ? and at >= ? and at <= ? and source != ? "
        f"and {field} is not null and {field}_reset >= ? and {field}_reset <= ? and (at < ? or id < ?)",
        (uuid, since, anchor_at, STATUSLINE) + group + (anchor_at, anchor_id),
    ).fetchone()[0]
    return (anchor_at, anchor_id), (anchor_reset, top)


def _stale(conn, row, row_id=None, fields=FIELDS):
    if row[1] != STATUSLINE:
        return False
    judged = _judged(row, fields)
    if not judged:
        return False
    uuid, at_ms = row[0], row[6]
    limit = (at_ms, LAST_ID if row_id is None else row_id)
    if any(_newer_window(conn, uuid, field, reset, at_ms) for field, _, reset in judged):
        return True
    pending = {field: _start(conn, uuid, field, reset, limit) for field, _, reset in judged}
    low = min(point[0] for point, _ in pending.values())
    walk = _Walk(tuple(pending), active=())
    query = f"select id, {COLUMNS} from readings where uuid = ? and at >= ? and at <= ? order by at asc, id asc"
    for other in conn.execute(query, (uuid, low, at_ms)):
        place = (other[7], other[0])
        if place >= limit:
            break
        for field, (point, seed) in list(pending.items()):
            if place >= point:
                walk.activate(field, seed)
                del pending[field]
        body = other[1:]
        if walk.stale(body):
            continue
        walk.take(body)
    for field, (_, seed) in pending.items():
        walk.activate(field, seed)
    return walk.stale(row)


def _first_fresh(conn, query, values, fields=FIELDS):
    for row in conn.execute(query, values):
        if not _stale(conn, row[1:], row[0], fields):
            return _row_to_reading(row[1:])
    return None


class _Group:
    def __init__(self, reset):
        self.reset = reset
        self.top = None
        self.usage_top = None
        self.since_anchor = None
        self.early = False


class _Walk:
    def __init__(self, fields, active=None):
        self.fields = fields
        self.active = set(fields) if active is None else set(active)
        self.groups = {field: [] for field in fields}

    def activate(self, field, seed):
        self.active.add(field)
        if seed is not None:
            reset, usage_top = seed
            group = _Group(reset)
            group.usage_top = usage_top
            group.top = usage_top
            self.groups[field].append(group)

    def _group(self, field, reset):
        for group in self.groups[field]:
            if abs(group.reset - reset) <= SAME_RESET_MS:
                return group
        return None

    def stale(self, row):
        if row[1] != STATUSLINE:
            return False
        for field, value, reset in self._values(row):
            groups = self.groups[field]
            if groups and max(g.reset for g in groups) - reset > SAME_RESET_MS:
                return True
            group = self._group(field, reset)
            if group is None or group.top is None:
                continue
            level = group.since_anchor if group.since_anchor is not None else group.top
            if value < level - STALE_SLACK:
                return True
            if group.early and value > level + STALE_SLACK and value >= group.usage_top - STALE_SLACK:
                return True
        return False

    def take(self, row):
        for field, value, reset in self._values(row):
            group = self._group(field, reset)
            if group is None:
                group = _Group(reset)
                self.groups[field].append(group)
            if row[1] != STATUSLINE:
                group.early = group.usage_top is not None and value < group.usage_top - STALE_SLACK
                group.usage_top = value if group.usage_top is None else max(group.usage_top, value)
                group.since_anchor = value
            elif group.since_anchor is not None:
                group.since_anchor = max(group.since_anchor, value)
            group.top = value if group.top is None else max(group.top, value)

    def _values(self, row):
        for field, value, reset in (("five", row[2], row[3]), ("week", row[4], row[5])):
            if field in self.active and value is not None and reset is not None:
                yield field, value, reset


class History:
    def __init__(self, path: Path | None = None):
        self._default = path is None
        self.path = default_path() if path is None else Path(path)
        self._prepare()

    def _prepare(self):
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(parent, 0o700)
        elif self._default:
            os.chmod(parent, 0o700)
        lock_fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(f"{self.path}.lock", 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            with closing(self._connect()) as conn:
                conn.execute("pragma journal_mode=wal")
                with conn:
                    for statement in SCHEMA:
                        conn.execute(statement)
            self._secure()
        finally:
            os.close(lock_fd)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=BUSY_SECONDS)

    def _secure(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except FileNotFoundError:
                pass

    def add_reading(self, uuid, source, five, five_reset, week, week_reset, at, per_minute=False) -> Reading:
        reading = Reading(uuid, source, _number(five, "five"), five_reset, _number(week, "week"), week_reset, at)
        at_ms = _floor_ms(at, "at")
        five_reset_ms = None if five_reset is None else _floor_ms(five_reset, "five_reset")
        week_reset_ms = None if week_reset is None else _floor_ms(week_reset, "week_reset")
        values = (reading.five, five_reset_ms, reading.week, week_reset_ms, at_ms)
        with closing(self._connect()) as conn:
            with conn:
                if _stale(conn, (reading.uuid, reading.source) + values):
                    return None
                kept = None
                if per_minute:
                    start = at_ms - at_ms % MINUTE_MS
                    kept = conn.execute(
                        "select id, at from readings where uuid = ? and source = ? and at >= ? and at < ? "
                        "order by at desc, id desc limit 1",
                        (reading.uuid, reading.source, start, start + MINUTE_MS),
                    ).fetchone()
                if kept is None:
                    conn.execute(
                        "insert into readings (uuid, source, five, five_reset, week, week_reset, at) "
                        "values (?, ?, ?, ?, ?, ?, ?)",
                        (reading.uuid, reading.source) + values,
                    )
                elif kept[1] <= at_ms:
                    conn.execute(
                        "update readings set five = ?, five_reset = ?, week = ?, week_reset = ?, at = ? where id = ?",
                        values + (kept[0],),
                    )
        self._secure()
        return Reading(
            reading.uuid,
            reading.source,
            reading.five,
            _from_ms(five_reset_ms),
            reading.week,
            _from_ms(week_reset_ms),
            _from_ms(at_ms),
        )

    def latest(self, uuid) -> Reading | None:
        with closing(self._connect()) as conn:
            return _first_fresh(conn, f"select id, {COLUMNS} from readings where uuid = ? order by at desc, id desc", (uuid,))

    def newest(self, uuid, since: datetime, sources=None, field=None) -> Reading | None:
        if field not in (None, "five", "week"):
            raise ValueError("field must be five, week or None")
        if sources is not None and (not sources or any(source not in SOURCES for source in sources)):
            raise ValueError("sources must name known sources")
        query = f"select id, {COLUMNS} from readings where uuid = ? and at >= ?"
        values = (uuid, _ceil_ms(since, "since"))
        if sources is not None:
            names = sorted(set(sources))
            query += " and source in (" + ", ".join("?" for _ in names) + ")"
            values += tuple(names)
        if field is not None:
            query += f" and {field} is not null"
        query += " order by at desc, id desc"
        with closing(self._connect()) as conn:
            return _first_fresh(conn, query, values, FIELDS if field is None else (field,))

    def readings(self, uuid, since: datetime, limit=None, fields=FIELDS) -> list[Reading]:
        since_ms = _ceil_ms(since, "since")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("limit must be a whole number of at least 1 or None")
        if any(field not in FIELDS for field in fields):
            raise ValueError("fields must name five or week")
        kept = []
        walk = _Walk(tuple(fields))
        with closing(self._connect()) as conn:
            start = since_ms
            if fields:
                lows = conn.execute("select min(five_reset), min(week_reset) from readings where uuid = ? and at >= ?", (uuid, since_ms)).fetchone()
                for field, low in zip(FIELDS, lows):
                    if field in fields and low is not None:
                        start = min(start, low - SPANS_MS[field])
            query = f"select {COLUMNS} from readings where uuid = ? and at >= ? order by at asc, id asc"
            for row in conn.execute(query, (uuid, start)):
                if walk.stale(row):
                    continue
                walk.take(row)
                if row[6] < since_ms:
                    continue
                kept.append(_row_to_reading(row))
                if limit is not None and len(kept) >= limit:
                    break
        return kept

    def prune(self, before: datetime) -> tuple[int, int]:
        before_ms = _ceil_ms(before, "before")
        with closing(self._connect()) as conn:
            with conn:
                readings = conn.execute("delete from readings where at < ?", (before_ms,)).rowcount
                events = conn.execute("delete from events where at < ?", (before_ms,)).rowcount
        self._secure()
        return readings, events

    def forget(self, uuid=None):
        """Drops one account's readings and alert records, or every row when no account is named (Remove and Reset)."""
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("delete from readings where ? is null or uuid = ?", (uuid, uuid))
                conn.execute("delete from events where ? is null or instr(key, ?) > 0", (uuid, uuid))
        self._secure()

    def copy_to(self, path):
        with closing(self._connect()) as conn:
            conn.execute("vacuum into ?", (str(path),))

    def event_at(self, key: str) -> datetime | None:
        _text(key, "key")
        with closing(self._connect()) as conn:
            row = conn.execute("select at from events where key = ?", (key,)).fetchone()
        return None if row is None else _from_ms(row[0])

    def add_event(self, key: str, at: datetime) -> bool:
        _text(key, "key")
        at_ms = _floor_ms(at, "at")
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute("insert or ignore into events (key, at) values (?, ?)", (key, at_ms))
                created = cursor.rowcount == 1
        self._secure()
        return created
