"""Sentences for the desktop widgets.

extend() adds a "widget" block to /state: the small widget's line and the large widget's headline come from the view
model's widget_lines (the same forecast as the hero), and the sub line names the other computer's account in use. stale_line is
shown once the app has stopped updating the widget, and more holds the line for each number of accounts a widget has no
room for. The app builds no sentence of its own.
"""

DEVBOX_USES = "The other computer uses {name}."
DEVBOX_AWAY = "Lifeline on the other computer is not answering."
MORE_ONE = "1 more account is in the menu."
MORE_MANY = "{count} more accounts are in the menu."


def more_lines(count):
    """One sentence for each number of rows a widget may have to leave out, from 1 to count - 1: more[k - 1] is the line
    for k accounts left out."""
    return [MORE_ONE if left == 1 else MORE_MANY.format(count=left) for left in range(1, max(count, 1))]


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def extend(state):
    mac = state.get("mac") if isinstance(state.get("mac"), dict) else {}
    dev = state.get("dev") if isinstance(state.get("dev"), dict) else {}
    devbox = state.get("devbox") if isinstance(state.get("devbox"), dict) else {}
    name = _text(dev.get("hero_name"))
    if devbox.get("answers") is not True:
        sub = DEVBOX_AWAY
    elif name:
        sub = DEVBOX_USES.format(name=name)
    else:
        sub = ""
    rows = mac.get("rows") if isinstance(mac.get("rows"), list) else []
    state["widget"] = {
        "small_line": _text(mac.get("widget_line")),
        "headline": _text(mac.get("widget_head")),
        "sub": sub,
        "stale_line": _text(mac.get("widget_stale")),
        "more": more_lines(len(rows)),
    }
    return state
