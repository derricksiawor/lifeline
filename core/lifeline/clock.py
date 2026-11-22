from datetime import timedelta, timezone

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
HALF_MINUTE = timedelta(seconds=30)


def nearest_minute(moment, tz=None):
    if moment.tzinfo is None and tz is None:
        return (moment + HALF_MINUTE).replace(second=0, microsecond=0)
    absolute = (moment.astimezone(timezone.utc) + HALF_MINUTE).replace(second=0, microsecond=0)
    return absolute.astimezone(tz)


def hhmm(moment, tz=None):
    return nearest_minute(moment, tz).strftime("%H:%M")


def day(date, today, on=True):
    ahead = (date - today).days
    if ahead < 0:
        named = past_day(date, today)
        return named if on or not named.startswith("on ") else named[3:]
    if ahead == 0:
        return "today"
    if ahead == 1:
        return "tomorrow"
    if ahead == 7:
        return f"next {WEEKDAYS[date.weekday()]}"
    named = WEEKDAYS[date.weekday()] if 2 <= ahead <= 6 else f"{date.day} {MONTHS[date.month - 1]}"
    return f"on {named}" if on else named


def past_day(date, today):
    back = (today - date).days
    if back == 0:
        return "today"
    if back == 1:
        return "yesterday"
    if 2 <= back <= 6:
        return f"on {WEEKDAYS[date.weekday()]}"
    return f"on {date.day} {MONTHS[date.month - 1]}"


def when(moment, now, tz=None, on=True):
    local = nearest_minute(moment, tz)
    today = now if now.tzinfo is None and tz is None else now.astimezone(tz)
    return f"{day(local.date(), today.date(), on)} at {local:%H:%M}"


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def gap(delta):
    minutes = int((delta + HALF_MINUTE).total_seconds() // 60)
    if minutes < 1:
        return "less than a minute"
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(_plural(days, "day"))
    if hours:
        parts.append(_plural(hours, "hour"))
    if minutes and not days:
        parts.append(_plural(minutes, "minute"))
    return " ".join(parts)
