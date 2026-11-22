import http.client
import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from lifeline import devbox, enroll, server
from lifeline import switch as switching
from tests.test_devbox import SWITCH_REFUSED, TEST_KEY, DevboxWorld
from tests.test_server import WORK, ALEX, ORDER, ORG, PERSONAL, details

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "bin" / "lifeline-signin-page"
STATE = "fake-state-0123456789"
PLANTED = {
    "CLAUDE_CODE_OAUTH_TOKEN": "planted-oauth-SECRET",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
    "ANTHROPIC_API_KEY": "planted-api-SECRET",
    "CLAUDECODE": "1",
}
FROM_MAC = "The other computer's sign-ins are added from the Mac with lifeline computer add, adopt and resign."

FAKE_LOGIN = r'''
import http.server, json, os, subprocess, sys, time
from urllib.parse import parse_qs, urlencode, urlsplit

mode = os.environ.get("FAKE_MODE", "ok")
folder = os.environ["CLAUDE_CONFIG_DIR"]
seen = {
    "argv": sys.argv[1:],
    "pid": os.getpid(),
    "cwd": os.getcwd(),
    "stdin_tty": os.isatty(0),
    "env": {
        key: value
        for key, value in os.environ.items()
        if key in ("CLAUDE_CONFIG_DIR", "USER", "BROWSER", "DISABLE_AUTOUPDATER", "LIFELINE_SIGNIN_PAGE", "CLAUDECODE")
        or key.startswith(("CLAUDE_", "ANTHROPIC_"))
    },
}
with open(os.environ["FAKE_LOG"], "w") as handle:
    json.dump(seen, handle)
done = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path != "/callback" or query.get("state") != [os.environ["FAKE_STATE"]]:
            self.send_response(400)
            self.end_headers()
            done["bad"] = True
            return
        oauth = {
            "accessToken": os.environ["FAKE_ACCESS"],
            "refreshToken": os.environ["FAKE_REFRESH"],
            "expiresAt": int(os.environ["FAKE_EXPIRES"]),
            "scopes": ["user:inference", "user:profile"],
        }
        with open(os.path.join(folder, ".credentials.json"), "w") as handle:
            json.dump({"claudeAiOauth": oauth}, handle)
        with open(os.path.join(folder, ".claude.json"), "w") as handle:
            json.dump({"oauthAccount": json.loads(os.environ["FAKE_DETAILS"])}, handle)
        self.send_response(302)
        self.send_header("Location", "https://platform.claude.com/oauth/code/success?app=claude-code")
        self.send_header("Content-Length", "0")
        self.end_headers()
        done["ok"] = True


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
host = "evil.example" if mode == "bad_page" else "claude.com"
url = "https://" + host + "/cai/oauth/authorize?" + urlencode(
    {
        "code": "true",
        "client_id": "client",
        "response_type": "code",
        "redirect_uri": "http://localhost:%d/callback" % port,
        "scope": "user:inference user:profile",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "state": os.environ["FAKE_STATE"],
    }
)
if mode != "no_page" and os.environ.get("BROWSER"):
    subprocess.run([os.environ["BROWSER"], url])
if mode in ("no_page", "bad_page", "hang"):
    time.sleep(120)
    sys.exit(3)
if mode == "fail":
    time.sleep(0.3)
    sys.exit(1)
server.timeout = 0.5
deadline = time.monotonic() + 30
while not done.get("ok") and time.monotonic() < deadline:
    server.handle_request()
sys.exit(0 if done.get("ok") else 4)
'''


def authorize_url(port=40123, host="claude.com", path="/cai/oauth/authorize", redirect=None, scheme="https", extra=(), drop=()):
    query = [
        ("code", "true"),
        ("client_id", "client"),
        ("response_type", "code"),
        ("redirect_uri", redirect if redirect is not None else f"http://localhost:{port}/callback"),
        ("code_challenge", "challenge"),
        ("code_challenge_method", "S256"),
        ("state", STATE),
        *extra,
    ]
    return f"{scheme}://{host}{path}?" + urlencode([(key, value) for key, value in query if key not in drop])


def write_fake_login(folder):
    path = Path(folder) / "fake-login"
    path.write_text(f"#!{sys.executable} -I\n" + FAKE_LOGIN)
    os.chmod(path, 0o700)
    return path


def browse(port, state=STATE, path="/callback"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", f"{path}?" + urlencode({"code": "the-code", "state": state}))
        response = conn.getresponse()
        response.read()
        return response.status, response.getheader("Location")
    finally:
        conn.close()


def pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


SUCCESS = "https://platform.claude.com/oauth/code/success?app=claude-code"


class SigninPageTests(unittest.TestCase):
    def test_an_authorize_page_gives_its_callback_port(self):
        self.assertEqual(devbox.signin_port(authorize_url(40123)), 40123)
        self.assertEqual(devbox.signin_port(authorize_url(51000, host="claude.ai", path="/oauth/authorize")), 51000)

    def test_any_other_page_is_refused(self):
        bad = {
            "plain http": authorize_url(scheme="http"),
            "other host": authorize_url(host="evil.example"),
            "look-alike host": authorize_url(host="claude.com.evil.example"),
            "user in host": authorize_url(host="claude.com@evil.example"),
            "other port": authorize_url(host="claude.com:8443"),
            "other path": authorize_url(path="/cai/oauth/elsewhere"),
            "no way back": authorize_url(redirect=""),
            "way back to another host": authorize_url(redirect="http://evil.example:40123/callback"),
            "way back by address": authorize_url(redirect="http://127.0.0.1:40123/callback"),
            "way back over https": authorize_url(redirect="https://localhost:40123/callback"),
            "way back elsewhere": authorize_url(redirect="http://localhost:40123/other"),
            "way back with no port": authorize_url(redirect="http://localhost/callback"),
            "way back with a user": authorize_url(redirect="http://someone@localhost:40123/callback"),
            "way back with a padded port": authorize_url(redirect="http://localhost:040123/callback"),
            "low port": authorize_url(80),
            "two ways back": authorize_url(extra=(("redirect_uri", "http://localhost:40124/callback"),)),
            "line break": authorize_url() + "\nhttps://evil.example",
            "not text": None,
            "too long": authorize_url(extra=(("pad", "x" * 9000),)),
            "no PKCE challenge": authorize_url(drop=("code_challenge",)),
            "an empty PKCE challenge": authorize_url(drop=("code_challenge",), extra=(("code_challenge", ""),)),
            "two PKCE challenges": authorize_url(extra=(("code_challenge", "other"),)),
            "no PKCE method": authorize_url(drop=("code_challenge_method",)),
            "a plain PKCE method": authorize_url(drop=("code_challenge_method",), extra=(("code_challenge_method", "plain"),)),
        }
        for name, url in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    devbox.signin_port(url)


class HookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name).resolve()

    def run_hook(self, args, place):
        env = {key: value for key, value in os.environ.items() if key != devbox.PAGE_VARIABLE}
        if place is not None:
            env[devbox.PAGE_VARIABLE] = str(place)
        return subprocess.run([str(HOOK), *args], env=env, capture_output=True, text=True, timeout=30)

    def test_the_hook_is_runnable_and_matches_the_service_path(self):
        self.assertTrue(os.access(HOOK, os.X_OK))
        self.assertEqual(devbox.hook_path(), HOOK)
        self.assertEqual(devbox.HOOK_NAME, "lifeline-signin-page")

    def test_the_hook_keeps_the_page_privately_and_says_nothing(self):
        place = self.folder / "page"
        done = self.run_hook([authorize_url()], place)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(place.read_text(), authorize_url() + "\n")
        self.assertEqual(stat.S_IMODE(place.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["page"])

    def test_the_hook_refuses_without_a_place_a_page_or_a_safe_place(self):
        self.assertNotEqual(self.run_hook([authorize_url()], None).returncode, 0)
        self.assertNotEqual(self.run_hook([], self.folder / "page").returncode, 0)
        self.assertFalse((self.folder / "page").exists())
        target = self.folder / "elsewhere"
        target.write_text("kept")
        link = self.folder / "linked"
        link.symlink_to(target)
        self.assertNotEqual(self.run_hook([authorize_url()], link).returncode, 0)
        self.assertEqual(target.read_text(), "kept")
        first = self.folder / "first"
        self.assertEqual(self.run_hook([authorize_url(40001)], first).returncode, 0)
        self.assertNotEqual(self.run_hook([authorize_url(40002)], first).returncode, 0)
        self.assertEqual(first.read_text(), authorize_url(40001) + "\n")


class Forwarder:
    def __init__(self, status=302, location=SUCCESS, error=None):
        self.status = status
        self.location = location
        self.error = error
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)
        if self.error is not None:
            raise self.error
        return self.status, self.location


def raw_request(port, data):
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    return b"".join(chunks).decode("latin-1")


class CallbackRelayTests(unittest.TestCase):
    def relay(self, forward):
        relay = devbox.CallbackRelay(0, forward)
        self.addCleanup(relay.close)
        return relay

    def test_the_callback_goes_through_the_forward_and_its_answer_comes_back(self):
        forward = Forwarder()
        relay = self.relay(forward)
        self.assertEqual(browse(relay.port), (302, SUCCESS))
        self.assertEqual(forward.targets, ["/callback?" + urlencode({"code": "the-code", "state": STATE})])
        self.assertTrue(relay.alive)
        relay.close()
        self.assertFalse(relay.alive)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", relay.port), timeout=2).close()

    def test_only_one_get_to_callback_is_passed_on(self):
        forward = Forwarder()
        relay = self.relay(forward)
        self.assertEqual(browse(relay.port, path="/favicon.ico")[0], 404)
        self.assertEqual(browse(relay.port, path="/callback/x")[0], 404)
        reply = raw_request(relay.port, b"POST /callback?code=x HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
        self.assertTrue(reply.startswith("HTTP/1.1 404"), reply)
        self.assertIn(devbox.ONLY_CALLBACK, reply)
        reply = raw_request(relay.port, b"GET //evil.example/callback HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(reply.startswith("HTTP/1.1 404"), reply)
        self.assertEqual(forward.targets, [])

    def test_a_failed_forward_tells_the_browser_and_keeps_no_location(self):
        for forward in (Forwarder(error=devbox.LinkError()), Forwarder(status=None), Forwarder(location="http://evil.example/")):
            with self.subTest(forward=forward):
                relay = self.relay(forward)
                reply = raw_request(relay.port, f"GET /callback?code=x&state=y HTTP/1.1\r\nHost: localhost:{relay.port}\r\n\r\n".encode())
                self.assertNotIn("Location:", reply)
                if forward.status is None or forward.error is not None:
                    self.assertTrue(reply.startswith("HTTP/1.1 502"), reply)
                    self.assertIn(devbox.NOT_PASSED, reply)
                else:
                    self.assertTrue(reply.startswith("HTTP/1.1 302"), reply)

    def test_only_a_browser_on_this_mac_with_a_code_and_state_is_passed_on(self):
        forward = Forwarder()
        relay = self.relay(forward)
        port = relay.port
        for host in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
            reply = raw_request(port, f"GET /callback?code=x&state=y HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            self.assertTrue(reply.startswith("HTTP/1.1 302"), (host, reply))
        for head in (
            f"GET /callback?code=x&state=y HTTP/1.1\r\nHost: evil.example:{port}\r\n\r\n",
            f"GET /callback?code=x&state=y HTTP/1.1\r\nHost: localhost:{port + 1}\r\n\r\n",
            "GET /callback?code=x&state=y HTTP/1.1\r\nHost: localhost\r\n\r\n",
            "GET /callback?code=x&state=y HTTP/1.1\r\n\r\n",
            f"GET /callback?code=x&state=y HTTP/1.1\r\nHost: localhost:{port}\r\nHost: evil.example\r\n\r\n",
            f"GET /callback?x HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n",
            f"GET /callback?code=x HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n",
            f"GET /callback?code=&state=y HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n",
        ):
            with self.subTest(head=head):
                self.assertTrue(raw_request(port, head.encode()).startswith("HTTP/1.1 404"))
        self.assertEqual(len(forward.targets), 3)

    def test_the_browser_is_sent_on_only_to_claude_and_anthropic(self):
        for location in (SUCCESS, "https://claude.ai/done", "https://console.anthropic.com/oauth/code/success"):
            self.assertEqual(devbox._location(location), location)
        for location in (
            "https://claude.ai.evil.example/done",
            "https://evil.example/claude.ai",
            "https://claude.ai@evil.example/",
            "https://evilclaude.ai/",
            "http://claude.ai/done",
            "https://[claude.ai/",
            "javascript:alert(1)",
            None,
        ):
            with self.subTest(location=location):
                self.assertIsNone(devbox._location(location))

    def test_the_relay_listens_on_loopback_only(self):
        relay = self.relay(Forwarder())
        self.assertEqual(relay.addresses[0], "127.0.0.1")
        self.assertTrue(set(relay.addresses) <= {"127.0.0.1", "::1"}, relay.addresses)

    def test_a_taken_port_is_refused(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        with self.assertRaises(devbox.RelayError):
            devbox.CallbackRelay(blocker.getsockname()[1], Forwarder())

    def test_a_taken_ipv6_loopback_port_is_refused_too(self):
        try:
            blocker = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            blocker.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            blocker.bind(("::1", 0))
            blocker.listen(1)
        except OSError:
            self.skipTest("this machine has no IPv6 loopback")
        self.addCleanup(blocker.close)
        port = blocker.getsockname()[1]
        with self.assertRaises(devbox.RelayError):
            devbox.CallbackRelay(port, Forwarder())

    def test_a_silent_browser_is_dropped(self):
        relay = self.relay(Forwarder())
        with mock.patch.object(devbox, "RELAY_HEAD_SECONDS", 0.3):
            sock = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
            try:
                sock.sendall(b"GET /callback")
                self.assertEqual(sock.recv(100), b"")
            finally:
                sock.close()


class LoginRig:
    def __init__(self, case, mode="ok", uuid=PERSONAL, **options):
        self.case = case
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.folder = self.root / "login"
        self.folder.mkdir(mode=0o700)
        self.log = self.root / "seen.json"
        self.oom = self.root / "oom_score_adj"
        self.oom.write_text("0")
        self.command = write_fake_login(self.root)
        env = {
            "FAKE_MODE": mode,
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": STATE,
            "FAKE_ACCESS": "rig-access-SECRET",
            "FAKE_REFRESH": "rig-refresh-SECRET",
            "FAKE_EXPIRES": "4102444800000",
            "FAKE_DETAILS": json.dumps(details(uuid)),
            **PLANTED,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        case.addCleanup(patcher.stop)
        settings = dict(
            command=str(self.command),
            scope=lambda: ["/usr/bin/env"],
            needs_scope=True,
            oom_path=str(self.oom),
            timeout=20,
            page_seconds=10,
            poll=0.05,
        )
        settings.update(options)
        self.login = devbox.DevboxLogin(**settings)
        self.pages = []
        self.answers = []

    def answer(self, port, url):
        self.pages.append((port, url))
        self.answers.append(devbox.replay_callback(port, "/callback?" + urlencode({"code": "the-code", "state": STATE})))

    def seen(self):
        return json.loads(self.log.read_text())


class DevboxLoginTests(unittest.TestCase):
    def test_the_login_gives_its_page_takes_the_answer_and_finishes(self):
        rig = LoginRig(self)
        rig.login(rig.folder, "ama@example.invalid", "tester", on_page=rig.answer)
        self.assertEqual(len(rig.pages), 1)
        port, url = rig.pages[0]
        self.assertEqual(devbox.signin_port(url), port)
        self.assertEqual(rig.answers, [(302, SUCCESS)])
        item = json.loads((rig.folder / ".credentials.json").read_text())
        self.assertEqual(item["claudeAiOauth"]["accessToken"], "rig-access-SECRET")
        seen = rig.seen()
        self.assertEqual(seen["argv"], ["auth", "login", "--claudeai", "--email", "ama@example.invalid"])
        self.assertEqual(seen["cwd"], str(rig.folder))
        self.assertFalse(seen["stdin_tty"])
        self.assertEqual(
            seen["env"],
            {
                "CLAUDE_CONFIG_DIR": str(rig.folder),
                "USER": "tester",
                "BROWSER": str(HOOK),
                "DISABLE_AUTOUPDATER": "1",
                "LIFELINE_SIGNIN_PAGE": str(rig.folder / devbox.PAGE_NAME),
            },
        )
        self.assertEqual(rig.oom.read_text(), "1000")
        self.assertTrue(pid_gone(seen["pid"]))

    def test_without_a_working_scope_the_signin_never_starts(self):
        rig = LoginRig(self, scope=lambda: [])
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=rig.answer)
        self.assertEqual(str(caught.exception), devbox.NO_SCOPE)
        self.assertFalse(rig.log.exists())
        self.assertEqual(rig.oom.read_text(), "0")

    def test_no_page_within_the_wait_stops_the_login(self):
        rig = LoginRig(self, mode="no_page", page_seconds=1)
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=rig.answer)
        self.assertEqual(str(caught.exception), devbox.NO_PAGE)
        self.assertEqual(rig.pages, [])
        self.assertTrue(pid_gone(rig.seen()["pid"]))

    def test_an_untrusted_page_is_never_passed_on(self):
        rig = LoginRig(self, mode="bad_page")
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=rig.answer)
        self.assertEqual(str(caught.exception), devbox.BAD_PAGE)
        self.assertEqual(rig.pages, [])
        self.assertTrue(pid_gone(rig.seen()["pid"]))

    def test_a_login_that_fails_is_reported(self):
        rig = LoginRig(self, mode="fail")
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=lambda port, url: rig.pages.append(port))
        self.assertEqual(str(caught.exception), server.LOGIN_REFUSED)

    def test_stop_ends_a_waiting_login(self):
        rig = LoginRig(self, mode="hang")
        stop = threading.Event()
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=lambda port, url: stop.set(), stop=stop)
        self.assertEqual(str(caught.exception), devbox.SIGNIN_STOPPED)
        self.assertTrue(pid_gone(rig.seen()["pid"]))

    def test_the_whole_login_has_a_time_limit(self):
        rig = LoginRig(self, mode="hang", timeout=1.5)
        started = time.monotonic()
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=lambda port, url: None)
        self.assertEqual(str(caught.exception), server.LOGIN_TIMED_OUT)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(pid_gone(rig.seen()["pid"]))

    def test_a_login_that_is_not_listening_stops(self):
        seen = []
        rig = LoginRig(self, mode="hang", listening=lambda host, port: seen.append((host, port)) or False)
        with self.assertRaises(server.LoginFailed) as caught:
            rig.login(rig.folder, None, "tester", on_page=rig.answer)
        self.assertEqual(str(caught.exception), devbox.NOT_LISTENING)
        self.assertEqual(rig.pages, [])
        self.assertEqual(seen[0][0], "127.0.0.1")
        self.assertTrue(pid_gone(rig.seen()["pid"]))

    def test_no_command_is_a_plain_refusal(self):
        login = devbox.DevboxLogin(scope=lambda: ["/usr/bin/env"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(devbox.shutil, "which", return_value=None):
            with self.assertRaises(server.LoginFailed) as caught:
                login(Path(tmp.name), None, "tester")
        self.assertEqual(str(caught.exception), server.NO_LOGIN_COMMAND)

    def test_the_claude_found_at_install_is_used_instead_of_a_path_search(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        claude = Path(tmp.name) / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n")
        claude.chmod(0o700)
        started = []

        def popen(args, **options):
            started.append(args)
            raise OSError("nothing runs here")

        login = devbox.DevboxLogin(scope=lambda: ["/usr/bin/env"], popen=popen)
        with mock.patch.dict(os.environ, {"LIFELINE_CLAUDE": str(claude)}), \
                mock.patch.object(devbox.shutil, "which", side_effect=lambda name, path=None: name if name == str(claude) else "/elsewhere/claude"):
            with self.assertRaises(server.LoginFailed):
                login(Path(tmp.name), None, "tester")
        self.assertEqual(started[0][:2], ["/usr/bin/env", str(claude)])
        with mock.patch.dict(os.environ, {"LIFELINE_CLAUDE": str(Path(tmp.name) / "gone")}):
            with self.assertRaises(server.LoginFailed) as caught:
                login(Path(tmp.name), None, "tester")
        self.assertEqual(str(caught.exception), server.NO_LOGIN_COMMAND)
        self.assertEqual(len(started), 1)

    def test_the_oom_setting_never_breaks_a_machine_without_it(self):
        devbox.oom_first("/nonexistent-folder-for-lifeline/oom_score_adj")()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "oom"
        target.write_text("0")
        devbox.oom_first(str(target))()
        self.assertEqual(target.read_text(), "1000")


class ScopeTests(unittest.TestCase):
    def test_the_scope_is_used_only_inside_a_unit_and_only_when_it_works(self):
        calls = []

        def which(name, path=None):
            return {"systemd-run": "/usr/bin/systemd-run", "true": "/usr/bin/true"}.get(name)

        def run(args, **options):
            calls.append(args)
            return mock.Mock(returncode=0)

        prefix = devbox.login_scope(env={"INVOCATION_ID": "abc", "PATH": "/usr/bin"}, which=which, run=run)
        self.assertEqual(
            prefix,
            [
                "/usr/bin/systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "-p",
                f"RuntimeMaxSec={devbox.SCOPE_SECONDS}",
                "--",
            ],
        )
        self.assertEqual(calls, [prefix + ["/usr/bin/true"]])
        self.assertEqual(devbox.login_scope(env={"PATH": "/usr/bin"}, which=which, run=run), [])
        self.assertEqual(devbox.login_scope(env={"INVOCATION_ID": "abc"}, which=lambda *a, **k: None, run=run), [])
        failing = lambda args, **options: mock.Mock(returncode=1)
        self.assertEqual(devbox.login_scope(env={"INVOCATION_ID": "abc"}, which=which, run=failing), [])

        def broken(args, **options):
            raise subprocess.TimeoutExpired(args, 20)

        self.assertEqual(devbox.login_scope(env={"INVOCATION_ID": "abc"}, which=which, run=broken), [])


class FakeDevboxLogin:
    def __init__(self, world, owner=None, failure=None):
        self.world = world
        self.owner = owner
        self.failure = failure
        self.calls = []
        self.go = threading.Event()
        self.stops = []
        self.port = 40123

    def __call__(self, folder, email=None, user=None, on_page=None, stop=None):
        self.calls.append({"folder": folder, "email": email, "user": user})
        self.stops.append(stop)
        if self.failure is not None:
            raise server.LoginFailed(self.failure)
        if on_page is not None:
            on_page(self.port, authorize_url(self.port))
        while not self.go.wait(0.02):
            if stop is not None and stop.is_set():
                raise server.LoginFailed(devbox.SIGNIN_STOPPED)
        uuid = self.owner
        oauth = self.world.issue(uuid)
        self.issued = oauth
        (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": details(uuid)}))


class ReplayRecorder:
    def __init__(self, status=302, location=SUCCESS, error=None):
        self.calls = []
        self.status = status
        self.location = location
        self.error = error

    def __call__(self, port, target):
        self.calls.append((port, target))
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return self.status, self.location


class DevboxSigninWorld:
    def __init__(self, case, owner=PERSONAL, failure=None, parked=(ALEX,), live=WORK):
        self.case = case
        self.world = DevboxWorld(case, parked=parked, live=live)
        self.login = FakeDevboxLogin(self.world, owner=owner, failure=failure)
        self.replays = ReplayRecorder()
        self.service = self.world.service(run_login=self.login, temp_root=self.world.folder / "login", callback_fn=self.replays)
        self.world.run(self.service)
        self.slot_before = self.world.slot_path.read_bytes()
        self.config_before = (self.world.home / ".claude.json").read_bytes()

    def tcp(self, method, route, body=None):
        return self.world.tcp(method, route, body)

    def job(self, job_id):
        return self.tcp("GET", f"/job/{job_id}")

    def wait_job(self, job_id, check):
        found = {}

        def ready():
            status, data = self.job(job_id)
            found["reply"] = (status, data)
            return check(data)

        self.case.assertTrue(self.world.wait_for(ready, 10), found)
        return found["reply"][1]

    def finish(self, job_id):
        self.login.go.set()
        return self.wait_job(job_id, lambda data: data.get("state") != "running")

    def assert_never_switched(self):
        self.case.assertEqual(self.world.slot_path.read_bytes(), self.slot_before)
        self.case.assertEqual((self.world.home / ".claude.json").read_bytes(), self.config_before)


class DevboxSigninRouteTests(unittest.TestCase):
    def test_add_gives_the_devbox_its_own_signin_and_reports_the_page(self):
        rig = DevboxSigninWorld(self)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL, "email": "ama@example.invalid"})
        self.assertEqual(status, 202)
        job_id = reply["job"]
        data = rig.wait_job(job_id, lambda data: data.get("url"))
        self.assertEqual((data["state"], data["kind"], data["uuid"], data["port"]), ("running", "add", PERSONAL, 40123))
        self.assertEqual(data["url"], authorize_url(40123))
        done = rig.finish(job_id)
        self.assertEqual((done["state"], done["result"]), ("done", "The other computer has its own sign-in for Personal Account now."))
        self.assertEqual(rig.world.store.get(PERSONAL), rig.login.issued)
        self.assertEqual(rig.login.calls[0]["email"], "ama@example.invalid")
        self.assertEqual(rig.login.calls[0]["user"], "tester")
        self.assertEqual(rig.login.calls[0]["folder"].parent, rig.world.folder / "login")
        self.assertEqual(list((rig.world.folder / "login").iterdir()), [])
        saved = rig.world.registry.by_uuid(PERSONAL)
        self.assertIsNotNone(saved.signed_in_at)
        rig.assert_never_switched()
        rig.world.assert_clean()

    def test_add_takes_over_the_live_account_and_refuses_one_it_holds_and_bad_requests(self):
        rig = DevboxSigninWorld(self)
        cases = [
            (
                {"uuid": ALEX},
                409,
                "The other computer already has its own sign-in for Alex's Account, so sign in again with lifeline computer resign 1.",
            ),
            ({"uuid": "55555555-eeee-4eee-8eee-000000000005"}, 404, devbox.UNKNOWN_HERE),
            ({}, 400, devbox.BAD_DEVBOX_ADD),
            ({"uuid": PERSONAL, "name": "X"}, 400, devbox.BAD_DEVBOX_ADD),
            ({"uuid": PERSONAL, "email": "--console"}, 400, devbox.BAD_DEVBOX_ADD),
            ({"uuid": 7}, 400, devbox.BAD_DEVBOX_ADD),
        ]
        for body, status, sentence in cases:
            with self.subTest(body=body):
                self.assertEqual(rig.tcp("POST", "/add", body), (status, {"error": sentence}))
        self.assertEqual(rig.tcp("POST", f"/resign/{PERSONAL}", {"email": "x y"}), (400, {"error": server.BAD_RESIGN}))
        self.assertEqual(rig.tcp("POST", "/resign/nobody", {}), (404, {"error": devbox.UNKNOWN_HERE}))
        status, reply = rig.tcp("POST", "/add", {"uuid": WORK})
        self.assertEqual(status, 202, reply)
        done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual((done["state"], done["result"], done["url"]), ("done", "The other computer already uses Work Account, so Lifeline kept the sign-in the other computer has.", None))
        self.assertEqual(rig.login.calls, [])
        rig.assert_never_switched()

    def test_resign_replaces_a_parked_signin(self):
        rig = DevboxSigninWorld(self, owner=ALEX)
        old = rig.world.store.get(ALEX)
        status, reply = rig.tcp("POST", f"/resign/{ALEX}", {})
        self.assertEqual(status, 202)
        done = rig.finish(reply["job"])
        self.assertEqual((done["state"], done["result"]), ("done", "The other computer has a new sign-in for Alex's Account."))
        self.assertNotEqual(rig.world.store.get(ALEX), old)
        self.assertEqual(rig.world.store.get(ALEX), rig.login.issued)
        rig.assert_never_switched()

    def test_resign_of_the_live_account_renews_its_slot_and_keeps_the_rest(self):
        rig = DevboxSigninWorld(self, owner=WORK)
        status, reply = rig.tcp("POST", f"/resign/{WORK}", {})
        done = rig.finish(reply["job"])
        self.assertEqual(done["state"], "done", done)
        item = json.loads(rig.world.slot_path.read_text())
        self.assertEqual(item["claudeAiOauth"], rig.login.issued)
        self.assertIn("mcpOAuth", item)
        self.assertEqual((rig.world.home / ".claude.json").read_bytes(), rig.config_before)
        self.assertIsNone(rig.world.store.get(WORK))

    def test_a_signin_for_another_account_changes_nothing(self):
        rig = DevboxSigninWorld(self, owner=ORG)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        done = rig.finish(reply["job"])
        self.assertEqual((done["state"], done["result"]), ("failed", "This sign-in belongs to a different account, so nothing was changed."))
        self.assertIsNone(rig.world.store.get(PERSONAL))
        self.assertIsNone(rig.world.store.get(ORG))
        self.assertEqual(list((rig.world.folder / "login").iterdir()), [])
        rig.assert_never_switched()

    def test_a_devbox_sign_in_that_times_out_says_so_in_one_sentence(self):
        rig = DevboxSigninWorld(self, failure=server.LOGIN_TIMED_OUT)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual(done["result"], "Lifeline stopped waiting for the other computer's sign-in after 10 minutes, so nothing was added to the other computer.")
        status, reply = rig.tcp("POST", f"/resign/{ALEX}", {})
        done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual(done["result"], "Lifeline stopped waiting for the other computer's sign-in after 10 minutes, so nothing changed on the other computer.")

    def test_the_mac_logs_a_devbox_time_out_in_one_sentence(self):
        rig = MacProxyRig(self, failure=server.LOGIN_TIMED_OUT)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        job = rig.finished(reply["job"])
        sentence = "Lifeline stopped waiting for the other computer's sign-in after 10 minutes, so nothing was added to the other computer."
        self.assertEqual((job["state"], job["result"]), ("failed", sentence))
        self.assertIn("WARNING " + sentence, rig.mac.collector.lines)
        self.assertFalse(any("did not finish" in line for line in rig.mac.collector.lines))

    def test_a_failed_login_gives_its_reason(self):
        rig = DevboxSigninWorld(self, failure=devbox.NO_PAGE)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual(done["state"], "failed")
        self.assertEqual(
            done["result"],
            "Claude Code on the other computer did not give a sign-in page within a minute, so Lifeline stopped the sign-in "
            "and nothing was added to the other computer.",
        )
        self.assertIsNone(done["url"])

    def test_adopt_takes_over_the_live_signin_in_place(self):
        rig = DevboxSigninWorld(self)
        status, reply = rig.tcp("POST", "/adopt", {})
        self.assertEqual(status, 202)
        done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual(
            (done["state"], done["result"]),
            ("done", "Lifeline on the other computer took over the sign-in its Claude Code holds for Work Account."),
        )
        rig.assert_never_switched()
        self.assertIsNone(rig.world.store.get(WORK))
        entry = switching.read_record_entry(switching.record_path_for(rig.world.folder / "lifeline.lock"))
        self.assertEqual(entry["uuid"], WORK)
        self.assertEqual(rig.login.calls, [])

    def test_adopt_takes_no_options_and_needs_an_account_the_mac_added(self):
        rig = DevboxSigninWorld(self)
        self.assertEqual(rig.tcp("POST", "/adopt", {"name": "X"}), (400, {"error": devbox.BAD_DEVBOX_ADOPT}))
        rig.world.registry.save([acc for acc in rig.world.registry.load() if acc.uuid != WORK])
        self.assertEqual(rig.tcp("POST", "/adopt", {}), (409, {"error": devbox.LIVE_UNKNOWN}))
        rig.assert_never_switched()

    def test_one_signin_at_a_time_and_stop_ends_it(self):
        rig = DevboxSigninWorld(self)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        job_id = reply["job"]
        rig.wait_job(job_id, lambda data: data.get("url"))
        for route, body in (("/add", {"uuid": ORG}), (f"/resign/{ALEX}", {}), ("/adopt", {})):
            self.assertEqual(rig.tcp("POST", route, body), (409, {"error": devbox.DEVBOX_BUSY, "job": job_id}), route)
        self.assertEqual(rig.tcp("POST", "/stop/nothing-like-it", {}), (404, {"error": devbox.NO_SUCH_SIGNIN}))
        self.assertEqual(rig.tcp("POST", f"/stop/{job_id}", {}), (200, {"stopping": True}))
        done = rig.wait_job(job_id, lambda data: data.get("state") != "running")
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["result"], "The sign-in was stopped before it finished, so nothing was added to the other computer.")
        self.assertIsNone(rig.world.store.get(PERSONAL))
        self.assertEqual(rig.tcp("POST", f"/stop/{job_id}", {}), (200, {"stopping": False}))
        self.assertEqual(rig.tcp("GET", "/job/unknown"), (404, {"error": devbox.NO_SUCH_SIGNIN}))

    def test_stopping_the_service_stops_a_waiting_signin(self):
        rig = DevboxSigninWorld(self)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        rig.wait_job(reply["job"], lambda data: data.get("url"))
        rig.service.stop()
        self.assertTrue(rig.login.stops[0].is_set())

    def test_the_new_routes_answer_on_tcp_and_the_local_socket_points_to_the_mac(self):
        rig = DevboxSigninWorld(self)
        for method, route, body in (
            ("POST", "/add", {"uuid": PERSONAL}),
            ("POST", f"/resign/{ALEX}", {}),
            ("POST", "/adopt", {}),
            ("GET", "/job/abc", None),
            ("POST", "/stop/abc", {}),
            ("POST", "/stop", {}),
            ("POST", "/callback/abc", {"target": "/callback?code=x"}),
        ):
            status, reply = rig.tcp(method, route, body)
            self.assertNotIn(status, (401, 403), (route, reply))
        rig.service.stop_running()
        self.assertTrue(rig.world.wait_for(lambda: not rig.service.jobs_running()))
        rig.login.calls.clear()
        for route, body in (("/add", {"uuid": PERSONAL}), (f"/resign/{ALEX}", {}), ("/adopt", {})):
            self.assertEqual(rig.world.local("POST", route, body), (403, {"error": FROM_MAC}), route)
        for route in ("/switch", "/undo"):
            self.assertEqual(rig.world.local("POST", route, {}), (403, {"error": SWITCH_REFUSED}))
            self.assertEqual(rig.tcp("POST", route, {})[0], 404)
        self.assertEqual(rig.login.calls, [])
        rig.assert_never_switched()

    def test_switch_and_undo_stay_refused_in_the_service(self):
        rig = DevboxSigninWorld(self)
        for call in (lambda: rig.service.switch_to(ORG), lambda: rig.service.undo()):
            with self.assertRaises(server.Refusal) as caught:
                call()
            self.assertEqual((caught.exception.status, caught.exception.sentence), (403, SWITCH_REFUSED))
        rig.assert_never_switched()

    def test_the_link_drives_a_signin_from_the_mac(self):
        rig = DevboxSigninWorld(self)
        link = rig.world.link()
        status, first = link.start_signin("/add", {"uuid": PERSONAL})
        self.assertEqual(status, 202)
        job_id = first["job"]
        status, data = link.start_signin("/add", {"uuid": PERSONAL})
        self.assertEqual((status, data), (409, {"error": devbox.DEVBOX_BUSY, "job": job_id}))
        self.assertTrue(rig.world.wait_for(lambda: link.job(job_id)[1].get("url")))
        self.assertEqual(link.stop_signin(job_id), (200, {"stopping": True}))
        self.assertTrue(rig.world.wait_for(lambda: link.job(job_id)[1].get("state") == "failed"))


PAST_MS = 1_600_000_000_000
FAR_MS = 4_102_444_800_000


def set_live(rig, expires, record=None):
    world = rig.world
    item = json.loads(world.slot_path.read_text())
    item["claudeAiOauth"]["expiresAt"] = expires
    world.slot.write(item)
    path = switching.record_path_for(world.folder / "lifeline.lock")
    if record == "stale":
        switching.record_confirmed(path, WORK, {"claudeAiOauth": {"refreshToken": "an-older-renewal-key"}}, world.clock.now)
    elif record == "current":
        switching.record_confirmed(path, WORK, item, world.clock.now)
    rig.slot_before = world.slot_path.read_bytes()
    return path


class LiveIdentityTests(unittest.TestCase):
    def test_a_signin_never_starts_while_the_devbox_cannot_tell_its_live_account(self):
        for route, body in (("/add", {"uuid": PERSONAL}), (f"/resign/{ALEX}", {}), ("/adopt", {})):
            with self.subTest(route):
                rig = DevboxSigninWorld(self)
                set_live(rig, PAST_MS, "stale")
                status, reply = rig.tcp("POST", route, body)
                self.assertEqual((status, reply), (409, {"error": devbox.LIVE_STALE}))
                self.assertEqual(rig.login.calls, [])
                self.assertEqual(rig.service.jobs_running(), [])
                rig.assert_never_switched()

    def test_an_add_with_an_expired_key_and_a_current_record_goes_ahead(self):
        rig = DevboxSigninWorld(self)
        set_live(rig, PAST_MS, "current")
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        self.assertEqual(status, 202)
        done = rig.finish(reply["job"])
        self.assertEqual((done["state"], done["result"]), ("done", "The other computer has its own sign-in for Personal Account now."))
        self.assertEqual(rig.world.store.get(PERSONAL), rig.login.issued)

    def test_a_fresh_key_anthropic_does_not_confirm_stops_the_job_before_the_login(self):
        rig = DevboxSigninWorld(self)
        set_live(rig, FAR_MS, "stale")
        rig.world.owners.clear()
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        self.assertEqual(status, 202)
        done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual((done["state"], done["result"]), ("failed", devbox.LIVE_UNCHECKED))
        self.assertNotIn("internet", done["result"])
        self.assertEqual(rig.login.calls, [])
        rig.assert_never_switched()

    def test_a_fresh_key_the_profile_confirms_is_recorded_before_the_login(self):
        rig = DevboxSigninWorld(self)
        path = set_live(rig, FAR_MS, "stale")
        seen = {}
        real = rig.login.__call__

        def login(folder, email=None, user=None, on_page=None, stop=None):
            seen["record"] = switching.read_record_entry(path)
            return real(folder, email, user, on_page, stop)

        rig.login.__call__ = login
        rig.service.parts.run_login = login
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        done = rig.finish(reply["job"])
        self.assertEqual(done["state"], "done", done)
        live = json.loads(rig.world.slot_path.read_text())["claudeAiOauth"]
        self.assertEqual(seen["record"]["uuid"], WORK)
        self.assertEqual(seen["record"]["refresh_sha256"], switching.fingerprint(live))
        self.assertFalse(seen["record"]["proven"])

    def test_the_account_route_says_whether_the_live_account_was_taken_over(self):
        rig = DevboxSigninWorld(self)
        accounts = rig.world.registry.load()
        rig.world.registry.save([replace(acc, oauth_account={}) if acc.uuid == WORK else acc for acc in accounts])
        rig.service.tick()
        status, reply = rig.tcp("GET", "/account")
        self.assertEqual((status, reply["account_uuid"], reply["live_registered"]), (200, WORK, False))
        link = rig.world.link()
        self.assertTrue(link.reachable())
        self.assertIs(link.live_registered(), False)
        status, reply = rig.tcp("POST", "/adopt", {})
        rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        rig.service.tick()
        self.assertIs(rig.tcp("GET", "/account")[1]["live_registered"], True)
        self.assertTrue(link.reachable())
        self.assertIs(link.live_registered(), True)
        rig.world.slot_path.write_text(json.dumps({"mcpOAuth": {}}))
        (rig.world.home / ".claude.json").write_text(json.dumps({"numStartups": 1}))
        rig.service.tick()
        self.assertIsNone(rig.tcp("GET", "/account")[1]["live_registered"])


class CallbackRouteTests(unittest.TestCase):
    def waiting(self):
        rig = DevboxSigninWorld(self)
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        job_id = reply["job"]
        rig.wait_job(job_id, lambda data: data.get("url"))
        return rig, job_id

    def test_the_answer_reaches_only_that_job_s_port_once(self):
        rig, job_id = self.waiting()
        target = "/callback?" + urlencode({"code": "the-code", "state": STATE})
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (200, {"status": 302, "location": SUCCESS}))
        self.assertEqual(rig.replays.calls, [(40123, target)])
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (409, {"error": devbox.ANSWERED}))
        self.assertEqual(len(rig.replays.calls), 1)
        done = rig.finish(job_id)
        self.assertEqual(done["state"], "done")
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (409, {"error": devbox.NOT_WAITING}))
        self.assertEqual(len(rig.replays.calls), 1)

    def test_a_wrong_callback_never_touches_the_login(self):
        rig, job_id = self.waiting()
        target = "/callback?code=x&state=y"
        self.assertEqual(rig.tcp("POST", "/callback/someone-else", {"target": target}), (404, {"error": devbox.NO_SUCH_SIGNIN}))
        for body in (
            {"target": "/elsewhere?code=x"},
            {"target": "//evil.example/callback"},
            {"target": "http://127.0.0.1:22/callback"},
            {"target": "/callback?code=x y"},
            {"target": "/callback#x"},
            {"target": "/callback?code=x"},
            {"target": "/callback?state=y"},
            {"target": 7},
            {"target": "/callback", "port": 22},
            {},
        ):
            self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", body), (400, {"error": devbox.BAD_CALLBACK}), body)
        self.assertEqual(rig.replays.calls, [])
        self.assertEqual(rig.service.job_info(job_id)["state"], "running")

    def test_an_answer_before_the_page_is_refused(self):
        rig = DevboxSigninWorld(self)
        rig.login.port = None
        original = rig.login.__call__

        def no_page(folder, email=None, user=None, on_page=None, stop=None):
            return original(folder, email, user, None, stop)

        rig.service.parts.run_login = no_page
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        job_id = reply["job"]
        self.assertTrue(rig.world.wait_for(lambda: rig.login.calls))
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": "/callback?code=x&state=y"}), (409, {"error": devbox.NOT_WAITING}))
        self.assertEqual(rig.replays.calls, [])
        rig.finish(job_id)

    def test_a_replay_that_fails_can_be_tried_again(self):
        rig, job_id = self.waiting()
        rig.replays.error = OSError("refused")
        target = "/callback?code=x&state=y"
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (502, {"error": devbox.NO_ANSWER}))
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (200, {"status": 302, "location": SUCCESS}))

    def test_a_refused_answer_leaves_room_for_the_real_one(self):
        rig, job_id = self.waiting()
        target = "/callback?code=x&state=y"
        rig.replays.status = 400
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target})[1]["status"], 400)
        rig.replays.status = 302
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (200, {"status": 302, "location": SUCCESS}))
        self.assertEqual(rig.tcp("POST", f"/callback/{job_id}", {"target": target}), (409, {"error": devbox.ANSWERED}))
        rig.finish(job_id)

    def test_the_running_signin_can_be_stopped_without_its_id(self):
        rig = DevboxSigninWorld(self)
        self.assertEqual(rig.tcp("POST", "/stop", {}), (200, {"stopping": False, "job": None}))
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL})
        job_id = reply["job"]
        rig.wait_job(job_id, lambda data: data.get("url"))
        self.assertEqual(rig.tcp("POST", "/stop", {}), (200, {"stopping": True, "job": job_id}))
        done = rig.wait_job(job_id, lambda data: data.get("state") != "running")
        self.assertEqual(done["state"], "failed")


class DevboxLoginWiringTests(unittest.TestCase):
    def test_the_devbox_parts_wire_the_real_login_and_the_linux_login_folder(self):
        world = DevboxWorld(self)
        with mock.patch.object(devbox.subprocess, "run", side_effect=AssertionError("no command may run")):
            parts = devbox.devbox_parts(system="linux")
        self.assertIsInstance(parts.run_login, devbox.DevboxLogin)
        self.assertIs(parts.callback_fn, devbox.replay_callback)
        self.assertEqual(parts.temp_root, world.home / ".local" / "share" / "com.example.lifeline" / "login")
        self.assertIsNone(parts.runner)

    def test_the_real_login_and_the_service_work_together(self):
        world = DevboxWorld(self, parked=(ALEX,))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        command = write_fake_login(tmp.name)
        oauth = world.issue(PERSONAL)
        env = {
            "FAKE_MODE": "ok",
            "FAKE_LOG": str(Path(tmp.name) / "seen.json"),
            "FAKE_STATE": STATE,
            "FAKE_ACCESS": oauth["accessToken"],
            "FAKE_REFRESH": oauth["refreshToken"],
            "FAKE_EXPIRES": str(oauth["expiresAt"]),
            "FAKE_DETAILS": json.dumps(details(PERSONAL)),
        }
        with mock.patch.dict(os.environ, env):
            login = devbox.DevboxLogin(command=str(command), scope=lambda: ["/usr/bin/env"], oom_path=str(Path(tmp.name) / "oom"), poll=0.05)
            service = world.service(run_login=login, temp_root=world.folder / "login", callback_fn=devbox.replay_callback)
            world.run(service)
            before = world.slot_path.read_bytes()
            status, reply = world.tcp("POST", "/add", {"uuid": PERSONAL})
            self.assertEqual(status, 202)
            job_id = reply["job"]
            self.assertTrue(world.wait_for(lambda: world.tcp("GET", f"/job/{job_id}")[1].get("url"), 15))
            target = "/callback?" + urlencode({"code": "the-code", "state": STATE})
            self.assertEqual(world.tcp("POST", f"/callback/{job_id}", {"target": target}), (200, {"status": 302, "location": SUCCESS}))
            self.assertTrue(world.wait_for(lambda: world.tcp("GET", f"/job/{job_id}")[1].get("state") != "running", 15))
        done = world.tcp("GET", f"/job/{job_id}")[1]
        self.assertEqual(done["state"], "done", done)
        stored = world.store.get(PERSONAL)
        self.assertEqual((stored["accessToken"], stored["refreshToken"]), (oauth["accessToken"], oauth["refreshToken"]))
        self.assertEqual(world.slot_path.read_bytes(), before)
        self.assertEqual(list((world.folder / "login").iterdir()), [])



class PinnedStatuslineTests(unittest.TestCase):
    def layout(self, link_to="abc123"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = Path(tmp.name).resolve() / "app"
        for name in ("abc123", "old999"):
            (app / name / "core" / "lifeline").mkdir(parents=True)
            (app / name / "bin").mkdir()
            (app / name / "bin" / "lifeline-statusline").write_text("x")
            (app / name / "core" / "lifeline" / "server.py").write_text("x")
        if link_to is not None:
            (app / "current").symlink_to(link_to)
        return app

    def test_a_pinned_copy_names_its_stable_current_path(self):
        app = self.layout()
        source = app / "abc123" / "core" / "lifeline" / "server.py"
        self.assertEqual(server.statusline_command(source), str(app / "current" / "bin" / "lifeline-statusline"))
        self.assertEqual(server.statusline_script(source), str(app / "abc123" / "bin" / "lifeline-statusline"))
        old = app / "old999" / "core" / "lifeline" / "server.py"
        self.assertEqual(server.statusline_command(old), str(app / "old999" / "bin" / "lifeline-statusline"))
        bare = self.layout(link_to=None)
        source = bare / "abc123" / "core" / "lifeline" / "server.py"
        self.assertEqual(server.statusline_command(source), str(bare / "abc123" / "bin" / "lifeline-statusline"))

    def test_the_repository_copy_names_its_own_script(self):
        self.assertEqual(server.statusline_command(), server.statusline_script())
        self.assertEqual(server.statusline_command(), str(REPO / "bin" / "lifeline-statusline"))

    def test_the_entry_written_is_the_stable_path_and_counts_as_lifeline_s(self):
        app = self.layout()
        source = app / "abc123" / "core" / "lifeline" / "server.py"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text('{\n  "model": "x"\n}\n')
        with mock.patch.object(server, "statusline_script", lambda source_=None: str(app / "abc123" / "bin" / "lifeline-statusline")), mock.patch.object(
            server, "statusline_command", lambda source_=None: str(app / "current" / "bin" / "lifeline-statusline")
        ):
            server.change_statusline(home, True, server._utc_now())
            written = json.loads((home / ".claude" / "settings.json").read_text())
            self.assertEqual(
                written["statusLine"], {"type": "command", "command": str(app / "current" / "bin" / "lifeline-statusline"), "refreshInterval": 5}
            )
            self.assertEqual(server.statusline_state(home), {"on": True, "ours": True, "other_present": False})
            server.change_statusline(home, False, server._utc_now())
        self.assertEqual(json.loads((home / ".claude" / "settings.json").read_text()), {"model": "x"})



class MacProxyRig:
    def __init__(self, case, owner=PERSONAL, link="real", authorize=True, failure=None, live=WORK):
        from tests.test_server import World

        self.case = case
        self.dev = DevboxSigninWorld(case, owner=owner, failure=failure, live=live)
        self.mac = World(case)
        self.relays = []
        self.opened = []
        self.authorize = authorize
        if link == "real":
            link = devbox.HttpLink(("127.0.0.1", self.dev.world.port()), key=TEST_KEY)
        self.reopened = []
        self.service = self.mac.service(link=link, relay_fn=self.relay, open_page=self.open_page, open_browser=self.open_browser)
        self.mac.run(self.service)

    def open_browser(self, url):
        self.reopened.append(url)
        return "Google Chrome", True

    def relay(self, port, forward):
        found = MacRelay(port, forward)
        self.relays.append(found)
        return found

    def open_page(self, url):
        self.opened.append((url, [(relay.port, relay.open) for relay in self.relays]))
        if self.authorize:
            self.dev.login.go.set()
        return True

    def job(self, job_id):
        jobs = self.mac.call("GET", "/state")[1]["jobs"]
        return next(job for job in jobs if job["id"] == job_id)

    def finished(self, job_id):
        self.case.assertTrue(self.mac.wait_for(lambda: self.job(job_id)["state"] != "running", 15))
        return self.job(job_id)


class MacRelay:
    def __init__(self, port, forward):
        self.port = port
        self.forward = forward
        self.open = True

    def close(self):
        self.open = False


class MacProxyTests(unittest.TestCase):
    def test_the_mac_service_adds_a_devbox_signin_for_the_app(self):
        rig = MacProxyRig(self)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        self.assertEqual(status, 202, reply)
        job = rig.finished(reply["job"])
        self.assertEqual((job["kind"], job["uuid"], job["state"]), ("devbox-add", PERSONAL, "done"))
        self.assertEqual(job["result"], "The other computer has its own sign-in for Personal Account now.")
        self.assertEqual(rig.opened, [(authorize_url(40123), [(40123, True)])])
        self.assertFalse(rig.relays[0].open)
        self.assertEqual(rig.dev.login.calls[0]["email"], "33333333@example.invalid")
        self.assertEqual(rig.dev.world.store.get(PERSONAL), rig.dev.login.issued)
        self.assertEqual(job["url"], authorize_url(40123))
        self.assertEqual(job["browser"], "Google Chrome")
        rig.dev.assert_never_switched()
        self.assertEqual(rig.mac.live_owner(), ALEX)
        rig.mac.assert_clean()

    def test_by_number_with_an_email_resign_and_adopt(self):
        rig = MacProxyRig(self, owner=ALEX)
        status, reply = rig.mac.call("POST", f"/devbox/resign/{ALEX}", {"email": "ama@example.invalid"})
        self.assertEqual(status, 202, reply)
        job = rig.finished(reply["job"])
        self.assertEqual((job["kind"], job["state"], job["result"]), ("devbox-resign", "done", "The other computer has a new sign-in for Alex's Account."))
        organization = rig.mac.registry.by_uuid(ALEX).organization
        self.assertTrue(organization.endswith("'s Organization"))
        self.assertEqual(job["guidance"], "If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize.")
        self.assertEqual(rig.dev.login.calls[0]["email"], "ama@example.invalid")
        status, reply = rig.mac.call("POST", "/devbox/adopt", {})
        job = rig.finished(reply["job"])
        self.assertIsNone(job["guidance"])
        self.assertEqual(
            (job["kind"], job["state"], job["result"]),
            ("devbox-adopt", "done", "Lifeline on the other computer took over the sign-in its Claude Code holds for Work Account."),
        )
        self.assertEqual(len(rig.opened), 1)
        rig.dev.login.owner = ORG
        rig.dev.login.go.clear()
        status, reply = rig.mac.call("POST", "/devbox/add", {"number": 4})
        job = rig.finished(reply["job"])
        self.assertEqual((job["uuid"], job["state"]), (ORG, "done"))
        rig.dev.assert_never_switched()

    def test_the_devbox_refusal_and_a_failed_signin_reach_the_job(self):
        rig = MacProxyRig(self, owner=ORG)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": WORK})
        job = rig.finished(reply["job"])
        self.assertEqual((job["state"], job["result"]), ("done", "The other computer already uses Work Account, so Lifeline kept the sign-in the other computer has."))
        self.assertEqual((rig.relays, rig.opened), ([], []))
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        job = rig.finished(reply["job"])
        self.assertEqual((job["state"], job["result"]), ("failed", "This sign-in belongs to a different account, so nothing was changed."))
        self.assertFalse(rig.relays[0].open)

    def test_bad_requests_are_refused_before_anything_starts(self):
        rig = MacProxyRig(self)
        cases = [
            ("/devbox/add", {}, 400, server.BAD_DEVBOX_ADD),
            ("/devbox/add", {"uuid": PERSONAL, "number": 3}, 400, server.BAD_DEVBOX_ADD),
            ("/devbox/add", {"uuid": PERSONAL, "name": "X"}, 400, server.BAD_DEVBOX_ADD),
            ("/devbox/add", {"uuid": PERSONAL, "email": "--console"}, 400, server.BAD_DEVBOX_ADD),
            ("/devbox/add", {"number": True}, 400, server.BAD_DEVBOX_ADD),
            ("/devbox/add", {"uuid": "nobody"}, 404, server.UNKNOWN_ACCOUNT),
            ("/devbox/add", {"number": 7}, 404, server.UNKNOWN_ACCOUNT),
            (f"/devbox/resign/{PERSONAL}", {"name": "X"}, 400, server.BAD_RESIGN),
            ("/devbox/resign/nobody", {}, 404, server.UNKNOWN_ACCOUNT),
            ("/devbox/adopt", {"name": "X"}, 400, devbox.BAD_DEVBOX_ADOPT),
            ("/devbox/switch", {}, 404, server.NOT_FOUND),
            ("/devbox/resign", {}, 404, server.NOT_FOUND),
            (f"/devbox/add/{PERSONAL}", {}, 404, server.NOT_FOUND),
            (f"/history/{PERSONAL}/x", None, 404, server.NOT_FOUND),
        ]
        for route, body, status, sentence in cases:
            with self.subTest(route=route, body=body):
                method = "GET" if route.startswith("/history") else "POST"
                self.assertEqual(rig.mac.call(method, route, body), (status, {"error": sentence}))
        self.assertEqual(rig.dev.login.calls, [])
        self.assertEqual(rig.opened, [])

    def test_one_devbox_signin_at_a_time_from_the_mac(self):
        rig = MacProxyRig(self, authorize=False)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        self.assertEqual(status, 202)
        self.assertTrue(rig.mac.wait_for(lambda: rig.opened, 10))
        self.assertEqual(rig.mac.call("POST", "/devbox/adopt", {}), (409, {"error": server.DEVBOX_PROXY_BUSY}))
        rig.dev.login.go.set()
        job = rig.finished(reply["job"])
        self.assertEqual(job["state"], "done")

    def test_without_a_devbox_link_nothing_starts(self):
        rig = MacProxyRig(self, link=server.NullLink())
        self.assertEqual(rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL}), (409, {"error": devbox.NO_DEVBOX_LINK}))
        wrong = MacProxyRig(self, link=devbox.HttpLink(("127.0.0.1", 9)))
        for route in ("/devbox/add", "/devbox/adopt"):
            body = {"uuid": PERSONAL} if route.endswith("add") else {}
            status, reply = wrong.mac.call("POST", route, body)
            self.assertEqual(status, 202, reply)
            job = wrong.finished(reply["job"])
            self.assertEqual((job["state"], job["result"]), ("failed", devbox.DEVBOX_SILENT))
        self.assertEqual(wrong.opened, [])

    def test_stopping_the_mac_service_stops_the_devbox_signin(self):
        rig = MacProxyRig(self, authorize=False)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        self.assertTrue(rig.mac.wait_for(lambda: rig.opened, 10))
        rig.service.stop()
        self.assertFalse(rig.relays[0].open)
        self.assertTrue(rig.dev.world.wait_for(lambda: not rig.dev.service.jobs_running(), 10))
        self.assertIsNone(rig.dev.world.store.get(PERSONAL))

    def test_cancelling_a_devbox_signin_from_the_mac_stops_it_on_the_devbox(self):
        rig = MacProxyRig(self, authorize=False)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        self.assertEqual(status, 202)
        self.assertTrue(rig.mac.wait_for(lambda: rig.opened, 10))
        self.assertEqual(rig.mac.call("POST", f"/jobs/{reply['job']}/cancel", {}), (200, {"cancelled": True}))
        job = rig.finished(reply["job"])
        self.assertEqual((job["state"], job["result"]), ("cancelled", server.JOB_CANCELLED))
        self.assertTrue(rig.dev.world.wait_for(lambda: not rig.dev.service.jobs_running(), 10))
        self.assertIsNone(rig.dev.world.store.get(PERSONAL))
        self.assertFalse(rig.relays[0].open)
        self.assertEqual(rig.mac.call("POST", f"/jobs/{reply['job']}/cancel", {}), (409, {"error": server.JOB_ENDED}))
        self.assertTrue(any("You cancelled adding an account on the other computer" in line for line in rig.mac.collector.lines))
        rig.dev.assert_never_switched()
        self.assertEqual(rig.mac.live_owner(), ALEX)
        rig.mac.assert_clean()

    def test_a_cancelled_second_step_says_what_the_first_step_left(self):
        rig = MacProxyRig(self, authorize=False)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL, "step": [2, 2]})
        self.assertEqual(status, 202, reply)
        self.assertTrue(rig.mac.wait_for(lambda: rig.opened, 10))
        self.assertEqual(rig.mac.call("POST", f"/jobs/{reply['job']}/cancel", {}), (200, {"cancelled": True}))
        job = rig.finished(reply["job"])
        self.assertEqual(
            (job["state"], job["result"]),
            ("cancelled", "Personal Account is on this Mac. You cancelled the other computer step, so the other computer has no sign-in for it yet."),
        )
        self.assertNotIn("Nothing changed", job["result"])
        self.assertTrue(rig.dev.world.wait_for(lambda: not rig.dev.service.jobs_running(), 10))
        rig.mac.assert_clean()

    def test_the_second_step_sentence_for_each_kind_and_ending(self):
        name = "Work Account"
        cancelled = {
            "devbox-add": "Work Account is on this Mac. You cancelled the other computer step, so the other computer has no sign-in for it yet.",
            "devbox-adopt": "Work Account is on this Mac. You cancelled the other computer step, so Lifeline does not manage the other computer's own sign-in for it yet.",
            "devbox-resign": "Work Account is signed in again on this Mac. You cancelled the other computer step, so the other computer keeps the sign-in it had.",
        }
        for kind, sentence in cancelled.items():
            with self.subTest(kind=kind):
                self.assertEqual(server.second_step_result(kind, name, "cancelled", server.JOB_CANCELLED), sentence)
                self.assertEqual(
                    server.second_step_result(kind, name, "cancelled", server.CANCEL_UNSURE),
                    sentence.split(" You cancelled")[0]
                    + " You cancelled the other computer step. If it started on the other computer, Lifeline stops it there as soon as the other computer answers.",
                )
        self.assertEqual(
            server.second_step_result("devbox-add", name, "failed", server.DEVBOX_PROXY_STOPPED),
            "Work Account is on this Mac. Lifeline stopped, so it cancelled the other computer step. The other computer has no sign-in for it yet.",
        )
        self.assertEqual(
            server.second_step_result("devbox-adopt", name, "failed", server.STOP_UNSURE),
            "Work Account is on this Mac. Lifeline stopped before the other computer answered. If the other computer step started there, the other computer ends it on its own within 10 minutes.",
        )
        self.assertIsNone(server.second_step_result("devbox-add", name, "failed", "The other computer refused the sign-in."))
        self.assertIsNone(server.second_step_result("devbox-add", name, "done", "Lifeline added it."))

    def test_the_mac_reopens_a_devbox_signin_page(self):
        rig = MacProxyRig(self, authorize=False)
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": PERSONAL})
        self.assertTrue(rig.mac.wait_for(lambda: rig.job(reply["job"])["url"], 10))
        self.assertEqual(rig.mac.call("POST", f"/jobs/{reply['job']}/open", {}), (200, {"browser": "Google Chrome", "opened": True}))
        self.assertEqual(rig.reopened, [authorize_url(40123)])
        self.assertTrue(rig.relays[0].open)
        rig.dev.login.go.set()
        job = rig.finished(reply["job"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(rig.mac.call("POST", f"/jobs/{reply['job']}/open", {}), (409, {"error": server.PAGE_ENDED}))
        rig.mac.assert_clean()

    def test_the_devbox_answers_none_of_the_proxy_routes(self):
        rig = MacProxyRig(self)
        for route in ("/devbox/add", "/devbox/adopt", f"/devbox/resign/{ALEX}"):
            self.assertEqual(rig.dev.world.local("POST", route, {})[0], 404)
            self.assertEqual(rig.dev.tcp("POST", route, {})[0], 404)

    def test_the_production_parts_wire_the_relay_and_the_browser(self):
        from tests.test_server import World

        World(self)
        with mock.patch.object(server.sys, "platform", "darwin"):
            parts = server.production_parts()
        self.assertIs(parts.open_page, devbox.open_page)
        self.assertEqual(devbox.OPEN_COMMAND, "/usr/bin/open")
        forward = Forwarder()
        with mock.patch.object(devbox, "CallbackRelay") as made:
            parts.relay_fn(40123, forward)
        made.assert_called_once_with(40123, forward)
        calls = []

        def fake_run(args, **options):
            calls.append((args, options.get("stdin")))
            return mock.Mock(returncode=0)

        with mock.patch.object(devbox.subprocess, "run", fake_run), mock.patch.object(devbox, "default_browser", lambda: "Safari"):
            self.assertTrue(devbox.open_page(authorize_url(40123)))
        self.assertEqual(calls, [(["/usr/bin/open", authorize_url(40123)], subprocess.DEVNULL)])
        with mock.patch.object(devbox.subprocess, "run", side_effect=OSError("no open")):
            self.assertFalse(devbox.open_page(authorize_url(40123)))



class SlowLinkTests(unittest.TestCase):
    def slow_server(self, delay, reply):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)
        seen = []

        def serve():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    seen.append(data)
                    time.sleep(delay)
                    body = json.dumps(reply).encode()
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)

        threading.Thread(target=serve, daemon=True).start()
        return listener.getsockname(), seen

    def test_a_devbox_that_answers_in_2_and_a_half_seconds_is_still_heard(self):
        address, _ = self.slow_server(2.5, {"account_uuid": WORK, "sessions": 0, "live_registered": True})
        link = devbox.HttpLink(address)
        self.assertTrue(link.reachable())
        self.assertEqual(link.devbox_account(), WORK)

    def test_a_job_start_that_times_out_is_asked_again_with_the_same_request(self):
        link = devbox.HttpLink(("127.0.0.1", 9))
        sent = []

        def once(method, target, body=None, timeout=None):
            sent.append(dict(body))
            if len(sent) == 1:
                raise devbox.LinkError()
            return 202, {"job": "a" * 32}

        link._call_once = once
        self.assertEqual(link.start_signin("/adopt", {"request": "b" * 32}), (202, {"job": "a" * 32}))
        self.assertEqual(sent, [{"request": "b" * 32}, {"request": "b" * 32}])
        sent.clear()
        with self.assertRaises(devbox.LinkError):
            link.start_signin("/adopt", {})
        self.assertEqual(len(sent), 1)

    def test_the_devbox_starts_one_job_for_one_request(self):
        rig = DevboxSigninWorld(self)
        token = "c" * 32
        first = rig.tcp("POST", "/add", {"uuid": PERSONAL, "request": token})
        second = rig.tcp("POST", "/add", {"uuid": PERSONAL, "request": token})
        self.assertEqual((first[0], second[0]), (202, 202))
        self.assertEqual(first[1]["job"], second[1]["job"])
        rig.wait_job(first[1]["job"], lambda data: data.get("url"))
        self.assertEqual(len(rig.login.calls), 1)
        self.assertEqual(rig.tcp("POST", "/adopt", {"request": "nope"})[0], 400)

    def test_a_silent_devbox_says_so_plainly(self):
        self.assertEqual(devbox.DEVBOX_SILENT, "The other computer did not answer in time, so nothing was changed.")

        class Unreached:
            def request(self, method, route, body=None):
                raise devbox.LinkUnreached()

        with self.assertRaises(devbox.SigninRefused) as caught:
            devbox.drive_signin(Unreached(), "/adopt", {}, accounts=[], relay=None, open_page=None, sleep=lambda s: None,
                                clock=time.monotonic, wants_page=False)
        self.assertEqual(caught.exception.sentence, devbox.DEVBOX_SILENT)

        bodies = []

        class TimesOut:
            def request(self, method, route, body=None):
                return 200, {"accounts": 0}

            def start_signin(self, route, body):
                bodies.append(dict(body))
                raise devbox.LinkError()

        with self.assertRaises(devbox.SigninRefused) as caught:
            devbox.drive_signin(TimesOut(), "/adopt", {}, accounts=[], relay=None, open_page=None, sleep=lambda s: None,
                                clock=time.monotonic, wants_page=False)
        self.assertEqual(caught.exception.sentence, devbox.DEVBOX_UNSURE)
        self.assertRegex(bodies[0].get("request", ""), r"^[0-9a-f]{32}$")
        self.assertNotIn("Check that Lifeline runs there", devbox.DEVBOX_SILENT + devbox.DEVBOX_UNSURE)


class BothStepsTests(unittest.TestCase):
    def test_after_the_mac_adds_the_devbox_account_the_devbox_step_works(self):
        from tests.test_server import TEAM, account as mac_account

        rig = MacProxyRig(self, live=TEAM)
        known = [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account",
                  "email": "33333333@example.invalid", "organization": "Example Org"}]
        (rig.mac.folder / "known_accounts.json").write_text(json.dumps(known))
        rig.service.tick()
        state = rig.service.state()
        self.assertEqual(state["devbox"]["live"], {
            "uuid": TEAM, "added": False, "known": True, "name": "Team Account", "short": "Team",
            "email": "33333333@example.invalid", "organization": "Example Org", "adopt_ready": False, "held": False,
            "note": "The other computer already uses Team Account, so you sign in once, here on this Mac. The other computer keeps its own sign-in.",
            "adopt_title": "Add Team Account from the other computer", "both_title": "Add both machines' own sign-ins",
        })
        self.assertEqual(
            rig.dev.service._live_note(),
            "The other computer's Claude Code uses Team Account, which Lifeline has not added yet, so Lifeline will not read it or renew its sign-in.",
        )
        status, reply = rig.mac.call("POST", "/devbox/adopt", {})
        self.assertEqual(rig.finished(reply["job"])["result"], devbox.LIVE_UNKNOWN)
        rig.mac.registry.upsert(mac_account(TEAM, 5))
        self.assertTrue(rig.service.state()["devbox"]["live"]["adopt_ready"])
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": TEAM})
        job = rig.finished(reply["job"])
        self.assertEqual((job["state"], job["result"]), ("done", "The other computer already uses Team Account, so Lifeline kept the sign-in the other computer has."))
        self.assertEqual((rig.relays, rig.opened), ([], []))

    def test_the_mac_offers_a_devbox_adopt_exactly_when_the_devbox_accepts_one(self):
        from tests.test_server import TEAM, account as mac_account

        cases = ((WORK, False, None), (TEAM, False, None), (TEAM, True, None), (ORG, False, None), (WORK, False, "stale"), (WORK, False, "unreadable"))
        for live, add_team, sign_in in cases:
            with self.subTest(live=live, add_team=add_team, sign_in=sign_in):
                rig = MacProxyRig(self, live=live)
                if add_team:
                    rig.mac.registry.upsert(mac_account(TEAM, 5))
                    rig.service._push_registry(rig.service.parts.link)
                if sign_in == "stale":
                    set_live(rig.dev, PAST_MS, "stale")
                elif sign_in == "unreadable":
                    rig.dev.world.slot_path.write_text("{nope")
                rig.service.tick()
                block = rig.service.state()["devbox"]["live"]
                offered = bool(block and block["adopt_ready"])
                status, reply = rig.dev.tcp("POST", "/adopt", {})
                accepted = status == 202
                if accepted:
                    rig.dev.wait_job(reply["job"], lambda data: data.get("state") != "running")
                self.assertEqual(offered, accepted, (live, add_team, status, reply))



class LinkEdgeTests(unittest.TestCase):
    def tried(self, first, second):
        link = devbox.HttpLink(("127.0.0.1", 9))
        outcomes = [first, second]

        def once(method, target, body=None, timeout=None):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        link._call_once = once
        return link

    def test_a_start_that_was_sent_never_reports_that_nothing_arrived(self):
        link = self.tried(devbox.LinkError(), devbox.LinkUnreached())
        with self.assertRaises(devbox.LinkError) as caught:
            link.start_signin("/adopt", {"request": "a" * 32})
        self.assertNotIsInstance(caught.exception, devbox.LinkUnreached)
        link = self.tried(devbox.LinkUnreached(), (202, {"job": "b" * 32}))
        self.assertEqual(link.start_signin("/adopt", {"request": "a" * 32}), (202, {"job": "b" * 32}))

    def unsure_link(self, found):
        calls = {"stops": [], "lookups": 0}

        class Unsure:
            def request(self, method, route, body=None):
                return 200, {"accounts": 0}

            def start_signin(self, route, body):
                calls["token"] = body["request"]
                raise devbox.LinkError()

            def find_request(self, token):
                calls["lookups"] += 1
                if found == "silent":
                    raise devbox.LinkError()
                if found == "never":
                    return 404, {"error": devbox.NO_SUCH_SIGNIN}
                return 200, {"job": "e" * 32, "state": "running"}

            def job(self, job_id):
                return 200, {"state": "done", "result": "Lifeline on the other computer took over the sign-in its Claude Code holds for Work Account."}

            def stop_signin(self, job_id):
                calls["stops"].append(job_id)
                return 200, {"stopping": True}

        return Unsure(), calls

    def drive(self, link, **extra):
        moments = iter(range(0, 10**6, 5))
        return devbox.drive_signin(link, "/adopt", {}, accounts=[], relay=None, open_page=None, sleep=lambda s: None,
                                   clock=lambda: next(moments), wants_page=False, **extra)

    def test_an_unsure_start_follows_the_job_it_finds(self):
        link, calls = self.unsure_link("found")
        job = self.drive(link)
        self.assertEqual(job["state"], "done")

    def test_an_unsure_start_that_never_arrived_says_nothing_changed(self):
        link, calls = self.unsure_link("never")
        with self.assertRaises(devbox.SigninRefused) as caught:
            self.drive(link)
        self.assertEqual(caught.exception.sentence, devbox.DEVBOX_SILENT)

    def test_an_unsure_start_hands_its_request_on_to_be_stopped_later(self):
        link, calls = self.unsure_link("silent")
        handed = []
        with self.assertRaises(devbox.SigninRefused) as caught:
            self.drive(link, on_unsure=handed.append)
        self.assertEqual(caught.exception.sentence, devbox.DEVBOX_UNSURE)
        self.assertEqual(devbox.DEVBOX_UNSURE, "The other computer did not answer in time. If the sign-in started there, Lifeline stops it as soon as the other computer answers.")
        self.assertEqual(handed, [calls["token"]])
        self.assertGreater(calls["lookups"], 1)

    def test_the_mac_stops_a_left_behind_devbox_job_once_the_devbox_answers(self):
        from tests.test_server import World

        world = World(self)
        link, calls = self.unsure_link("found")
        world.link = link
        service = world.service(link=link)
        service._orphans.add("f" * 32)
        service._stop_orphans(link)
        self.assertEqual(calls["stops"], ["e" * 32])
        self.assertEqual(service._orphans, set())

    def test_the_link_sends_an_empty_known_list(self):
        link = devbox.HttpLink(("127.0.0.1", 9))
        sent = []

        def call(method, route, body=None, **options):
            sent.append((method, route, body))
            return 200, {}

        link._call = call
        link.push_registry([], known=[])
        self.assertEqual(sent, [("POST", "/registry", {"accounts": [], "known": []})])

    def test_the_devbox_finds_a_job_by_its_request(self):
        rig = DevboxSigninWorld(self)
        token = "1" * 32
        status, reply = rig.tcp("POST", "/add", {"uuid": PERSONAL, "request": token})
        self.assertEqual(rig.tcp("GET", f"/request/{token}")[1]["job"], reply["job"])
        self.assertEqual(rig.tcp("GET", "/request/" + "2" * 32), (404, {"error": devbox.NO_SUCH_SIGNIN}))

    def test_two_tries_that_arrive_together_get_one_job(self):
        rig = DevboxSigninWorld(self)
        token = "3" * 32
        both = threading.Barrier(2)
        real = rig.service._live_check

        def meeting():
            try:
                both.wait(3)
            except threading.BrokenBarrierError:
                pass
            return real()

        rig.service._live_check = meeting
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(rig.tcp("POST", "/add", {"uuid": PERSONAL, "request": token}))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([status for status, _ in replies], [202, 202], replies)
        self.assertEqual(replies[0][1]["job"], replies[1][1]["job"])
        rig.wait_job(replies[0][1]["job"], lambda data: data.get("url"))
        self.assertEqual(len(rig.login.calls), 1)
        self.assertEqual(rig.tcp("POST", f"/resign/{ALEX}", {"request": token}), (409, {"error": devbox.REQUEST_MISMATCH}))

    def test_a_takeover_asks_to_keep_only_the_account_it_was_asked_for(self):
        rig = DevboxSigninWorld(self)
        asked = []
        real = enroll.adopt_live

        def watching(**options):
            asked.append(options.get("expected"))
            return real(**options)

        with mock.patch.object(devbox.enrolling, "adopt_live", watching):
            status, reply = rig.tcp("POST", "/add", {"uuid": WORK})
            done = rig.wait_job(reply["job"], lambda data: data.get("state") != "running")
        self.assertEqual(asked, [WORK])
        self.assertEqual(done["result"], "The other computer already uses Work Account, so Lifeline kept the sign-in the other computer has.")

    def test_an_unreadable_list_refuses_an_adopt_plainly(self):
        rig = DevboxSigninWorld(self)
        rig.world.registry.path.write_text("{nope")
        self.assertEqual(rig.tcp("POST", "/adopt", {}), (500, {"error": server.NO_ACCOUNTS}))

    def test_a_refused_push_keeps_the_old_known_list_and_an_empty_list_clears_it(self):
        rig = DevboxSigninWorld(self)
        good = [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account",
                 "email": "33333333@example.invalid", "organization": "Example Org"}]
        self.assertEqual(rig.tcp("POST", "/registry", {"accounts": [], "known": good})[0], 200)
        self.assertEqual(rig.service._known_accounts(), good)
        self.assertEqual(rig.tcp("POST", "/registry", {"accounts": [{"uuid": WORK}], "known": []})[0], 400)
        self.assertEqual(rig.service._known_accounts(), good)
        self.assertEqual(rig.tcp("POST", "/registry", {"accounts": [], "known": []})[0], 200)
        self.assertEqual(rig.service._known_accounts(), [])

    def test_the_devbox_says_whether_it_can_run_a_sign_in(self):
        rig = DevboxSigninWorld(self)
        self.assertIs(rig.tcp("GET", "/account")[1]["can_sign_in"], True)
        rig.service.parts.run_login = None
        self.assertIs(rig.tcp("GET", "/account")[1]["can_sign_in"], False)
        mac = MacProxyRig(self)
        mac.service.tick()
        self.assertIs(mac.service.state()["devbox"]["can_sign_in"], True)

    def test_a_devbox_that_takes_2_and_a_half_seconds_per_answer_still_adopts(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        replies = {"/registry": (200, {"accounts": 0}), "/adopt": (202, {"job": "9" * 32}),
                   "/job/" + "9" * 32: (200, {"state": "done", "result": "Lifeline on the other computer took over the sign-in its Claude Code holds for Work Account."})}

        def serve():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    target = data.split(b" ")[1].decode()
                    status, body = replies.get(target, (404, {"error": "x."}))
                    time.sleep(2.5)
                    text = json.dumps(body).encode()
                    conn.sendall(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(text)}\r\n\r\n".encode() + text)

        threading.Thread(target=serve, daemon=True).start()
        link = devbox.HttpLink(listener.getsockname())
        job = devbox.drive_signin(link, "/adopt", {}, accounts=[], relay=None, open_page=None, sleep=lambda s: None,
                                  clock=time.monotonic, wants_page=False)
        self.assertEqual(job["state"], "done")



class CoreSentenceTests(unittest.TestCase):
    def test_held_says_an_adopt_would_change_nothing(self):
        rig = MacProxyRig(self)
        rig.service.tick()
        live = rig.service.state()["devbox"]["live"]
        self.assertEqual((live["adopt_ready"], live["held"]), (True, False))
        status, reply = rig.mac.call("POST", "/devbox/adopt", {})
        self.assertEqual(rig.finished(reply["job"])["state"], "done")
        rig.service.tick()
        live = rig.service.state()["devbox"]["live"]
        self.assertEqual((live["adopt_ready"], live["held"]), (True, True))

    def test_the_add_screen_sentences_come_from_the_core(self):
        from tests.test_server import TEAM

        rig = MacProxyRig(self, live=TEAM)
        known = [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account",
                  "email": "33333333@example.invalid", "organization": "Example Org"}]
        (rig.mac.folder / "known_accounts.json").write_text(json.dumps(known))
        rig.service.tick()
        state = rig.service.state()
        live = state["devbox"]["live"]
        self.assertEqual(live["note"], "The other computer already uses Team Account, so you sign in once, here on this Mac. The other computer keeps its own sign-in.")
        self.assertEqual(live["adopt_title"], "Add Team Account from the other computer")
        self.assertEqual(live["both_title"], "Add both machines' own sign-ins")
        self.assertEqual(state["mac"]["live"]["adopt_title"], "Add Alex's Account from this Mac")
        (rig.mac.folder / "known_accounts.json").write_text("[]")
        live = rig.service.state()["devbox"]["live"]
        self.assertEqual((live["note"], live["adopt_title"]), (None, "Add the other computer's own sign-in"))
        accounts = {a["uuid"]: a for a in state["accounts"]}
        self.assertEqual(accounts[ALEX]["devbox_already"], "The other computer already has its own sign-in for Alex's Account.")
        self.assertIsNone(accounts[PERSONAL]["devbox_already"])

    def test_the_step_card_and_the_result_say_the_same_thing(self):
        rig = MacProxyRig(self)
        rig.service.tick()
        status, reply = rig.mac.call("POST", "/devbox/add", {"uuid": WORK, "step": [2, 2]})
        self.assertEqual(status, 202, reply)
        card = [j for j in rig.service.state()["jobs"] if j["id"] == reply["job"]][0]["card"]
        self.assertEqual(card, "Step 2 of 2: The other computer already uses Work Account, so Lifeline is keeping the sign-in the other computer has.")
        job = rig.finished(reply["job"])
        self.assertEqual(job["result"], "The other computer already uses Work Account, so Lifeline kept the sign-in the other computer has.")
        self.assertEqual(rig.mac.call("POST", "/devbox/add", {"uuid": WORK, "step": [3, 2]})[0], 400)

    def test_every_job_card_comes_from_the_core(self):
        from tests.test_server import World

        world = World(self, registered=(ALEX, WORK, PERSONAL))
        world.login_gate = threading.Event()
        self.addCleanup(world.login_gate.set)
        service = world.service()
        job = service.start_job("add", None, {"step": [1, 2]})["job"]
        card = [j for j in service.state()["jobs"] if j["id"] == job][0]["card"]
        self.assertEqual(card, "Step 1 of 2: press Authorize in Google Chrome to add a new account to this Mac.")
        world.login_gate.set()
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        rig = MacProxyRig(self)
        rig.service.tick()
        status, reply = rig.mac.call("POST", "/devbox/adopt", {})
        card = [j for j in rig.service.state()["jobs"] if j["id"] == reply["job"]][0]["card"]
        self.assertEqual(card, "Lifeline on the other computer is taking over the sign-in its Claude Code holds for Work Account.")
        rig.finished(reply["job"])



class ExpectedAdoptTests(unittest.TestCase):
    def test_the_devbox_adopts_only_the_account_it_was_told_to_expect(self):
        rig = MacProxyRig(self)
        rig.service.tick()
        before = (rig.dev.world.registry.load(), switching.read_record_entry(rig.dev.service.record_path))
        status, reply = rig.dev.tcp("POST", "/adopt", {"expected": PERSONAL})
        self.assertEqual(
            (status, reply),
            (409, {"error": "The other computer's Claude Code uses Work Account, not Personal Account, so nothing was changed."}),
        )
        self.assertEqual((rig.dev.world.registry.load(), switching.read_record_entry(rig.dev.service.record_path)), before)
        status, reply = rig.dev.tcp("POST", "/adopt", {"expected": WORK})
        self.assertEqual(status, 202, reply)
        self.assertEqual(rig.dev.wait_job(reply["job"], lambda d: d.get("state") != "running")["state"], "done")
        for bad in ({"expected": "not an id"}, {"expected": 3}, {"expected": WORK, "name": "X"}):
            self.assertEqual(rig.dev.tcp("POST", "/adopt", bad), (400, {"error": devbox.BAD_DEVBOX_ADOPT}))

    def test_a_change_after_the_request_fails_the_job_naming_both_accounts(self):
        rig = MacProxyRig(self)
        rig.service.tick()
        with mock.patch.object(devbox.enrolling, "adopt_live", side_effect=enroll.EnrollError(enroll.LIVE_NOT_ASKED)):
            status, reply = rig.dev.tcp("POST", "/adopt", {"expected": WORK})
            job = rig.dev.wait_job(reply["job"], lambda d: d.get("state") != "running")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"], "The other computer's Claude Code no longer uses Work Account, so nothing was changed.")

    def test_the_mac_sends_the_expected_account_and_reports_a_mismatch_plainly(self):
        rig = MacProxyRig(self)
        rig.service.tick()
        sent = []
        real = rig.service.parts.link.start_signin

        def spy(route, body):
            sent.append((route, {k: v for k, v in body.items() if k != "request"}))
            return real(route, body)

        rig.service.parts.link.start_signin = spy
        status, reply = rig.mac.call("POST", "/devbox/adopt", {"expected": PERSONAL})
        self.assertEqual(status, 202, reply)
        job = rig.finished(reply["job"])
        self.assertEqual(sent[-1], ("/adopt", {"expected": PERSONAL}))
        self.assertEqual(
            (job["state"], job["result"]),
            ("failed", "The other computer's Claude Code uses Work Account, not Personal Account, so nothing was changed."),
        )
        status, reply = rig.mac.call("POST", "/devbox/adopt", {"expected": WORK, "step": [2, 2]})
        self.assertEqual(rig.finished(reply["job"])["state"], "done")
        self.assertEqual(rig.mac.call("POST", "/devbox/adopt", {"expected": "x" * 36})[0], 404)
        self.assertEqual(rig.mac.call("POST", "/devbox/adopt", {"expected": 7})[0], 400)



class SigninEdgeTests(unittest.TestCase):
    def unsure_link(self, found):
        return LinkEdgeTests.unsure_link(self, found)

    def drive(self, link, sleep=lambda s: None, **extra):
        moments = iter(range(0, 10**6, 5))
        return devbox.drive_signin(link, "/adopt", {}, accounts=[], relay=None, open_page=None, sleep=sleep,
                                   clock=lambda: next(moments), wants_page=False, **extra)

    def test_a_stop_during_the_lookup_hands_the_request_on(self):
        link, calls = self.unsure_link("silent")
        handed = []

        def stop(seconds):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.drive(link, sleep=stop, on_unsure=handed.append)
        self.assertEqual(handed, [calls["token"]])

    def test_a_stop_during_the_start_hands_the_request_on(self):
        link, calls = self.unsure_link("silent")
        handed = []

        def start(route, body):
            calls["token"] = body["request"]
            raise KeyboardInterrupt()

        link.start_signin = start
        with self.assertRaises(KeyboardInterrupt):
            self.drive(link, on_unsure=handed.append)
        self.assertEqual(handed, [calls["token"]])

    def test_a_plain_refusal_keeps_no_request(self):
        link, calls = self.unsure_link("silent")
        handed = []

        def start(route, body):
            return 409, {"error": devbox.DEVBOX_BUSY}

        link.start_signin = start
        with self.assertRaises(devbox.SigninRefused):
            self.drive(link, on_unsure=handed.append)
        self.assertEqual(handed, [])

    def proxy_in_lookup(self):
        from tests.test_server import World

        world = World(self)
        link, calls = self.unsure_link("silent")
        looked = threading.Event()
        real = link.find_request

        def find(token):
            looked.set()
            return real(token)

        link.find_request = find
        service = world.service(link=link)
        job = service.start_devbox_job("adopt", {})["job"]
        self.assertTrue(looked.wait(10))
        return world, service, job, calls

    def finished(self, world, service, job):
        self.assertTrue(world.wait_for(lambda: [j for j in service.state()["jobs"] if j["id"] == job][0]["state"] != "running", 10))
        return [j for j in service.state()["jobs"] if j["id"] == job][0]

    def test_a_cancel_during_the_lookup_keeps_the_request_and_says_so(self):
        world, service, job, calls = self.proxy_in_lookup()
        service.cancel_job(f"{job}/cancel")
        found = self.finished(world, service, job)
        self.assertEqual(
            (found["state"], found["result"]),
            ("cancelled", "You cancelled the sign-in. If it started on the other computer, Lifeline stops it there as soon as the other computer answers."),
        )
        self.assertEqual(service._orphans, {calls["token"]})

    def test_a_mac_stop_during_the_lookup_never_claims_it_stopped_the_devbox(self):
        world, service, job, calls = self.proxy_in_lookup()
        service.stop()
        with service._state:
            found = dict(service._jobs[job])
        self.assertEqual(
            found["result"],
            "Lifeline stopped before the other computer answered. If the sign-in started there, the other computer ends it on its own within 10 minutes.",
        )
        self.assertEqual(calls["stops"], [])

    def test_the_proxy_keeps_the_request_of_a_start_the_devbox_never_confirmed(self):
        from tests.test_server import World

        world = World(self)
        link, calls = self.unsure_link("silent")
        service = world.service(link=link)
        with mock.patch.object(devbox, "UNSURE_SECONDS", 0.2), mock.patch.object(devbox, "JOB_POLL_SECONDS", 0.05):
            job = service.start_devbox_job("adopt", {})["job"]
            found = self.finished(world, service, job)
        self.assertEqual(found["result"], devbox.DEVBOX_UNSURE)
        self.assertEqual(service._orphans, {calls["token"]})

    def test_a_pinned_adopt_checks_the_expected_account_when_it_runs(self):
        rig = MacProxyRig(self)
        rig.service.tick()
        seen = []
        real = devbox.enrolling.adopt_live

        def spy(**options):
            seen.append(options.get("expected"))
            return real(**options)

        with mock.patch.object(devbox.enrolling, "adopt_live", spy):
            status, reply = rig.dev.tcp("POST", "/adopt", {"expected": WORK})
            job = rig.dev.wait_job(reply["job"], lambda d: d.get("state") != "running")
        self.assertEqual(job["state"], "done")
        self.assertEqual(seen, [WORK])


if __name__ == "__main__":
    unittest.main()
