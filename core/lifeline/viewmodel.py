import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo

from lifeline import clock as clocks
from lifeline import forecast as forecasting
from lifeline import reader as reading
from lifeline.anthropic import CertificateFailed, RateLimited, Unavailable, UnexpectedReply
from lifeline.forecast import Forecast

STALE_AFTER = timedelta(minutes=10)
SIGNIN_WARNING = timedelta(days=3)
DEVBOX_HINT = "Lifeline switches only this Mac, so switch the other computer there."
CANNOT_READ = "Anthropic does not let Lifeline read its usage. Lifeline tries again once you sign in to it again."
CANNOT_READ_ROW = "Lifeline cannot read it. Sign in again to fix it."
NEEDS_SIGNIN_ROW = "Needs a new sign-in."
NEEDS_SIGNIN_CLICK = "Needs a new sign-in. Click it to sign in."
CLIMBING = "In use elsewhere."
ADD_BROWSER = "It takes one Authorize in your browser."
ADD_ADOPT = "Lifeline adds the sign-in this Mac already has, so no browser opens."
ADD_FIRST = "Add your first account"
ADD_THIS = "Add this account"
PAUSED_UNKNOWN = "Lifeline could not take a reading"
PAUSE_CAUSES = {
    **{reading.READ_ERRORS[kind]: clause for kind, clause in (
        (RateLimited, "Anthropic asked Lifeline to wait"),
        (Unavailable, "Lifeline could not reach Anthropic"),
        (UnexpectedReply, "Anthropic sent a reply Lifeline could not read"),
        (CertificateFailed, "Lifeline could not confirm it was talking to Anthropic"),
    )},
    **{reading.RENEW_ERRORS[kind]: clause for kind, clause in (
        (RateLimited, "Anthropic asked Lifeline to wait"),
        (Unavailable, "Lifeline could not reach Anthropic"),
        (UnexpectedReply, "Anthropic sent a reply Lifeline could not read"),
        (CertificateFailed, "Lifeline could not confirm it was talking to Anthropic"),
    )},
    reading.FAILED_ERROR: "Lifeline could not take a reading",
    reading.STORE_ERROR: "Lifeline could not open the sign-in it keeps for this account",
    reading.UNWRITABLE_ERROR: "Lifeline could not save the sign-in it keeps for this account",
    reading.NO_RECOVERY_ERROR: "Lifeline could not write to the folder where it keeps safe copies of sign-ins",
    reading.GUARD_ERROR: "Lifeline could not check that it is safe to renew the sign-in",
    reading.LOCK_ERROR: "Lifeline could not open one of its own files",
    reading.HISTORY_ERROR: "Lifeline could not save the last reading",
    reading.UNSAVED_ERROR: "Lifeline renewed the sign-in but could not save it yet",
    reading.DEAD_ERROR: "Anthropic refused its sign-in",
    reading.NOT_ALLOWED_ERROR: "Anthropic does not let Lifeline read its usage",
}
PERSONAL_SUFFIX = "'s Organization"
KEYS = 9
WINDOW_WORDS = {"five": "5 hours", "week": "week"}


@dataclass(frozen=True)
class AccountView:
    uuid: str
    name: str
    short: str
    plan: str
    key: int
    five: float | None
    week: float | None
    five_reset: datetime | None
    week_reset: datetime | None
    forecast: Forecast | None
    needs_signin: bool
    cannot_read: bool
    paused_until: datetime | None
    reading_at: datetime | None
    signin_ends: datetime | None = None
    on_mac: bool = True
    binding: str = "five"
    other_forecast: Forecast | None = None
    on_devbox: bool = False
    climbing: bool | None = None
    pause_reason: str | None = None


@dataclass(frozen=True)
class MenuState:
    accounts: tuple[AccountView, ...]
    mac_uuid: str | None
    devbox_uuid: str | None
    view: str
    just_switched: tuple[str, str, str] | None
    auto_switch_target: str | None
    back_uuid: str | None
    sessions_mac: int
    sessions_dev: int
    now: datetime
    tz: tzinfo
    sessions_moved: int = 0
    sessions_behind: int = 0
    live_signed_in: bool = False
    back_window: str = ""
    countdown_left: int = 5
    devbox_answers: bool = True
    mac_unadded_known: dict | None = None
    devbox_unadded_known: dict | None = None
    mac_adopt_ready: bool = False
    clickable: bool = True


def _used(value):
    return 0.0 if value is None else max(0.0, float(value))


def _whole(value):
    return int(math.floor(_used(value)))


def _pct(value):
    return f"{_whole(value)}%"


def _width(value):
    return f"{max(0, min(_whole(value), 100))}%"


def _clock(when, tz):
    return clocks.hhmm(when, tz)


def _weekly(account):
    return account.binding == "week"


def _main(account):
    return account.week if _weekly(account) else account.five


def _main_reset(account):
    return account.week_reset if _weekly(account) else account.five_reset


def _read(account):
    return account.five is not None or account.week is not None


def paused_sentence(reason, until, now, tz):
    clause = PAUSE_CAUSES.get(reason, PAUSED_UNKNOWN)
    return f"{clause}, so readings are paused until {clocks.when(until, now, tz, on=False)}."


def _until(account, now, tz, on=True):
    reset = _main_reset(account)
    if reset is None or reset <= now:
        return None
    return clocks.when(reset, now, tz, on)


def _bound(account, now):
    if account.five is None and account.week is None:
        return account
    return replace(account, binding=forecasting.binding(account.five, account.five_reset, account.week, account.week_reset, now))


def _read_when(moment, now, tz):
    if clocks.nearest_minute(moment, tz).date() == now.astimezone(tz).date():
        return f"at {_clock(moment, tz)}"
    return clocks.when(moment, now, tz)


def _last_read(moment, now, tz):
    return f"Last read {_read_when(moment, now, tz)}."


def _read_lately(account, now):
    return account.reading_at is not None and now - account.reading_at < forecasting.RECENT


def _other(account):
    if account.five is None or account.week is None:
        return ""
    return "five" if _weekly(account) else "week"


def _reset_of(account, window):
    return account.week_reset if window == "week" else account.five_reset


def _value(account, window):
    return account.week if window == "week" else account.five


def _forecast_of(account, window):
    if window == account.binding:
        return account.forecast
    if window and window == _other(account):
        return account.other_forecast
    return None


def _ahead(account, window):
    forecast = _forecast_of(account, window)
    return None if forecast is None else forecast.at_reset_pct


def _until_in(account, window, now, tz, on=True):
    reset = _reset_of(account, window)
    if reset is None or reset <= now:
        return None
    return clocks.when(reset, now, tz, on)


def _beside(account, window):
    if account.five is None or account.week is None or window not in WINDOW_WORDS:
        return ""
    return "week" if window == "five" else "five"


def _passed_in(account, window, now):
    forecast = _forecast_of(account, window)
    return _full(forecast) and forecast.runs_out_at is not None and forecast.runs_out_at <= now


def _display(account, now):
    if account.five is None and account.week is None:
        return account.binding
    if account.five is None:
        return "week"
    if account.week is None:
        return "five"
    five = forecasting.live(account.five, account.five_reset, now)
    week = forecasting.live(account.week, account.week_reset, now)
    if week >= forecasting.LIMIT:
        return "week"
    if five >= forecasting.LIMIT:
        return "five"
    first = _first_out(account, now)
    if first is not None:
        return first[1]
    if week >= forecasting.SWITCH_AT:
        return "week"
    if five >= forecasting.SWITCH_AT:
        return "five"
    passed = [(_forecast_of(account, window).runs_out_at, window) for window in ("five", "week") if _passed_in(account, window, now)]
    if passed:
        return min(passed)[1]
    return "week" if week >= forecasting.LOW_AT else "five"


def _thin_text(account, window, now, tz):
    value = _value(account, window)
    reset = _reset_of(account, window)
    week = window == "week"
    moment = _runs_out(_forecast_of(account, window), reset, now)
    if moment is not None:
        words = "its week runs out" if week else "its 5 hours run out"
        return f"At this pace {words} {clocks.when(moment, now, tz)}."
    if _whole(value) == 0:
        return f"It has all of its {WINDOW_WORDS[window]}."
    lead = f"Its week is {_whole(value)}% used" if week else f"Its 5 hours are {_whole(value)}% used"
    if reset is not None and reset > now:
        day = clocks.when(reset, now, tz)
        if day.startswith("next "):
            return f"{lead}. {'It resets' if week else 'They reset'} {day}."
        return f"{lead} and {'resets' if week else 'reset'} {day}."
    return f"{lead}."


NO_THIN = {"thin_window": "", "thin_w": "0%", "thin_label": "", "text": ""}


def _thin(account, window, now, tz):
    other = _beside(account, window)
    if not other:
        return dict(NO_THIN)
    value = _value(account, other)
    label = f"Week {_pct(value)}" if other == "week" else f"5 hours {_pct(value)}"
    return {"thin_window": other, "thin_w": _width(value), "thin_label": label, "text": _thin_text(account, other, now, tz)}


def _runs_out(forecast, reset, now):
    if forecast is None or forecast.runs_out_at is None or forecast.at_reset_pct is None or reset is None:
        return None
    if forecast.at_reset_pct < 100 or not now < forecast.runs_out_at < reset:
        return None
    return forecast.runs_out_at


def _first_out(account, now):
    found = []
    for forecast, window in ((account.forecast, account.binding), (account.other_forecast, _other(account))):
        if window:
            moment = _runs_out(forecast, _reset_of(account, window), now)
            if moment is not None:
                found.append((moment, window, forecast))
    return min(found, key=lambda item: item[0]) if found else None


def _has_used(value, window):
    words = WINDOW_WORDS[window]
    return f"It has all of its {words}." if _whole(value) == 0 else f"It has used {_whole(value)}% of its {words}."


def _signed_day(moment, now, tz):
    return clocks.day(clocks.nearest_minute(moment, tz).date(), now.astimezone(tz).date(), on=False)


def signin_text(now, tz, mac_needs, mac_ends, devbox):
    mac = "needs" if mac_needs else ("until", _signed_day(mac_ends, now, tz)) if mac_ends is not None else "on"
    if devbox is None:
        dev = "hidden"
    elif not devbox.get("held"):
        dev = "none"
    elif devbox.get("needs"):
        dev = "needs"
    elif devbox.get("ends") is not None:
        dev = ("until", _signed_day(devbox["ends"], now, tz))
    else:
        dev = "on"
    there = f"on the other computer until {dev[1]}" if isinstance(dev, tuple) else "on the other computer"
    if mac == "needs":
        return {
            "needs": "It needs a new sign-in here and on the other computer.",
            "none": "It needs a new sign-in here, and the other computer has none yet.",
            "hidden": "It needs a new sign-in here, and the other computer is not answering.",
        }.get(dev) or f"It needs a new sign-in here, but it is signed in {there}."
    here = f"here until {mac[1]}" if isinstance(mac, tuple) else "here"
    return {
        "needs": f"It is signed in {here}, but the other computer needs a new sign-in.",
        "none": f"It is signed in {here}, and not yet on the other computer.",
        "hidden": f"It is signed in {here}, but the other computer is not answering.",
    }.get(dev) or (f"It is signed in here, and {there}." if here == "here" and isinstance(dev, tuple) else f"It is signed in {here} and {there}.")


def _window_phrase(value, window):
    words = WINDOW_WORDS[window]
    return ("all", f"all of its {words}") if _whole(value) == 0 else ("used", f"{_whole(value)}% of its {words}")


def usage_line(account, now, tz):
    if account.needs_signin:
        return "It needs a new sign-in. Sign in again to read its usage."
    if account.cannot_read:
        return CANNOT_READ
    if account.five is None and account.week is None:
        return "Lifeline has no reading for it yet."
    account = _bound(account, now)
    main = _display(account, now)
    other = _beside(account, main)
    value = _value(account, main)
    sentences = []
    if _used(value) >= 100:
        until = _until_in(account, main, now, tz, on=False)
        sentences.append(f"It is out until {until}." if until else "It is out.")
        if other:
            sentences.append(_has_used(_value(account, other), other))
        return " ".join(sentences)
    parts = [_window_phrase(value, main)]
    if other:
        parts.append(_window_phrase(_value(account, other), other))
    if len(parts) == 1:
        kind, words = parts[0]
        sentences.append(f"It has {words}." if kind == "all" else f"It has used {words}.")
    else:
        (first_kind, first), (second_kind, second) = parts
        if first_kind == second_kind:
            sentences.append(f"It has {first} and {second}." if first_kind == "all" else f"It has used {first} and {second}.")
        else:
            lead = f"It has {first}" if first_kind == "all" else f"It has used {first}"
            tail = f"has {second}" if second_kind == "all" else f"has used {second}"
            sentences.append(f"{lead} and {tail}.")
    found = _first_out(account, now)
    ahead = _ahead(account, main)
    if found is not None:
        moment, window, _ = found
        words = "its week runs out" if window == "week" else "its 5 hours run out"
        sentences.append(f"At this pace {words} {clocks.when(moment, now, tz)}.")
    elif ahead is not None and _used(value) < ahead < 100 and _whole(ahead) > _whole(value):
        words = "its week resets" if main == "week" else "its 5 hours reset"
        sentences.append(f"At this pace it reaches {_whole(ahead)}% before {words}.")
    return " ".join(sentences)


def models_line(limits, now, tz):
    kept = [
        item for item in limits or []
        if isinstance(item, dict) and isinstance(item.get("model"), str) and item["model"].strip()
        and isinstance(item.get("week"), (int, float)) and _whole(item["week"]) >= 1
    ]
    if not kept:
        return ""
    used = [f"{item['model'].strip()} has used {_whole(item['week'])}%" for item in kept]
    many = len(kept) > 1
    text = f"{_join(used)} of {'their' if many else 'its'} own weekly limit{'s' if many else ''}."
    resets = {item.get("week_reset") for item in kept}
    reset = resets.pop() if len(resets) == 1 else None
    if isinstance(reset, datetime) and reset > now:
        text += f" {'They reset' if many else 'It resets'} {clocks.when(reset, now, tz)}."
    return text


def account_line(name, full, short, organization, email, shared, plan, mac_needs, mac_ends, now, tz):
    full = (full or "").strip()
    short = (short or "").strip() or (name or "").strip()
    named = full if full and full != (name or "").strip() else ""
    first = f"Its full name is {named}, and the menu bar calls it {short}." if named else f"The menu bar calls it {short}."
    belongs = ""
    if shared and organization and email:
        organization = organization.strip()
        if organization.endswith(PERSONAL_SUFFIX):
            belongs = "a personal account"
        elif organization not in f"{full} {name or ''}":
            belongs = f"in the {organization} organization"
    plan = (plan or "").replace(", ", " ").strip()
    parts = [part for part in (belongs, f"on the {plan} plan" if plan else "") if part]
    if mac_needs:
        second = f"It is {', '.join(parts)} and needs a new sign-in here." if parts else "It needs a new sign-in here."
    else:
        signed = f"signed in here until {_signed_day(mac_ends, now, tz)}" if mac_ends is not None else "signed in here"
        second = f"It is {', '.join(parts + [signed])}."
    return f"{first} {second}"


def projects_line(projects):
    names = []
    for item in projects or []:
        if not isinstance(item, dict):
            continue
        cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else ""
        folder = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd.strip("/") else ""
        name = (folder or (item.get("name") if isinstance(item.get("name"), str) else "")).strip()
        if name and name not in names:
            names.append(name)
    return f"Open on this Mac: {_join(names)}." if names else ""


def _unadded(machine, known):
    if known:
        return f"{machine} uses {known['name']}, which Lifeline has not added yet."
    return f"{machine} uses an account Lifeline has not added yet."


def _press(key, mac_view):
    if not 1 <= key <= KEYS:
        return ""
    return f"Or press {key}." if mac_view else f"Or hold Shift and press {key}."


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _climbing(account):
    if account.climbing is not None:
        return account.climbing
    forecast = account.forecast
    if forecast is None:
        return False
    if forecast.pace_per_min is not None:
        return forecast.pace_per_min > 0
    return forecast.at_reset_pct is not None and forecast.at_reset_pct > _used(_main(account)) + 1


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _gap(delta):
    return clocks.gap(delta)


def _projected(account):
    values = [f.at_reset_pct for f in (account.forecast, account.other_forecast) if f is not None and f.at_reset_pct is not None]
    return max(values) if values else None


def _full(forecast):
    return forecast is not None and forecast.at_reset_pct is not None and forecast.at_reset_pct >= 100


def _passed_at(account, now):
    found = [f.runs_out_at for f in (account.forecast, account.other_forecast) if _full(f) and f.runs_out_at is not None and f.runs_out_at <= now]
    return min(found) if found else None


def _passed_out(account, now):
    return any(_full(f) and f.runs_out_at is not None and f.runs_out_at <= now for f in (account.forecast, account.other_forecast))


def _ends_at_reset(forecast, reset):
    if not _full(forecast) or forecast.at_reset_pct >= 101:
        return False
    return forecast.runs_out_at is None or (reset is not None and forecast.runs_out_at >= reset)


def _just_as(window, named):
    if window == "week":
        return "At this pace it uses all of its week just as it resets."
    return "At this pace it uses all of its 5 hours just as they reset."


def _uses_all(account, window):
    named = bool(_beside(account, window))
    found = []
    for each in (window, _beside(account, window)):
        if each and _ends_at_reset(_forecast_of(account, each), _reset_of(account, each)):
            found.append(_just_as(each, named))
    return " ".join(found)


def _tone(account, in_use, now):
    used = _used(_main(account))
    if used >= 100:
        return "limit"
    ahead = _projected(account)
    if used >= forecasting.LOW_AT or (in_use and ahead is not None and ahead >= 100):
        return "low"
    return "calm"


def _sessions(count):
    return f"{_plural(count, 'session')} open"


def _devbox_needs_you(account, now):
    if account is None:
        return False
    if account.needs_signin or _tone(account, True, now) != "calm":
        return True
    return account.signin_ends is not None and account.signin_ends - now <= SIGNIN_WARNING


def _moved(moved, behind, origin, total=0):
    parts = []
    waiting = total - moved - behind
    if moved > 0 and waiting > 0:
        verb = "has" if moved == 1 else "have"
        parts.append(f"{moved} of your {total} open Claude Code sessions {verb} moved so far.")
    elif moved == 1:
        parts.append("Your open Claude Code session moved with it.")
    elif moved > 1:
        parts.append(f"Your {moved} open Claude Code sessions moved with it.")
    if behind == 1 and origin is not None:
        parts.append(f"1 open Claude Code session was still on {origin.name} at its last message.")
    elif behind > 1 and origin is not None:
        parts.append(f"{behind} open Claude Code sessions were still on {origin.name} at their last message.")
    return parts


def _no_room(subject, candidates, now, tz, already):
    coming = [(reset, a) for a in candidates if (reset := _main_reset(a)) is not None and reset > now]
    mine = _main_reset(subject)
    known = len(coming) == len(candidates)
    if known and mine is not None and mine > now and all(mine <= reset for reset, _ in coming):
        verb = "is back first" if _used(_main(subject)) >= forecasting.LIMIT else "resets first"
        named = clocks.when(mine, now, tz) in already or clocks.when(mine, now, tz, on=False) in already
        moment = "" if named else f", {clocks.when(mine, now, tz)}"
        return f"No other account has room, and this one {verb}{moment}."
    if coming:
        nearest = min(coming, key=lambda pair: pair[0])[1]
        return f"No other account has room. {nearest.name} is back {_until(nearest, now, tz)}."
    return "No other account has room."


def no_best_reason(subject, accounts, usable, mac_side, now, tz, already="", signins_known=True):
    others = [a for a in accounts if a.uuid != subject.uuid]
    candidates = [a for a in others if usable(a) and _main(a) is not None]
    if not others:
        return "There is no other account to switch to."
    if not mac_side and not any(a.on_devbox for a in others):
        return "No other account has a working sign-in on the other computer."
    if signins_known and all(a.needs_signin for a in others):
        return "Every other account needs a new sign-in."
    if mac_side and any(not a.on_mac for a in others) and all(a.needs_signin or not a.on_mac for a in others):
        return "The other accounts are set to be used on the other computer only."
    if candidates and len(candidates) == len(others) and all(_used(_main(a)) >= forecasting.SWITCH_AT for a in candidates):
        return _no_room(subject, candidates, now, tz, already)
    return "No other account can take over right now."


def _devbox_note(account, now, tz):
    if account is None:
        return ""
    if account.needs_signin:
        reason = f"{account.name} needs a new sign-in."
    elif _used(_main(account)) >= 100:
        until = _until(account, now, tz, on=False)
        reason = f"{account.name} is out until {until}." if until else f"{account.name} is out."
    elif _first_out(account, now) is not None:
        reason = f"{account.name} runs out {clocks.when(_first_out(account, now)[0], now, tz)}."
    elif _tone(account, True, now) != "calm":
        reason = f"{account.name} has used {_whole(_main(account))}% of its {WINDOW_WORDS[account.binding]}."
    else:
        reason = f"{account.name} needs a new sign-in by {clocks.when(account.signin_ends, now, tz, on=False)}."
    return f"The other computer needs you. {reason}"


def _clock_phrase(when):
    """'today at 17:00' becomes '17:00'; any other day keeps its words ('tomorrow at 09:00')."""
    return when[len("today at "):] if when.startswith("today at ") else when


def widget_lines(account, shown, now, tz):
    """The desktop widgets' two sentences: a short line for the small widget and a headline for the large
    one, from the same forecast as the hero. A run-out before the reset gives 'Lasts until 16:20.'; an account that will
    not run out before its reset says so plainly."""
    name = account.name
    if account.needs_signin:
        return "Needs a new sign-in.", f"{name} needs a new sign-in."
    if not _read(account):
        return "No reading yet.", f"Lifeline has no reading for {name} yet."
    used = _used(_value(account, shown))
    words = WINDOW_WORDS[shown]
    if used >= 100:
        until = _until_in(account, shown, now, tz, on=False)
        if not until:
            return "Out for now.", f"{name} is out."
        at = _clock_phrase(until)
        return f"Out until {at}.", f"{name} is out until {at}."
    first = _first_out(account, now)
    if first is not None:
        at = _clock_phrase(clocks.when(first[0], now, tz, on=False))
        return f"Lasts until {at}.", f"{name} lasts until {at}."
    if _passed_out(account, now):
        return "Runs out now.", f"{name} runs out now."
    until = _until_in(account, shown, now, tz)
    if until:
        at = _clock_phrase(until)
        phrase = f"at {at}" if at != until else until
        resets = "resets" if shown == "week" else "reset"
        return f"Lasts until its reset {phrase}.", f"{name} lasts until its {words} {resets} {phrase}."
    if used > 0:
        return f"Has used {_whole(used)}% of its {words}.", f"{name} has used {_whole(used)}% of its {words}."
    return f"Has all of its {words}.", f"{name} has all of its {words}."


def widget_stale_line(now, tz):
    """The sentence a widget shows once the app has stopped updating it: when this reading was made, with
    its date, so it stays true on any later day."""
    local = clocks.nearest_minute(now, tz)
    return f"Lifeline last updated this at {local:%H:%M} on {local.day} {clocks.MONTHS[local.month - 1]}."


def build(state: MenuState, appearance: str) -> dict:
    if appearance not in ("dark", "light"):
        raise ValueError("appearance must be dark or light")
    if state.view not in ("mac", "dev"):
        raise ValueError("view must be mac or dev")

    state = replace(state, accounts=tuple(_bound(a, state.now) for a in state.accounts))
    tz = state.tz
    by_uuid = {a.uuid: a for a in state.accounts}
    mac_view = state.view == "mac"
    machine = "this Mac" if mac_view else "the other computer"
    machine_cap = "This Mac" if mac_view else "The other computer"
    sessions_count = state.sessions_mac if mac_view else state.sessions_dev
    view_uuid = state.mac_uuid if mac_view else state.devbox_uuid
    account = by_uuid.get(view_uuid)
    mac_account = by_uuid.get(state.mac_uuid)

    def in_use(uuid):
        return uuid is not None and uuid in (state.mac_uuid, state.devbox_uuid)

    def usable_here(a):
        return (a.on_mac if mac_view else a.on_devbox) and not a.needs_signin and not a.cannot_read

    candidates = [a for a in state.accounts if a.uuid != view_uuid and usable_here(a) and _main(a) is not None]
    best = min(candidates, key=lambda a: (_used(_main(a)), _used(a.week)), default=None)
    if best is not None and _used(_main(best)) >= forecasting.SWITCH_AT:
        best = None
    current = by_uuid.get(view_uuid)
    if best is not None and current is not None and _main(current) is not None and _used(_main(best)) >= _used(_main(current)):
        best = None

    display = {a.uuid: _display(a, state.now) for a in state.accounts}

    def where(a):
        users = []
        if a.uuid == state.mac_uuid:
            users.append("this Mac")
        if state.devbox_answers and a.uuid == state.devbox_uuid:
            users.append("the other computer")
        if a.needs_signin:
            return NEEDS_SIGNIN_CLICK if mac_view and state.clickable else NEEDS_SIGNIN_ROW
        who = "both machines" if len(users) == 2 else _join(users) if users else ""
        if a.cannot_read:
            return f"Used by {who}. Lifeline cannot read it." if users else CANNOT_READ_ROW
        window = display[a.uuid]
        out = _used(_value(a, window)) >= 100
        first = _first_out(a, state.now) if not users and not out and _climbing(a) else None
        if first is not None:
            return f"{CLIMBING} It runs out {clocks.when(first[0], state.now, tz)}."
        lead = f"Used by {who}." if users else (CLIMBING if _climbing(a) else "Not in use.")
        until = _until_in(a, window, state.now, tz)
        if _read(a) and until is not None:
            lead += f" Back {until}." if out else f" Resets {until}."
        return lead

    def stale(a):
        return a.reading_at is not None and state.now - a.reading_at > STALE_AFTER

    result = {
        "appearance": appearance,
        "view": state.view,
        "view_label": machine_cap,
        "sessions": _sessions(sessions_count),
        "countdown": False,
        "unadded_known": None,
    }

    if mac_account is None:
        result.update(icon_name="No account", icon_pct="", icon_w="4%", icon_tone="calm", icon_binding="", icon_display="")
    elif not _read(mac_account):
        result.update(
            icon_name=mac_account.short, icon_pct="", icon_w="4%", icon_tone="calm", icon_binding=mac_account.binding, icon_display="",
        )
    else:
        mac_window = display[mac_account.uuid]
        mac_value = _value(mac_account, mac_window)
        result.update(
            icon_name=mac_account.short,
            icon_pct=_pct(mac_value),
            icon_w=f"{max(min(_whole(mac_value), 100), 4)}%",
            icon_tone=_tone(mac_account, True, state.now),
            icon_binding=mac_account.binding,
            icon_display=mac_window,
        )
    result["icon_dot"] = _devbox_needs_you(by_uuid.get(state.devbox_uuid), state.now)
    result["devbox_note"] = _devbox_note(by_uuid.get(state.devbox_uuid), state.now, tz) if result["icon_dot"] and mac_view else ""
    if mac_account is None and state.mac_uuid is not None:
        label = _unadded("This Mac", state.mac_unadded_known)
    elif mac_account is None:
        label = "Lifeline cannot tell yet which account Claude Code uses." if state.live_signed_in else "No account is in use on this Mac."
    elif not _read(mac_account):
        label = f"Lifeline has no reading for {mac_account.name} yet."
    else:
        mac_window = display[mac_account.uuid]
        label = f"{mac_account.name} has used {_pct(_value(mac_account, mac_window))} of its {WINDOW_WORDS[mac_window]}."
    result["icon_label"] = label[:-1] + ", and the other computer needs you." if result["icon_dot"] else label
    result["pending_back"] = "Switching this Mac back." if mac_view else ""

    rows = []
    for a in state.accounts:
        window = display[a.uuid]
        read = _read(a) and not a.cannot_read
        value = _value(a, window) if read else None
        used = _used(value)
        width = max(0, min(_whole(used), 100))
        ahead = _ahead(a, window)
        if in_use(a.uuid) and _reset_of(a, window) is not None and used < 100 and ahead is not None:
            end = max(0, min(_whole(ahead), 100))
        else:
            end = width
        weekly = window == "week"
        pct = _pct(value) if read else ""
        thin = _thin(a, window, state.now, tz) if read else dict(NO_THIN)
        thin_week = thin["thin_window"] == "week"
        rows.append({
            "uuid": a.uuid,
            "name": a.name,
            "binding": a.binding,
            "display": window if read else "",
            "figure": (f"Week {pct}" if weekly else pct) if read else "",
            "pct": pct,
            "pct_window": ("week" if weekly else "5 hours") if read else "",
            "pct_label": (f"Week {pct}" if weekly else f"5 hours {pct}") if read else "",
            "week_pct": thin["thin_label"] if thin_week else "",
            "five_w": f"{width}%",
            "fore_w": f"{max(end - width, 0)}%",
            "week_w": thin["thin_w"] if thin_week else "0%",
            "thin_window": thin["thin_window"],
            "thin_w": thin["thin_w"],
            "thin_label": thin["thin_label"],
            "tone": _tone(a, in_use(a.uuid), state.now),
            "unused": read and _whole(value) == 0,
            "is_best": best is not None and a.uuid == best.uuid,
            "is_active": view_uuid is not None and a.uuid == view_uuid,
            "pending_head": f"Switching this Mac to {(a.name or '').strip() or 'the account'}." if mac_view and a.uuid != view_uuid else "",
            "needs_signin": a.needs_signin,
            "cannot_read": a.cannot_read,
            "where": where(a),
            "stale": stale(a),
            "read_note": _last_read(a.reading_at, state.now, tz) if stale(a) else "",
            "second_text": thin["text"],
            "second_w": thin["thin_w"],
            "second_window": thin["thin_window"],
        })
    result["rows"] = rows

    if account is None:
        head, sub = f"No account is in use on {machine}.", ""
        kind, text = "none", ""
        known = state.mac_unadded_known if mac_view else state.devbox_unadded_known
        if view_uuid is not None:
            head = _unadded(machine_cap, known)
            result["unadded_known"] = known
            if mac_view:
                text = f"Add {known['name']}" if known else ADD_THIS
                kind, sub = ("adopt", ADD_ADOPT) if state.mac_adopt_ready else ("add", ADD_BROWSER)
        elif mac_view and state.live_signed_in:
            head, sub = "Lifeline cannot tell yet which account Claude Code uses.", "It checks again within 5 minutes."
            if not state.accounts:
                kind, text, sub = "add", ADD_FIRST, f"{sub} Adding an account takes one Authorize in your browser."
        elif mac_view and not state.accounts:
            kind, text, sub = "add", ADD_FIRST, ADD_BROWSER
        result.update(
            hero_name="", hero_plan="", head=head, sub=sub, binding="", display="",
            hero_w="0%", fore_w="0%", hero_tone="calm", hero_used="", hero_reset="",
            week_w="0%", week_text="", second_text="", second_w="0%", second_window="",
            thin_window="", thin_w="0%", thin_label="",
            action_kind=kind, action_text=text, action_target="", hint=DEVBOX_HINT if not mac_view else "",
            widget_line=head, widget_head=head, widget_stale=widget_stale_line(state.now, tz),
        )
        return result

    shown = display[account.uuid]
    weekly = shown == "week"
    read = _read(account)
    used = _used(_value(account, shown))
    reset = _reset_of(account, shown)
    until = _until_in(account, shown, state.now, tz)
    ahead = _ahead(account, shown)
    best_note = ""
    if best is not None:
        fresh = not _weekly(best) and best.five is not None and _used(best.five) == 0
        best_note = f"{best.name} has all of its 5 hours." if fresh else f"{best.name} has the most room."

    def why_none(already):
        return no_best_reason(account, state.accounts, usable_here, mac_view, state.now, tz, already)

    def offer_best():
        return ("primary", f"Switch {machine} to {best.name}", best.uuid, _press(best.key, mac_view))

    kind, text, target, hint = "none", "", "", ""
    ordinary = False
    countdown = False
    just = state.just_switched
    back = by_uuid.get(state.back_uuid)
    target_account = by_uuid.get(state.auto_switch_target)
    first = _first_out(account, state.now)
    out = f"{account.name} is out until {_until_in(account, shown, state.now, tz, on=False)}." if until else f"{account.name} is out."

    if account.needs_signin:
        head = f"{account.name} needs a new sign-in."
        sub = "Sign in again to read its usage."
        kind, text, target = "resign", "Sign in again", account.uuid
    elif mac_view and target_account is not None:
        left = max(1, state.countdown_left)
        head = f"Switching this Mac to {target_account.name} in {_plural(left, 'second')}."
        sub = out if _used(_main(account)) >= 100 else f"{account.name} has used {_whole(_main(account))}% of its {WINDOW_WORDS[account.binding]}."
        kind, text = "cancel", "Cancel the switch"
        countdown = True
    elif just is not None and just[0] == state.view:
        head = f"{machine_cap} now uses {account.name}."
        origin = by_uuid.get(just[1])
        parts = _moved(state.sessions_moved, state.sessions_behind, origin, sessions_count)
        if origin is not None:
            if _read(origin) and _used(_main(origin)) >= 100 and _until(origin, state.now, tz):
                parts.append(f"{origin.name} is back {_until(origin, state.now, tz)}.")
            else:
                parts.append(f"Undo takes you back to {origin.name}.")
        sub = " ".join(parts)
        kind, text = "undo", "Undo the switch"
    elif mac_view and back is not None and back.uuid != state.mac_uuid:
        head = f"{back.name} is back."
        again = "Its week has reset, so you can use it again." if (state.back_window or back.binding) == "week" else "It has all of its 5 hours again."
        sub = f"{again} This Mac stays on {account.name} until you switch."
        kind, text, target, hint = "quiet", f"Switch back to {back.name}", back.uuid, _press(back.key, True)
    elif not read:
        head = f"Lifeline has no reading for {account.name} yet."
        sub = ""
    elif used >= 100:
        head = out
        sub = best_note or why_none(head)
        if best is not None:
            kind, text, target, hint = offer_best()
    elif first is not None:
        ordinary = True
        runs_out, window, _ = first
        ends = _reset_of(account, window)
        gap = _gap(ends - runs_out)
        head = f"{account.name} runs out {clocks.when(runs_out, state.now, tz)}."
        if window == "week":
            sub = f"That is {gap} before its week resets {clocks.when(ends, state.now, tz)}."
        else:
            sub = f"That is {gap} before its 5 hours reset {clocks.when(ends, state.now, tz)}."
        if best_note:
            sub += f" {best_note}"
        if best is not None:
            kind, text, target, hint = offer_best()
    elif _passed_out(account, state.now):
        ordinary = True
        if _read_lately(account, state.now):
            head = f"{account.name} runs out now."
            sub = f"Lifeline last read {_whole(used)}% {_read_when(account.reading_at, state.now, tz)}."
        else:
            head = f"{account.name} was due to run out {clocks.when(_passed_at(account, state.now), state.now, tz)}."
            sub = f"Lifeline last read it {_read_when(account.reading_at, state.now, tz)}." if account.reading_at is not None else ""
        sub = " ".join(part for part in (sub, best_note) if part)
        if best is not None:
            kind, text, target, hint = offer_best()
    elif until:
        ordinary = True
        words = WINDOW_WORDS[shown]
        head = f"{account.name} lasts until its {words} {'resets' if weekly else 'reset'} {until}."
        sub = _uses_all(account, shown)
        if not sub and ahead is not None and used < ahead < 100:
            sub = f"At this pace it reaches {_whole(ahead)}% before its {words} {'resets' if weekly else 'reset'}."
    elif weekly:
        ordinary = True
        head = f"{account.name} has used {_whole(used)}% of its week."
        sub = _uses_all(account, shown)
    elif used > 0:
        ordinary = True
        head = f"{account.name} has used {_whole(used)}% of its 5 hours."
        sub = ""
    else:
        ordinary = True
        head = f"{account.name} has all of its 5 hours."
        sub = "Its 5 hours start when you use it."

    if ordinary and _used(_main(account)) >= forecasting.SWITCH_AT:
        extra = best_note or why_none(f"{head} {sub}")
        if extra not in sub:
            sub = " ".join(part for part in (sub, extra) if part)
        if best is not None:
            kind, text, target, hint = offer_best()

    if not mac_view:
        kind, text, target, hint = "none", "", "", DEVBOX_HINT
        countdown = False

    notes = []
    if account.cannot_read and not account.needs_signin:
        notes.append(CANNOT_READ)
    if account.reading_at is not None and state.now - account.reading_at > STALE_AFTER and "Lifeline last read" not in head + sub:
        notes.append(_last_read(account.reading_at, state.now, tz))
    if account.paused_until is not None and account.paused_until > state.now:
        notes.append(paused_sentence(account.pause_reason, account.paused_until, state.now, tz))
    if notes:
        sub = " ".join(part for part in [sub] + notes if part)

    used_w = max(0, min(_whole(used), 100))
    fore_end = max(0, min(_whole(ahead), 100)) if reset is not None and used < 100 and ahead is not None else used_w
    if not read:
        hero_used, hero_reset = "", ""
    else:
        hero_used = f"{_whole(used)}% of its {WINDOW_WORDS[shown]} used"
        if used >= 100 and until:
            hero_reset = f"Back {until}"
        elif until and used < 100:
            hero_reset = f"Resets {until}"
        elif used > 0:
            hero_reset = "Reset time unknown"
        else:
            hero_reset = "Not started"

    thin = _thin(account, shown, state.now, tz) if read else dict(NO_THIN)
    thin_week = thin["thin_window"] == "week"
    widget_line, widget_head = widget_lines(account, shown, state.now, tz)

    result.update(
        hero_name=account.name,
        widget_line=widget_line,
        widget_head=widget_head,
        widget_stale=widget_stale_line(state.now, tz),
        hero_plan=account.plan,
        head=head,
        sub=sub,
        binding=account.binding,
        display=shown if read else "",
        hero_w=f"{used_w}%",
        fore_w=f"{max(fore_end - used_w, 0)}%",
        hero_tone=_tone(account, in_use(account.uuid), state.now),
        hero_used=hero_used,
        hero_reset=hero_reset,
        week_w=thin["thin_w"] if thin_week else "0%",
        week_text=thin["text"] if thin_week else "",
        second_text=thin["text"],
        second_w=thin["thin_w"],
        second_window=thin["thin_window"],
        thin_window=thin["thin_window"],
        thin_w=thin["thin_w"],
        thin_label=thin["thin_label"],
        action_kind=kind,
        action_text=text,
        action_target=target,
        hint=hint,
        countdown=countdown,
        countdown_left=max(1, state.countdown_left) if countdown else 0,
    )
    return result
