from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

WINDOW = timedelta(hours=5)
WEEK_WINDOW = timedelta(days=7)
WEEK_RECENT = timedelta(hours=24)
WEEK_MIN_SPAN = timedelta(hours=1)
RECENT = timedelta(minutes=20)
MIN_SPAN = timedelta(minutes=3)
LEARNED_FRESH = timedelta(minutes=30)
LEARN_DAYS = timedelta(days=28)
MIN_DAYS = 7
MAX_GAP = timedelta(minutes=30)
RESET_DROP = 1.0
SAME_RESET = timedelta(seconds=60)
STALE_SLACK = 1.0
LIMIT = 100.0
SWITCH_AT = 99.0
LOW_AT = 80.0
RECENT_WEIGHT = 0.7
LEARNED_WEIGHT = 0.3


@dataclass(frozen=True)
class Forecast:
    at_reset_pct: float | None
    runs_out_at: datetime | None
    pace_per_min: float | None
    basis: str
    restarted_at: datetime | None = None


def live(value, reset, now):
    if value is None:
        return None
    return 0.0 if reset is not None and reset <= now else max(0.0, value)


def binding(five, five_reset, week, week_reset, now):
    five, week = live(five, five_reset, now), live(week, week_reset, now)
    if week is None:
        return "five"
    return "week" if five is None or week > five or min(five, week) >= LIMIT else "five"


def fullest(five, five_reset, week, week_reset, now):
    values = [v for v in (live(five, five_reset, now), live(week, week_reset, now)) if v is not None]
    return max(values) if values else None


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone aware datetime")
    return value


def _minutes(delta):
    return delta.total_seconds() / 60.0


def _higher(top, level):
    return level if top is None else max(top, level)


def _after_last_reset(points, value="five"):
    field = value + "_reset"
    kept, current, top, usage_top, restarted = [], None, None, None, None
    for point in points:
        level, reset = getattr(point, value), getattr(point, field, None)
        usage = getattr(point, "source", None) != "statusline"
        if reset is not None and current is not None:
            if reset - current > SAME_RESET:
                kept, current, top, restarted = [point], reset, level, None
                usage_top = level if usage else None
            elif current - reset > SAME_RESET:
                continue
            elif usage and usage_top is not None and level < usage_top - STALE_SLACK:
                kept, top, usage_top, restarted = [point], level, level, point.at
            elif usage or level >= top - STALE_SLACK:
                kept.append(point)
                top = max(top, level)
                usage_top = _higher(usage_top, level) if usage else usage_top
            continue
        if reset is None and kept and getattr(kept[-1], value) - level > RESET_DROP:
            kept, top, usage_top = [point], level, level if usage else None
            continue
        kept.append(point)
        top = _higher(top, level)
        usage_top = _higher(usage_top, level) if usage else usage_top
        if reset is not None:
            current = reset
    return kept, restarted


def _sorted_points(readings, value="five"):
    return sorted((r for r in readings if getattr(r, value) is not None), key=lambda r: r.at)


def _slope(points, value="five"):
    t0 = points[0].at
    xs = [_minutes(p.at - t0) for p in points]
    ys = [getattr(p, value) for p in points]
    n = len(points)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _recent_pace(points, now, value="five", span=RECENT, min_span=MIN_SPAN):
    recent = [p for p in points if p.at >= now - span]
    if len(recent) < 2 or recent[-1].at - recent[0].at < min_span:
        return None
    slope = _slope(recent, value)
    if slope is None:
        return None
    return max(0.0, slope)


def forecast(
    readings, reset_at, now, learned=None, hour=None, value="five", window=WINDOW, recent=RECENT, min_span=MIN_SPAN
) -> Forecast:
    _aware(now, "now")
    if reset_at is not None:
        _aware(reset_at, "reset_at")
    if hour is None:
        hour = now.hour
    elif isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError("hour must be an integer from 0 to 23")

    usable = [r for r in _sorted_points(readings, value) if r.at <= now]
    if reset_at is None:
        return Forecast(getattr(usable[-1], value) if usable else 0.0, None, None, "none")

    points, restarted = _after_last_reset([r for r in usable if r.at >= reset_at - window], value)
    if not points:
        return Forecast(None, None, None, "none")

    latest = points[-1]
    level = getattr(latest, value)
    recent = _recent_pace(points, now, value, recent, min_span)
    learned_pace = (learned or {}).get(hour)

    if recent is not None and recent > 0 and learned_pace is not None:
        pace, basis = RECENT_WEIGHT * recent + LEARNED_WEIGHT * learned_pace, "blended"
    elif recent is not None:
        pace, basis = recent, "recent"
    elif learned_pace is not None and now - latest.at < LEARNED_FRESH:
        pace, basis = learned_pace, "learned"
    else:
        pace, basis = None, "none"

    ahead = 0.0 if pace is None else pace * max(0.0, _minutes(reset_at - latest.at))

    if level >= LIMIT:
        first = next(p for p in points if getattr(p, value) >= LIMIT)
        return Forecast(level + ahead, first.at, pace, basis, restarted)

    if pace is None:
        return Forecast(level, None, None, "none", restarted)

    runs_out = None
    if pace > 0:
        needed = (LIMIT - level) / pace
        if needed < _minutes(reset_at - latest.at):
            runs_out = latest.at + timedelta(minutes=needed)
    return Forecast(level + ahead, runs_out, pace, basis, restarted)


def _windows(points):
    windows, window, current, top, usage_top = [], [], None, None, None
    for point in points:
        level, reset = point.five, getattr(point, "five_reset", None)
        usage = getattr(point, "source", None) != "statusline"
        split = False
        if reset is not None and current is not None and reset - current > SAME_RESET:
            split = True
        elif reset is not None and current is not None and current - reset > SAME_RESET:
            continue
        elif usage and usage_top is not None and level < usage_top - RESET_DROP:
            split = True
        elif not usage and top is not None and level < top - STALE_SLACK:
            continue
        if split and window:
            windows.append(window)
            window, top, usage_top = [], None, None
        window.append(point)
        top = _higher(top, level)
        usage_top = _higher(usage_top, level) if usage else usage_top
        if reset is not None:
            current = reset
    if window:
        windows.append(window)
    return windows


def learn(readings, tz: tzinfo, now: datetime | None = None) -> dict[int, float]:
    points = _sorted_points(readings)
    if now is not None:
        _aware(now, "now")
    if not points:
        return {}
    cutoff = (points[-1].at if now is None else now) - LEARN_DAYS
    points = [p for p in points if p.at >= cutoff]

    windows = _windows(points)

    rates = {}
    days = {}
    for window in windows:
        for a, b in zip(window, window[1:]):
            if not timedelta(0) < b.at - a.at <= MAX_GAP:
                continue
            minutes = _minutes(b.at - a.at)
            local = a.at.astimezone(tz)
            rates.setdefault(local.hour, []).append(max(0.0, (b.five - a.five) / minutes))
            days.setdefault(local.hour, set()).add(local.date())
    return {h: sum(v) / len(v) for h, v in rates.items() if len(days[h]) >= MIN_DAYS}
