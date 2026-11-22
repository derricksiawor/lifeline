import json
import math
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from lifeline import clock as clocks
from lifeline import ident
from lifeline import forecast as forecasting
from lifeline.accounts import Account, resolve, stated, with_org

POST_LIMIT = 0.2
REPLY_LIMIT = 16 * 1024
INPUT_LIMIT = 1024 * 1024
CLAUDE_JSON_LIMIT = 20 * 1024 * 1024
RETRY_DELAY = 0.02
NOT_SIGNED_IN = "Not signed in"


def socket_path(home, env, system=None):
    override = env.get("LIFELINE_SOCKET")
    if override:
        return override
    return str(ident.data_folder(home, system or platform.system(), env) / "lifeline.sock")


def registry_path(home, system=None):
    return ident.data_folder(home, system or platform.system()) / "accounts.json"


UNREADABLE = object()


def read_claude_json_once(path):
    if os.path.getsize(path) > CLAUDE_JSON_LIMIT:
        return UNREADABLE, False
    with open(path, encoding="utf-8") as handle:
        return json.load(handle), True


def read_oauth_account(home):
    path = Path(home) / ".claude.json"
    data = UNREADABLE
    for attempt in range(2):
        try:
            data, retryable = read_claude_json_once(path)
            break
        except FileNotFoundError:
            return None
        except Exception:
            retryable = True
        if attempt == 0 and retryable:
            time.sleep(RETRY_DELAY)
            continue
        break
    if data is UNREADABLE:
        return UNREADABLE
    if not isinstance(data, dict):
        return None
    value = data.get("oauthAccount")
    return value if isinstance(value, dict) else None


def _account(item):
    try:
        return with_org(Account(**item))
    except Exception:
        return None


def registry_name(home, login, org):
    try:
        with open(registry_path(home), encoding="utf-8") as handle:
            items = json.load(handle)
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    accounts = [found for found in (_account(item) for item in items if isinstance(item, dict)) if found is not None]
    found = resolve(accounts, login, org)
    name = found.name if found is not None else None
    return name if isinstance(name, str) and name else None


def account_name(home, oauth):
    if oauth is UNREADABLE:
        return None
    if not oauth:
        return NOT_SIGNED_IN
    login, org = stated(oauth)
    name = registry_name(home, login, org) if login is not None else None
    if name:
        return name
    email = oauth.get("emailAddress")
    if isinstance(email, str) and email:
        return email
    return NOT_SIGNED_IN


def window(data, key):
    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    value = limits.get(key)
    return value if isinstance(value, dict) else None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def reset_moment(value):
    try:
        if number(value) is not None:
            return datetime.fromtimestamp(value, timezone.utc).astimezone()
        if isinstance(value, str):
            moment = datetime.fromisoformat(value.strip())
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone()
    except (ValueError, OverflowError, OSError):
        return None
    return None


def build_line(name, five_hour, seven_day=None, now=None):
    now = (now or datetime.now()).astimezone()
    windows = {}
    for key, used in (("five", five_hour), ("week", seven_day)):
        percent = number(used.get("used_percentage")) if used is not None else None
        if percent is not None:
            windows[key] = (percent, reset_moment(used.get("resets_at")))
    if not windows:
        return name or ""
    five, week = windows.get("five", (None, None)), windows.get("week", (None, None))
    bound = forecasting.binding(five[0], five[1], week[0], week[1], now)
    percent, moment = windows[bound]
    if moment is not None and moment <= now:
        percent = 0.0
    whole = max(0, min(100, math.floor(percent)))
    line = f"{whole}% of its week used" if bound == "week" else f"{whole}% of its 5 hours used"
    if name:
        line = f"{name}, {line}"
    if moment is not None and moment > now:
        line += f", resets {clocks.when(moment, now)}"
    return line


def finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def pick(item):
    if item is None:
        return None
    return {
        "used_percentage": finite(item.get("used_percentage")),
        "resets_at": finite(item.get("resets_at")),
    }


def build_body(data, oauth):
    session = data.get("session_id")
    uuid = oauth.get("accountUuid") if isinstance(oauth, dict) else None
    org = oauth.get("organizationUuid") if isinstance(oauth, dict) else None
    return {
        "source": "statusline",
        "session_id": session if isinstance(session, str) else None,
        "account_uuid": uuid if isinstance(uuid, str) else None,
        "org_uuid": org if isinstance(org, str) and org else None,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "five_hour": pick(window(data, "five_hour")),
        "seven_day": pick(window(data, "seven_day")),
    }


def post(path, body, limit=POST_LIMIT):
    deadline = time.monotonic() + limit

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = (
        b"POST /reading HTTP/1.1\r\nHost: lifeline\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload
    )
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(remaining())
        conn.connect(path)
        conn.settimeout(remaining())
        conn.sendall(request)
        data = b""
        while len(data) <= REPLY_LIMIT:
            conn.settimeout(remaining())
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        conn.close()
    _, _, body = data.partition(b"\r\n\r\n")
    try:
        reply = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return reply if isinstance(reply, dict) else None


def _same_account(reply, oauth):
    login, org = stated(oauth)
    if not isinstance(reply, dict) or login is None or reply.get("account_uuid") != login:
        return False
    said = reply.get("org_uuid")
    return said is None or said == org


def current_windows(reply, oauth):
    if not _same_account(reply, oauth):
        return None
    five, week = number(reply.get("five")), number(reply.get("week"))
    if five is None and week is None:
        return None
    five_hour = None if five is None else {"used_percentage": five, "resets_at": reply.get("five_reset")}
    seven_day = None if week is None else {"used_percentage": week, "resets_at": reply.get("week_reset")}
    return five_hour, seven_day


def shown_windows(reply, oauth, data):
    current = current_windows(reply, oauth)
    if current is not None:
        return current
    if isinstance(reply, dict) and reply.get("reading_named") is True:
        return window(data, "five_hour"), window(data, "seven_day")
    return None, None


def main(raw, home, env, now=None):
    data = {}
    oauth = None
    line = NOT_SIGNED_IN
    try:
        if len(raw) <= INPUT_LIMIT:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
    except Exception:
        pass
    signed_in = False
    try:
        oauth = read_oauth_account(home)
        signed_in = True
    except Exception:
        pass
    reply = None
    try:
        reply = post(socket_path(home, env), build_body(data, oauth))
    except Exception:
        pass
    if signed_in:
        try:
            five_hour, seven_day = shown_windows(reply, oauth, data)
            line = build_line(account_name(home, oauth), five_hour, seven_day, now)
        except Exception:
            pass
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


def run():
    try:
        raw = sys.stdin.read(INPUT_LIMIT + 1)
    except Exception:
        raw = ""
    try:
        return main(raw, Path.home(), os.environ)
    except Exception:
        return 0
