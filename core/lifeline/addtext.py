"""Sentences for the add, waiting, naming and result screens.

decorate() adds them to the /state dictionary before it is sent, so the app builds no sentence of its own.
"""

from lifeline import clock as clocks
from lifeline import settingstext
from lifeline import widgettext

TITLE = "Add an account"
LEAD = "Sign in to the account once. After that, switching to it never asks you to sign in."
TWICE = "The other computer gets its own sign-in too, so you press Authorize twice."
DEVBOX_AWAY = "Lifeline on the other computer is not answering, so accounts are added to this Mac."
FOR_DEVBOX = "This sign-in is for the other computer, so choose the same account again."
PAGE_NOT_OPENED = "Lifeline could not open the sign-in page by itself, so press Open the sign-in page."
PAGE_NOT_OPENED_ABOVE = "Lifeline could not open the sign-in page by itself, so press Open the sign-in page above."
FAILED = "Lifeline could not finish adding the account."
LOST = "Lifeline lost track of that sign-in, so open the sign-in page again."
CANCELLING = "Cancelling the sign-in."
CANCELLED = "You cancelled the sign-in. Nothing changed."
NAMES_SHOWN = "The menu shows {menu}, and the menu bar shows {bar}."
NAMES_SHOWN_HIDDEN = "The menu shows {menu}. The menu bar shows {bar} when you turn its name on in Alerts and settings."
ADDED = "{name} is added."
SLOW_ANSWER = "Lifeline did not answer within {seconds} seconds, so the result is not known yet."
UNREADABLE = "Lifeline answered, but the menu could not read the answer."
STAYS = "{name} stays signed in and in use on this Mac while you do this."
OPENS = "Lifeline opens Claude's sign-in in {browser}, your default browser."
OPENS_DEFAULT = "Lifeline opens Claude's sign-in in your default browser."
CHOOSE_ADDING = "If Claude asks you to choose an organization, choose the one you are adding, then press Authorize."
CHOOSE_PERSONAL = "If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize."
CHOOSE_NAMED = "If Claude asks you to choose an organization, choose {organization}, then press Authorize."
CHOOSE_THIS = "If Claude asks you to choose an organization, choose the one this account uses, then press Authorize."
DEFAULT_BROWSER = "your default browser"
PERSONAL_SUFFIX = "'s Organization"
REMOVE = "Lifeline removes {name}, its saved sign-in and its history{where}."
REMOVE_IN_USE = "Lifeline removes {name}, its saved sign-in and its history{where}, and Claude Code keeps using it."
REMOVE_WHERE = ", here and on the other computer"

SIGNIN_KINDS = frozenset({"add", "resign", "devbox-add", "devbox-resign"})
DEVBOX_KINDS = frozenset({"devbox-add", "devbox-adopt", "devbox-resign"})
ENDED = frozenset({"done", "failed", "cancelled"})
ADD_KINDS = frozenset({"add", "devbox-add", "adopt", "devbox-adopt"})
RESIGN_KINDS = frozenset({"resign", "devbox-resign"})
NAMED_KINDS = frozenset({"add", "adopt"})


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _personal(organization):
    organization = _text(organization)
    return organization == "personal" or organization.endswith(PERSONAL_SUFFIX)


def choose(organization, fallback=CHOOSE_ADDING):
    organization = _text(organization)
    if not organization:
        return fallback
    if _personal(organization):
        return CHOOSE_PERSONAL
    return CHOOSE_NAMED.format(organization=organization)


def opens(browser):
    browser = _text(browser)
    return OPENS.format(browser=browser) if browser else OPENS_DEFAULT


def _stays(state):
    rows = ((state.get("mac") or {}).get("rows")) or []
    name = next((_text(row.get("name")) for row in rows if row.get("is_active") is True), "")
    return STAYS.format(name=name) if name else None


def _pick_line(entry):
    email, organization = _text(entry.get("email")), _text(entry.get("organization"))
    if _personal(organization):
        return f"It is the personal account for {email}." if email else "It is a personal account."
    if email and organization:
        return f"It signs in as {email} and belongs to the {organization} organization."
    if email:
        return f"It signs in as {email}."
    if organization:
        return f"It belongs to the {organization} organization."
    return None


def _known_explain(entry, browser):
    who = _text(entry.get("email")) or _text(entry.get("name")) or "the account you are adding"
    return f"{opens(browser)} Sign in there as {who}. {choose(entry.get('organization'))}"


def _job_name(job, names):
    return names.get(job.get("uuid")) or None


def _title(job, name):
    kind, state = job.get("kind") or "", job.get("state")
    if job.get("step") == [2, 2] and kind in DEVBOX_KINDS and state in ("cancelled", "failed"):
        return "You cancelled the other computer step" if state == "cancelled" else "The other computer step did not finish"
    if state == "cancelled":
        return "Lifeline cancelled the sign-in"
    done = state == "done"
    if kind in ADD_KINDS:
        if not done:
            return "Lifeline did not add the account"
        return f"Lifeline added {name}" if name else "Lifeline added the account"
    if not name:
        return "The sign-in is finished" if done else "The sign-in did not finish"
    return f"The sign-in for {name} is finished" if done else f"The sign-in for {name} did not finish"


def _label(job, name):
    kind = job.get("kind") or ""
    if kind in ADD_KINDS:
        return TITLE
    if kind in RESIGN_KINDS:
        return f"Sign in again to {name}" if name else "Sign in again"
    return "Lifeline"


def _split(result, names=()):
    result = _text(result)
    kept = {}
    for index, name in enumerate(sorted((n for n in names if ". " in n), key=len, reverse=True)):
        token = f"\x00{index}\x00"
        kept[token] = name
        result = result.replace(name, token)
    head, dot, rest = result.partition(". ")
    head, rest = (head + ".", rest.strip()) if dot else (result, "")
    for token, name in kept.items():
        head, rest = head.replace(token, name), rest.replace(token, name)
    return head, rest


def _add_steps(job, known, guidance):
    email = _text((known.get(job.get("known")) or {}).get("email"))
    return f"Sign in there as {email or 'the account you are adding'}. {guidance or CHOOSE_ADDING}"


def _job_lines(job, name, known, guidance, stays):
    kind = job.get("kind") or ""
    if kind == "add":
        lines = [_add_steps(job, known, guidance)]
        card = list(lines)
        if stays:
            lines.append(stays)
    elif kind == "devbox-add":
        lines = [_add_steps(job, known, guidance), FOR_DEVBOX]
        card = list(lines)
    elif kind in RESIGN_KINDS:
        step = guidance or CHOOSE_THIS
        lines = [f"Sign in there as {name or 'the account'}. {step}"]
        card = [step]
    else:
        return [], []
    if job.get("opened") is False:
        lines.append(PAGE_NOT_OPENED)
        card.append(PAGE_NOT_OPENED)
    return lines, card


def _signed_day(moment, now, tz):
    return clocks.day(clocks.nearest_minute(moment, tz).date(), now.astimezone(tz).date(), on=False)


def _added_text(account, now, tz):
    plan = _text(account.get("plan")).replace(", ", " ")
    plan_line = f"It is on the {plan} plan." if plan else ""
    ends = account.get("signin_ends")
    until = f"You will not need to sign in again until {_signed_day(ends, now, tz)}." if hasattr(ends, "astimezone") else ""

    def joined(*parts):
        return " ".join(part for part in parts if part)

    return {
        "mac": joined("It is signed in on this Mac.", plan_line, until),
        "both": joined("It is signed in on this Mac and on the other computer.", plan_line, until),
        "devbox": joined("It is signed in on the other computer, and this Mac will not use it.", plan_line),
    }


def decorate(state, now, tz):
    browser = state.get("signin_browser")
    stays = _stays(state)
    state["add_text"] = {
        "title": TITLE,
        "lead": LEAD,
        "explain": f"{opens(browser)} Sign in there as the account you are adding. {CHOOSE_ADDING}",
        "twice": TWICE,
        "devbox_away": DEVBOX_AWAY,
        "for_devbox": FOR_DEVBOX,
        "stays": stays,
        "page_not_opened": PAGE_NOT_OPENED,
        "page_not_opened_above": PAGE_NOT_OPENED_ABOVE,
        "failed": FAILED,
        "lost": LOST,
        "cancelling": CANCELLING,
        "cancelled": CANCELLED,
        "names_shown": NAMES_SHOWN,
        "names_shown_hidden": NAMES_SHOWN_HIDDEN,
        "added": ADDED,
        "slow_answer": SLOW_ANSWER,
        "unreadable": UNREADABLE,
    }
    known = {}
    state["known_accounts"] = [dict(entry) if isinstance(entry, dict) else entry for entry in state.get("known_accounts") or []]
    for entry in state["known_accounts"]:
        if isinstance(entry, dict):
            entry["pick_line"] = _pick_line(entry)
            entry["explain"] = _known_explain(entry, browser)
            known[entry.get("name")] = entry
    names = {}
    for account in state.get("accounts") or []:
        if isinstance(account, dict):
            names[account.get("uuid")] = _text(account.get("name"))
            account["added_text"] = _added_text(account, now, tz)
            in_use = set(account.get("in_use") or ()) - {"idle"}
            where = REMOVE_WHERE if (state.get("devbox") or {}).get("set_up") else ""
            account["remove_text"] = (REMOVE_IN_USE if in_use else REMOVE).format(name=names[account.get("uuid")] or "the account", where=where)
    every_name = [n for n in names.values() if n] + [_text(k) for k in known if _text(k)]
    for job in state.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        name = _job_name(job, names)
        kind = job.get("kind") or ""
        guidance = _text(job.get("guidance")) or None
        if kind in SIGNIN_KINDS:
            place = _text(job.get("browser")) or DEFAULT_BROWSER
            job["waiting"] = f"Waiting for you to finish the sign-in in {place}."
            job["compact"] = f"Finish the sign-in using {place}. This stays here until it is done."
        job["lines"], job["card_lines"] = _job_lines(job, name, known, guidance, stays)
        job["label"] = _label(job, name)
        job["compact_title"] = _text(job.get("known")) or name or TITLE
        if job.get("state") not in ENDED:
            continue
        job["title"] = _title(job, name)
        if job.get("state") == "done":
            job["result_head"], job["result_rest"] = job["title"] + ".", ""
            if kind in NAMED_KINDS:
                shown = _text(job.get("suggested_name")) or name
                if shown:
                    job["added_head"] = ADDED.format(name=shown)
        else:
            job["result_head"], job["result_rest"] = _split(job.get("result"), every_name)
    settingstext.extend(state)
    widgettext.extend(state)
    return state
