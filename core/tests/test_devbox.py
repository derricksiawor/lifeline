import copy
import http.client
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import devbox, pairing
from lifeline import server
from lifeline import switch as switching
from lifeline.accounts import Account, Registry
from lifeline.anthropic import Unavailable
from lifeline.history import History
from lifeline.reader import AccountStatus
from lifeline.slot import FileSlot
from lifeline.store import FileStore
from tests.test_server import (
    CALM,
    WORK,
    ALEX,
    MCP_SECRET,
    ORDER,
    ORG,
    PERSONAL,
    R_WORK,
    R_ALEX,
    START,
    TAGS,
    TEAM,
    TEAM_ORG,
    WEEK,
    Clock,
    Collector,
    FakeProfiles,
    FakeRefresh,
    FakeUsage,
    UnixConnection,
    World,
    account,
    details,
    iso,
    pair_of,
    statusline_body,
)

UTC = timezone.utc
JSON_SECRET = "account-file-mcp-SECRET-planted"
SWITCH_REFUSED = "Lifeline switches only this Mac, so nothing was changed."
TEST_KEY = bytes(range(32))
LOCALHOST = ("127.0.0.1", 0)
TCP_ROUTES = (
    ("GET", "/readings", None),
    ("POST", "/readings", {"readings": []}),
    ("GET", "/account", None),
    ("POST", "/registry", {"accounts": []}),
)


def free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class RecordingUsage(FakeUsage):
    def __init__(self, world):
        super().__init__(world)
        self.tokens = []
        self.fail = False

    def __call__(self, token):
        self.tokens.append(token)
        if self.fail:
            raise Unavailable("Could not reach the server")
        return super().__call__(token)


class DevboxWorld:
    def __init__(self, case, registered=ORDER, live=WORK, parked=(ALEX, ORG), figures=None, clock=None, ends=None):
        self.case = case
        tmp = TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        case.addCleanup(patcher.stop)
        self.folder = self.home / ".local" / "share" / "com.example.lifeline"
        self.folder.mkdir(parents=True, mode=0o700)
        self.clock = clock or Clock()
        self.owners = {}
        self.serial = 0
        self.figures = dict(figures or CALM)
        self.registry = Registry(self.folder / "accounts.json")
        self.ends = dict(ends or {})
        self.registry.save([account(uuid, ORDER.index(uuid) + 1) for uuid in registered])
        self.store = FileStore(self.home / ".config" / "com.example.lifeline" / "signins")
        self.history = History(self.folder / "history.db")
        self.slot_path = self.home / ".claude" / ".credentials.json"
        self.slot = FileSlot(self.slot_path)
        self.signins = {}
        self.live = live
        if live is not None:
            self.signins[live] = self.issue(live)
            self.slot.write({"claudeAiOauth": self.signins[live], "mcpOAuth": {"github|1": {"accessToken": MCP_SECRET}}})
        for uuid in parked:
            self.signins[uuid] = self.issue(uuid)
            self.store.put(uuid, self.signins[uuid])
        self.write_config(live)
        self.pairing_path = pairing.default_path(self.home)
        server._write_private(self.pairing_path, {"key": TEST_KEY.hex()})
        self.profiles = FakeProfiles(self)
        self.usage = RecordingUsage(self)
        self.refresh = FakeRefresh(self)
        self.sessions = set()
        self.socket_dir = tempfile.mkdtemp(prefix="lifeline-test-", dir="/tmp")
        case.addCleanup(shutil.rmtree, self.socket_dir)
        self.socket_path = os.path.join(self.socket_dir, "d.sock")
        self.replies = []
        self.collector = Collector()
        self.collector.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("lifeline")
        logger.addHandler(self.collector)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        case.addCleanup(logger.removeHandler, self.collector)
        case.addCleanup(logger.setLevel, previous)

    def issue(self, uuid, expires=None):
        self.serial += 1
        tag = TAGS.get(uuid, uuid[:8])
        oauth = {
            "accessToken": f"devbox-access-{tag}-{self.serial}-SECRET",
            "refreshToken": f"devbox-refresh-{tag}-{self.serial}-SECRET",
            "expiresAt": expires if expires is not None else int((self.clock.now + timedelta(hours=8)).timestamp() * 1000),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
        if uuid in self.ends:
            oauth["refreshTokenExpiresAt"] = int(self.ends[uuid].timestamp() * 1000)
        self.owners[oauth["accessToken"]] = uuid
        self.owners[oauth["refreshToken"]] = uuid
        return oauth

    def write_config(self, uuid):
        data = {"numStartups": 3, "mcpServers": {"x": {"env": {"TOKEN": JSON_SECRET}}}}
        if uuid is not None:
            data["oauthAccount"] = details(uuid)
        (self.home / ".claude.json").write_text(json.dumps(data, indent=2))

    def secrets(self):
        return [token for token in self.owners] + [MCP_SECRET, JSON_SECRET, TEST_KEY.hex()]

    def parts(self, **extra):
        options = dict(
            home=self.home,
            folder=self.folder,
            slot=self.slot,
            store=self.store,
            registry=self.registry,
            history=self.history,
            profile_fn=self.profiles,
            usage_fn=self.usage,
            refresh_fn=self.refresh,
            run_login=None,
            runner=None,
            user="tester",
            link=server.NullLink(),
            clock=self.clock,
            tz=UTC,
            monotonic=self.clock.monotonic,
            sessions_fn=lambda: set(self.sessions),
            settle=0,
            lock_timeout=5.0,
            session_timeout=5.0,
            temp_root=None,
            backups=self.home / ".lifeline-backups",
        )
        options.update(extra)
        return server.Parts(**options)

    def service(self, **extra):
        return devbox.DevboxService(self.parts(**extra))

    def run(self, service, address=LOCALHOST):
        holder = {}
        ready = threading.Event()

        def on_ready(stop):
            holder["stop"] = stop
            ready.set()

        def target():
            holder["code"] = devbox.serve(
                lambda: service,
                self.socket_path,
                address=address,
                install_signals=False,
                ready=on_ready,
                ticking=False,
                service_lock=self.folder / "service.lock",
            )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.case.assertTrue(ready.wait(10), "the other computer service did not start")
        self.service_obj = service

        def stop():
            if "stop" in holder:
                holder["stop"]()
            thread.join(10)

        self.case.addCleanup(stop)
        return stop, holder

    def port(self):
        return self.service_obj.tcp_address[1]

    def tcp(self, method, route, body=None, extra_headers=None, key=TEST_KEY):
        conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=10)
        try:
            headers = dict(extra_headers or {})
            payload = None if body is None else json.dumps(body).encode("utf-8")
            if key is not None:
                headers.update(pairing.request_headers(key, method, route, payload or b""))
            if payload is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, route, body=payload, headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8")
            status = response.status
            kind = response.getheader("Content-Type")
        finally:
            conn.close()
        self.replies.append(text)
        if not text:
            return status, {}
        self.case.assertTrue(kind.startswith("application/json"), kind)
        return status, json.loads(text)

    def local(self, method, route, body=None):
        conn = UnixConnection(self.socket_path)
        try:
            payload = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, route, body=payload, headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8")
            status = response.status
        finally:
            conn.close()
        self.replies.append(text)
        return status, json.loads(text)

    def link(self, clock=None, address=None):
        return devbox.HttpLink(address or ("127.0.0.1", self.port()), key=TEST_KEY, clock=clock or self.clock)

    def wait_for(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.01)
        return check()

    def assert_clean(self, extra=()):
        texts = self.replies + self.collector.lines + list(extra)
        for text in texts:
            self.case.assertNotIn("Traceback", text)
            for secret in self.secrets():
                self.case.assertNotIn(secret, text)


class ListenAddressTests(unittest.TestCase):
    def test_the_service_listens_only_where_it_says_unless_told_otherwise(self):
        clean = {key: value for key, value in os.environ.items() if key != "LIFELINE_LISTEN"}
        for environ, expected in (({}, "192.0.2.7"), ({"LIFELINE_LISTEN": "198.51.100.4"}, "198.51.100.4"), ({"LIFELINE_LISTEN": "0.0.0.0"}, "0.0.0.0")):
            with self.subTest(environ=environ), mock.patch.dict(os.environ, dict(clean, **environ), clear=True), \
                    mock.patch.object(pairing, "own_address", return_value="192.0.2.7"), mock.patch.object(devbox, "_Listener") as made:
                devbox.listen("service", None)
            made.assert_called_once_with("service", (expected, pairing.PORT))
        with mock.patch.object(devbox, "_Listener") as made:
            devbox.listen("service", ("127.0.0.1", 0))
        made.assert_called_once_with("service", ("127.0.0.1", 0))


class UnpairTests(unittest.TestCase):
    def test_only_a_signed_unpair_drops_the_key(self):
        world = DevboxWorld(self)
        world.run(world.service())
        self.assertEqual(world.tcp("POST", "/unpair", {}, key=None)[0], 401)
        self.assertEqual(world.service_obj.keeper.key(), TEST_KEY)
        self.assertEqual(world.tcp("POST", "/unpair", {})[0], 200)
        self.assertIsNone(world.service_obj.keeper.key())
        self.assertEqual(world.tcp("GET", "/account")[0], 401)
        world.assert_clean()


class DevboxRouteTests(unittest.TestCase):
    def test_every_tcp_route_answers_with_no_key(self):
        world = DevboxWorld(self)
        world.run(world.service())
        for method, route, body in TCP_ROUTES:
            status, reply = world.tcp(method, route, body)
            self.assertEqual(status, 200, (method, route, reply))
        self.assertEqual(world.tcp("GET", "/nowhere")[0], 404)
        self.assertEqual(world.tcp("DELETE", "/readings")[0], 405)
        self.assertFalse([line for line in world.collector.lines if "connection that failed" in line])
        world.assert_clean()

    def test_the_body_cap_and_sentences_hold_on_tcp(self):
        world = DevboxWorld(self)
        world.run(world.service())
        sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
        signed = "".join(f"{k}: {v}\r\n" for k, v in pairing.request_headers(TEST_KEY, "POST", "/registry", b"").items())
        try:
            sock.sendall(
                b"POST /registry HTTP/1.1\r\nHost: d"
                + b"\r\nContent-Length: "
                + str(server.BODY_LIMIT + 1).encode()
                + b"\r\nConnection: close\r\n"
                + signed.encode()
                + b"\r\n"
            )
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            reply = b"".join(chunks).decode()
        finally:
            sock.close()
        self.assertIn("413", reply.split("\r\n")[0])
        self.assertTrue(reply.endswith("\r\n\r\n"), "a request not yet proven gets no words")
        status, body = world.tcp("GET", "/readings?since=yesterday")
        self.assertEqual((status, body["error"]), (400, "The since value must be a time such as 2026-11-19T15:41:00Z."))
        status, body = world.tcp("POST", "/registry", {"accounts": [{"uuid": WORK}]})
        self.assertEqual(status, 400)
        self.assertEqual(
            body["error"],
            "Each account in the list must come with its Claude account, number, name, short name, full name, email and plan.",
        )
        world.assert_clean()

    def test_the_local_socket_works_beside_the_listener(self):
        world = DevboxWorld(self)
        service = world.service()
        service.tick()
        world.run(service)
        self.assertIsNotNone(service.tcp_address)
        status, state = world.local("GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["dev"]["hero_name"], "Work Account")
        status, body = world.local("POST", "/reading", statusline_body("s1", WORK, R_WORK, five=30.0, week=55.0))
        self.assertEqual((status, body["saved"], body["account_uuid"], body["five"], body["week"]), (200, True, WORK, 30.0, 55.0))
        world.assert_clean()

    def test_the_listener_waits_for_its_address_and_tries_again(self):
        world = DevboxWorld(self)
        service = world.service()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        with mock.patch.object(devbox, "BIND_RETRY_SECONDS", 0.05):
            world.run(service, address=("127.0.0.1", port))
            self.assertIsNone(service.tcp_address)
            self.assertIn(
                "WARNING Lifeline could not listen for the Mac yet, so it will try again every minute.",
                world.collector.lines,
            )
            blocker.close()
            self.assertTrue(world.wait_for(lambda: service.tcp_address is not None))
        self.assertEqual(world.tcp("GET", "/account")[0], 200)

    def test_the_devbox_refuses_to_change_its_account(self):
        world = DevboxWorld(self)
        service = world.service()
        world.run(service)
        before_slot = world.slot_path.read_bytes()
        before_store = {uuid: world.store.get(uuid) for uuid in ORDER}
        before_registry = (world.folder / "accounts.json").read_bytes()
        for route, body in (
            ("/switch", {"uuid": ORG}),
            ("/switch", {"number": 1}),
            ("/undo", None),
        ):
            status, reply = world.local("POST", route, body)
            self.assertEqual((status, reply), (403, {"error": SWITCH_REFUSED}), route)
        for route, body in (
            ("/add", {}),
            ("/add", {"email": "ama@example.invalid"}),
            (f"/resign/{WORK}", None),
            ("/adopt", {}),
        ):
            status, reply = world.local("POST", route, body)
            self.assertEqual((status, reply), (403, {"error": devbox.FROM_MAC}), route)
        for call in (
            lambda: service.switch_to(ORG),
            lambda: service.undo(),
            lambda: service.start_job("switch", WORK, {}),
        ):
            with self.assertRaises(server.Refusal) as caught:
                call()
            self.assertEqual((caught.exception.status, caught.exception.sentence), (403, SWITCH_REFUSED))
        status, reply = world.local("POST", f"/rename/{WORK}", {"name": "X"})
        self.assertEqual((status, reply["error"]), (403, "The other computer takes its account names from the Mac, so rename them there."))
        status, reply = world.local("POST", "/settings", {"auto_switch": False})
        self.assertEqual((status, reply["error"]), (403, "The other computer takes its settings from the Mac, so change them there."))
        self.assertEqual(world.local("POST", "/cancel")[1]["error"], "There is no switch to cancel.")
        self.assertEqual(world.slot_path.read_bytes(), before_slot)
        self.assertEqual({uuid: world.store.get(uuid) for uuid in ORDER}, before_store)
        self.assertEqual((world.folder / "accounts.json").read_bytes(), before_registry)
        for route in ("/switch", "/undo"):
            self.assertEqual(world.tcp("POST", route, {})[0], 404)
        world.assert_clean()

    def test_the_local_state_shows_the_devbox(self):
        world = DevboxWorld(self)
        world.sessions.update({"a", "b"})
        service = world.service()
        service.tick()
        world.run(service)
        status, state = world.local("GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["devbox"]["account_uuid"], WORK)
        self.assertEqual(state["dev"]["hint"], "Lifeline switches only this Mac, so switch the other computer there.")
        self.assertEqual(state["dev"]["sessions"], "2 sessions open")
        self.assertEqual(state["sessions"]["dev"]["open"], 2)
        self.assertEqual(state["accounts"][1]["in_use"], ["devbox"])
        self.assertEqual(world.local("GET", f"/history/{WORK}")[0], 200)


def read_until_closed(sock):
    chunks = []
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        pass
    return b"".join(chunks).decode("utf-8", "replace")


def rss_kb():
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    return int(out.strip())


class ListenerLimitTests(unittest.TestCase):
    def started(self, head_seconds=None):
        world = DevboxWorld(self)
        if head_seconds is not None:
            patcher = mock.patch.object(devbox, "HEAD_SECONDS", head_seconds)
            patcher.start()
            self.addCleanup(patcher.stop)
        world.run(world.service())
        return world

    def drip(self, sock, every=0.3):
        stop = threading.Event()
        self.addCleanup(stop.set)

        def run():
            try:
                while not stop.wait(every):
                    sock.sendall(b"a")
            except OSError:
                pass

        threading.Thread(target=run, daemon=True).start()
        return stop

    def test_the_limits_are_5_seconds_4_kb_and_a_1_second_drain(self):
        self.assertEqual((devbox.HEAD_SECONDS, devbox.HEAD_LIMIT, devbox.DRAIN_SECONDS), (5.0, 4096, 1.0))
        self.assertEqual(devbox.REQUEST_SECONDS, 10.0)

    def test_a_stranger_gets_a_bare_refusal_that_names_nothing(self):
        world = self.started()
        port = world.port()
        wrong = pairing.request_headers(bytes(32), "GET", "/account", b"")
        for request, status in (
            (b"GET /account HTTP/1.1\r\nHost: d\r\n\r\n", "401"),
            (("GET /account HTTP/1.1\r\nHost: d\r\n" + "".join(f"{k}: {v}\r\n" for k, v in wrong.items()) + "\r\n").encode(), "401"),
            (b"GET /account HTTP/1.1\r\nHost: d\r\nX-Pad: " + b"a" * devbox.HEAD_LIMIT + b"\r\n\r\n", "431"),
            (b"NONSENSE\r\n\r\n", None),
        ):
            with self.subTest(request=request[:40]):
                sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                self.addCleanup(sock.close)
                sock.sendall(request)
                reply = read_until_closed(sock)
                head, _, body = reply.partition("\r\n\r\n")
                if status is not None:
                    self.assertTrue(head.startswith(f"HTTP/1.1 {status}"), head)
                    self.assertEqual(body, "")
                self.assertNotIn("server:", reply.lower())
                self.assertNotIn("lifeline", reply.lower())
                self.assertNotIn("{", reply)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/account", headers=pairing.request_headers(TEST_KEY, "GET", "/account", b""))
        response = conn.getresponse()
        response.read()
        self.assertEqual((response.status, response.getheader("Server")), (200, None))

    def test_an_unsigned_body_is_never_waited_for(self):
        world = self.started()
        sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
        self.addCleanup(sock.close)
        started = time.monotonic()
        sock.sendall(b"POST /registry HTTP/1.1\r\nHost: d\r\nContent-Type: application/json\r\nContent-Length: 65536\r\n\r\n")
        reply = read_until_closed(sock)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(reply.startswith("HTTP/1.1 401"), reply[:60])

    def test_a_signed_looking_body_that_drips_is_cut_at_the_request_deadline(self):
        patcher = mock.patch.object(devbox, "REQUEST_SECONDS", 1.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        world = self.started()
        signed = pairing.request_headers(TEST_KEY, "POST", "/registry", b"a" * 100)
        sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
        self.addCleanup(sock.close)
        head = "POST /registry HTTP/1.1\r\nHost: d\r\nContent-Length: 100\r\n" + "".join(f"{k}: {v}\r\n" for k, v in signed.items())
        sock.sendall((head + "\r\n").encode())
        self.drip(sock, every=0.2)
        started = time.monotonic()
        reply = read_until_closed(sock)
        took = time.monotonic() - started
        self.assertGreater(took, 0.7)
        self.assertLess(took, 2.0)
        self.assertNotIn("HTTP/1.1 200", reply)

    def test_a_header_drip_is_cut_at_the_deadline(self):
        world = self.started(head_seconds=1.0)
        sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /account HTTP/1.1\r\nHost: d\r\nX-Pad: ")
        started = time.monotonic()
        self.drip(sock, every=0.4)
        reply = read_until_closed(sock)
        took = time.monotonic() - started
        self.assertGreater(took, 0.8)
        self.assertLess(took, 1.8)
        if reply:
            self.assertTrue(reply.startswith("HTTP/1.1 408"), reply[:60])
            self.assertTrue(reply.endswith("\r\n\r\n"), reply)
            self.assertNotIn("account_uuid", reply)

    def test_a_silent_peer_is_cut_at_the_deadline(self):
        world = self.started(head_seconds=1.0)
        sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /account HTTP/1.1\r\nHost: d\r\n")
        started = time.monotonic()
        reply = read_until_closed(sock)
        took = time.monotonic() - started
        self.assertGreater(took, 0.8)
        self.assertLess(took, 1.8)
        self.assertTrue(reply.startswith("HTTP/1.1 408"), reply[:60])

    def test_dripping_peers_hold_a_request_no_longer_than_the_deadline(self):
        world = self.started(head_seconds=1.0)
        for _ in range(devbox.CONNECTION_LIMIT):
            sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
            self.addCleanup(sock.close)
            sock.sendall(b"GET /account HTTP/1.1\r\nHost: d\r\nX-Pad: ")
            self.drip(sock)
        time.sleep(0.3)
        started = time.monotonic()
        status, body = world.tcp("GET", "/account")
        took = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertLess(took, 1.0 + 0.8)

    def test_a_peer_dripping_after_a_refusal_frees_its_slot_within_a_second(self):
        world = self.started()
        for _ in range(devbox.CONNECTION_LIMIT):
            sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
            self.addCleanup(sock.close)
            sock.sendall(b"GET /nowhere HTTP/1.1\r\nHost: d\r\n\r\n")
            self.assertTrue(sock.recv(65536).startswith(b"HTTP/1.1 401"))
            self.drip(sock, every=0.2)
        time.sleep(0.2)
        started = time.monotonic()
        status, body = world.tcp("GET", "/account")
        took = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertLess(took, devbox.DRAIN_SECONDS + 0.8)

    def test_a_head_over_4_kb_is_refused_before_anything_is_read(self):
        world = self.started()
        sock = socket.create_connection(("127.0.0.1", world.port()), timeout=10)
        self.addCleanup(sock.close)
        head = b"GET /account HTTP/1.1\r\nHost: d\r\nX-Pad: "
        sock.sendall(head + b"a" * (devbox.HEAD_LIMIT + 1 - len(head)))
        reply = read_until_closed(sock)
        self.assertTrue(reply.startswith("HTTP/1.1 431"), reply[:60])
        self.assertNotIn("too large", reply)
        pad = devbox.HEAD_LIMIT - 400
        status, _ = world.tcp("GET", "/account", extra_headers={"X-Pad": "a" * pad})
        self.assertEqual(status, 200)

    def test_memory_stays_flat_against_large_header_lines(self):
        world = self.started()
        line = b"X-Pad: " + b"a" * 65000 + b"\r\n"
        base = rss_kb()

        def flood():
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", world.port()), timeout=5)
                sock.sendall(b"GET /account HTTP/1.1\r\nHost: x\r\n")
                for _ in range(98):
                    sock.sendall(line)
            except OSError:
                pass
            finally:
                if sock is not None:
                    sock.close()

        threads = [threading.Thread(target=flood, daemon=True) for _ in range(devbox.CONNECTION_LIMIT + 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        grown = rss_kb() - base
        self.assertLess(grown, 20 * 1024)
        self.assertEqual(world.tcp("GET", "/account")[0], 200)


class DevboxReadingTests(unittest.TestCase):
    def test_it_reads_only_the_accounts_it_holds_with_the_slots_own_key(self):
        world = DevboxWorld(self)
        before = world.slot_path.read_bytes()
        service = world.service()
        service.tick()
        self.assertEqual(sorted(world.usage.calls), sorted([WORK, ALEX, ORG]))
        self.assertIn(world.signins[WORK]["accessToken"], world.usage.tokens)
        self.assertEqual(world.refresh.calls, 0)
        self.assertEqual(world.slot_path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(os.stat(world.slot_path).st_mode), 0o600)
        self.assertEqual(sorted(service.covers()), sorted([WORK, ALEX, ORG]))

    def test_the_live_signin_is_never_renewed(self):
        world = DevboxWorld(self, live=WORK)
        stale = dict(world.signins[WORK], expiresAt=int((START - timedelta(hours=1)).timestamp() * 1000))
        world.slot.write({"claudeAiOauth": stale})
        before = world.slot_path.read_bytes()
        service = world.service()
        service.tick()
        world.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(world.refresh.calls, 0)
        self.assertNotIn(WORK, world.usage.calls)
        self.assertEqual(world.slot_path.read_bytes(), before)

    def test_readings_keep_their_original_times_and_list_status_and_cover(self):
        world = DevboxWorld(self)
        dead = world.store.get(ORG)
        world.owners.pop(dead["accessToken"])
        world.owners.pop(dead["refreshToken"])
        service = world.service()
        service.tick()
        world.history.add_reading(WORK, "statusline", 33.0, R_WORK, 56.0, WEEK, START + timedelta(seconds=7, microseconds=250000))
        world.clock.advance(minutes=1)
        world.run(service)
        status, body = world.tcp("GET", "/readings?since=" + iso(START - timedelta(hours=1)))
        self.assertEqual(status, 200)
        times = [(r["uuid"], r["source"], r["at"]) for r in body["readings"]]
        self.assertIn((WORK, "statusline", "2026-11-19T15:41:07.250000Z"), times)
        self.assertIn((WORK, "usage", iso(START)), times)
        self.assertEqual(sorted(body["covers"]), sorted([WORK, ALEX]))
        self.assertTrue(body["accounts"][ORG]["needs_signin"])
        self.assertEqual(body["accounts"][PERSONAL]["paused_until"], None)
        self.assertFalse(body["more"])
        status, later = world.tcp("GET", "/readings?since=" + "2026-11-19T15:41:07.250000Z")
        self.assertEqual([r["at"] for r in later["readings"]], ["2026-11-19T15:41:07.250000Z"])
        world.assert_clean()

    def test_readings_come_in_pages(self):
        world = DevboxWorld(self)
        service = world.service()
        for minute in range(5):
            world.history.add_reading(WORK, "usage", float(minute), R_WORK, 50.0, WEEK, START - timedelta(minutes=10 - minute))
        world.run(service)
        with mock.patch.object(devbox, "READINGS_PAGE", 3):
            status, body = world.tcp("GET", "/readings?since=" + iso(START - timedelta(hours=1)))
        self.assertEqual(len(body["readings"]), 3)
        self.assertTrue(body["more"])
        self.assertEqual([r["five"] for r in body["readings"]], [0.0, 1.0, 2.0])

    def test_the_account_route_gives_local_facts(self):
        world = DevboxWorld(self)
        world.sessions.add("s1")
        service = world.service()
        service.tick()
        world.run(service)
        status, body = world.tcp("GET", "/account")
        self.assertEqual(
            (status, body),
            (200, {"account_uuid": WORK, "sessions": 1, "live_registered": True, "live_email": None,
                   "live_organization": None, "live_ready": True, "live_held": True, "can_sign_in": False}),
        )
        world.write_config(ORG)
        service.tick()
        self.assertEqual(world.tcp("GET", "/account")[1]["account_uuid"], ORG)
        world.assert_clean()


class RegistryMergeTests(unittest.TestCase):
    def test_the_devbox_merges_the_macs_list_and_keeps_its_own_details(self):
        world = DevboxWorld(self, registered=(ALEX, WORK, ORG))
        world.run(world.service())
        pushed = [
            {"uuid": WORK, "number": 1, "name": "Command Account", "short": "Command", "full": "Example Team Account", "email": "hello@example.net", "plan": "Max 20x"},
            {"uuid": ALEX, "number": 2, "name": "Alex's Account", "short": "Alex", "full": "Alex's Account", "email": "j@example.invalid", "plan": "Max 5x"},
            {"uuid": PERSONAL, "number": 3, "name": "Personal Account", "short": "Personal", "full": "Example Org Personal Account", "email": "sam@example.org", "plan": "Max 20x"},
        ]
        status, body = world.tcp("POST", "/registry", {"accounts": pushed})
        self.assertEqual((status, body), (200, {"accounts": 4}))
        saved = {a.uuid: a for a in world.registry.load()}
        self.assertEqual((saved[WORK].name, saved[WORK].key, saved[WORK].email), ("Command Account", 1, "hello@example.net"))
        self.assertEqual(saved[WORK].oauth_account, details(WORK))
        self.assertEqual((saved[PERSONAL].key, saved[PERSONAL].signed_in_at, saved[PERSONAL].oauth_account), (3, None, {}))
        self.assertEqual(saved[ORG].key, 4)
        clash = pushed + [{"uuid": "66666666-ffff-4fff-8fff-000000000006", "number": 4, "name": "Ama's Account", "short": "Ama", "full": "Ama's Account", "email": "a@example.invalid", "plan": "Max 5x"}]
        status, body = world.tcp("POST", "/registry", {"accounts": clash})
        self.assertEqual(body, {"accounts": 4})
        self.assertNotIn(ORG, {a.uuid for a in world.registry.load()})
        self.assertIsNotNone(world.store.get(ORG))
        for bad in (
            pushed + pushed[:1],
            [dict(pushed[0], number=0)],
            [dict(pushed[0], name="Two\nlines")],
            [dict(pushed[0], uuid="../x")],
            [dict(pushed[0], secret="x")],
            [dict(pushed[1], number=1), pushed[0]],
        ):
            self.assertEqual(world.tcp("POST", "/registry", {"accounts": bad})[0], 400)
        self.assertEqual(stat.S_IMODE(os.stat(world.folder / "accounts.json").st_mode), 0o600)
        world.assert_clean()


class LinkTests(unittest.TestCase):
    def pair(self, mac_figures=None, devbox_figures=None, **devbox_options):
        mac = World(self, figures=mac_figures)
        dev = DevboxWorld(self, clock=mac.clock, figures=devbox_figures, **devbox_options)
        dev.run(dev.service())
        return mac, dev

    def test_the_link_follows_the_contract(self):
        mac, dev = self.pair(ends={WORK: START + timedelta(days=20)})
        dev.history.add_reading(ORG, "usage", 3.0, None, 9.0, WEEK, START - timedelta(hours=2))
        dev.history.add_reading(ORG, "usage", 2.0, None, 9.0, WEEK, START - timedelta(days=2))
        dev.service_obj.tick()
        link = dev.link(clock=mac.clock)
        self.assertTrue(link.reachable())
        self.assertEqual(link.devbox_account(), WORK)
        readings = link.fetch_readings()
        self.assertEqual(readings[0].at, START - timedelta(hours=2))
        readings = readings[1:]
        self.assertEqual(sorted(r.uuid for r in readings), sorted([WORK, ALEX, ORG]))
        self.assertTrue(all(r.at == START and r.at.tzinfo is not None for r in readings))
        again = link.fetch_readings()
        self.assertEqual(sorted(r.uuid for r in again), sorted([WORK, ALEX, ORG]))
        status = link.devbox_status()
        self.assertEqual(sorted(status["covers"]), sorted([WORK, ALEX, ORG]))
        self.assertEqual(status["signin_ends"], {WORK: "2026-12-09T15:41:00Z"})
        self.assertIsInstance(status["statuses"][WORK], AccountStatus)
        self.assertTrue(link.push_registry([{"uuid": WORK, "number": 2, "name": "Work Account", "short": "Work", "full": "Example Team's Account", "email": "hello@example.net", "plan": "Max 20x"}]))
        dev.service_obj.report("A note from the other computer.")
        link.fetch_readings()
        notes = link.devbox_status()["reports"]
        self.assertEqual([n["title"] for n in notes], ["A note from the other computer."])
        self.assertEqual(notes[0]["at"], iso(START))
        dev.assert_clean()

    def test_every_call_gives_up_within_its_route_limit_and_reads_try_twice(self):
        silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        silent.bind(("127.0.0.1", 0))
        silent.listen(16)
        self.addCleanup(silent.close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        trickle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trickle.bind(("127.0.0.1", 0))
        trickle.listen(16)
        self.addCleanup(trickle.close)

        def drip(conn):
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                while not stop.wait(0.3):
                    conn.sendall(b"X")
            except OSError:
                pass
            finally:
                conn.close()

        def accept():
            trickle.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = trickle.accept()
                except OSError:
                    continue
                threading.Thread(target=drip, args=(conn,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        self.assertEqual(
            (devbox.LINK_TIMEOUT, devbox.READINGS_SECONDS, devbox.PUSH_SECONDS, devbox.START_SECONDS), (8.0, 15.0, 15.0, 15.0)
        )
        limits = {"reachable": (1.0, 1.7), "readings": (1.6, 2.4), "registry": (1.0, 1.7)}
        patch = mock.patch.object(devbox, "READINGS_SECONDS", 0.9)
        patch.start()
        self.addCleanup(patch.stop)
        for address in (silent.getsockname(), trickle.getsockname()):
            link = devbox.HttpLink(address, timeout=0.6, clock=Clock())
            calls = {
                "reachable": (link.reachable, False),
                "readings": (link.fetch_readings, "raised"),
                "registry": (lambda: link.push_registry([]), False),
            }
            results = {}

            def run(name, call):
                started = time.monotonic()
                try:
                    outcome = call()
                except Exception:
                    outcome = "raised"
                results[name] = (outcome, time.monotonic() - started)

            threads = [threading.Thread(target=run, args=(name, call)) for name, (call, _) in calls.items()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            for name, (call, expected) in calls.items():
                outcome, took = results[name]
                self.assertEqual(outcome, expected, (address, name))
                low, high = limits[name]
                self.assertLess(took, high, (address, name, took))
                self.assertGreater(took, low, (address, name, took))

    def test_the_mac_reads_what_the_devbox_does_not_cover(self):
        mac, dev = self.pair(live=WORK, parked=(ORG,))
        dev.service_obj.tick()
        service = mac.service(link=dev.link(clock=mac.clock))
        service.tick()
        self.assertEqual(sorted(mac.usage.calls), sorted([ALEX, PERSONAL]))
        self.assertEqual(sorted(dev.usage.calls), sorted([WORK, ORG]))
        self.assertEqual(mac.history.latest(ORG).at, START)
        state = service.state()
        live = state["devbox"].pop("live")
        self.assertIs(state["devbox"].pop("can_sign_in"), False)
        self.assertEqual(state["devbox"], {"name": None, "address": None, "reachable": True, "answers": True, "account_uuid": WORK, "live_registered": True, "set_up": True})
        self.assertEqual((live["added"], live["name"], live["adopt_ready"]), (True, "Work Account", True))
        self.assertEqual(state["accounts"][1]["in_use"], ["devbox"])
        names = [a.name for a in dev.registry.load()]
        self.assertEqual(len(names), 4)
        mac.registry.upsert(Account(**dict(vars(mac.registry.by_uuid(ORG)), name="Org Account")))
        service.tick()
        self.assertEqual(dev.registry.by_uuid(ORG).name, "Org Account")
        mac.clock.advance(minutes=5)
        service.tick()
        self.assertEqual(sorted(mac.usage.calls), sorted([ALEX, PERSONAL, ALEX, PERSONAL]))

    def test_no_secret_crosses_the_wire(self):
        figures = dict(CALM)
        figures[WORK] = (100.0, R_WORK, 56.0, WEEK)
        mac, dev = self.pair(devbox_figures=figures, parked=(ORG,))
        mac.figures[ALEX] = (100.0, R_ALEX, 24.0, WEEK)
        proxy = Proxy(self, ("127.0.0.1", dev.port()))
        service = mac.service(link=devbox.HttpLink(proxy.address, key=TEST_KEY, clock=mac.clock))
        service.update_settings({"auto_switch": False})
        for _ in range(4):
            dev.service_obj.tick()
            service.tick()
            mac.clock.advance(seconds=30)
        service.update_settings({"alerts": {"week": False}})
        service.tick()
        mac.registry.upsert(Account(**dict(vars(mac.registry.by_uuid(ORG)), name="Org Account")))
        service.tick()
        sent = proxy.texts("up")
        back = proxy.texts("down")
        joined = " ".join(sent)
        for route in ("GET /account", "GET /readings", "POST /registry"):
            self.assertIn(route, joined)
        for text in sent + back:
            for secret in mac.secrets() + dev.secrets():
                self.assertNotIn(secret, text)
            self.assertNotIn("Authorization", text)
        mac.assert_clean()
        dev.assert_clean()


class Renew:
    def __init__(self, world):
        self.world = world
        self.calls = 0

    def __call__(self, signin):
        self.calls += 1
        old = signin["claudeAiOauth"]
        uuid = self.world.owners[old["refreshToken"]]
        out = copy.deepcopy(signin)
        out["claudeAiOauth"] = self.world.issue(uuid, expires=4_102_444_800_000)
        return out


def expire_live(world):
    item = json.loads(world.slot_path.read_text())
    item["claudeAiOauth"]["expiresAt"] = 1_600_000_000_000
    world.slot.write(item)


def stale_record(world):
    path = switching.record_path_for(world.folder / "lifeline.lock")
    switching.record_confirmed(path, WORK, {"claudeAiOauth": {"refreshToken": "an-older-renewal-key"}}, world.clock.now)
    return path


class DevboxIdentityTests(unittest.TestCase):
    def test_the_devbox_records_the_live_pair_its_profile_confirms(self):
        world = DevboxWorld(self, parked=(ALEX,))
        path = stale_record(world)
        service = world.service()
        service.tick()
        entry = switching.read_record_entry(path)
        live = json.loads(world.slot_path.read_text())["claudeAiOauth"]
        self.assertEqual((entry["uuid"], entry["refresh_sha256"], entry["proven"]), (WORK, switching.fingerprint(live), False))
        written = path.read_bytes()
        world.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(path.read_bytes(), written)

    def test_after_a_restart_with_an_expired_key_the_devbox_still_reads_and_renews(self):
        world = DevboxWorld(self, parked=(ALEX,))
        stale_record(world)
        world.service().tick()
        expire_live(world)
        parked = dict(world.store.get(ALEX), expiresAt=1_600_000_000_000)
        world.store.put(ALEX, parked)
        renew = Renew(world)
        world.clock.advance(minutes=6)
        restarted = world.service(refresh_fn=renew)
        restarted.tick()
        self.assertEqual(renew.calls, 1)
        readings = [r for r in world.history.readings(ALEX, world.clock.now) if r.source == "usage"]
        self.assertEqual(len(readings), 1)
        self.assertIn(ALEX, restarted.readings_since({})["covers"])

    def test_the_mac_never_writes_the_record_from_a_reading(self):
        world = World(self)
        path = world.record_path
        switching.record_confirmed(path, ALEX, {"claudeAiOauth": {"refreshToken": "an-older-renewal-key"}}, world.clock.now)
        before = path.read_bytes()
        service = world.service()
        service.reader.tick()
        self.assertEqual(path.read_bytes(), before)

    def test_covers_lists_only_accounts_the_devbox_read_in_the_last_ten_minutes(self):
        world = DevboxWorld(self, parked=(ALEX,))
        stale_record(world)
        expire_live(world)
        parked = dict(world.store.get(ALEX), expiresAt=1_600_000_000_000)
        world.store.put(ALEX, parked)
        blind = world.service()
        blind.tick()
        self.assertEqual(blind.readings_since({})["covers"], [])
        self.assertEqual(blind.covers(), [])
        fresh = DevboxWorld(self, parked=(ALEX, ORG))
        service = fresh.service()
        service.tick()
        self.assertEqual(service.covers(), sorted([WORK, ALEX, ORG]))
        fresh.usage.fail = True
        fresh.clock.advance(minutes=6)
        service.tick()
        self.assertEqual(service.covers(), sorted([WORK, ALEX, ORG]))
        fresh.clock.advance(minutes=5)
        service.tick()
        self.assertEqual(service.covers(), [])


def week_key(uuid, reset):
    return "week:{}:{}".format(uuid, reset.isoformat())


class ModelLimitTests(unittest.TestCase):
    def test_the_mac_shows_model_limits_the_devbox_read(self):
        from lifeline.anthropic import Limit, Usage, Window

        mac = World(self)
        dev = DevboxWorld(self, clock=mac.clock, parked=(ALEX, PERSONAL))
        reset = datetime(2026, 11, 26, 2, 0, tzinfo=UTC)
        real = dev.usage

        def usage(token):
            found = real(token)
            if dev.owners.get(token) != PERSONAL:
                return found
            scoped = Limit("weekly_scoped", "weekly", 4.0, reset, "Fable", None, "normal", False)
            return Usage(None, Window(57.0, reset), fetched_at=found.fetched_at, limits=(scoped,))

        dev.run(dev.service(usage_fn=usage))
        dev.service_obj.tick()
        status, body = dev.tcp("GET", "/readings")
        self.assertEqual(body["accounts"][PERSONAL]["model_limits"], [{"model": "Fable", "week": 4.0, "week_reset": iso(reset)}])
        service = mac.service(link=dev.link(clock=mac.clock))
        service.tick()
        accounts = {a["uuid"]: a for a in service.state()["accounts"]}
        self.assertIn(PERSONAL, service._covered)
        self.assertEqual(accounts[PERSONAL]["model_limits"], [{"model": "Fable", "week": 4.0, "week_reset": iso(reset)}])
        self.assertEqual(accounts[PERSONAL]["binding"], "week")


class MoreRemoteTests(unittest.TestCase):
    def test_the_devbox_reads_signin_ends_from_its_own_signins(self):
        end_cmd = START + timedelta(days=12)
        end_org = START + timedelta(days=2, hours=3)
        world = DevboxWorld(self, ends={WORK: end_cmd, ORG: end_org})
        service = world.service()
        service.tick()
        world.run(service)
        accounts = world.tcp("GET", "/readings")[1]["accounts"]
        self.assertEqual(accounts[WORK]["signin_ends"], iso(end_cmd))
        self.assertEqual(accounts[ORG]["signin_ends"], iso(end_org))
        self.assertIsNone(accounts[ALEX]["signin_ends"])
        self.assertIsNone(accounts[PERSONAL]["signin_ends"])
        self.assertNotIn("signed_in_at", accounts[WORK])
        stale = dict(world.signins[WORK], refreshTokenExpiresAt=int(end_cmd.timestamp() * 1000), expiresAt=int((end_cmd + timedelta(days=4)).timestamp() * 1000))
        world.slot.write({"claudeAiOauth": stale})
        service.tick()
        self.assertIsNone(world.tcp("GET", "/readings")[1]["accounts"][WORK]["signin_ends"])
        state = service.state()
        self.assertEqual(state["accounts"][3]["signin_ends"], iso(end_org))

    def test_readings_from_the_mac_are_saved_once(self):
        world = DevboxWorld(self)
        world.run(world.service())
        mine = {"uuid": PERSONAL, "source": "usage", "five": 4.0, "five_reset": iso(R_ALEX), "week": 31.0, "week_reset": iso(WEEK), "at": "2026-11-19T15:40:00.125000Z"}
        stranger = dict(mine, uuid="77777777-0000-4000-8000-000000000007")
        bad = dict(mine, five="lots")
        status, body = world.tcp("POST", "/readings", {"readings": [mine, stranger, bad]})
        self.assertEqual((status, body), (200, {"saved": 1}))
        self.assertEqual(world.tcp("POST", "/readings", {"readings": [mine]})[1], {"saved": 0})
        rows = world.history.readings(PERSONAL, START - timedelta(hours=1))
        self.assertEqual([(r.source, r.five) for r in rows], [("usage", 4.0)])
        for payload in ({"readings": "all"}, {"readings": [mine] * 201}, {"rows": []}):
            self.assertEqual(world.tcp("POST", "/readings", payload)[0], 400)
        world.assert_clean()

    def test_the_devbox_prunes_its_history_once_a_day(self):
        world = DevboxWorld(self)
        world.history.add_reading(PERSONAL, "usage", 1.0, None, 1.0, None, START - timedelta(days=91))
        service = world.service()
        service.tick()
        self.assertEqual(world.history.readings(PERSONAL, START - timedelta(days=200)), [])


class MoreLinkTests(unittest.TestCase):
    def pair(self, mac_figures=None, devbox_figures=None, **devbox_options):
        mac = World(self, figures=mac_figures)
        dev = DevboxWorld(self, clock=mac.clock, figures=devbox_figures, **devbox_options)
        dev.run(dev.service())
        return mac, dev

    def test_a_dead_devbox_signin_reaches_the_mac(self):
        mac, dev = self.pair()
        dead = dev.store.get(ORG)
        dev.owners.pop(dead["accessToken"])
        dev.owners.pop(dead["refreshToken"])
        dev.service_obj.tick()
        service = mac.service(link=dev.link(clock=mac.clock))
        service.tick()
        state = service.state()
        rows = {a["uuid"]: a for a in state["accounts"]}
        self.assertEqual((rows[ORG]["devbox_needs_signin"], rows[ORG]["devbox_cannot_read"]), (True, False))
        self.assertEqual(rows[WORK]["devbox_needs_signin"], False)
        self.assertFalse(rows[ORG]["needs_signin"])
        self.assertEqual({r["uuid"]: r for r in state["dev"]["rows"]}[ORG]["where"], "Needs a new sign-in.")

    def test_the_devbox_signin_end_reaches_the_mac_from_the_signin_itself(self):
        mac, dev = self.pair(ends={WORK: START + timedelta(days=2)})
        dev.service_obj.tick()
        service = mac.service(link=dev.link(clock=mac.clock))
        service.update_settings({"auto_switch": False})
        service.set_presence({"idle_seconds": 5, "locked": False, "asleep": False})
        service.tick()
        state = service.state()
        self.assertEqual(state["accounts"][1]["devbox_signin_ends"], iso(START + timedelta(days=2)))
        self.assertTrue(state["mac"]["icon_dot"])
        key = "signin-ends:devbox:{}:2026-11-21".format(WORK)
        self.assertIn(key, [n["id"] for n in state["notifications"]])


class Proxy:
    def __init__(self, case, upstream):
        self.upstream = upstream
        self.records = []
        self.lock = threading.Lock()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.2)
        self.address = self.listener.getsockname()
        self.stop = threading.Event()
        case.addCleanup(self.close)
        threading.Thread(target=self.accept, daemon=True).start()

    def close(self):
        self.stop.set()
        self.listener.close()

    def accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except OSError:
                continue
            threading.Thread(target=self.relay, args=(client,), daemon=True).start()

    def relay(self, client):
        upstream = socket.create_connection(self.upstream, timeout=10)
        up, down = [], []
        with self.lock:
            self.records.append(("up", up))
            self.records.append(("down", down))
        pumps = [
            threading.Thread(target=self.pump, args=(client, upstream, up), daemon=True),
            threading.Thread(target=self.pump, args=(upstream, client, down), daemon=True),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join(10)
        client.close()
        upstream.close()

    @staticmethod
    def pump(source, target, record):
        source.settimeout(10)
        try:
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    break
                record.append(chunk)
                target.sendall(chunk)
        except OSError:
            pass
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def texts(self, direction):
        with self.lock:
            return [b"".join(chunks).decode("utf-8", "replace") for kind, chunks in self.records if kind == direction]


class DevboxWiringTests(unittest.TestCase):
    def test_devbox_parts_use_the_linux_folders(self):
        world = DevboxWorld(self)
        with mock.patch.object(devbox.subprocess, "run", side_effect=AssertionError("no command may run")):
            parts = devbox.devbox_parts(system="linux")
        self.assertEqual(parts.home, world.home)
        self.assertEqual(parts.folder, world.home / ".local" / "share" / "com.example.lifeline")
        self.assertIsInstance(parts.slot, FileSlot)
        self.assertEqual(parts.slot.path, world.home / ".claude" / ".credentials.json")
        self.assertIsInstance(parts.store, FileStore)
        self.assertEqual(parts.store.folder, world.home / ".config" / "com.example.lifeline" / "signins")
        self.assertEqual(parts.registry.path, world.home / ".local" / "share" / "com.example.lifeline" / "accounts.json")
        self.assertEqual(parts.history.path, world.home / ".local" / "share" / "com.example.lifeline" / "history.db")
        self.assertIsInstance(parts.link, server.NullLink)
        self.assertIsNone(parts.runner)
        mac = devbox.devbox_parts(system="darwin")
        self.assertEqual(mac.folder, world.home / "Library" / "Application Support" / "com.example.lifeline")
        self.assertEqual((type(mac.slot).__name__, type(mac.store).__name__), ("MacKeychainSlot", "MacKeychainStore"))
        self.assertIsNotNone(mac.runner)

    def test_main_on_linux_serves_the_devbox_mode(self):
        seen = {}

        def fake_run():
            seen["ran"] = True
            return 0

        with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(devbox, "run", fake_run), mock.patch.object(
            server.logging, "basicConfig"
        ):
            self.assertEqual(server.main([]), 0)
        self.assertTrue(seen["ran"])



class DevboxOrganizationTests(unittest.TestCase):
    def world(self, team_registered=True, live=TEAM):
        world = DevboxWorld(self, registered=(ALEX, WORK, PERSONAL), live=None, parked=(ALEX, WORK, PERSONAL))
        if team_registered:
            world.registry.upsert(account(TEAM, 4))
        world.signins[live] = world.issue(live)
        world.slot.write({"claudeAiOauth": world.signins[live]})
        world.write_config(live)
        return world

    def test_the_devbox_names_its_live_organization(self):
        world = self.world()
        service = world.service()
        service.tick()
        self.assertEqual(service.account_info()["account_uuid"], TEAM)
        self.assertIs(service.account_info()["live_registered"], True)
        record = switching.read_record_entry(switching.record_path_for(world.folder / "lifeline.lock"))
        self.assertEqual(record["uuid"], TEAM)

    def test_an_organization_the_devbox_does_not_have_is_never_named_as_the_other(self):
        world = self.world(team_registered=False)
        service = world.service()
        service.tick()
        self.assertEqual(service.account_info()["account_uuid"], TEAM)
        self.assertIs(service.account_info()["live_registered"], False)
        self.assertIsNone(world.registry.by_uuid(TEAM))

    def test_an_unreadable_registry_names_no_live_account(self):
        world = self.world()
        service = world.service()
        service._devbox["account_uuid"] = WORK
        world.registry.path.write_text("{not json")
        self.assertIsNone(service._live_uuid())

    def test_an_unknown_live_organization_is_not_labelled(self):
        world = self.world()
        data = json.loads((world.home / ".claude.json").read_text())
        data["oauthAccount"].pop("organizationUuid")
        (world.home / ".claude.json").write_text(json.dumps(data))
        self.assertIsNone(world.service()._live_uuid())

    def test_the_registry_push_carries_each_accounts_login_and_organization(self):
        world = DevboxWorld(self, registered=(ALEX, WORK, PERSONAL))
        world.run(world.service())
        item = {"uuid": TEAM, "number": 4, "name": "Team Account", "short": "Team", "full": "Example Org Team Account",
                "email": "sam@example.invalid", "plan": "Max 5x", "login_uuid": PERSONAL, "org_uuid": TEAM_ORG}
        plain = {"uuid": WORK, "number": 2, "name": "Work Account", "short": "Work", "full": "Example Team's Account",
                 "email": "hello@example.invalid", "plan": "Max 20x"}
        status, body = world.tcp("POST", "/registry", {"accounts": [item, plain]})
        self.assertEqual((status, body), (200, {"accounts": 4}))
        saved = {a.uuid: a for a in world.registry.load()}
        self.assertEqual((saved[TEAM].login, saved[TEAM].org_uuid), (PERSONAL, TEAM_ORG))
        self.assertEqual(saved[WORK].org_uuid, pair_of(WORK)[1])
        for bad in (dict(item, org_uuid=5), dict(item, login_uuid="../x"), dict(item, org_uuid="")):
            with self.subTest(bad=bad):
                self.assertEqual(world.tcp("POST", "/registry", {"accounts": [bad]})[0], 400)
        self.assertEqual(devbox.registry_payload([saved[TEAM]])[0]["org_uuid"], TEAM_ORG)
        world.assert_clean()

    def test_the_devbox_takes_more_than_four_accounts(self):
        world = DevboxWorld(self, registered=(ALEX, WORK, PERSONAL))
        world.run(world.service())
        items = [
            {"uuid": f"77777777-0000-4000-8000-00000000000{n}", "number": n, "name": f"Account {n}", "short": f"A{n}",
             "full": f"Account {n}", "email": f"a{n}@example.invalid", "plan": "Max 5x"}
            for n in range(1, 7)
        ]
        status, body = world.tcp("POST", "/registry", {"accounts": items})
        self.assertEqual((status, body), (200, {"accounts": 6}))
        self.assertEqual(sorted(a.key for a in world.registry.load()), [1, 2, 3, 4, 5, 6])
        for bad in (0, -2, True):
            with self.subTest(bad=bad):
                self.assertEqual(world.tcp("POST", "/registry", {"accounts": [dict(items[0], number=bad)]})[0], 400)



class UnaddedLoginTests(unittest.TestCase):
    def test_the_devbox_reports_a_login_it_has_not_added(self):
        world = DevboxOrganizationTests.world(self, team_registered=False)
        service = world.service()
        service.tick()
        info = service.account_info()
        self.assertEqual((info["live_email"], info["live_organization"]), ("33333333@example.invalid", "Example Org"))
        added = DevboxOrganizationTests.world(self)
        service = added.service()
        service.tick()
        info = service.account_info()
        self.assertEqual((info["live_email"], info["live_organization"]), (None, None))

    def test_the_mac_names_the_devbox_login_from_its_known_list(self):
        mac = World(self)
        known = [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account",
                  "email": "33333333@example.invalid", "organization": "Example Org"}]
        (mac.folder / "known_accounts.json").write_text(json.dumps(known))
        dev = DevboxWorld(self, clock=mac.clock, live=TEAM)
        dev.run(dev.service())
        dev.service_obj.tick()
        service = mac.service(link=dev.link(clock=mac.clock))
        service.tick()
        state = service.state()
        self.assertEqual(state["dev"]["head"], "The other computer uses Team Account, which Lifeline has not added yet.")
        self.assertEqual(state["dev"]["unadded_known"], {"name": "Team Account", "short": "Team", "email": "33333333@example.invalid"})
        (mac.folder / "known_accounts.json").write_text("[]")
        self.assertEqual(service.state()["dev"]["head"], "The other computer uses an account Lifeline has not added yet.")
        self.assertIsNone(service.state()["dev"]["unadded_known"])



class EachSideMostRoomTests(unittest.TestCase):
    def sides(self, parked):
        figures = dict(CALM)
        figures[ALEX] = (59.0, R_ALEX, 30.0, WEEK)
        figures[PERSONAL] = (10.0, R_WORK, 93.0, WEEK)
        mac = World(self, figures=figures)
        dev = DevboxWorld(self, clock=mac.clock, figures=figures, live=None, parked=parked)
        dev.run(dev.service())
        dev.service_obj.tick()
        service = mac.service(link=dev.link(clock=mac.clock))
        service.tick()
        return service.state()

    def test_tonight_the_devbox_badges_nothing(self):
        state = self.sides(parked=())
        self.assertEqual([r["uuid"] for r in state["dev"]["rows"] if r["is_best"]], [])

    def test_a_devbox_with_sign_ins_and_nothing_in_use_badges_alex(self):
        state = self.sides(parked=(ALEX, PERSONAL))
        self.assertEqual([r["uuid"] for r in state["dev"]["rows"] if r["is_best"]], [ALEX])



class RealDevboxSigninTests(unittest.TestCase):
    def test_the_state_names_only_the_sign_ins_the_devbox_holds(self):
        mac = World(self)
        dev = DevboxWorld(self, clock=mac.clock, live=None, parked=(ALEX,))
        dev.run(dev.service())
        dev.service_obj.tick()
        service = mac.service(link=dev.link(clock=mac.clock))
        service.tick()
        accounts = {a["uuid"]: a for a in service.state()["accounts"]}
        self.assertIs(accounts[ALEX]["devbox_signed_in"], True)
        self.assertIs(accounts[ALEX]["devbox_needs_signin"], False)
        self.assertIs(accounts[PERSONAL]["devbox_signed_in"], False)
        self.assertIsNone(accounts[PERSONAL]["devbox_needs_signin"])
        self.assertIsNone(accounts[PERSONAL]["devbox_cannot_read"])
        self.assertIsNone(accounts[PERSONAL]["devbox_signin_ends"])
        self.assertTrue(accounts[PERSONAL]["signin_text"].endswith(", and not yet on the other computer."), accounts[PERSONAL]["signin_text"])
        self.assertNotIn("not yet", accounts[ALEX]["signin_text"])
        self.assertIn("on the other computer", accounts[ALEX]["signin_text"])


if __name__ == "__main__":
    unittest.main()
