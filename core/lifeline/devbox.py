import errno
import http.client
import json
import logging
import math
import os
import plistlib
import secrets
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from lifeline import anthropic, ident, pairing, server, statusline, writelock
from lifeline import enroll as enrolling
from lifeline import switch as switching
from lifeline.accounts import KNOWN_KEYS, Account, Registry, agrees, identity, organization_kind, signin_end, stated
from lifeline.history import SOURCES, History, Reading
from lifeline.reader import KNOWN_ERRORS, AccountStatus
from lifeline.slot import FileSlot, MacKeychainSlot, run_security
from lifeline.store import FileStore, MacKeychainStore

log = logging.getLogger("lifeline.devbox")

LINK_TIMEOUT = 8.0
READINGS_SECONDS = 15.0
PUSH_SECONDS = 15.0
START_SECONDS = 15.0
REPLY_LIMIT = 4 * 1024 * 1024
READINGS_PAGE = 1000
FIRST_SPAN = timedelta(days=1)
READINGS_SPAN = timedelta(days=8)
BIND_RETRY_SECONDS = 60
CONNECTION_LIMIT = 16
HEAD_SECONDS = 5.0
REQUEST_SECONDS = 10.0
REFUSED_TRIES = 3
PAIRING_ACCOUNT = server.PAIRING_ACCOUNT
HEAD_LIMIT = 4096
DRAIN_SECONDS = 1.0
SLOT_POLL_SECONDS = 0.25
QUEUE_SIZE = 64
PUSH_LIMIT = server.PUSH_LIMIT
REPORTS_LIMIT = 20
TEXT_LIMIT = 500
OAUTH_KEY = switching.OAUTH_KEY
REGISTRY_KEYS = frozenset({"uuid", "number", "name", "short", "full", "email", "plan"})
IDENTITY_KEYS = frozenset({"login_uuid", "org_uuid"})
ACCOUNT_ID = re.compile(r"[A-Za-z0-9-]{1,128}")
LOGIN_TARGET = "127.0.0.1"
HOOK_NAME = "lifeline-signin-page"
PAGE_VARIABLE = "LIFELINE_SIGNIN_PAGE"
PAGE_NAME = "signin-page"
PAGE_LIMIT = 8192
PAGE_SECONDS = 60
LISTEN_SECONDS = 5.0
LOGIN_POLL_SECONDS = 0.25
END_SECONDS = 5
SCOPE_SECONDS = server.LOGIN_TIMEOUT + 60
SCOPE_PROBE_SECONDS = 20
TRUSTED_PAGE_HOSTS = frozenset({"claude.com", "claude.ai"})
TRUSTED_LOCATION_HOSTS = ("claude.ai", "claude.com", "anthropic.com")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
PAGE_PATH_END = "/oauth/authorize"
JOB_JOIN_SECONDS = 10
SIGNIN_KINDS = ("add", "resign", "adopt")
DEVBOX_ADD_KEYS = frozenset({"uuid", "email"})
JOB_FIELDS = ("id", "kind", "uuid", "state", "result", "started_at", "finished_at", "port", "url")
RELAY_LISTEN = ("127.0.0.1", "::1")
RELAY_HEAD_LIMIT = 8192
RELAY_HEAD_SECONDS = 10
CALLBACK_SECONDS = 60
CALLBACK_LIMIT = 4096
LOCATION_LIMIT = 2048
STOP_WAIT_SECONDS = 15
OOM_PATH = "/proc/self/oom_score_adj"
OOM_FIRST = b"1000"
OPEN_COMMAND = "/usr/bin/open"
OPEN_SECONDS = 20
LAUNCH_SERVICES = Path("Library") / "Preferences" / "com.apple.LaunchServices" / "com.apple.launchservices.secure.plist"
WEB_SCHEMES = ("https", "http")
SYSTEM_BROWSER = "com.apple.safari"
BROWSER_NAMES = {
    "com.apple.safari": "Safari",
    "com.google.chrome": "Google Chrome",
    "com.brave.browser": "Brave Browser",
    "org.mozilla.firefox": "Firefox",
    "com.microsoft.edgemac": "Microsoft Edge",
    "company.thebrowser.browser": "Arc",
    "com.operasoftware.opera": "Opera",
    "com.vivaldi.vivaldi": "Vivaldi",
}
APP_FOLDERS = (Path("/Applications"), Path("/Applications/Utilities"), Path.home() / "Applications")
PAGE_WAIT_SECONDS = 90
JOB_WAIT_SECONDS = server.LOGIN_TIMEOUT + 120
JOB_POLL_SECONDS = 1
LINK_TRIES = 5
COVER_SPAN = timedelta(minutes=10)
ENDED = "ended"
GONE = "gone"
WAITING = "waiting"

SWITCH_REFUSED = server.DEVBOX_SWITCH_REFUSED
MAC_NAMES = "The other computer takes its account names from the Mac, so rename them there."
MAC_SETTINGS = "The other computer takes its settings from the Mac, so change them there."
NOT_YET = "Lifeline could not listen for the Mac yet, so it will try again every minute."
LISTENING = "Lifeline is listening for the Mac."
BAD_SINCE = "The since value must be a time such as 2026-11-19T15:41:00Z."
BAD_PUSH = "Readings from the Mac must come as a list of at most 200 readings."
BAD_REGISTRY = "Each account in the list must come with its Claude account, number, name, short name, full name, email and plan."
NO_REGISTRY_SAVE = "Lifeline on the other computer could not save the list of accounts."
NO_PAGE = "Claude Code on the other computer did not give a sign-in page within a minute, so Lifeline stopped the sign-in."
BAD_PAGE = "Claude Code on the other computer gave a sign-in page Lifeline does not trust, so Lifeline stopped the sign-in."
NOT_LISTENING = "Claude Code on the other computer is not listening for the sign-in answer, so Lifeline stopped the sign-in."
NO_SCOPE = "Lifeline on the other computer could not run the sign-in outside its own memory limit, so it did not start the sign-in."
LIVE_STALE = (
    "Claude Code on the other computer has not renewed its sign-in lately, so Lifeline cannot tell which account it holds. "
    "Use Claude Code on the other computer once, then try again."
)
LIVE_UNCHECKED = (
    "Anthropic did not confirm which account Claude Code on the other computer holds, so Lifeline did not start the sign-in. "
    "Try again in a moment."
)
LIVE_UNREADABLE = "Lifeline on the other computer could not read Claude Code's sign-in, so it did not start the sign-in."
NO_PAIRING_SAVE = "Lifeline could not save the pairing on this computer."
BAD_PAIR = "Pairing takes only the Mac's random value and its half of the key."
NOT_WAITING = "That sign-in is not waiting for an answer."
ANSWERED = "That sign-in already has its answer."
BAD_CALLBACK = "The sign-in answer must be one /callback address."
NO_ANSWER = "Claude Code on the other computer did not take the sign-in answer."
NOTHING_RUNNING = "No sign-in is running on the other computer."
PASSED_ON = "Lifeline passed your sign-in to the other computer. You can close this window."
NOT_PASSED = "Lifeline could not pass your sign-in to the other computer. The terminal or the menu says why."
ONLY_CALLBACK = "Lifeline here only passes the sign-in answer to the other computer."
SIGNIN_STOPPED = "The sign-in was stopped before it finished."
FROM_MAC = "The other computer's sign-ins are added from the Mac with lifeline computer add, adopt and resign."
MAC_FORGETS = "Accounts are removed and reset from the Mac, which changes the other computer too, so nothing was changed."
UNKNOWN_HERE = "The other computer does not know that account yet, so nothing was changed."
BAD_DEVBOX_ADD = "Adding an account on the other computer takes the account and, if you like, an email."
BAD_DEVBOX_ADOPT = "Taking over the other computer's own sign-in takes only the account it should be, since its names come from the Mac."
LIVE_NOT_EXPECTED = "The other computer's Claude Code uses {live}, not {expected}, so nothing was changed."
LIVE_GONE = "The other computer's Claude Code no longer uses {expected}, so nothing was changed."
NOT_ADDED_LIVE = "an account Lifeline has not added"
LIVE_UNKNOWN = "The other computer uses an account the Mac has not added, so nothing was changed."
DEVBOX_BUSY = "Lifeline on the other computer is already signing in to an account. Finish that sign-in first."
NO_SUCH_SIGNIN = "Lifeline on the other computer does not know that sign-in."
NO_LOGIN_HERE = "Lifeline on the other computer cannot run a sign-in."
HELD_ADD = "The other computer already has its own sign-in for {name}, so sign in again with lifeline computer resign {number}."
ADDED = "The other computer has its own sign-in for {name} now."
RESIGNED = "The other computer has a new sign-in for {name}."
ADOPTED = "Lifeline on the other computer took over the sign-in its Claude Code holds for {name}."
KEPT_LIVE = "The other computer already uses {name}, so Lifeline kept the sign-in the other computer has."
REQUEST_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
NO_DEVBOX_LINK = "This Mac is not paired with another computer yet, so nothing was started. Add one in Alerts and settings."
DEVBOX_SILENT = "The other computer did not answer in time, so nothing was changed."
DEVBOX_UNSURE = (
    "The other computer did not answer in time. If the sign-in started there, Lifeline stops it as soon as the other computer answers."
)
REQUEST_MISMATCH = "That request belongs to a different sign-in, so nothing was changed."
UNSURE_SECONDS = 60
DEVBOX_REFUSED = "Lifeline on the other computer could not finish that request."
DEVBOX_LOST = "The other computer stopped answering, so Lifeline stopped waiting. The other computer ends the sign-in on its own within 10 minutes."
DEVBOX_JOB_LOST = "The other computer no longer lists that sign-in, so Lifeline cannot tell how it ended."
UNTRUSTED_PAGE = "The other computer gave a sign-in page Lifeline does not trust, so Lifeline stopped the sign-in."
NO_DEVBOX_PAGE = "The other computer did not give a sign-in page in time, so Lifeline stopped the sign-in."
DEVBOX_TOO_LONG = "Lifeline stopped waiting after 12 minutes, so it stopped the sign-in."
DEVBOX_PORT_TAKEN = "Port {port} on this Mac is in use, so the sign-in could not come back. Run the command again."


class LinkError(Exception):
    pass


class LinkUnreached(LinkError):
    pass


class RelayError(Exception):
    pass


class SigninRefused(Exception):
    def __init__(self, sentence, job=None):
        super().__init__(sentence)
        self.sentence = sentence
        self.job = job


def _ran(run, args):
    try:
        done = run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=OPEN_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return getattr(done, "returncode", None) == 0


def _web_handler(prefs):
    try:
        data = plistlib.loads(Path(prefs).read_bytes())
    except FileNotFoundError:
        return SYSTEM_BROWSER
    except Exception:
        return None
    handlers = data.get("LSHandlers") if isinstance(data, dict) else None
    found = {}
    for item in handlers if isinstance(handlers, list) else []:
        if isinstance(item, dict) and item.get("LSHandlerURLScheme") in WEB_SCHEMES and isinstance(item.get("LSHandlerRoleAll"), str):
            found.setdefault(item["LSHandlerURLScheme"], item["LSHandlerRoleAll"])
    return next((found[scheme] for scheme in WEB_SCHEMES if scheme in found), SYSTEM_BROWSER)


def _app_name(bundle, folders):
    for folder in folders:
        try:
            apps = sorted(Path(folder).glob("*.app"))
        except OSError:
            continue
        for app in apps:
            try:
                info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
            except Exception:
                continue
            if isinstance(info, dict) and str(info.get("CFBundleIdentifier", "")).lower() == bundle:
                name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem
                return name if isinstance(name, str) and name.strip() else app.stem
    return None


def default_browser(prefs=None, folders=None):
    prefs = Path.home() / LAUNCH_SERVICES if prefs is None else prefs
    bundle = _web_handler(prefs)
    if not bundle:
        return None
    bundle = bundle.lower()
    return BROWSER_NAMES.get(bundle) or _app_name(bundle, APP_FOLDERS if folders is None else folders)


def open_default(url, run=None, browser_fn=None):
    run = subprocess.run if run is None else run
    try:
        browser = (browser_fn or default_browser)()
    except Exception:
        browser = None
    return browser, _ran(run, [OPEN_COMMAND, url])


def open_page(url):
    return open_default(url)[1]


def mac_relay(port, forward):
    return CallbackRelay(port, forward)


def registry_payload(accounts):
    return [
        {
            "uuid": account.uuid,
            "number": account.key,
            "name": account.name,
            "short": account.short,
            "full": account.full,
            "email": account.email or "",
            "plan": account.plan or "",
            "login_uuid": account.login,
            "org_uuid": account.org_uuid,
        }
        for account in sorted(accounts, key=lambda account: account.key)
    ]


def _said(value):
    if not isinstance(value, str) or len(value) > TEXT_LIMIT or not value.isprintable():
        return None
    value = value.strip()
    return value if value and value[-1] in ".!?" else None


def _answer(action, unsure=DEVBOX_SILENT):
    try:
        status, data = action()
    except LinkUnreached:
        raise SigninRefused(DEVBOX_SILENT) from None
    except Exception:
        raise SigninRefused(unsure) from None
    data = data if isinstance(data, dict) else {}
    if not 200 <= status < 300:
        running = data.get("job")
        running = running if isinstance(running, str) and ACCOUNT_ID.fullmatch(running) else None
        raise SigninRefused(_said(data.get("error")) or DEVBOX_REFUSED, running)
    return data


def adopt_ready(live_uuid, registered, live_ready=True):
    return isinstance(live_uuid, str) and bool(live_uuid) and live_uuid in registered and live_ready is True


def stop_remote(link, job_id):
    try:
        link.stop_signin(job_id)
    except Exception:
        pass


def _follow(link, job_id, sleep, clock, until_page, seconds):
    deadline = clock() + seconds
    misses = 0
    while True:
        try:
            status, data = link.job(job_id)
        except Exception:
            status, data = None, None
        if status == 404:
            raise SigninRefused(DEVBOX_JOB_LOST)
        if status == 200 and isinstance(data, dict):
            misses = 0
            if data.get("state") != "running" or (until_page and data.get("url")):
                return data
        else:
            misses += 1
            if misses >= LINK_TRIES:
                raise SigninRefused(DEVBOX_LOST)
        if clock() >= deadline:
            stop_remote(link, job_id)
            raise SigninRefused(NO_DEVBOX_PAGE if until_page else DEVBOX_TOO_LONG)
        sleep(JOB_POLL_SECONDS)


def drive_signin(
    link,
    route,
    body,
    *,
    accounts,
    relay,
    open_page,
    sleep,
    clock,
    wants_page,
    on_job=None,
    on_page=None,
    page_seconds=PAGE_WAIT_SECONDS,
    job_seconds=JOB_WAIT_SECONDS,
    on_unsure=None,
):
    _answer(lambda: link.request("POST", "/registry", {"accounts": registry_payload(accounts)}))
    token = uuidlib.uuid4().hex
    body = dict(body or {}, request=token)
    try:
        reply = _answer(lambda: link.start_signin(route, body), DEVBOX_UNSURE)
    except SigninRefused as refused:
        if refused.sentence != DEVBOX_UNSURE:
            raise
        reply = None
    except BaseException:
        _hand_on(on_unsure, token)
        raise
    if reply is None:
        try:
            found = _find_started(link, token, sleep, clock)
        except BaseException:
            _hand_on(on_unsure, token)
            raise
        if found is None:
            raise SigninRefused(DEVBOX_SILENT)
        if found is _UNKNOWN:
            _hand_on(on_unsure, token)
            raise SigninRefused(DEVBOX_UNSURE)
        reply = {"job": found}
    job_id = reply.get("job")
    if not isinstance(job_id, str) or not job_id:
        raise SigninRefused(DEVBOX_REFUSED)
    if on_job is not None:
        on_job(job_id)
    held = None
    try:
        job = _follow(link, job_id, sleep, clock, wants_page, page_seconds)
        if wants_page and job.get("state") == "running":
            url, port = job.get("url"), job.get("port")
            try:
                found = signin_port(url)
            except ValueError:
                found = None
            if found is None or isinstance(port, bool) or found != port:
                stop_remote(link, job_id)
                raise SigninRefused(UNTRUSTED_PAGE)
            try:
                held = relay(port, lambda target: _forward(link, job_id, target))
            except RelayError:
                stop_remote(link, job_id)
                raise SigninRefused(DEVBOX_PORT_TAKEN.format(port=port)) from None
            if not getattr(held, "alive", True):
                stop_remote(link, job_id)
                raise SigninRefused(DEVBOX_PORT_TAKEN.format(port=port))
            try:
                opened = bool(open_page(url)) if open_page is not None else False
            except Exception:
                opened = False
            if on_page is not None:
                on_page(url, opened)
            job = _follow(link, job_id, sleep, clock, False, job_seconds)
    finally:
        if held is not None:
            held.close()
    return job


_UNKNOWN = object()


def _hand_on(on_unsure, token):
    if on_unsure is None:
        return
    try:
        on_unsure(token)
    except Exception:
        log.warning("Lifeline could not keep a sign-in on the other computer to stop later.")


def _find_started(link, token, sleep, clock):
    deadline = clock() + UNSURE_SECONDS
    while True:
        try:
            status, data = link.find_request(token)
        except Exception:
            status, data = None, None
        if status == 404:
            return None
        found = data.get("job") if status == 200 and isinstance(data, dict) else None
        if isinstance(found, str) and ACCOUNT_ID.fullmatch(found):
            return found
        if clock() >= deadline:
            return _UNKNOWN
        sleep(JOB_POLL_SECONDS)


def _forward(link, job_id, target):
    status, data = link.callback(job_id, target)
    if status == 200 and isinstance(data, dict):
        code = data.get("status")
        location = data.get("location")
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
            return code, location if isinstance(location, str) else None
    return None, None


def stop_running(link):
    return _answer(lambda: link.request("POST", "/stop", {}))


def wait_ended(link, job_id, sleep, clock, seconds=STOP_WAIT_SECONDS):
    deadline = clock() + seconds
    while True:
        try:
            status, data = link.job(job_id)
        except Exception:
            status, data = None, None
        if status == 200 and isinstance(data, dict) and data.get("state") != "running":
            return ENDED, data
        if status == 404:
            return GONE, None
        if clock() >= deadline:
            return WAITING, None
        sleep(1)


def replay_callback(port, target, timeout=CALLBACK_SECONDS):
    conn = http.client.HTTPConnection(LOGIN_TARGET, port, timeout=timeout)
    try:
        conn.request("GET", target, headers={"Host": f"localhost:{port}", "Connection": "close"})
        response = conn.getresponse()
        response.read(PAGE_LIMIT)
        return response.status, response.getheader("Location")
    finally:
        conn.close()


def callback_target(value):
    if not isinstance(value, str) or not value or len(value) > CALLBACK_LIMIT or not value.isascii():
        raise ValueError("not a callback")
    if not value.isprintable() or any(ch.isspace() for ch in value) or not value.startswith("/callback"):
        raise ValueError("not a callback")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path != "/callback" or parts.fragment:
        raise ValueError("not a callback")
    query = parse_qs(parts.query, keep_blank_values=True)
    if not all(len(query.get(name, [])) == 1 and query[name][0] for name in ("code", "state")):
        raise ValueError("not a callback")
    return value


def hook_path():
    return Path(__file__).resolve().parents[2] / "bin" / HOOK_NAME


def signin_port(url):
    if not isinstance(url, str) or not url or len(url) > PAGE_LIMIT:
        raise ValueError("not a sign-in page")
    if not url.isprintable() or any(ch.isspace() for ch in url):
        raise ValueError("not a sign-in page")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in TRUSTED_PAGE_HOSTS or parts.fragment:
        raise ValueError("not a sign-in page")
    if not parts.path.endswith(PAGE_PATH_END):
        raise ValueError("not a sign-in page")
    query = parse_qs(parts.query, keep_blank_values=True)
    challenge = query.get("code_challenge") or []
    if query.get("code_challenge_method") != ["S256"] or len(challenge) != 1 or not challenge[0]:
        raise ValueError("no PKCE")
    found = query.get("redirect_uri")
    if not found or len(found) != 1:
        raise ValueError("no single way back")
    back = urlsplit(found[0])
    if back.scheme != "http" or back.hostname != "localhost" or back.path != "/callback" or back.query or back.fragment:
        raise ValueError("not a local way back")
    try:
        port = back.port
    except ValueError:
        raise ValueError("not a port") from None
    if port is None or not 1024 <= port <= 65535 or back.netloc != f"localhost:{port}":
        raise ValueError("not a port")
    return port


def login_scope(env=None, which=None, run=None):
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    run = subprocess.run if run is None else run
    if not env.get("INVOCATION_ID"):
        return []
    tool = which("systemd-run", path=env.get("PATH"))
    truth = which("true", path=env.get("PATH"))
    if tool is None or truth is None:
        return []
    prefix = [tool, "--user", "--scope", "--quiet", "--collect", "-p", f"RuntimeMaxSec={SCOPE_SECONDS}", "--"]
    try:
        done = run(
            prefix + [truth],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SCOPE_PROBE_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return prefix if getattr(done, "returncode", None) == 0 else []


def _answers(host, port, seconds=LISTEN_SECONDS):
    deadline = time.monotonic() + seconds
    while True:
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def _read_page(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(PAGE_LIMIT + 2)
    except OSError:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text[:-1] if text.endswith("\n") else text


def _end(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        process.terminate()
    try:
        process.wait(END_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        process.kill()
    try:
        process.wait(END_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def oom_first(path=OOM_PATH):
    def apply():
        try:
            fd = os.open(path, os.O_WRONLY)
        except FileNotFoundError:
            return
        try:
            os.write(fd, OOM_FIRST)
        finally:
            os.close(fd)

    return apply


class DevboxLogin:
    def __init__(
        self,
        *,
        command=None,
        hook=None,
        popen=subprocess.Popen,
        scope=login_scope,
        listening=_answers,
        oom_path=OOM_PATH,
        timeout=server.LOGIN_TIMEOUT,
        page_seconds=PAGE_SECONDS,
        poll=LOGIN_POLL_SECONDS,
        clock=time.monotonic,
        needs_scope=sys.platform != "darwin",
    ):
        self.needs_scope = needs_scope
        self.command = command
        self.hook = Path(hook) if hook is not None else hook_path()
        self.popen = popen
        self.scope = scope
        self.listening = listening
        self.oom_path = oom_path
        self.timeout = timeout
        self.page_seconds = page_seconds
        self.poll = poll
        self.clock = clock

    def _pause(self, stop):
        if stop is not None:
            stop.wait(self.poll)
        else:
            time.sleep(self.poll)

    def __call__(self, folder, email=None, user=None, on_page=None, stop=None):
        folder = Path(folder)
        command = shutil.which(self.command or ident.claude() or server.LOGIN_COMMAND, path=server._search_path())
        if command is None:
            raise server.LoginFailed(server.NO_LOGIN_COMMAND)
        prefix = list(self.scope() or []) if self.scope is not None else []
        if not prefix and self.needs_scope:
            log.warning(NO_SCOPE)
            raise server.LoginFailed(NO_SCOPE)
        args = [command, "auth", "login", "--claudeai"] + (["--email", email] if email else [])
        page = folder / PAGE_NAME
        env = enrolling.login_environment(folder, user if user is not None else enrolling.login_user())
        env.update({"BROWSER": str(self.hook), PAGE_VARIABLE: str(page), "DISABLE_AUTOUPDATER": "1"})
        try:
            process = self.popen(
                prefix + args,
                env=env,
                cwd=str(folder),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=oom_first(self.oom_path),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise server.LoginFailed(server.LOGIN_REFUSED) from None
        failure = None
        code = None
        started = self.clock()
        shown = False
        try:
            while True:
                code = process.poll()
                if code is not None:
                    break
                if stop is not None and stop.is_set():
                    failure = SIGNIN_STOPPED
                    break
                waited = self.clock() - started
                if waited >= self.timeout:
                    failure = server.LOGIN_TIMED_OUT
                    break
                if not shown:
                    url = _read_page(page)
                    if url is None and waited >= self.page_seconds:
                        failure = NO_PAGE
                        break
                    if url is not None:
                        try:
                            port = signin_port(url)
                        except ValueError:
                            failure = BAD_PAGE
                            break
                        if not self.listening(LOGIN_TARGET, port):
                            failure = NOT_LISTENING
                            break
                        shown = True
                        if on_page is not None:
                            on_page(port, url)
                self._pause(stop)
        finally:
            _end(process)
        if failure is not None:
            raise server.LoginFailed(failure)
        if code != 0:
            raise server.LoginFailed(server.LOGIN_REFUSED)


def _location(value):
    """The page the browser goes on to after a sign-in, only when it is Claude's or Anthropic's."""
    if not isinstance(value, str) or len(value) > LOCATION_LIMIT or not value.isprintable():
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme != "https" or "@" in parts.netloc or not any(host == name or host.endswith("." + name) for name in TRUSTED_LOCATION_HOSTS):
        return None
    return value


class CallbackRelay:
    def __init__(self, port, forward, *, listen=RELAY_LISTEN):
        self.forward = forward
        self._lock = threading.Lock()
        self._closed = False
        self._servers = []
        self._threads = []
        self._open = set()
        first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            first.bind((listen[0], port))
            first.listen(16)
        except OSError:
            first.close()
            raise RelayError("taken") from None
        self._servers.append(first)
        self.port = first.getsockname()[1]
        for extra in listen[1:]:
            self._extra(extra)
        for listener in self._servers:
            listener.settimeout(0.25)
            thread = threading.Thread(target=self._accept, args=(listener,), name="lifeline-relay", daemon=True)
            self._threads.append(thread)
            thread.start()

    @property
    def addresses(self):
        with self._lock:
            return [listener.getsockname()[0] for listener in self._servers]

    @property
    def alive(self):
        with self._lock:
            return not self._closed and bool(self._threads) and all(thread.is_alive() for thread in self._threads)

    def _extra(self, address):
        try:
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            return
        try:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind((address, self.port))
            listener.listen(16)
        except OSError as error:
            listener.close()
            if error.errno == errno.EADDRINUSE:
                self.close()
                raise RelayError("taken") from None
            return
        self._servers.append(listener)

    def _accept(self, listener):
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                browser, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                if self._closed:
                    browser.close()
                    return
                self._open.add(browser)
            threading.Thread(target=self._serve, args=(browser,), daemon=True).start()

    def _head(self, browser):
        deadline = time.monotonic() + RELAY_HEAD_SECONDS
        data = b""
        while b"\r\n\r\n" not in data and len(data) < RELAY_HEAD_LIMIT:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            browser.settimeout(left)
            chunk = browser.recv(RELAY_HEAD_LIMIT)
            if not chunk:
                return None
            data += chunk
        if b"\r\n\r\n" not in data:
            return None
        return data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")

    def _serve(self, browser):
        try:
            lines = self._head(browser) or [""]
            words = lines[0].split(" ")
            hosts = [value.strip() for name, _, value in (line.partition(":") for line in lines[1:]) if name.strip().lower() == "host"]
            local = len(hosts) == 1 and hosts[0] in [f"{name}:{self.port}" for name in LOCAL_HOSTS]
            target = words[1] if len(words) == 3 and words[0] == "GET" and local else None
            try:
                target = callback_target(target)
            except ValueError:
                target = None
            if target is None:
                self._reply(browser, 404, None, ONLY_CALLBACK)
                return
            try:
                status, location = self.forward(target)
            except Exception:
                status, location = None, None
            if status is None:
                self._reply(browser, 502, None, NOT_PASSED)
            else:
                self._reply(browser, status, _location(location), PASSED_ON if status < 400 else NOT_PASSED)
        except OSError:
            pass
        finally:
            with self._lock:
                self._open.discard(browser)
            try:
                browser.close()
            except OSError:
                pass

    @staticmethod
    def _reply(browser, status, location, text):
        body = text.encode("utf-8")
        lines = [f"HTTP/1.1 {status} Lifeline", "Content-Type: text/plain; charset=utf-8", f"Content-Length: {len(body)}"]
        if location is not None:
            lines.append(f"Location: {location}")
        lines.append("Connection: close")
        browser.settimeout(RELAY_HEAD_SECONDS)
        browser.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

    def close(self):
        with self._lock:
            self._closed = True
            servers, self._servers = self._servers, []
            open_now, self._open = list(self._open), set()
        for listener in servers:
            try:
                listener.shutdown(socket.SHUT_RDWR)  # Linux keeps a listener open while accept() waits, unless shut down
            except OSError:
                pass
            try:
                listener.close()
            except OSError:
                pass
        for each in open_now:
            try:
                each.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                each.close()
            except OSError:
                pass


def _precise(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _line(text, limit=TEXT_LIMIT):
    return isinstance(text, str) and len(text) <= limit and not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _text(text, limit=TEXT_LIMIT):
    return _line(text, limit) and bool(text.strip())


def devbox_parts(home=None, system=None):
    home = Path(home) if home is not None else Path.home()
    mac = (system or sys.platform) == "darwin"
    folder = ident.data_folder(home, "darwin" if mac else "linux")
    user = enrolling.login_user()
    return server.Parts(
        home=home,
        folder=folder,
        slot=MacKeychainSlot(account=enrolling.keychain_account(user)) if mac else FileSlot(home / ".claude" / ".credentials.json"),
        store=MacKeychainStore() if mac else FileStore(ident.signin_folder(home)),
        registry=Registry(folder / "accounts.json"),
        history=History(folder / "history.db"),
        profile_fn=anthropic.profile,
        usage_fn=anthropic.usage,
        refresh_fn=anthropic.refresh,
        run_login=DevboxLogin(),
        callback_fn=replay_callback,
        runner=run_security if mac else None,
        user=user,
        link=server.NullLink(),
        clock=server._utc_now,
        tz=server._local_zone(),
        sessions_fn=lambda: server.live_sessions(home),
        temp_root=enrolling.login_root(home, "darwin" if mac else "linux"),
        backups=folder / switching.BACKUPS_NAME,
    )


def keeper(home=None, runner=None):
    """The other computer's pairing: in the keychain on a Mac, where runner reaches it, or else a file only its owner can read."""
    slot = MacKeychainSlot(ident.bundle_id(), PAIRING_ACCOUNT, runner) if runner else None
    return pairing.Keeper(pairing.default_path(home), server._write_private, server._read_private, slot=slot)


def _identity_field(value, name, empty_ok):
    found = value.get(name)
    if found is None:
        return empty_ok
    return isinstance(found, str) and ACCOUNT_ID.fullmatch(found) is not None


def _registry_item(value):
    if not isinstance(value, dict) or not REGISTRY_KEYS <= set(value) <= REGISTRY_KEYS | IDENTITY_KEYS:
        return None
    uuid, number = value["uuid"], value["number"]
    if not isinstance(uuid, str) or not ACCOUNT_ID.fullmatch(uuid):
        return None
    if not _identity_field(value, "login_uuid", True) or not _identity_field(value, "org_uuid", True):
        return None
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    if not all(_text(value[name], 200) for name in ("name", "short", "full")):
        return None
    if not all(_line(value[name], 200) for name in ("email", "plan")):
        return None
    return dict(value)


class DevboxService(server.Service):
    machine = "devbox"

    def __init__(self, parts, **options):
        super().__init__(parts, **options)
        self.tcp_address = None
        self.keeper = keeper(parts.home, parts.runner)
        self._covers = frozenset()
        self._signin_ends = {}
        self._sessions_open = 0
        self._live_registered = None
        self._live_outsider = None
        self._held = frozenset()
        self._pushed_known = []
        self._looked = False
        self._stops = {}
        self._job_threads = {}
        self.reader.on_identified = self._record_live_pair

    def mac_uuid(self):
        return None

    def _usable_on_devbox(self):
        with self._state:
            return frozenset(self._covers)

    def pair(self, body):
        nonce = body.get("nonce") if set(body) == {"nonce", "half"} else None
        if not isinstance(nonce, str) or len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
            raise server.Refusal(400, BAD_PAIR)
        try:
            half = pairing.half_from(body["half"])
        except ValueError:
            raise server.Refusal(400, BAD_PAIR) from None
        try:
            _, own = self.keeper.pair(bytes.fromhex(nonce), half)
        except Exception:
            raise server.Refusal(500, NO_PAIRING_SAVE) from None
        log.info("Lifeline paired this computer with a Mac.")
        return {"name": pairing.own_name(), "half": format(own, "x")}

    def unpair(self):
        try:
            self.keeper.unpair()
        except Exception:
            raise server.Refusal(500, NO_PAIRING_SAVE) from None
        log.info("Lifeline on this computer forgot the Mac's key.")
        return {}

    def covers(self):
        with self._state:
            return sorted(self._covers)

    def resolve(self, body):
        raise server.Refusal(403, SWITCH_REFUSED)

    def switch_to(self, uuid):
        raise server.Refusal(403, SWITCH_REFUSED)

    def undo(self):
        raise server.Refusal(403, SWITCH_REFUSED)

    def start_job(self, kind, uuid=None, body=None):
        if kind not in SIGNIN_KINDS:
            raise server.Refusal(403, SWITCH_REFUSED)
        body = dict(body or {})
        request = body.pop("request", None)
        if request is not None and (not isinstance(request, str) or not REQUEST_ID.fullmatch(request)):
            raise server.Refusal(400, {"add": BAD_DEVBOX_ADD, "resign": server.BAD_RESIGN}.get(kind, BAD_DEVBOX_ADOPT))
        email = None
        if kind == "add":
            uuid = body.get("uuid")
            if set(body) - DEVBOX_ADD_KEYS or not isinstance(uuid, str) or not ACCOUNT_ID.fullmatch(uuid):
                raise server.Refusal(400, BAD_DEVBOX_ADD)
            email = self._email(body, BAD_DEVBOX_ADD)
        elif kind == "resign":
            if set(body) - server.RESIGN_KEYS:
                raise server.Refusal(400, server.BAD_RESIGN)
            email = self._email(body, server.BAD_RESIGN)
        elif set(body) - {"expected"} or (
            "expected" in body and not (isinstance(body["expected"], str) and ACCOUNT_ID.fullmatch(body["expected"]))
        ):
            raise server.Refusal(400, BAD_DEVBOX_ADOPT)
        takeover = False
        pinned = False
        if kind == "adopt":
            uuid = self._live_uuid()
            expected = body.get("expected")
            if expected is not None and uuid != expected:
                raise server.Refusal(409, self._not_expected(expected, uuid))
            pinned = expected is not None
            if not adopt_ready(uuid, self._registered()):
                raise server.Refusal(409, LIVE_UNKNOWN)
        else:
            account = self._known(uuid)
            if account is None:
                raise server.Refusal(404, UNKNOWN_HERE)
            takeover = kind == "add" and self._live_uuid() == uuid
            if kind == "add" and not takeover and self._parked(uuid) is not None:
                raise server.Refusal(409, HELD_ADD.format(name=account.name, number=account.key))
        if kind != "adopt" and not takeover and self.parts.run_login is None:
            raise server.Refusal(409, NO_LOGIN_HERE)
        check = self._live_check()
        job_id = uuidlib.uuid4().hex
        stop = threading.Event()
        thread = threading.Thread(
            target=self._signin_job, args=(job_id, kind, uuid, email, stop, check, takeover, pinned), name="lifeline-job", daemon=True
        )
        with self._state:
            if request is not None:
                same = next((job for job in self._jobs.values() if job.get("request") == request), None)
                if same is not None:
                    if same["kind"] != kind or same["uuid"] != uuid:
                        raise server.Refusal(409, REQUEST_MISMATCH)
                    return {"job": same["id"]}
            if self._forgetting:
                raise server.Refusal(409, switching.LIFELINE_BUSY)
            running = self._running_job()
            if running is not None:
                raise server.Refusal(409, DEVBOX_BUSY, {"job": running})
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "uuid": uuid,
                "state": "running",
                "result": "",
                "started_at": self._now(),
                "finished_at": None,
                "port": None,
                "url": None,
                "answered": False,
                "request": request,
            }
            self._stops[job_id] = stop
            self._job_threads[job_id] = thread
        thread.start()
        return {"job": job_id}

    @staticmethod
    def _email(body, sentence):
        if "email" not in body:
            return None
        value = body["email"]
        if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
            raise server.Refusal(400, sentence)
        value = value.strip()
        if not server._email_shape(value):
            raise server.Refusal(400, sentence)
        return value

    def _known_accounts(self):
        own = super()._known_accounts()
        if own:
            return own
        with self._state:
            return [dict(item) for item in self._pushed_known]

    def _registered(self):
        try:
            return {account.uuid for account in self.parts.registry.load()}
        except Exception:
            raise server.Refusal(500, server.NO_ACCOUNTS) from None

    def _live_held(self):
        try:
            item, readable = self._live_item()
        except Exception:
            return False
        if not readable or item is None:
            return False
        uuid = self._devbox["account_uuid"]
        return uuid is not None and self._recorded_for(item[OAUTH_KEY]) == uuid

    def _live_ready(self):
        try:
            self._live_check()
        except server.Refusal:
            return False
        except Exception:
            return False
        return True

    def _known(self, uuid):
        try:
            return self.parts.registry.by_uuid(uuid)
        except Exception:
            raise server.Refusal(500, server.NO_ACCOUNTS) from None

    def _live_item(self):
        try:
            item = self.parts.slot.read()
        except Exception:
            return None, False
        oauth = item.get(OAUTH_KEY) if isinstance(item, dict) else None
        return (item if isinstance(oauth, dict) and oauth else None), True

    def _recorded_for(self, oauth):
        entry = switching.read_record_entry(self.record_path)
        mark = switching.fingerprint(oauth)
        if entry is None or entry["uuid"] is None or mark is None or entry["refresh_sha256"] != mark:
            return None
        return entry["uuid"]

    def _expired(self, oauth):
        expires = oauth.get("expiresAt") if isinstance(oauth, dict) else None
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            return False
        return expires <= self._wall() * 1000

    def _live_check(self):
        item, readable = self._live_item()
        if not readable:
            raise server.Refusal(409, LIVE_UNREADABLE)
        if item is None:
            return None
        oauth = item[OAUTH_KEY]
        if self._recorded_for(oauth) is not None:
            return None
        if self._expired(oauth):
            raise server.Refusal(409, LIVE_STALE)
        return item

    def _confirm_live(self, item):
        try:
            found = switching.identify_live(
                item,
                profile_fn=self.parts.profile_fn,
                home=self.parts.home,
                record_path=self.record_path,
                registry=self.parts.registry,
                clock=self._wall,
            )
        except Exception:
            raise enrolling.EnrollError(LIVE_UNCHECKED) from None
        self._record_live_pair(item, found)

    def _record_live_pair(self, item, uuid):
        oauth = item.get(OAUTH_KEY) if isinstance(item, dict) else None
        mark = switching.fingerprint(oauth)
        if mark is None or not isinstance(uuid, str) or not uuid or self._recorded_for(oauth) == uuid:
            return
        with writelock.held(self.lock_path, timeout=self.parts.lock_timeout):
            current, readable = self._live_item()
            if not readable or current is None or switching.fingerprint(current[OAUTH_KEY]) != mark:
                return
            if self._recorded_for(current[OAUTH_KEY]) == uuid:
                return
            switching.record_confirmed(self.record_path, uuid, current, self._now())

    def _unconfirmed(self):
        item, readable = self._live_item()
        if not readable:
            return LIVE_UNREADABLE
        if item is not None and self._expired(item[OAUTH_KEY]):
            return LIVE_STALE
        return LIVE_UNCHECKED

    def _show_page(self, job_id, port, url):
        with self._state:
            job = self._jobs.get(job_id)
            if job is not None:
                job["port"] = port
                job["url"] = url

    def _signin_job(self, job_id, kind, uuid, email, stop, check=None, takeover=False, pinned=False):
        state, result = "failed", server.JOB_FAILED[kind]
        reasons = []
        try:
            if check is not None:
                self._confirm_live(check)
            if kind == "adopt" or takeover:
                result = self._take_over(KEPT_LIVE if takeover else ADOPTED, expected=uuid if takeover or pinned else None)
            else:
                result = self._sign_in(job_id, kind, uuid, email, stop, reasons)
            state = "done"
        except enrolling.EnrollError as error:
            notes = [note for note in getattr(error, "__notes__", []) if isinstance(note, str)]
            said = self._unconfirmed() if str(error) == switching.UNCONFIRMED else str(error)
            if str(error) == enrolling.LIVE_NOT_ASKED and (takeover or pinned):
                said = self._not_expected(uuid)
            if said == enrolling.LOGIN_FAILED:
                said, reasons = server.login_result(reasons[0] if reasons else None, kind, devbox=True), []
            result = " ".join([said, *reasons, *notes])
        except Exception as error:
            log.warning("A sign-in on the other computer failed (%s).", type(error).__name__)
        with self._state:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(state=state, result=result, finished_at=self._now())
            self._stops.pop(job_id, None)
            self._job_threads.pop(job_id, None)

    def _sign_in(self, job_id, kind, uuid, email, stop, reasons):
        def login(folder):
            try:
                self.parts.run_login(
                    folder,
                    email,
                    user=self.parts.user,
                    on_page=lambda port, url: self._show_page(job_id, port, url),
                    stop=stop,
                )
            except server.LoginFailed as failure:
                reasons.append(str(failure))
                raise

        updated = enrolling.resign(
            uuid,
            run_login=login,
            runner=None,
            store=self.parts.store,
            registry=self.parts.registry,
            profile_fn=self.parts.profile_fn,
            user=self.parts.user,
            temp_root=self.parts.temp_root,
            live_slot=self.parts.slot,
            home=self.parts.home,
            lock_path=self.lock_path,
            lock_timeout=self.parts.lock_timeout,
            saved_in=enrolling.SAVED_IN_FILE,
        )
        return (ADDED if kind == "add" else RESIGNED).format(name=updated.name)

    def _not_expected(self, expected, live=_UNKNOWN):
        if live is _UNKNOWN:
            try:
                live = self._live_uuid()
            except Exception:
                live = None
        def name_of(uuid):
            try:
                found = self.parts.registry.by_uuid(uuid) if uuid is not None else None
            except Exception:
                found = None
            return found.name if found is not None else NOT_ADDED_LIVE

        if live == expected:
            return LIVE_GONE.format(expected=name_of(expected))
        return LIVE_NOT_EXPECTED.format(live=name_of(live), expected=name_of(expected))

    def _take_over(self, sentence=ADOPTED, expected=None):
        account = enrolling.adopt_live(
            expected=expected,
            slot=self.parts.slot,
            registry=self.parts.registry,
            profile_fn=self.parts.profile_fn,
            home=self.parts.home,
            record_path=self.record_path,
            lock_path=self.lock_path,
            lock_timeout=self.parts.lock_timeout,
        )
        return sentence.format(name=account.name)

    def request_info(self, token):
        with self._state:
            found = next((job for job in self._jobs.values() if token and job.get("request") == token), None)
            if found is None:
                raise server.Refusal(404, NO_SUCH_SIGNIN)
            return {"job": found["id"], "state": found["state"]}

    def job_info(self, job_id):
        with self._state:
            job = self._jobs.get(job_id)
            if job is None:
                raise server.Refusal(404, NO_SUCH_SIGNIN)
            return {field: job.get(field) for field in JOB_FIELDS}

    def stop_signin(self, job_id):
        with self._state:
            job = self._jobs.get(job_id)
            if job is None:
                raise server.Refusal(404, NO_SUCH_SIGNIN)
            event = self._stops.get(job_id)
            running = job["state"] == "running" and event is not None
        if running:
            event.set()
        return {"stopping": running}

    def jobs_running(self):
        with self._state:
            return [job_id for job_id, job in self._jobs.items() if job["state"] == "running"]

    def _running_job(self):
        return next((job_id for job_id, job in self._jobs.items() if job["state"] == "running"), None)

    def stop_running(self):
        with self._state:
            job_id = self._running_job()
        if job_id is None:
            return {"stopping": False, "job": None}
        return dict(self.stop_signin(job_id), job=job_id)

    def answer_callback(self, job_id, body):
        try:
            target = callback_target(body.get("target") if set(body) == {"target"} else None)
        except ValueError:
            raise server.Refusal(400, BAD_CALLBACK) from None
        with self._state:
            job = self._jobs.get(job_id)
            if job is None:
                raise server.Refusal(404, NO_SUCH_SIGNIN)
            port = job.get("port")
            if job["state"] != "running" or not isinstance(port, int):
                raise server.Refusal(409, NOT_WAITING)
            if job.get("answered"):
                raise server.Refusal(409, ANSWERED)
            job["answered"] = True
        replay = self.parts.callback_fn or replay_callback
        try:
            status, location = replay(port, target)
        except Exception:
            status, location = None, None
        if not (isinstance(status, int) and 200 <= status < 400):
            # Claude Code turned it down, so the real answer may still come.
            with self._state:
                job["answered"] = False
        if status is None:
            raise server.Refusal(502, NO_ANSWER)
        return {"status": status, "location": _location(location)}

    def stop(self):
        with self._state:
            events = list(self._stops.values())
            threads = list(self._job_threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + JOB_JOIN_SECONDS
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        super().stop()

    def rename(self, uuid, body):
        raise server.Refusal(403, MAC_NAMES)

    def update_settings(self, body):
        raise server.Refusal(403, MAC_SETTINGS)

    def _auto_step(self, now):
        return

    def tick(self):
        now = self._now()
        for name, step in (
            ("reading", self._read_own),
            ("account", self._account_step),
            ("forecast", self._learn_step),
            ("tidy", self._tidy_step),
            ("prune", self._prune_step),
        ):
            self._guarded_step(name, step, now)
        self.ticks += 1

    def _read_own(self, now):
        for sentence in self.reader.tick():
            self.report(sentence)

    def _live_uuid(self):
        return self._live()[0]

    def _live(self):
        found = statusline.read_oauth_account(self.parts.home)
        if found is statusline.UNREADABLE:
            with self._state:
                return self._devbox["account_uuid"], found
        login, org = stated(found)
        if login is None:
            return None, found
        try:
            accounts = self.parts.registry.load()
        except Exception:
            return None, found
        return identity(accounts, login, org), found

    def _live_signin(self):
        try:
            item = self.parts.slot.read()
        except Exception:
            return None
        oauth = item.get(OAUTH_KEY) if isinstance(item, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        return oauth if isinstance(token, str) and token else None

    def _parked(self, uuid):
        try:
            found = self.parts.store.get(uuid)
        except Exception:
            return None
        return found if isinstance(found, dict) else None

    def _account_step(self, now):
        uuid, details = self._live()
        try:
            registered = [account.uuid for account in self.parts.registry.load()]
        except Exception:
            registered = []
        held = {}
        if uuid in registered:
            live = self._live_signin()
            if live is not None:
                held[uuid] = live
        for each in registered:
            if each not in held:
                parked = self._parked(each)
                if parked is not None:
                    held[each] = parked
        ends = {each: signin_end(oauth, now) for each, oauth in held.items()}
        covers = set()
        for each in held:
            status = self.reader.status(each)
            read = self.reader.read_at(each)
            if not status.needs_signin and not status.cannot_read and read is not None and now - read <= COVER_SPAN:
                covers.add(each)
        live_registered = None
        if uuid is not None:
            try:
                found = self.parts.registry.by_uuid(uuid)
            except Exception:
                found = None
            live_registered = found is not None and agrees(found.oauth_account, found)
        if details is statusline.UNREADABLE:
            outsider = self._live_outsider
        elif uuid is not None and live_registered is False and found is None and isinstance(details, dict):
            email = details.get("emailAddress")
            outsider = {"email": email, "organization": organization_kind(details)} if _text(email, 320) else None
        else:
            outsider = None
        sessions = self._ask(self.parts.sessions_fn) if self.parts.sessions_fn is not None else None
        with self._state:
            self._devbox["reachable"] = True
            self._devbox["account_uuid"] = uuid
            self._live_registered = live_registered
            self._live_outsider = outsider
            self._covers = frozenset(covers)
            self._held = frozenset(held)
            self._signin_ends = {each: end for each, end in ends.items() if end is not None}
            self._sessions_open = len(sessions) if isinstance(sessions, (set, frozenset)) else 0
            self._looked = True

    def _session_pair(self, snap, now):
        return (0, 0), self._session_counts(snap.devbox, now)

    def _signin_end(self, account):
        with self._state:
            found = self._signin_ends.get(account.uuid)
        return found if found is not None else super()._signin_end(account)

    def _devbox_side(self):
        try:
            uuids = [account.uuid for account in self.parts.registry.load()]
        except Exception:
            uuids = []
        with self._state:
            ends = dict(self._signin_ends)
        return {uuid: self.reader.status(uuid) for uuid in uuids}, ends

    def take_mac_readings(self, body):
        readings = body.get("readings") if set(body) == {"readings"} else None
        if not isinstance(readings, list) or len(readings) > PUSH_LIMIT:
            raise server.Refusal(400, BAD_PUSH)
        known = {account.uuid for account in self.parts.registry.load()}
        now = self._now()
        saved = 0
        for item in readings:
            reading = _reading(item)
            if reading is None or reading.uuid not in known or server.has_reading_at(self.parts.history, reading.uuid, reading.at):
                continue
            stored = self.parts.history.add_reading(
                reading.uuid, reading.source, reading.five, reading.five_reset, reading.week, reading.week_reset, min(reading.at, now)
            )
            saved += stored is not None
        return {"saved": saved}

    def _since(self, query, default):
        raw = query.get("since", [""])[-1] if isinstance(query, dict) else ""
        if not raw:
            return default
        moment = server._parse_time(raw)
        if moment is None:
            raise server.Refusal(400, BAD_SINCE)
        return moment

    def readings_since(self, query):
        now = self._now()
        since = max(self._since(query, now - FIRST_SPAN), now - READINGS_SPAN)
        accounts = self.parts.registry.load()
        rows = []
        for account in accounts:
            rows.extend(self.parts.history.readings(account.uuid, since, limit=READINGS_PAGE + 1))
        rows.sort(key=lambda r: r.at)
        page = rows[:READINGS_PAGE]
        statuses = {}
        with self._state:
            held = self._held
        for account in accounts:
            status = self.reader.status(account.uuid)
            statuses[account.uuid] = {
                "held": account.uuid in held,
                "needs_signin": status.needs_signin,
                "cannot_read": status.cannot_read,
                "paused_until": status.paused_until,
                "reading_error": status.last_error,
                "signin_ends": self._signin_end(account),
                "model_limits": [
                    dict(item, week_reset=None if item["week_reset"] is None else _precise(item["week_reset"]))
                    for item in self._model_limits(account.uuid)
                ],
            }
        with self._state:
            covers = sorted(self._covers)
            reports = [
                {"id": note["id"], "title": note["title"], "at": _precise(note["at"])}
                for note in self._notifications
                if note["kind"] == "report" and note["id"].startswith("report-")
            ][-REPORTS_LIMIT:]
        return {
            "at": now,
            "readings": [_wire_reading(r) for r in page],
            "more": len(rows) > len(page),
            "covers": covers,
            "accounts": statuses,
            "reports": reports,
        }

    def account_info(self):
        if not self._looked:
            self._guarded_step("account", self._account_step, self._now())
        with self._state:
            return {
                "account_uuid": self._devbox["account_uuid"],
                "sessions": self._sessions_open,
                "live_registered": self._live_registered,
                "live_email": self._live_outsider["email"] if self._live_outsider else None,
                "live_organization": self._live_outsider["organization"] if self._live_outsider else None,
                "live_ready": self._live_ready(),
                "live_held": self._live_held(),
                "can_sign_in": self.parts.run_login is not None,
            }

    def merge_registry(self, body):
        accounts = body.get("accounts") if set(body) in ({"accounts"}, {"accounts", "known"}) else None
        kept = None
        if "known" in body:
            known = body.get("known")
            if not isinstance(known, list) or len(known) > 200:
                raise server.Refusal(400, BAD_REGISTRY)
            kept = [
                {key: item[key].strip() for key in KNOWN_KEYS}
                for item in known
                if isinstance(item, dict) and all(isinstance(item.get(key), str) and _text(item[key], 320) for key in KNOWN_KEYS)
            ]
        if not isinstance(accounts, list):
            raise server.Refusal(400, BAD_REGISTRY)
        pushed = [_registry_item(item) for item in accounts]
        if any(item is None for item in pushed):
            raise server.Refusal(400, BAD_REGISTRY)
        if len({item["uuid"] for item in pushed}) != len(pushed) or len({item["number"] for item in pushed}) != len(pushed):
            raise server.Refusal(400, BAD_REGISTRY)
        try:
            with writelock.held(self.lock_path, timeout=self.parts.lock_timeout):
                current = self.parts.registry.load()
                known = {account.uuid: account for account in current}
                merged = []
                for item in pushed:
                    old = known.get(item["uuid"])
                    login = item.get("login_uuid") or (old.login if old is not None else item["uuid"])
                    merged.append(
                        Account(
                            uuid=item["uuid"],
                            email=item["email"],
                            name=item["name"],
                            short=item["short"],
                            full=item["full"],
                            plan=item["plan"],
                            key=item["number"],
                            oauth_account=old.oauth_account if old is not None else {},
                            signed_in_at=old.signed_in_at if old is not None else None,
                            signin_ends=old.signin_ends if old is not None else None,
                            org_uuid=item.get("org_uuid") or (old.org_uuid if old is not None else None),
                            login_uuid=login if login != item["uuid"] else None,
                        )
                    )
                named = {item["uuid"] for item in pushed}
                taken = {item["number"] for item in pushed}
                for old in current:
                    if old.uuid not in named and old.key not in taken:
                        merged.append(old)
                        taken.add(old.key)
                self.parts.registry.save(merged)
            if kept is not None:
                with self._state:
                    self._pushed_known = kept
        except writelock.WriteLockTimeout:
            raise server.Refusal(409, switching.LIFELINE_BUSY) from None
        except server.Refusal:
            raise
        except Exception:
            raise server.Refusal(500, NO_REGISTRY_SAVE) from None
        return {"accounts": len(merged)}


def _refuse(sentence):
    def handler(service, query, body, rest):
        raise server.Refusal(403, sentence)

    return handler


LOCAL_ROUTES = {
    ("state", False): server.MAC_ROUTES[("state", False)],
    ("history", True): server.MAC_ROUTES[("history", True)],
    ("reading", False): server.MAC_ROUTES[("reading", False)],
    ("cancel", False): server.MAC_ROUTES[("cancel", False)],
    ("switch", False): {"POST": _refuse(SWITCH_REFUSED)},
    ("undo", False): {"POST": _refuse(SWITCH_REFUSED)},
    ("add", False): {"POST": _refuse(FROM_MAC)},
    ("resign", True): {"POST": _refuse(FROM_MAC)},
    ("adopt", False): {"POST": _refuse(FROM_MAC)},
    ("rename", True): {"POST": _refuse(MAC_NAMES)},
    ("settings", False): {"POST": _refuse(MAC_SETTINGS)},
    ("remove", True): {"POST": _refuse(MAC_FORGETS)},
    ("reset", False): {"POST": _refuse(MAC_FORGETS)},
}

TCP_ROUTES = {
    ("readings", False): {
        "GET": lambda s, query, body, rest: (200, s.readings_since(query)),
        "POST": lambda s, query, body, rest: (200, s.take_mac_readings(body)),
    },
    ("account", False): {"GET": lambda s, query, body, rest: (200, s.account_info())},
    ("pair", False): {"POST": lambda s, query, body, rest: (200, s.pair(body))},
    ("unpair", False): {"POST": lambda s, query, body, rest: (200, s.unpair())},
    ("registry", False): {"POST": lambda s, query, body, rest: (200, s.merge_registry(body))},
    ("add", False): {"POST": lambda s, query, body, rest: (202, s.start_job("add", None, body))},
    ("resign", True): {"POST": lambda s, query, body, rest: (202, s.start_job("resign", rest, body))},
    ("adopt", False): {"POST": lambda s, query, body, rest: (202, s.start_job("adopt", None, body))},
    ("job", True): {"GET": lambda s, query, body, rest: (200, s.job_info(rest))},
    ("request", True): {"GET": lambda s, query, body, rest: (200, s.request_info(rest))},
    ("stop", True): {"POST": lambda s, query, body, rest: (200, s.stop_signin(rest))},
    ("stop", False): {"POST": lambda s, query, body, rest: (200, s.stop_running())},
    ("callback", True): {"POST": lambda s, query, body, rest: (200, s.answer_callback(rest, body))},
    **server.FORGET_ROUTES,
}


class _HeadRefused(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class _Prefixed:
    def __init__(self, data, rest, sock=None, deadline=None):
        self._data = data
        self._at = 0
        self._rest = rest
        self._sock, self._deadline = sock, deadline

    def readline(self, limit=-1):
        if self._at >= len(self._data):
            return self._rest.readline(limit)
        end = self._data.find(b"\n", self._at)
        stop = len(self._data) if end < 0 else end + 1
        if limit is not None and limit >= 0:
            stop = min(stop, self._at + limit)
        line = self._data[self._at:stop]
        self._at = stop
        return line

    def read(self, size=-1):
        whole = size is None or size < 0
        part = self._data[self._at:] if whole else self._data[self._at:self._at + size]
        self._at += len(part)
        if whole:
            return part + self._rest.read()
        while len(part) < size:
            if self._sock is not None:
                left = self._deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                self._sock.settimeout(left)
            chunk = self._rest.read1(size - len(part))
            if not chunk:
                break
            part += chunk
        return part

    def close(self):
        self._rest.close()


class _TcpHandler(server._Handler):
    _raw = None
    _proof = None

    def _drain(self):
        return

    def _raw_body(self):
        if self._raw is None:
            self._raw = super()._raw_body()
            self.connection.settimeout(self.timeout)
        return self._raw

    def _route(self, method):
        self._raw, self._proof = None, None
        keeper = self.server.service.keeper
        if keeper.fresh(self.headers):
            self._proof = keeper.check(method, self.path, self.headers, self._raw_body())
        if self._proof is None:
            raise server.Refusal(401, "")
        return super()._route(method)

    def _signed(self, status, data):
        key = self.server.service.keeper.key() if self._proof else None
        return [(pairing.PROOF_HEADER, pairing.reply_proof(key, self._proof, status, data))] if key else []

    def send_response(self, code, message=None):
        # No Server or Date header, so a stranger learns nothing about what answers.
        self.send_response_only(code, message)

    def _reply(self, status, payload):
        if self._proof is not None:
            return super()._reply(status, payload)
        # Until the signature checks out, the reply is a bare status: no words, no name.
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass

    def handle(self):
        self.close_connection = True
        deadline = time.monotonic() + REQUEST_SECONDS
        try:
            head = self._read_head()
        except _HeadRefused as refused:
            self._refuse_head(refused.status)
            return
        except OSError:
            return
        self.connection.settimeout(self.timeout)
        self.rfile = _Prefixed(head, self.rfile, self.connection, deadline)
        self.handle_one_request()
        self.close_connection = True

    def _read_head(self):
        sock = self.connection
        deadline = time.monotonic() + HEAD_SECONDS
        data = b""
        while True:
            end = data.find(b"\r\n\r\n")
            if end >= 0:
                if end + 4 > HEAD_LIMIT:
                    raise _HeadRefused(431)
                return data
            if len(data) >= HEAD_LIMIT:
                raise _HeadRefused(431)
            left = deadline - time.monotonic()
            if left <= 0:
                raise _HeadRefused(408)
            sock.settimeout(left)
            try:
                chunk = sock.recv(HEAD_LIMIT)
            except TimeoutError:
                raise _HeadRefused(408) from None
            if not chunk:
                raise _HeadRefused(None)
            data += chunk

    def _refuse_head(self, status):
        if status is not None:
            self.request_version = "HTTP/1.1"
            self.requestline = ""
            self.command = ""
            try:
                self.connection.settimeout(DRAIN_SECONDS)
                self._reply(status, {})
                self.wfile.flush()
            except OSError:
                pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = QUEUE_SIZE

    def __init__(self, address, service):
        self.service = service
        self.routes = TCP_ROUTES
        self.closing = False
        self._slots = threading.BoundedSemaphore(CONNECTION_LIMIT)
        super().__init__(address, _TcpHandler, bind_and_activate=False)

    def process_request(self, request, client_address):
        while not self._slots.acquire(timeout=SLOT_POLL_SECONDS):
            if self.closing:
                self.close_request(request)
                return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def shutdown_request(self, request):
        try:
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        deadline = time.monotonic() + DRAIN_SECONDS
        left = server.BODY_LIMIT + HEAD_LIMIT
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                request.settimeout(remaining)
                chunk = request.recv(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        self.close_request(request)

    def handle_error(self, request, client_address):
        log.warning("Lifeline closed a connection that failed.")


class _Listener:
    def __init__(self, service, address):
        self.service = service
        self.address = address
        self.server = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        if self._open():
            return
        log.warning(NOT_YET)
        self._thread = threading.Thread(target=self._retry, name="lifeline-listen", daemon=True)
        self._thread.start()

    def _open(self):
        with self._lock:
            if self._stop.is_set():
                return True
            candidate = _TcpServer(self.address, self.service)
            try:
                candidate.server_bind()
                candidate.server_activate()
            except OSError:
                candidate.server_close()
                return False
            self.server = candidate
            self.service.tcp_address = tuple(candidate.server_address[:2])
            threading.Thread(
                target=candidate.serve_forever, kwargs={"poll_interval": 0.25}, name="lifeline-tcp", daemon=True
            ).start()
        log.info(LISTENING)
        return True

    def _retry(self):
        while not self._stop.wait(BIND_RETRY_SECONDS):
            if self._open():
                return

    def stop(self):
        self._stop.set()
        with self._lock:
            found, self.server = self.server, None
        if found is not None:
            found.closing = True
            found.shutdown()
            found.server_close()
        if self._thread is not None:
            self._thread.join(5)


def listen(service, address):
    listener = _Listener(service, address or (pairing.listen_host(), pairing.PORT))
    listener.start()
    return listener.stop


def serve(factory, path, *, address=None, install_signals=True, ready=None, ticking=True, service_lock=None):
    return server.serve(
        factory,
        path,
        install_signals=install_signals,
        ready=ready,
        ticking=ticking,
        service_lock=service_lock,
        routes=LOCAL_ROUTES,
        extra=lambda service: listen(service, address),
    )


def run():
    try:
        home = Path.home()
        return serve(lambda: DevboxService(devbox_parts(home)), statusline.socket_path(home, os.environ))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(server.NO_START, file=sys.stderr)
        log.warning("Lifeline stopped because of a problem (%s).", type(error).__name__)
        return 1


def _reject_constant(name):
    raise ValueError(name)


def _parse_reply(raw):
    head, sep, rest = raw.partition(b"\r\n\r\n")
    if not sep:
        raise LinkError()
    lines = head.decode("latin-1").split("\r\n")
    words = lines[0].split(" ", 2)
    if len(words) < 2 or not words[0].startswith("HTTP/") or not (words[1].isascii() and words[1].isdigit()):
        raise LinkError()
    length = None
    proof = ""
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == pairing.PROOF_HEADER.lower():
            proof = value.strip()
        if name.strip().lower() == "content-length":
            value = value.strip()
            if not (value.isascii() and value.isdigit()):
                raise LinkError()
            length = int(value)
    body = rest if length is None else rest[:length]
    if length is not None and len(body) < length:
        raise LinkError()
    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant) if body.strip() else {}
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise LinkError() from None
    return int(words[1]), data, proof, body


def _figure(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("not a figure")
    return float(value)


def _moment(value):
    if value is None:
        return None
    moment = server._parse_time(value)
    if moment is None:
        raise ValueError("not a time")
    return moment


def _reading(item):
    if not isinstance(item, dict):
        return None
    source = item.get("source") if item.get("source") in SOURCES else "usage"
    try:
        return Reading(
            item.get("uuid"),
            source,
            _figure(item.get("five")),
            _moment(item.get("five_reset")),
            _figure(item.get("week")),
            _moment(item.get("week_reset")),
            _moment(item.get("at")),
        )
    except (ValueError, TypeError):
        return None


def _statuses(value):
    found = {}
    if not isinstance(value, dict):
        return found
    for uuid, item in value.items():
        if not isinstance(uuid, str) or not isinstance(item, dict):
            continue
        paused = item.get("paused_until")
        error = item.get("reading_error")
        found[uuid] = AccountStatus(
            needs_signin=item.get("needs_signin") is True,
            cannot_read=item.get("cannot_read") is True,
            paused_until=server._parse_time(paused) if isinstance(paused, str) else None,
            last_error=error if error in KNOWN_ERRORS else None,
        )
    return found


def _held(value):
    if not isinstance(value, dict):
        return None
    told = {uuid: item.get("held") for uuid, item in value.items() if isinstance(uuid, str) and isinstance(item, dict)}
    if not any(isinstance(flag, bool) for flag in told.values()):
        return None
    return frozenset(uuid for uuid, flag in told.items() if flag is True)


def _ends(value):
    found = {}
    if not isinstance(value, dict):
        return found
    for uuid, item in value.items():
        stamp = item.get("signin_ends") if isinstance(item, dict) else None
        if isinstance(uuid, str) and isinstance(stamp, str) and server._parse_time(stamp) is not None:
            found[uuid] = stamp
    return found


def _model_limits(value):
    found = {}
    if not isinstance(value, dict):
        return found
    for uuid, item in value.items():
        rows = item.get("model_limits") if isinstance(item, dict) else None
        if not isinstance(uuid, str) or not isinstance(rows, list):
            continue
        kept = []
        for row in rows[:16]:
            if not isinstance(row, dict):
                continue
            model, week, reset = row.get("model"), row.get("week"), row.get("week_reset")
            moment = server._parse_time(reset) if isinstance(reset, str) else None
            if not _text(model, 128) or isinstance(week, bool) or not isinstance(week, (int, float)) or not math.isfinite(week):
                continue
            if reset is not None and moment is None:
                continue
            kept.append({"model": model, "percent": float(week), "resets_at": moment})
        found[uuid] = kept
    return found


def _uuids(value):
    if not isinstance(value, list):
        return None
    return [uuid for uuid in value if isinstance(uuid, str) and uuid]


def _reports(value):
    if not isinstance(value, list):
        return []
    return [
        {"id": item["id"], "title": item["title"], "at": item.get("at")}
        for item in value
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("title"), str)
    ]


def _wire_reading(reading):
    return {
        "uuid": reading.uuid,
        "source": reading.source,
        "five": reading.five,
        "five_reset": None if reading.five_reset is None else _precise(reading.five_reset),
        "week": reading.week,
        "week_reset": None if reading.week_reset is None else _precise(reading.week_reset),
        "at": _precise(reading.at),
    }


def pair(address, code, timeout=LINK_TIMEOUT):
    """Pairs this Mac with the computer at address. Returns the reply's status, the computer's name and the shared key."""
    first = pairing.code_key(code)
    nonce = secrets.token_bytes(16)
    secret, half = pairing.new_half()
    made = {}

    def reply_key(data):
        made["key"] = pairing.shared_key(first, nonce, secret, pairing.half_from(data.get("half")))
        return made["key"]

    link = HttpLink(pairing.parse_address(address), key=first, reply_key=reply_key, timeout=timeout)
    status, data = link._call_once("POST", pairing.PAIR_ROUTE, {"nonce": nonce.hex(), "half": format(half, "x")})
    name = data.get("name") if isinstance(data, dict) else None
    return status, name if _text(name, 64) else None, made.get("key")


def paired_link(computer):
    """The link to the paired computer, from the record kept by the Mac, or None."""
    if not isinstance(computer, dict):
        return None
    try:
        key = bytes.fromhex(computer.get("key"))
        return HttpLink(pairing.parse_address(computer.get("address")), key=key) if len(key) == 32 else None
    except (TypeError, ValueError):
        return None


class HttpLink:
    def __init__(self, address, *, key=None, reply_key=None, timeout=None, clock=server._utc_now):
        self.host, self.port = address
        self.key, self.reply_key = key, reply_key or key
        self.timeout = LINK_TIMEOUT if timeout is None else timeout
        self.clock = clock
        self._lock = threading.Lock()
        self._since = None
        self._account = None
        self._sessions = 0
        self._registered = None
        self._outsider = None
        self._live_ready = None
        self._can_sign_in = None
        self._live_held = None
        self._refused = 0
        self._facts = None

    def _call(self, method, target, body=None, timeout=None, retry=None):
        retry = method == "GET" if retry is None else retry
        try:
            return self._call_once(method, target, body, timeout)
        except LinkError:
            if not retry:
                raise
        return self._call_once(method, target, body, timeout)

    def _call_once(self, method, target, body=None, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        payload = b"" if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        lines = [
            f"{method} {target} HTTP/1.1",
            "Host: lifeline",
            "Accept: application/json",
            "Connection: close",
        ]
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
        signed = pairing.request_headers(self.key, method, target, payload) if self.key else {}
        lines += [f"{name}: {value}" for name, value in signed.items()]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + payload

        def left():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkError()
            return remaining

        try:
            conn = socket.create_connection((self.host, self.port), timeout=left())
        except OSError:
            raise LinkUnreached() from None
        chunks = []
        size = 0
        try:
            conn.settimeout(left())
            conn.sendall(request)
            while True:
                conn.settimeout(left())
                chunk = conn.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > REPLY_LIMIT:
                    raise LinkError()
                chunks.append(chunk)
        except OSError:
            raise LinkError() from None
        finally:
            conn.close()
        status, data, proof, raw = _parse_reply(b"".join(chunks))
        if status == 401:
            # Unsigned, so anyone on the way could have sent it. Nothing in it is shown.
            with self._lock:
                self._refused += 1
            return status, {}
        try:
            key = self.reply_key(data) if callable(self.reply_key) else self.reply_key
        except (TypeError, ValueError, AttributeError):
            raise LinkError() from None
        expected = pairing.reply_proof(key, signed.get(pairing.PROOF_HEADER), status, raw) if key else ""
        if not pairing.same(expected, proof):
            raise LinkError()
        if 200 <= status < 300:
            with self._lock:
                self._refused = 0
        return status, data

    def refused(self):
        """True after several unsigned refusals in a row, so one forged reply never asks for pairing again."""
        with self._lock:
            return self._refused >= REFUSED_TRIES

    def unpair(self):
        """Asks the other computer to forget this Mac's key. Its reply cannot be signed once the key is gone."""
        try:
            self._call_once("POST", "/unpair", {}, timeout=3)
        except LinkError:
            pass

    def reachable(self):
        try:
            status, data = self._call("GET", "/account")
        except LinkError:
            return False
        if status != 200 or not isinstance(data, dict):
            return False
        account = data.get("account_uuid")
        sessions = data.get("sessions")
        registered = data.get("live_registered")
        ready, signs, held = data.get("live_ready"), data.get("can_sign_in"), data.get("live_held")
        email, organization = data.get("live_email"), data.get("live_organization")
        outsider = None
        if _text(email, 320) and (organization is None or _text(organization, 200)):
            outsider = {"email": email, "organization": organization}
        with self._lock:
            self._account = account if isinstance(account, str) and account else None
            self._sessions = sessions if isinstance(sessions, int) and not isinstance(sessions, bool) and sessions >= 0 else 0
            self._registered = registered if isinstance(registered, bool) else None
            self._outsider = outsider
            self._live_ready = ready if isinstance(ready, bool) else None
            self._can_sign_in = signs if isinstance(signs, bool) else None
            self._live_held = held if isinstance(held, bool) else None
        return True

    def live_held(self):
        with self._lock:
            return self._live_held

    def live_ready(self):
        with self._lock:
            return self._live_ready

    def can_sign_in(self):
        with self._lock:
            return self._can_sign_in

    def devbox_account(self):
        with self._lock:
            return self._account

    def live_registered(self):
        with self._lock:
            return self._registered

    def live_outsider(self):
        with self._lock:
            return dict(self._outsider) if self._outsider else None

    def fetch_readings(self):
        now = self.clock()
        with self._lock:
            since = self._since if self._since is not None else now - FIRST_SPAN
        status, data = self._call("GET", "/readings?" + urlencode({"since": _precise(since)}), timeout=READINGS_SECONDS)
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("readings"), list):
            raise LinkError()
        readings = [reading for reading in (_reading(item) for item in data["readings"]) if reading is not None]
        facts = {
            "covers": _uuids(data.get("covers")),
            "statuses": _statuses(data.get("accounts")),
            "signin_ends": _ends(data.get("accounts")),
            "held": _held(data.get("accounts")),
            "reports": _reports(data.get("reports")),
            "model_limits": _model_limits(data.get("accounts")),
        }
        with self._lock:
            if readings:
                newest = max(reading.at for reading in readings)
                if self._since is None or newest > self._since:
                    self._since = newest
            self._facts = facts
        return readings

    def devbox_status(self):
        with self._lock:
            if self._facts is None:
                return None
            facts = self._facts
            return {
                "covers": None if facts["covers"] is None else list(facts["covers"]),
                "statuses": dict(facts["statuses"]),
                "signin_ends": dict(facts["signin_ends"]),
                "held": None if facts["held"] is None else sorted(facts["held"]),
                "reports": [dict(item) for item in facts["reports"]],
                "sessions": self._sessions,
                "model_limits": {uuid: [dict(row) for row in rows] for uuid, rows in facts["model_limits"].items()},
            }

    def push_registry(self, accounts, known=None):
        body = {"accounts": accounts} if known is None else {"accounts": accounts, "known": known}
        try:
            status, _ = self._call("POST", "/registry", body, retry=True)
        except LinkError:
            return False
        return status == 200

    def push_readings(self, readings):
        try:
            status, _ = self._call(
                "POST", "/readings", {"readings": [_wire_reading(reading) for reading in readings]}, timeout=PUSH_SECONDS
            )
        except LinkError:
            return False
        return status == 200

    def request(self, method, route, body=None):
        return self._call(method, route, body, retry=method == "GET" or route == "/registry")

    def start_signin(self, route, body):
        sent = False
        try:
            return self._call_once("POST", route, body, START_SECONDS)
        except LinkUnreached:
            pass
        except LinkError:
            if not (isinstance(body, dict) and body.get("request")):
                raise
            sent = True
        try:
            return self._call_once("POST", route, body, START_SECONDS)
        except LinkUnreached:
            if sent:
                raise LinkError() from None
            raise

    def find_request(self, token):
        return self._call("GET", "/request/" + quote(str(token), safe=""))

    def job(self, job_id):
        return self._call("GET", "/job/" + quote(str(job_id), safe=""))

    def stop_signin(self, job_id):
        return self._call("POST", "/stop/" + quote(str(job_id), safe=""), {}, retry=True)

    def callback(self, job_id, target):
        return self._call("POST", "/callback/" + quote(str(job_id), safe=""), {"target": target}, timeout=CALLBACK_SECONDS + 5)
