"""Sentences for Alerts and settings.

extend() adds them to /state's settings_text. Each description fits two lines at 13 pt in the 560 pt sheet.
The notes keyed by state are for facts only the app knows (notification permission, login item, shortcut);
the app picks one by its own state and builds no sentence.
"""

PAGE_TITLE = "Alerts and settings"
LEAD = "Alerts reach you as notifications on this Mac."
ACCOUNTS_NOTE = "Press an account's number in the menu to switch this Mac to it."

ROWS = {
    "will_run_out": ("Warn me before an account runs out", "Lifeline learns how fast you use each account and warns you before one runs out."),
    "limit": ("Tell me when an account reaches its limit", "You hear at once, together with the account that has the most room."),
    "back": ("Tell me when an account is back", "You hear when an account you ran out on can be used again."),
    "week": ("Warn me about the weekly limit too", "You get a warning when an account has used 90% of its week."),
    "signin_ends": (
        "Warn me before a sign-in ends",
        "Each sign-in lasts 30 days. You get a warning 3 days before, and signing in again takes one Authorize.",
    ),
    "mac": (
        "Notify me on this Mac while I am using it",
        "Notifications come with Switch and Later buttons. An alert that comes while you are away waits in the menu until you see it.",
    ),
    "shortcut": (
        "Open the menu from anywhere",
        "Control Option C opens the menu. Press an account's number to switch this Mac to it, or click its arrow for its details.",
    ),
    "login": ("Start when the Mac starts", "The menu bar icon is there every time you log in."),
    "show_name": ("Show the name and percentage in the menu bar", "When it is off, the menu bar shows only the line."),
    "statusline": (
        "Show the account in Claude Code",
        "Each Claude Code session shows the account in use and how much of its limit is used, on a line under the prompt.",
    ),
}
COMPUTER_TITLE = "Another computer"
COMPUTER_NONE = (
    "Lifeline can also read the accounts Claude Code uses on a Linux computer or another Mac. "
    "Install it there as the README says, then enter the address and pairing code it shows."
)
COMPUTER_ANSWERS = "This Mac reads {name} at {address}, and every request between them is signed with the key they share."
COMPUTER_SILENT = "This Mac is paired with {name} at {address}, but {name} is not answering now."
NOTIFICATION_NOTES = {
    "unavailable": "Mac notifications work only when Lifeline runs as an app.",
    "banners": "Lifeline notifications are Banners and go away quickly, so pick Alerts under Notifications in System Settings.",
    "list_only": "Lifeline notifications go straight to Notification Center, so pick Alerts under Notifications in System Settings.",
    "refused": "Notifications for Lifeline are off, so turn them on under Notifications in System Settings.",
    "failed": "macOS did not show Lifeline's last notification, so check Lifeline under Notifications in System Settings.",
}
SHORTCUT_NOTES = {
    "taken": "Another app already uses Control Option C, so this shortcut is off.",
    "refused": "macOS did not accept Control Option C, so this shortcut is off.",
}
LOGIN_NOTES = {
    "not_installed": "Lifeline can start with the Mac once it runs from an Applications folder.",
    "needs_approval": "Allow Lifeline under Login Items in System Settings so it can start with the Mac.",
    "failed": "macOS did not accept this change, so try again or check Login Items in System Settings.",
}
STATUSLINE_OTHER = "Another status line is set there, so Lifeline leaves it alone."
STATUSLINE_UNKNOWN = "Lifeline could not read this setting just now."
RESET = (
    "Lifeline saves a copy of its history and settings, then removes every account, sign-in and setting{where}. "
    "Claude Code keeps the account it uses."
)
RESET_WHERE = " here and on the other computer"


def extend(state):
    text = state.get("settings_text") if isinstance(state.get("settings_text"), dict) else {}
    state["settings_text"] = text
    computer = state.get("devbox") or {}
    text["page"] = {"title": PAGE_TITLE, "lead": LEAD}
    text["accounts_note"] = ACCOUNTS_NOTE
    text["reset"] = RESET.format(where=RESET_WHERE if computer.get("set_up") else "")
    for key, (title, description) in ROWS.items():
        text[key] = {"title": title, "description": description}
    text["mac"]["notes"] = dict(NOTIFICATION_NOTES)
    text["shortcut"]["notes"] = dict(SHORTCUT_NOTES)
    text["login"]["notes"] = dict(LOGIN_NOTES)
    if computer.get("name"):
        said = COMPUTER_ANSWERS if computer.get("answers") is True else COMPUTER_SILENT
        description = said.format(name=computer["name"], address=computer.get("address"))
    else:
        description = COMPUTER_NONE
    text["computer"] = {"title": COMPUTER_TITLE, "description": description}
    line = state.get("statusline") or {}
    text["statusline"]["note"] = STATUSLINE_OTHER if line.get("other_present") else (STATUSLINE_UNKNOWN if line.get("on") is None else None)
    return state
