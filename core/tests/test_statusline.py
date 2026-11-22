import io
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lifeline import statusline
from lifeline.accounts import Account, Registry

UUID = "11111111-2222-3333-4444-555555555555"
ROOT = Path(__file__).resolve().parents[2]
STATUS_NOW = datetime(2026, 11, 19, 16, 35)


def make_account(uuid=UUID, name="Alex's Account"):
    return Account(uuid=uuid, email="alex@example.com", name=name, short="Alex",
                   full=name, plan="default_claude_max_5x", key=1)


class Server:
    def __init__(self, path, answer=True, reply=None):
        self.path = path
        self.answer = answer
        self.reply = b"{}" if reply is None else json.dumps(reply).encode()
        self.requests = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(4)
        self.held = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.held.append(conn)
            data = b""
            conn.settimeout(2)
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                while len(body) < length:
                    body += conn.recv(4096)
                self.requests.append((head.decode(), body))
                if self.answer:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(self.reply)).encode() + b"\r\nConnection: close\r\n\r\n" + self.reply)
                    conn.close()
            except OSError:
                pass

    def close(self):
        self.listener.close()
        for conn in self.held:
            try:
                conn.close()
            except OSError:
                pass


class StatuslineCase(unittest.TestCase):
    def setUp(self):
        self.tmp_path = tempfile.mkdtemp(prefix="lifeline-test-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp_path)
        self.home = Path(self.tmp_path)
        self.socket_path = os.path.join(self.tmp_path, "s.sock")

    def write_registry(self, accounts):
        registry = Registry(statusline.registry_path(self.home))
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.save(accounts)

    def write_claude_json(self, uuid=UUID, email="alex@example.com"):
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": email}}))

    def run_line(self, payload, post=False, now=None, lifeline=True):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        env = {"LIFELINE_SOCKET": self.socket_path}
        out, err = io.StringIO(), io.StringIO()
        stand_in = None
        if lifeline and not os.path.exists(self.socket_path):
            stand_in = Server(self.socket_path, reply={"saved": True, "reading_named": True})
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = statusline.main(raw, self.home, env, now=now or STATUS_NOW)
        finally:
            if stand_in is not None:
                stand_in.close()
                Path(self.socket_path).unlink(missing_ok=True)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(code, 0)
        return out.getvalue()

    def setup_signed_in(self):
        self.write_registry([make_account()])
        self.write_claude_json()


class LineTests(StatuslineCase):
    def test_prints_line_from_rate_limits(self):
        self.setup_signed_in()
        reset = datetime(2026, 11, 19, 17, 0).timestamp()
        out = self.run_line({"session_id": "s1", "rate_limits": {
            "five_hour": {"used_percentage": 42.4, "resets_at": reset}}})
        self.assertEqual(out, "Alex's Account, 42% of its 5 hours used, resets today at 17:00\n")

    def test_a_week_only_account_prints_its_week(self):
        self.setup_signed_in()
        reset = datetime(2026, 11, 26, 1, 59, 59, 678000).timestamp()
        out = self.run_line({"session_id": "s1", "rate_limits": {
            "seven_day": {"used_percentage": 56.7, "resets_at": reset}}})
        self.assertEqual(out, "Alex's Account, 56% of its week used, resets next Thursday at 02:00\n")
        out = self.run_line({"rate_limits": {"five_hour": None, "seven_day": {"used_percentage": 5}}})
        self.assertEqual(out, "Alex's Account, 5% of its week used\n")

    def test_the_live_week_line_reads_next_thursday(self):
        reset = datetime(2026, 11, 26, 1, 59, 59, 678000).timestamp()
        line = statusline.build_line("Personal Account", None, {"used_percentage": 61.2, "resets_at": reset}, now=STATUS_NOW)
        self.assertEqual(line, "Personal Account, 61% of its week used, resets next Thursday at 02:00")

    def test_the_line_shows_the_window_with_the_least_room(self):
        five_at = datetime(2026, 11, 19, 18, 35).timestamp()
        week_at = datetime(2026, 11, 26, 2, 0).timestamp()
        cases = (
            ((10, 95), "Personal Account, 95% of its week used, resets next Thursday at 02:00"),
            ((0, 100), "Personal Account, 100% of its week used, resets next Thursday at 02:00"),
            ((42, 18), "Personal Account, 42% of its 5 hours used, resets today at 18:35"),
            ((60, 60), "Personal Account, 60% of its 5 hours used, resets today at 18:35"),
        )
        for (five, week), line in cases:
            with self.subTest(five=five, week=week):
                built = statusline.build_line(
                    "Personal Account",
                    {"used_percentage": five, "resets_at": five_at},
                    {"used_percentage": week, "resets_at": week_at},
                    now=STATUS_NOW,
                )
                self.assertEqual(built, line)

    def test_a_passed_reset_is_left_out(self):
        passed = datetime(2026, 11, 19, 10, 0).timestamp()
        week_at = datetime(2026, 11, 26, 2, 0).timestamp()
        line = statusline.build_line("Alex's Account", {"used_percentage": 42, "resets_at": passed}, None, now=STATUS_NOW)
        self.assertEqual(line, "Alex's Account, 0% of its 5 hours used")
        line = statusline.build_line("Alex's Account", None, {"used_percentage": 100, "resets_at": passed}, now=STATUS_NOW)
        self.assertEqual(line, "Alex's Account, 0% of its week used")
        line = statusline.build_line(
            "Alex's Account", {"used_percentage": 100, "resets_at": passed}, {"used_percentage": 30, "resets_at": week_at}, now=STATUS_NOW
        )
        self.assertEqual(line, "Alex's Account, 30% of its week used, resets next Thursday at 02:00")
        line = statusline.build_line(
            "Alex's Account",
            {"used_percentage": 20, "resets_at": datetime(2026, 11, 19, 18, 35).timestamp()},
            {"used_percentage": 90, "resets_at": passed},
            now=STATUS_NOW,
        )
        self.assertEqual(line, "Alex's Account, 20% of its 5 hours used, resets today at 18:35")

    def test_the_line_shows_the_account_figure_not_its_own_stale_post(self):
        self.setup_signed_in()
        week_at = datetime(2026, 11, 26, 1, 59, 59, 678000).astimezone()
        reply = {"saved": False, "account_uuid": UUID, "five": None, "five_reset": None, "week": 81.0, "week_reset": week_at.isoformat()}
        server = Server(self.socket_path, reply=reply)
        self.addCleanup(server.close)
        out = self.run_line({"rate_limits": {"seven_day": {"used_percentage": 75, "resets_at": week_at.timestamp()}}})
        self.assertEqual(out, "Alex's Account, 81% of its week used, resets next Thursday at 02:00\n")
        self.assertEqual(len(server.requests), 1)

    def test_a_reply_for_another_account_or_without_figures_shows_the_name_alone(self):
        self.setup_signed_in()
        week_at = datetime(2026, 11, 26, 1, 59, 59, 678000).astimezone()
        for reply in (
            {"saved": True, "account_uuid": "someone-else", "five": None, "week": 81.0, "week_reset": week_at.isoformat()},
            {"saved": True, "account_uuid": UUID},
            {"saved": True, "account_uuid": UUID, "five": None, "five_reset": None, "week": None, "week_reset": None},
            {"saved": True, "account_uuid": UUID, "reading_named": False},
        ):
            with self.subTest(reply=reply):
                server = Server(self.socket_path, reply=reply)
                out = self.run_line({"rate_limits": {"seven_day": {"used_percentage": 75, "resets_at": week_at.timestamp()}}})
                server.close()
                Path(self.socket_path).unlink(missing_ok=True)
                self.assertEqual(out, "Alex's Account\n")

    def test_the_sessions_own_numbers_show_only_when_lifeline_says_they_are_the_named_accounts(self):
        self.setup_signed_in()
        week_at = datetime(2026, 11, 26, 1, 59, 59, 678000).astimezone()
        server = Server(self.socket_path, reply={"saved": True, "account_uuid": UUID, "reading_named": True})
        self.addCleanup(server.close)
        out = self.run_line({"rate_limits": {"seven_day": {"used_percentage": 75, "resets_at": week_at.timestamp()}}})
        self.assertEqual(out, "Alex's Account, 75% of its week used, resets next Thursday at 02:00\n")

    def test_an_idle_session_after_a_switch_shows_the_new_accounts_figures(self):
        self.setup_signed_in()
        week_at = datetime(2026, 11, 24, 19, 0).astimezone()
        reply = {"saved": True, "account_uuid": UUID, "reading_named": False, "five": None, "week": 87.0, "week_reset": week_at.isoformat()}
        server = Server(self.socket_path, reply=reply)
        self.addCleanup(server.close)
        old = datetime(2026, 11, 20, 2, 0).astimezone()
        out = self.run_line({"rate_limits": {"seven_day": {"used_percentage": 99, "resets_at": old.timestamp()}}})
        self.assertEqual(out, "Alex's Account, 87% of its week used, resets on Tuesday at 19:00\n")

    def test_every_reset_names_its_day(self):
        now = datetime(2026, 11, 18, 20, 0)
        cases = (
            (datetime(2026, 11, 18, 23, 59, 29), "resets today at 23:59"),
            (datetime(2026, 11, 18, 23, 59, 45), "resets tomorrow at 00:00"),
            (datetime(2026, 11, 20, 2, 0), "resets on Friday at 02:00"),
            (datetime(2026, 11, 25, 2, 0), "resets next Wednesday at 02:00"),
            (datetime(2026, 11, 27, 2, 0), "resets on 27 November at 02:00"),
        )
        for moment, words in cases:
            with self.subTest(words=words):
                line = statusline.build_line("Alex's Account", {"used_percentage": 42, "resets_at": moment.timestamp()}, None, now=now)
                self.assertEqual(line, f"Alex's Account, 42% of its 5 hours used, {words}")

    def test_one_in_the_morning_reads_tomorrow(self):
        self.setup_signed_in()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 42.4, "resets_at": datetime(2026, 11, 20, 1, 0).timestamp()}}})
        self.assertEqual(out, "Alex's Account, 42% of its 5 hours used, resets tomorrow at 01:00\n")

    def test_a_shown_reset_rounds_to_the_nearest_minute(self):
        self.setup_signed_in()
        reset = datetime(2026, 11, 19, 16, 59, 59, 700000)
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": reset.timestamp()}}})
        self.assertTrue(out.endswith("resets today at 17:00\n"), out)
        reset = datetime(2026, 11, 19, 23, 59, 45)
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": reset.timestamp()}}})
        self.assertTrue(out.endswith("resets tomorrow at 00:00\n"), out)

    def test_prints_name_only_when_rate_limits_absent(self):
        self.setup_signed_in()
        self.assertEqual(self.run_line({"session_id": "s1"}), "Alex's Account\n")

    def test_lifeline_down_prints_the_name_alone(self):
        self.setup_signed_in()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": datetime(2026, 11, 19, 17, 0).timestamp()}}}, lifeline=False)
        self.assertEqual(out, "Alex's Account\n")

    def test_resets_at_iso(self):
        self.setup_signed_in()
        iso = "2026-11-19T15:30:00Z"
        local = datetime(2026, 11, 19, 15, 30, tzinfo=timezone.utc).astimezone()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": iso}}}, now=local - timedelta(seconds=1))
        self.assertTrue(out.endswith(f"resets today at {local:%H:%M}\n"), out)

    def test_resets_at_float_epoch(self):
        self.setup_signed_in()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": datetime(2026, 11, 19, 17, 0, 0, 700000).timestamp()}}})
        self.assertTrue(out.endswith("resets today at 17:00\n"), out)

    def test_no_reset_time_ends_after_used(self):
        self.setup_signed_in()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 10}}})
        self.assertEqual(out, "Alex's Account, 10% of its 5 hours used\n")

    def test_garbage_reset_time_ends_after_used(self):
        self.setup_signed_in()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": "soon"}}})
        self.assertEqual(out, "Alex's Account, 10% of its 5 hours used\n")

    def test_rounds_down(self):
        self.setup_signed_in()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 99.9}}})
        self.assertEqual(out, "Alex's Account, 99% of its 5 hours used\n")

    def test_clamps_above_100_and_below_0(self):
        self.setup_signed_in()
        high = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 140.5}}})
        low = self.run_line({"rate_limits": {"five_hour": {"used_percentage": -3}}})
        self.assertEqual(high, "Alex's Account, 100% of its 5 hours used\n")
        self.assertEqual(low, "Alex's Account, 0% of its 5 hours used\n")

    def test_non_numeric_percentage_prints_name_only(self):
        self.setup_signed_in()
        for bad in ("12", None, True, float("nan")):
            out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": bad}}})
            self.assertEqual(out, "Alex's Account\n")

    def test_unknown_account_prints_email(self):
        self.write_registry([make_account()])
        self.write_claude_json(uuid="99999999-0000-0000-0000-000000000000", email="other@example.com")
        self.assertEqual(self.run_line({}), "other@example.com\n")

    def test_missing_registry_prints_email(self):
        self.write_claude_json()
        self.assertEqual(self.run_line({}), "alex@example.com\n")

    def test_broken_registry_prints_email(self):
        path = statusline.registry_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.write_claude_json()
        self.assertEqual(self.run_line({}), "alex@example.com\n")

    def test_no_oauth_account_prints_not_signed_in(self):
        self.write_registry([make_account()])
        (self.home / ".claude.json").write_text("{}")
        self.assertEqual(self.run_line({}), "Not signed in\n")

    def test_missing_claude_json_prints_not_signed_in(self):
        self.assertEqual(self.run_line({}), "Not signed in\n")

    def test_broken_claude_json_prints_no_name(self):
        (self.home / ".claude.json").write_text("{broken")
        self.assertEqual(self.run_line({}), "\n")

    def test_bad_json_input_exits_zero_with_line(self):
        self.setup_signed_in()
        for raw in ("not json", "", "[1,2]", "null", "5"):
            self.assertEqual(self.run_line(raw), "Alex's Account\n")

    def test_rate_limits_wrong_shape(self):
        self.setup_signed_in()
        for value in ("x", 5, [], {"five_hour": 3}, {"five_hour": None}):
            self.assertEqual(self.run_line({"rate_limits": value}), "Alex's Account\n")


class SafetyTests(StatuslineCase):
    def test_deeply_nested_input_prints_line(self):
        self.setup_signed_in()
        self.assertEqual(self.run_line("[" * 100000), "Alex's Account\n")
        self.assertEqual(self.run_line('{"a":' * 100000), "Alex's Account\n")

    def test_oversize_input_ignored(self):
        self.setup_signed_in()
        big = json.dumps({"pad": "x" * (statusline.INPUT_LIMIT + 10),
                          "rate_limits": {"five_hour": {"used_percentage": 50}}})
        self.assertEqual(self.run_line(big), "Alex's Account\n")

    def test_half_written_claude_json_prints_no_figures_it_cannot_pair(self):
        self.write_registry([make_account()])
        (self.home / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "')
        server = Server(self.socket_path)
        self.addCleanup(server.close)
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 42.4, "resets_at": datetime(2026, 11, 20, 1, 0).timestamp()}}})
        self.assertEqual(out, "\n")
        self.assertIsNone(json.loads(server.requests[0][1])["account_uuid"])

    def test_half_written_claude_json_without_usage_prints_empty_line(self):
        (self.home / ".claude.json").write_text("{")
        self.assertEqual(self.run_line({}), "\n")

    def test_claude_json_completed_during_retry_prints_name(self):
        self.setup_signed_in()
        path = self.home / ".claude.json"
        good = path.read_text()
        path.write_text(good[:20])

        def finish():
            time.sleep(0.005)
            path.write_text(good)

        thread = threading.Thread(target=finish)
        thread.start()
        out = self.run_line({})
        thread.join()
        self.assertEqual(out, "Alex's Account\n")

    def test_valid_claude_json_without_oauth_account_stays_not_signed_in(self):
        (self.home / ".claude.json").write_text('{"other": 1}')
        self.assertEqual(self.run_line({"rate_limits": {"five_hour": {"used_percentage": 3}}}),
                         "Not signed in, 3% of its 5 hours used\n")

    def test_deeply_nested_claude_json_and_registry_degrade(self):
        (self.home / ".claude.json").write_text("[" * 100000)
        self.assertEqual(self.run_line({}), "\n")
        self.write_claude_json()
        path = statusline.registry_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("[" * 100000)
        self.assertEqual(self.run_line({}), "alex@example.com\n")

    def test_oversize_claude_json_skipped(self):
        self.write_registry([make_account()])
        (self.home / ".claude.json").write_text("{}")
        original = statusline.CLAUDE_JSON_LIMIT
        statusline.CLAUDE_JSON_LIMIT = 1
        self.addCleanup(setattr, statusline, "CLAUDE_JSON_LIMIT", original)
        self.assertEqual(self.run_line({"rate_limits": {"five_hour": {"used_percentage": 7}}}),
                         "7% of its 5 hours used\n")

    def test_registry_path_matches_accounts_module(self):
        from lifeline.accounts import default_registry_path
        for system in ("Darwin", "Linux"):
            self.assertEqual(statusline.registry_path(self.home, system),
                             default_registry_path(self.home, system))

    def test_launcher_with_missing_core_exits_zero_silently(self):
        folder = self.home / "bare" / "bin"
        folder.mkdir(parents=True)
        launcher = folder / "lifeline-statusline"
        shutil.copy(ROOT / "bin" / "lifeline-statusline", launcher)
        launcher.chmod(0o755)
        result = subprocess.run([str(launcher)], input="{}", capture_output=True, text=True,
                                env={"HOME": str(self.home), "PATH": os.environ["PATH"]}, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_launcher_with_broken_module_exits_zero_silently(self):
        folder = self.home / "broken"
        (folder / "bin").mkdir(parents=True)
        (folder / "core" / "lifeline").mkdir(parents=True)
        (folder / "core" / "lifeline" / "__init__.py").write_text("")
        (folder / "core" / "lifeline" / "statusline.py").write_text("raise RuntimeError\n")
        launcher = folder / "bin" / "lifeline-statusline"
        shutil.copy(ROOT / "bin" / "lifeline-statusline", launcher)
        launcher.chmod(0o755)
        result = subprocess.run([str(launcher)], input="{}", capture_output=True, text=True,
                                env={"HOME": str(self.home), "PATH": os.environ["PATH"]}, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


class PostTests(StatuslineCase):
    def test_stale_regular_file_at_socket_path(self):
        self.setup_signed_in()
        Path(self.socket_path).write_text("old")
        start = time.monotonic()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 5}}})
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertEqual(out, "Alex's Account\n")

    def test_server_closing_without_reply(self):
        self.setup_signed_in()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        listener.listen(1)
        self.addCleanup(listener.close)

        def close_at_once():
            conn, _ = listener.accept()
            conn.close()

        thread = threading.Thread(target=close_at_once, daemon=True)
        thread.start()
        start = time.monotonic()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 5}}})
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertEqual(out, "Alex's Account\n")

    def test_non_finite_numbers_sent_as_null(self):
        self.setup_signed_in()
        server = Server(self.socket_path)
        self.addCleanup(server.close)
        raw = '{"rate_limits":{"five_hour":{"used_percentage":NaN,"resets_at":Infinity},' \
              '"seven_day":{"used_percentage":-Infinity,"resets_at":5}}}'
        out = self.run_line(raw)
        self.assertEqual(out, "Alex's Account\n")
        body = json.loads(server.requests[0][1], parse_constant=lambda c: self.fail(c))
        self.assertEqual(body["five_hour"], {"used_percentage": None, "resets_at": None})
        self.assertEqual(body["seven_day"], {"used_percentage": None, "resets_at": 5})

    def test_post_body_matches_contract(self):
        self.setup_signed_in()
        server = Server(self.socket_path)
        self.addCleanup(server.close)
        payload = {"session_id": "sess-9", "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600, "extra": 1},
            "seven_day": {"used_percentage": 41.2, "resets_at": "2026-11-20T10:00:00Z"},
            "spend_limit": {"used_percentage": 1}}}
        before = datetime.now(timezone.utc)
        self.run_line(payload)
        after = datetime.now(timezone.utc)
        self.assertEqual(len(server.requests), 1)
        head, body = server.requests[0]
        self.assertTrue(head.startswith("POST /reading HTTP/1.1"))
        self.assertIn("content-type: application/json", head.lower())
        data = json.loads(body)
        at = datetime.fromisoformat(data.pop("at"))
        self.assertEqual(at.utcoffset().total_seconds(), 0)
        self.assertTrue(before.replace(microsecond=0) <= at <= after)
        self.assertEqual(data, {
            "source": "statusline",
            "session_id": "sess-9",
            "account_uuid": UUID,
            "org_uuid": None,
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41.2, "resets_at": "2026-11-20T10:00:00Z"},
        })

    def test_post_body_nulls(self):
        (self.home / ".claude.json").write_text("{}")
        server = Server(self.socket_path)
        self.addCleanup(server.close)
        self.run_line({})
        data = json.loads(server.requests[0][1])
        self.assertEqual(data["session_id"], None)
        self.assertEqual(data["account_uuid"], None)
        self.assertEqual(data["org_uuid"], None)
        self.assertEqual(data["five_hour"], None)
        self.assertEqual(data["seven_day"], None)

    def test_silent_server_abandoned_within_limit(self):
        self.setup_signed_in()
        server = Server(self.socket_path, answer=False)
        self.addCleanup(server.close)
        start = time.monotonic()
        out = self.run_line({"rate_limits": {"five_hour": {"used_percentage": 5}}})
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.6)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertEqual(out, "Alex's Account\n")

    def test_missing_socket_is_fast_and_silent(self):
        self.setup_signed_in()
        start = time.monotonic()
        self.run_line({"rate_limits": {"five_hour": {"used_percentage": 5}}})
        self.assertLess(time.monotonic() - start, 0.2)

    def test_socket_path_rules(self):
        self.assertEqual(statusline.socket_path(Path("/h"), {"LIFELINE_SOCKET": "/x/y.sock"}, "Darwin"), "/x/y.sock")
        self.assertEqual(statusline.socket_path(Path("/h"), {}, "Darwin"),
                         "/h/Library/Application Support/com.example.lifeline/lifeline.sock")
        self.assertEqual(statusline.socket_path(Path("/h"), {}, "Linux"), "/h/.local/share/com.example.lifeline/lifeline.sock")


class LauncherTests(StatuslineCase):
    def test_launcher_runs_through_symlink(self):
        self.setup_signed_in()
        link = self.home / "linked"
        link.symlink_to(ROOT / "bin" / "lifeline-statusline")
        env = {"HOME": str(self.home), "LIFELINE_SOCKET": self.socket_path, "PATH": os.environ["PATH"]}
        result = subprocess.run([str(link)], input=json.dumps({"session_id": "a"}), capture_output=True,
                                text=True, env=env, cwd=self.tmp_path, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "Alex's Account\n")

    def test_launcher_says_when_python_is_too_old_and_still_exits_zero(self):
        code = (
            "import sys; sys.version_info = (3, 9, 6, 'final', 0); path = sys.argv[1]; sys.argv = sys.argv[1:]; "
            "exec(compile(open(path).read(), path, 'exec'), {'__name__': '__main__', '__file__': path})"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "bin" / "lifeline-statusline")],
                                input="{}", capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "Lifeline needs Python 3.13 or newer, but python3 here is 3.9.\n", ""))

    def test_launcher_under_the_system_python(self):
        system = "/usr/bin/python3"
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        if not os.path.exists(system):
            self.skipTest("No system python3 on this computer.")
        probe = subprocess.run([system, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                               capture_output=True, text=True, env=env, timeout=60)
        version = probe.stdout.strip()
        if probe.returncode != 0 or not version or tuple(int(part) for part in version.split(".")) >= (3, 13):
            self.skipTest("The system python3 is not older than 3.13.")
        result = subprocess.run([str(ROOT / "bin" / "lifeline-statusline")], input="{}", capture_output=True,
                                text=True, env=env, timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"Lifeline needs Python 3.13 or newer, but python3 here is {version}.\n", ""))



PERSONAL_ORG = "b2b2b2b2-aaaa-4aaa-8aaa-000000000001"
TEAM_ORG = "f6f6f6f6-bbbb-4bbb-8bbb-000000000002"


class OrganizationLineTests(StatuslineCase):
    def write_two_organizations(self):
        personal = Account(uuid=UUID, email="sam@example.com", name="Personal Account", short="Personal",
                           full="Personal Account", plan="default_claude_max_5x", key=1,
                           oauth_account={"accountUuid": UUID, "organizationUuid": PERSONAL_ORG})
        team = Account(uuid=f"{UUID}-{TEAM_ORG}", email="sam@example.com", name="Team Account", short="Team",
                       full="Team Account", plan="default_claude_max_5x", key=2,
                       oauth_account={"accountUuid": UUID, "organizationUuid": TEAM_ORG}, login_uuid=UUID)
        self.write_registry([personal, team])

    def write_live(self, org):
        account = {"accountUuid": UUID, "emailAddress": "sam@example.com"}
        if org is not None:
            account["organizationUuid"] = org
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": account}))

    def test_the_post_names_the_login_and_its_organization(self):
        self.write_two_organizations()
        self.write_live(TEAM_ORG)
        server = Server(self.socket_path)
        self.addCleanup(server.close)
        self.run_line({"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1738425600}}})
        data = json.loads(server.requests[0][1])
        self.assertEqual((data["account_uuid"], data["org_uuid"]), (UUID, TEAM_ORG))

    def test_the_name_follows_the_organization_and_an_unknown_one_is_never_guessed(self):
        self.write_two_organizations()
        payload = {"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": (STATUS_NOW.astimezone() + timedelta(hours=2)).timestamp()}}}
        for org, name in ((TEAM_ORG, "Team Account"), (PERSONAL_ORG, "Personal Account"), (None, "sam@example.com"),
                          ("99999999-cccc-4ccc-8ccc-000000000003", "sam@example.com")):
            with self.subTest(org=org):
                self.write_live(org)
                self.assertEqual(self.run_line(payload), f"{name}, 20% of its 5 hours used, resets today at 18:35\n")

    def test_a_reply_for_the_other_organization_is_never_shown(self):
        self.write_two_organizations()
        self.write_live(TEAM_ORG)
        week_at = datetime(2026, 11, 26, 1, 59, 59, 678000).astimezone()
        payload = {"rate_limits": {"seven_day": {"used_percentage": 75, "resets_at": week_at.timestamp()}}}
        for reply_org, shown in ((PERSONAL_ORG, None), (TEAM_ORG, 81)):
            with self.subTest(reply_org=reply_org):
                reply = {"saved": True, "account_uuid": UUID, "org_uuid": reply_org, "five": None, "five_reset": None,
                         "week": 81.0, "week_reset": week_at.isoformat()}
                server = Server(self.socket_path, reply=reply)
                out = self.run_line(payload)
                server.close()
                Path(self.socket_path).unlink(missing_ok=True)
                expected = "Team Account\n" if shown is None else f"Team Account, {shown}% of its week used, resets next Thursday at 02:00\n"
                self.assertEqual(out, expected)


if __name__ == "__main__":
    unittest.main()
