from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from . import clock as clocks
from . import viewmodel
from .forecast import LIMIT, STALE_SLACK, SWITCH_AT, WEEK_WINDOW, WINDOW, Forecast, binding

KINDS = ("will_run_out", "limit", "back", "week", "signin_ends")
CHANNELS = ("mac",)
WEEK_WARNING = 90.0
SIGNIN_DAYS = 3
BUSY = ("this Mac", "the other computer")
BACK_WITHIN = timedelta(minutes=60)
WEEK_BACK = "Its week has reset, so you can use it again."
LATER_SUFFIX = ":later"
REMINDER_SUFFIX = ":reminder"
REMIND_BEFORE = timedelta(days=1)
REMIND_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class AccountState:
    uuid: str
    name: str
    five: float | None
    five_reset: datetime | None
    week: float | None
    week_reset: datetime | None
    forecast: Forecast | None
    used_by: tuple[str, ...]
    signin_ends: datetime | None
    signin_machine: str | None
    on_mac: bool = True
    binding: str = "five"
    needs_signin: bool = False
    other_forecast: Forecast | None = None
    on_devbox: bool = False
    cannot_read: bool = False


@dataclass(frozen=True)
class Alert:
    key: str
    kind: str
    title: str
    body: str
    channels: tuple[str, ...]
    uuid: str


def later_key(uuid, ends):
    return "signin-ends:{}:{}".format(uuid, clocks.nearest_minute(ends, timezone.utc).date().isoformat())


def signin_key(uuid, ends, tz):
    return "signin-ends:{}:{}".format(uuid, clocks.nearest_minute(ends, tz).date().isoformat())


def _iso(value):
    moment = value.astimezone(timezone.utc) + timedelta(seconds=30)
    return moment.replace(second=0, microsecond=0).isoformat()


def format_gap(gap):
    return clocks.gap(gap)


def _weekly(state):
    return state.binding == "week"


def week_may_have_ended(week_reset, now):
    return week_reset is None or week_reset - WEEK_WINDOW >= now - BACK_WITHIN - timedelta(minutes=1)


def _bound(states):
    return [s if s.five is None and s.week is None else replace(s, binding=binding(s.five, None, s.week, None, None)) for s in states]


def main_value(state):
    return state.week if _weekly(state) else state.five


def _main_reset(state):
    return state.week_reset if _weekly(state) else state.five_reset


def _in_use(state):
    return bool(state.used_by)


def _side(subject):
    return BUSY[1] if BUSY[1] in subject.used_by and BUSY[0] not in subject.used_by else BUSY[0]


def _usable_on(state, side):
    return not state.cannot_read and (state.on_devbox if side == BUSY[1] else state.on_mac and not state.needs_signin)


def _best(states, subject):
    side = _side(subject)
    choices = [
        s
        for s in states
        if s.uuid != subject.uuid
        and side not in s.used_by
        and _usable_on(s, side)
        and main_value(s) is not None
    ]
    if not choices:
        return None
    best = min(choices, key=lambda s: (main_value(s), float("inf") if s.week is None else s.week))
    if main_value(best) >= SWITCH_AT:
        return None
    if main_value(subject) is not None and main_value(best) >= main_value(subject):
        return None
    return best


def _no_best(states, subject, now, tz, already=""):
    if main_value(subject) is None or main_value(subject) < SWITCH_AT:
        return ""
    side = _side(subject)
    mac_side = side == BUSY[0]
    return viewmodel.no_best_reason(
        subject, states, lambda s: _usable_on(s, side), mac_side, now, tz, already, signins_known=mac_side
    )


def ended_at_limit(history, uuid, now, field):
    since = now - (WEEK_WINDOW if field == "week" else WINDOW) - BACK_WITHIN
    ended = []
    tops = {}
    usage_tops = {}
    for r in history.readings(uuid, since, fields=(field,)):
        value, reset = getattr(r, field), getattr(r, field + "_reset")
        if value is None:
            continue
        if value >= LIMIT and reset is not None and r.at <= reset <= now and now - reset <= BACK_WITHIN:
            ended.append(reset)
        if reset is None:
            continue
        key = clocks.nearest_minute(reset, timezone.utc)
        top, usage_top = tops.get(key), usage_tops.get(key)
        usage = r.source != "statusline"
        if usage and usage_top is not None and value < usage_top - STALE_SLACK:
            if top >= LIMIT and r.at <= now and now - r.at <= BACK_WITHIN:
                ended.append(r.at)
            tops[key], usage_tops[key] = value, value
            continue
        tops[key] = value if top is None else max(top, value)
        if usage:
            usage_tops[key] = value if usage_top is None else max(usage_top, value)
    return max(ended) if ended else None


def restarted_lately(forecast, now):
    restarted = getattr(forecast, "restarted_at", None)
    return restarted is not None and now - restarted <= BACK_WITHIN


def _restarted(state, window):
    forecast = state.forecast if window == ("week" if _weekly(state) else "five") else state.other_forecast
    return getattr(forecast, "restarted_at", None)


def _start(state, window, reset):
    restarted = _restarted(state, window)
    return _iso(restarted) if restarted is not None else _iso(reset - (WEEK_WINDOW if window == "week" else WINDOW))


def _runs_out(forecast, reset, now):
    if forecast is None or forecast.runs_out_at is None or forecast.at_reset_pct is None or reset is None:
        return None
    if forecast.at_reset_pct < LIMIT or not now < forecast.runs_out_at < reset:
        return None
    return forecast.runs_out_at


def _first_out(state, now):
    windows = [(state.forecast, "week" if _weekly(state) else "five")]
    if state.five is not None and state.week is not None:
        windows.append((state.other_forecast, "five" if _weekly(state) else "week"))
    found = []
    for forecast, window in windows:
        moment = _runs_out(forecast, state.week_reset if window == "week" else state.five_reset, now)
        if moment is not None:
            found.append((moment, window))
    return min(found) if found else None


def _fresh_five(best):
    return best is not None and not _weekly(best) and best.five == 0


def _join(*sentences):
    return " ".join(s for s in sentences if s)


def _room(best):
    return "" if best is None else "{} has the most room.".format(best.name)


class Alerts:
    def __init__(self, history, settings, tz, clock):
        self.history = history
        self.settings = dict(settings)
        self.tz = tz
        self.clock = clock

    def _enabled(self, name):
        return bool(self.settings.get(name, True))

    def _asked(self, key):
        lookup = getattr(self.history, "event_at", None)
        try:
            return lookup(key) if lookup is not None else None
        except Exception:
            return None

    def _ended_at_limit(self, uuid, now, weekly=False):
        return ended_at_limit(self.history, uuid, now, "week" if weekly else "five")

    def _candidates(self, states, now):
        states = _bound(states)
        for s in states:
            weekly = _weekly(s)
            value, reset = main_value(s), _main_reset(s)
            suffix = "-week" if weekly else ""
            if reset is not None and reset <= now:
                reset = None
            week_limit = False
            first = _first_out(s, now)
            if self._enabled("will_run_out") and _in_use(s) and first is not None and (value is None or value < LIMIT):
                runs_out, window = first
                ends = s.week_reset if window == "week" else s.five_reset
                title = "{} runs out {}".format(s.name, clocks.when(runs_out, now, self.tz))
                whose = "its week resets" if window == "week" else "its 5 hours reset"
                why = "That is {} before {} {}.".format(format_gap(ends - runs_out), whose, clocks.when(ends, now, self.tz))
                best = _best(states, s)
                yield (
                    "will_run_out",
                    "will-run-out{}:{}:{}".format("-week" if window == "week" else "", s.uuid, _start(s, window, ends)),
                    s,
                    title,
                    _join(why, _room(best) if best is not None else _no_best(states, s, now, self.tz, title + " " + why)),
                )
            if reset is not None:
                start = _start(s, "week" if weekly else "five", reset)
                if self._enabled("limit") and _in_use(s) and value is not None and value >= LIMIT:
                    best = _best(states, s)
                    until = clocks.when(reset, now, self.tz, on=False)
                    title = "{} is out until {}".format(s.name, until)
                    if best is None:
                        body = _no_best(states, s, now, self.tz, title)
                    elif _fresh_five(best):
                        body = "{} has all of its 5 hours.".format(best.name)
                    else:
                        body = _room(best)
                    week_limit = weekly
                    yield (
                        "limit",
                        "limit{}:{}:{}".format(suffix, s.uuid, start),
                        s,
                        title,
                        body,
                    )
            if self._enabled("back") and value is not None and value < LIMIT:
                week_forecast = s.forecast if weekly else s.other_forecast
                near = week_may_have_ended(s.week_reset, now) or restarted_lately(week_forecast, now)
                windows = (False, True) if near else (False,)
                ends = [(end, ended) for ended in windows if (end := self._ended_at_limit(s.uuid, now, ended)) is not None]
                if ends:
                    end, ended = max(ends)
                    left, window = (s.week, "week") if ended else (s.five, "5 hours")
                    if left is None:
                        body = ""
                    elif left == 0:
                        body = WEEK_BACK if ended else "It has all of its 5 hours again."
                    else:
                        body = "It has {}% of its {} left.".format(100 - int(left), window)
                    key = "back{}:{}:{}".format("-week" if ended else "", s.uuid, _iso(end))
                    yield ("back", key, s, "{} is back".format(s.name), body)
            if (
                self._enabled("week")
                and not week_limit
                and s.week is not None
                and s.week >= WEEK_WARNING
                and s.week_reset is not None
            ):
                yield (
                    "week",
                    "week:{}:{}".format(s.uuid, _iso(s.week_reset)),
                    s,
                    "{} has used {}% of its week".format(s.name, int(s.week)),
                    "It resets {}.".format(clocks.when(s.week_reset, now, self.tz)),
                )
            if (
                self._enabled("signin_ends")
                and s.signin_ends is not None
                and now < s.signin_ends <= now + timedelta(days=SIGNIN_DAYS)
            ):
                local = clocks.nearest_minute(s.signin_ends, self.tz)
                days = (local.date() - now.astimezone(self.tz).date()).days
                if days <= 0:
                    title = "A sign-in ends today"
                elif days == 1:
                    title = "A sign-in ends tomorrow"
                else:
                    title = "A sign-in ends in {} days".format(days)
                who = s.name if not s.signin_machine else "{} on {}".format(s.name, s.signin_machine)
                key = "signin-ends:{}:{}".format(s.uuid, local.date().isoformat())
                body = "{} needs a new sign-in by {}. Press Sign in again, then Authorize in your browser.".format(
                    who, clocks.when(s.signin_ends, now, self.tz, on=False)
                )
                yield ("signin_ends", key, s, title, body)
                lasting = later_key(s.uuid, s.signin_ends)
                asked = self._asked(lasting + LATER_SUFFIX)
                if asked is not None and now >= max(s.signin_ends - REMIND_BEFORE, asked + REMIND_AFTER):
                    yield ("signin_ends", lasting + REMINDER_SUFFIX, s, title, body)

    def candidates(self, states, now=None):
        return list(self._candidates(list(states), self.clock() if now is None else now))

    def evaluate(self, states):
        now = self.clock()
        states = list(states)
        found = []
        for kind, key, subject, title, body in self._candidates(states, now):
            if not self.history.add_event(key, now):
                continue
            if kind == "limit" and key.startswith("limit-week:") and subject.week_reset is not None:
                self.history.add_event("week:{}:{}".format(subject.uuid, _iso(subject.week_reset)), now)
            if self._enabled("mac"):
                found.append(Alert(key, kind, title, body, CHANNELS, subject.uuid))
        return found


def room(states, uuid, now=None, tz=None, already=""):
    states = _bound(states)
    subject = next((s for s in states if s.uuid == uuid), None)
    if subject is None:
        return ""
    best = _best(states, subject)
    if best is None:
        now = now or datetime.now(timezone.utc)
        return _no_best(states, subject, now, tz if tz is not None else datetime.now().astimezone().tzinfo, already)
    if _fresh_five(best):
        return "{} has all of its 5 hours.".format(best.name)
    return _room(best)

