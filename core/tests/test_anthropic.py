import email.utils
import json
import time
import unittest
from datetime import datetime, timedelta, timezone

from lifeline import anthropic
from lifeline.anthropic import (
    Client,
    NotAllowed,
    RateLimited,
    SignInDead,
    Unavailable,
    UnexpectedReply,
)
from tests.fake_anthropic import FakeAnthropic
import ssl
import urllib.error
import urllib.request
from unittest import mock
from lifeline import tls

ACCESS = "sk-ant-oat01-ACCESS-SECRET-aaaaaaaa"
RENEWAL = "sk-ant-ort01-RENEWAL-SECRET-bbbbbbbb"
NEW_ACCESS = "sk-ant-oat01-NEWACCESS-SECRET-cccccccc"
NEW_RENEWAL = "sk-ant-ort01-NEWRENEWAL-SECRET-dddddddd"
SECRETS = (ACCESS, RENEWAL, NEW_ACCESS, NEW_RENEWAL)

PROFILE = "/api/oauth/profile"
USAGE = "/api/oauth/usage"
TOKEN = "/v1/oauth/token"


def signin():
    return {
        "claudeAiOauth": {
            "accessToken": ACCESS,
            "refreshToken": RENEWAL,
            "expiresAt": 1,
            "scopes": ["user:profile"],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        },
        "mcpOAuth": {"keep": "me"},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.server = FakeAnthropic().start()
        self.addCleanup(self.server.stop)
        self.client = Client(
            profile_url=self.server.base + PROFILE,
            usage_url=self.server.base + USAGE,
            token_url=self.server.base + TOKEN,
            timeout_s=2,
        )

    def assertNoSecrets(self, exc):
        text = str(exc) + repr(exc) + repr(exc.args)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__context__ is None or exc.__suppress_context__)


class TestUsage(Base):
    def test_usage_parses_windows(self):
        self.server.script(USAGE, body={
            "five_hour": {"utilization": 42.5, "resets_at": "2026-11-19T15:10:00+00:00"},
            "seven_day": {"utilization": 7, "resets_at": "2026-11-24T00:00:00Z"},
        })
        before = datetime.now(timezone.utc)
        result = anthropic.usage(ACCESS, self.client)
        self.assertEqual(result.five_hour.utilization, 42.5)
        self.assertEqual(result.five_hour.resets_at, datetime(2026, 11, 19, 15, 10, tzinfo=timezone.utc))
        self.assertEqual(result.seven_day.utilization, 7.0)
        self.assertIsInstance(result.seven_day.utilization, float)
        self.assertEqual(result.seven_day.resets_at, datetime(2026, 11, 24, tzinfo=timezone.utc))
        self.assertEqual(result.extra, {})
        self.assertGreaterEqual(result.fetched_at, before)

    def test_usage_parses_unknown_fields(self):
        extra = {"seven_day_opus": {"utilization": 1}, "extra_usage": None, "new": [1, {"a": 2}]}
        self.server.script(USAGE, body={
            "five_hour": {"utilization": 1, "resets_at": None, "future": "x"},
            "seven_day": None,
            **extra,
        })
        result = anthropic.usage(ACCESS, self.client)
        self.assertEqual(result.extra, extra)
        self.assertIsNone(result.seven_day)
        self.assertIsNone(result.five_hour.resets_at)

    def test_the_limits_list_fills_a_missing_week_and_keeps_scoped_rows(self):
        self.server.script(USAGE, body={
            "five_hour": None,
            "seven_day": None,
            "limits": [
                {"kind": "weekly_all", "group": "weekly", "percent": 57, "resets_at": "2026-11-26T01:59:59.678Z",
                 "severity": "normal", "is_active": True, "scope": None},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "resets_at": "2026-11-26T02:00:00Z",
                 "severity": "normal", "is_active": False, "scope": {"model": {"display_name": "Fable"}},
                 "allowance_share": 50, "nested": {"x": 1}},
                {"kind": "weekly_scoped", "percent": "57"},
                {"kind": "weekly_scoped", "percent": True},
                {"kind": "weekly_scoped", "percent": 3, "resets_at": "not a time"},
                "junk",
            ],
        })
        result = anthropic.usage(ACCESS, self.client)
        self.assertIsNone(result.five_hour)
        self.assertEqual(result.seven_day, anthropic.Window(57.0, datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=timezone.utc)))
        self.assertEqual([limit.kind for limit in result.limits], ["weekly_all", "weekly_scoped"])
        scoped = result.limits[1]
        self.assertEqual(
            (scoped.group, scoped.percent, scoped.resets_at, scoped.model, scoped.surface, scoped.severity, scoped.active),
            ("weekly", 0.0, datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc), "Fable", None, "normal", False),
        )
        self.assertEqual(scoped.extra, {"allowance_share": 50})

    def test_a_session_row_fills_a_missing_five_hour_window_and_the_windows_win_otherwise(self):
        self.server.script(USAGE, body={
            "five_hour": None,
            "seven_day": {"utilization": 20, "resets_at": "2026-11-24T00:00:00Z"},
            "limits": [
                {"kind": "session", "group": "session", "percent": 30, "resets_at": "2026-11-19T17:00:00Z"},
                {"kind": "weekly_all", "group": "weekly", "percent": 99, "resets_at": "2026-11-25T00:00:00Z"},
            ],
        })
        result = anthropic.usage(ACCESS, self.client)
        self.assertEqual(result.five_hour, anthropic.Window(30.0, datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)))
        self.assertEqual(result.seven_day, anthropic.Window(20.0, datetime(2026, 11, 24, tzinfo=timezone.utc)))

    def test_every_window_null(self):
        self.server.script(USAGE, body={"five_hour": None, "seven_day": None})
        result = anthropic.usage(ACCESS, self.client)
        self.assertIsNone(result.five_hour)
        self.assertIsNone(result.seven_day)

    def test_windows_missing_entirely(self):
        self.server.script(USAGE, body={"other": 1})
        result = anthropic.usage(ACCESS, self.client)
        self.assertIsNone(result.five_hour)
        self.assertIsNone(result.seven_day)
        self.assertEqual(result.extra, {"other": 1})

    def test_boundaries_accepted(self):
        self.server.script(USAGE, body={
            "five_hour": {"utilization": 0, "resets_at": None},
            "seven_day": {"utilization": 100, "resets_at": None},
        })
        result = anthropic.usage(ACCESS, self.client)
        self.assertEqual(result.five_hour.utilization, 0.0)
        self.assertEqual(result.seven_day.utilization, 100.0)

    def test_utilization_over_100_passes_through(self):
        self.server.script(USAGE, body={
            "five_hour": {"utilization": 100.4, "resets_at": None},
            "seven_day": {"utilization": 130, "resets_at": None},
        })
        result = anthropic.usage(ACCESS, self.client)
        self.assertEqual(result.five_hour.utilization, 100.4)
        self.assertEqual(result.seven_day.utilization, 130.0)

    def test_malformed_windows_raise_unexpected_reply(self):
        bad = [
            "text",
            5,
            [],
            {},
            {"utilization": "5"},
            {"utilization": None},
            {"utilization": True},
            {"utilization": -1},
            {"utilization": float("nan")},
            {"utilization": float("inf")},
            {"utilization": "50"},
            {"utilization": 5, "resets_at": 12},
            {"utilization": 5, "resets_at": "not a time"},
        ]
        for window in bad:
            for key in ("five_hour", "seven_day"):
                with self.subTest(window=window, key=key):
                    server = FakeAnthropic().start()
                    self.addCleanup(server.stop)
                    server.script(USAGE, body=json.dumps({key: window}))
                    client = Client(usage_url=server.base + USAGE, timeout_s=2)
                    with self.assertRaises(UnexpectedReply):
                        anthropic.usage(ACCESS, client)

    def test_non_object_and_non_json_replies(self):
        for body in ("[1, 2]", "null", "not json", "", "{"):
            with self.subTest(body=body):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(USAGE, body=body)
                client = Client(usage_url=server.base + USAGE, timeout_s=2)
                with self.assertRaises(UnexpectedReply):
                    anthropic.usage(ACCESS, client)

    def test_a_reply_over_the_cap_is_refused_without_reading_it_all(self):
        self.assertEqual(anthropic.REPLY_LIMIT, 4 * 1024 * 1024)
        with mock.patch.object(anthropic, "REPLY_LIMIT", 1000):
            padded = json.dumps({"five_hour": None, "seven_day": None, "pad": "x" * 2000})
            self.server.script(USAGE, body=padded)
            self.server.script(USAGE, body={"five_hour": None, "seven_day": None})
            with self.assertRaises(UnexpectedReply):
                anthropic.usage(ACCESS, self.client)
            anthropic.usage(ACCESS, self.client)

    def test_headers_sent(self):
        self.server.script(USAGE, body={"five_hour": None, "seven_day": None})
        self.server.script(PROFILE, body=profile_body())
        anthropic.usage(ACCESS, self.client)
        anthropic.profile(ACCESS, self.client)
        for path in (USAGE, PROFILE):
            sent = self.server.requests_to(path)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].method, "GET")
            self.assertEqual(sent[0].headers["authorization"], f"Bearer {ACCESS}")
            self.assertEqual(sent[0].headers["anthropic-beta"], "oauth-2025-04-20")
            self.assertEqual(sent[0].headers["user-agent"], "lifeline")
            self.assertEqual(sent[0].body, b"")


def profile_body(tier="default_claude_max_5x"):
    return {
        "account": {"uuid": "acc-1", "email": "a@example.com", "full_name": "A"},
        "organization": {"uuid": "org-1", "rate_limit_tier": tier, "name": "O"},
    }


class TestProfile(Base):
    def test_profile_parses(self):
        self.server.script(PROFILE, body=profile_body())
        result = anthropic.profile(ACCESS, self.client)
        self.assertEqual(result.account_uuid, "acc-1")
        self.assertEqual(result.email, "a@example.com")
        self.assertEqual(result.org_uuid, "org-1")
        self.assertEqual(result.rate_limit_tier, "default_claude_max_5x")

    def test_profile_null_tier(self):
        self.server.script(PROFILE, body=profile_body(tier=None))
        self.assertIsNone(anthropic.profile(ACCESS, self.client).rate_limit_tier)

    def test_profile_bad_shapes(self):
        bad = [
            {},
            {"account": None, "organization": {"uuid": "o"}},
            {"account": {"uuid": "a", "email": "e"}, "organization": None},
            {"account": {"uuid": "a"}, "organization": {"uuid": "o"}},
            {"account": {"uuid": 5, "email": "e"}, "organization": {"uuid": "o"}},
            {"account": [], "organization": []},
            {"account": {"uuid": "a", "email": "e"}, "organization": {"uuid": "o", "rate_limit_tier": 3}},
        ]
        for body in bad:
            with self.subTest(body=body):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(PROFILE, body=body)
                client = Client(profile_url=server.base + PROFILE, timeout_s=2)
                with self.assertRaises(UnexpectedReply):
                    anthropic.profile(ACCESS, client)


class TestErrors(Base):
    def test_429_raises_with_retry_after(self):
        self.server.script(USAGE, status=429, headers={"Retry-After": "3600"}, body={"error": "x"})
        with self.assertRaises(RateLimited) as ctx:
            anthropic.usage(ACCESS, self.client)
        self.assertEqual(ctx.exception.retry_after_s, 3600)
        self.assertNoSecrets(ctx.exception)

    def test_429_without_retry_after_defaults_to_300(self):
        self.server.script(USAGE, status=429)
        with self.assertRaises(RateLimited) as ctx:
            anthropic.usage(ACCESS, self.client)
        self.assertEqual(ctx.exception.retry_after_s, 300)

    def test_429_with_http_date_retry_after(self):
        ahead = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3600), usegmt=True)
        cases = (
            (ahead, 3590, 3601),
            ("Wed, 21 Oct 2015 07:28:00 GMT", 0, 0),
            ("Wed, 21 Oct 2015 07:28:00 -0000", 0, 0),
        )
        for value, low, high in cases:
            with self.subTest(value=value):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(USAGE, status=429, headers={"Retry-After": value})
                client = Client(usage_url=server.base + USAGE, timeout_s=2)
                with self.assertRaises(RateLimited) as ctx:
                    anthropic.usage(ACCESS, client)
                self.assertIsInstance(ctx.exception.retry_after_s, int)
                self.assertGreaterEqual(ctx.exception.retry_after_s, low)
                self.assertLessEqual(ctx.exception.retry_after_s, high)
                self.assertNoSecrets(ctx.exception)

    def test_429_with_unusable_retry_after_defaults_to_300(self):
        for value in ("soon", "-5", "", "Wed, 32 Oct 2026 07:28:00 GMT", "1.5"):
            with self.subTest(value=value):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(USAGE, status=429, headers={"Retry-After": value})
                client = Client(usage_url=server.base + USAGE, timeout_s=2)
                with self.assertRaises(RateLimited) as ctx:
                    anthropic.usage(ACCESS, client)
                self.assertEqual(ctx.exception.retry_after_s, 300)

    def test_429_on_profile_and_refresh(self):
        self.server.script(PROFILE, status=429, headers={"Retry-After": "12"})
        self.server.script(TOKEN, status=429, headers={"Retry-After": "34"})
        with self.assertRaises(RateLimited) as a:
            anthropic.profile(ACCESS, self.client)
        with self.assertRaises(RateLimited) as b:
            anthropic.refresh(signin(), self.client)
        self.assertEqual((a.exception.retry_after_s, b.exception.retry_after_s), (12, 34))

    def test_401_raises_signin_dead(self):
        for path, call in (
            (USAGE, lambda: anthropic.usage(ACCESS, self.client)),
            (PROFILE, lambda: anthropic.profile(ACCESS, self.client)),
            (TOKEN, lambda: anthropic.refresh(signin(), self.client)),
        ):
            with self.subTest(path=path):
                self.server.script(path, status=401, body={"error": "unauthorized"})
                with self.assertRaises(SignInDead) as ctx:
                    call()
                self.assertNoSecrets(ctx.exception)

    def test_invalid_grant_raises_signin_dead(self):
        self.server.script(TOKEN, status=400, body={"error": "invalid_grant", "error_description": "Refresh token not found"})
        with self.assertRaises(SignInDead) as ctx:
            anthropic.refresh(signin(), self.client)
        self.assertNoSecrets(ctx.exception)

    def test_other_400_is_not_signin_dead(self):
        for body in ({"error": "invalid_request"}, "not json", {"error": {"type": "invalid_grant"}}, ["invalid_grant"]):
            with self.subTest(body=body):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(TOKEN, status=400, body=body)
                client = Client(token_url=server.base + TOKEN, timeout_s=2)
                with self.assertRaises(UnexpectedReply):
                    anthropic.refresh(signin(), client)

    def test_500_raises_unavailable(self):
        for status in (500, 502, 503, 529):
            with self.subTest(status=status):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(USAGE, status=status, body="oops")
                client = Client(usage_url=server.base + USAGE, timeout_s=2)
                with self.assertRaises(Unavailable) as ctx:
                    anthropic.usage(ACCESS, client)
                self.assertNoSecrets(ctx.exception)

    def test_timeout_raises_unavailable(self):
        self.server.script(USAGE, body={"five_hour": None, "seven_day": None}, delay=1.5)
        client = Client(usage_url=self.server.base + USAGE, timeout_s=0.2)
        started = time.monotonic()
        with self.assertRaises(Unavailable) as ctx:
            anthropic.usage(ACCESS, client)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertNoSecrets(ctx.exception)

    def test_connection_refused_raises_unavailable(self):
        dead = FakeAnthropic().start()
        base = dead.base
        dead.stop()
        client = Client(usage_url=base + USAGE, profile_url=base + PROFILE, token_url=base + TOKEN, timeout_s=2)
        for call in (
            lambda: anthropic.usage(ACCESS, client),
            lambda: anthropic.profile(ACCESS, client),
            lambda: anthropic.refresh(signin(), client),
        ):
            with self.assertRaises(Unavailable) as ctx:
                call()
            self.assertNoSecrets(ctx.exception)

    def test_other_4xx_is_unexpected_reply(self):
        for status in (400, 404, 409, 422):
            with self.subTest(status=status):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(USAGE, status=status)
                client = Client(usage_url=server.base + USAGE, timeout_s=2)
                with self.assertRaises(UnexpectedReply):
                    anthropic.usage(ACCESS, client)

    def test_403_raises_not_allowed_never_signin_dead(self):
        for path, call in (
            (USAGE, lambda: anthropic.usage(ACCESS, self.client)),
            (PROFILE, lambda: anthropic.profile(ACCESS, self.client)),
        ):
            with self.subTest(path=path):
                self.server.script(path, status=403, body={"error": {"message": "missing scope user:profile"}})
                with self.assertRaises(NotAllowed) as ctx:
                    call()
                self.assertNotIsInstance(ctx.exception, SignInDead)
                self.assertNoSecrets(ctx.exception)

    def test_truncated_body_raises_unavailable(self):
        for path, call in (
            (USAGE, lambda c: anthropic.usage(ACCESS, c)),
            (PROFILE, lambda c: anthropic.profile(ACCESS, c)),
            (TOKEN, lambda c: anthropic.refresh(signin(), c)),
        ):
            with self.subTest(path=path):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(path, body="{\"a\": 1", claimed_length=500)
                client = Client(profile_url=server.base + PROFILE, usage_url=server.base + USAGE,
                                token_url=server.base + TOKEN, timeout_s=2)
                with self.assertRaises(Unavailable) as ctx:
                    call(client)
                self.assertNoSecrets(ctx.exception)

    def test_garbage_status_line_raises_unavailable(self):
        for raw in (b"garbage\r\n\r\n", b"HTTP/1.1 abc OK\r\n\r\n", b""):
            with self.subTest(raw=raw):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(USAGE, raw=raw)
                client = Client(usage_url=server.base + USAGE, timeout_s=2)
                with self.assertRaises(Unavailable) as ctx:
                    anthropic.usage(ACCESS, client)
                self.assertNoSecrets(ctx.exception)

    def test_truncated_error_body_still_maps_status(self):
        self.server.script(USAGE, status=429, headers={"Retry-After": "77"}, body="{", claimed_length=500)
        with self.assertRaises(RateLimited) as ctx:
            anthropic.usage(ACCESS, self.client)
        self.assertEqual(ctx.exception.retry_after_s, 77)
        self.server.script(TOKEN, status=400, body="{\"error\": \"invalid_g", claimed_length=500)
        with self.assertRaises(UnexpectedReply):
            anthropic.refresh(signin(), self.client)

    def test_deeply_nested_body_raises_unexpected_reply(self):
        self.server.script(USAGE, body="[" * 200000)
        with self.assertRaises(UnexpectedReply) as ctx:
            anthropic.usage(ACCESS, self.client)
        self.assertNoSecrets(ctx.exception)
        self.server.script(TOKEN, status=400, body="[" * 200000)
        with self.assertRaises(UnexpectedReply):
            anthropic.refresh(signin(), self.client)

    def test_huge_utilization_raises_unexpected_reply(self):
        self.server.script(USAGE, body='{"five_hour": {"utilization": 1' + "0" * 400 + ', "resets_at": null}}')
        with self.assertRaises(UnexpectedReply):
            anthropic.usage(ACCESS, self.client)

    def test_redirect_is_not_followed(self):
        target = FakeAnthropic().start()
        self.addCleanup(target.stop)
        self.server.script(USAGE, status=302, headers={"Location": target.base + USAGE})
        self.server.script(TOKEN, status=307, headers={"Location": target.base + TOKEN})
        with self.assertRaises(UnexpectedReply) as ctx:
            anthropic.usage(ACCESS, self.client)
        self.assertNoSecrets(ctx.exception)
        with self.assertRaises(UnexpectedReply):
            anthropic.refresh(signin(), self.client)
        self.assertEqual(target.requests, [])

    def test_default_timeout_is_twenty_seconds(self):
        self.assertEqual(anthropic.TIMEOUT_S, 20)
        self.assertEqual(anthropic.DEFAULT_CLIENT.timeout_s, 20)

    def test_default_addresses(self):
        self.assertEqual(anthropic.PROFILE_URL, "https://api.anthropic.com/api/oauth/profile")
        self.assertEqual(anthropic.USAGE_URL, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(anthropic.TOKEN_URL, "https://platform.claude.com/v1/oauth/token")

    def test_timeout_is_passed_to_every_request(self):
        from unittest import mock

        self.server.script(USAGE, body={"five_hour": None, "seven_day": None})
        built = anthropic.opener()
        real = built.open
        with mock.patch.object(built, "open", side_effect=real) as spy:
            anthropic.usage(ACCESS, Client(usage_url=self.server.base + USAGE))
        self.assertEqual(spy.call_args.kwargs["timeout"], 20)

    def test_secrets_absent_from_unexpected_reply_text(self):
        self.server.script(USAGE, body={"five_hour": {"utilization": ACCESS}})
        with self.assertRaises(UnexpectedReply) as ctx:
            anthropic.usage(ACCESS, self.client)
        self.assertNoSecrets(ctx.exception)

    def test_secrets_absent_from_results(self):
        self.server.script(PROFILE, body=profile_body())
        self.server.script(USAGE, body={"five_hour": None, "seven_day": None})
        self.assertNotIn(ACCESS, repr(anthropic.profile(ACCESS, self.client)))
        self.assertNotIn(ACCESS, repr(anthropic.usage(ACCESS, self.client)))


def token_reply(**over):
    body = {
        "access_token": NEW_ACCESS,
        "refresh_token": NEW_RENEWAL,
        "expires_in": 28800,
        "scope": "user:inference user:profile user:sessions:claude_code",
        "token_type": "Bearer",
    }
    body.update(over)
    return body


class TestRefresh(Base):
    def test_refresh_request_shape(self):
        self.server.script(TOKEN, body=token_reply())
        anthropic.refresh(signin(), self.client)
        sent = self.server.requests_to(TOKEN)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].method, "POST")
        self.assertEqual(sent[0].headers["content-type"], "application/json")
        self.assertEqual(sent[0].headers["user-agent"], "lifeline")
        self.assertEqual(sent[0].json(), {
            "grant_type": "refresh_token",
            "refresh_token": RENEWAL,
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        })

    def test_refresh_replaces_fields_and_keeps_the_rest(self):
        self.server.script(TOKEN, body=token_reply())
        old = signin()
        snapshot = json.loads(json.dumps(old))
        before = int(time.time() * 1000)
        new = anthropic.refresh(old, self.client)
        after = int(time.time() * 1000)
        self.assertEqual(old, snapshot)
        self.assertIsNot(new, old)
        oauth = new["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], NEW_ACCESS)
        self.assertEqual(oauth["refreshToken"], NEW_RENEWAL)
        self.assertEqual(oauth["scopes"], ["user:inference", "user:profile", "user:sessions:claude_code"])
        self.assertGreaterEqual(oauth["expiresAt"], before + 28800 * 1000)
        self.assertLessEqual(oauth["expiresAt"], after + 28800 * 1000)
        self.assertEqual(oauth["subscriptionType"], "max")
        self.assertEqual(oauth["rateLimitTier"], "default_claude_max_5x")
        self.assertEqual(new["mcpOAuth"], {"keep": "me"})
        new["mcpOAuth"]["keep"] = "changed"
        self.assertEqual(old["mcpOAuth"], {"keep": "me"})

    def test_refresh_without_scope_keeps_old_scopes(self):
        body = token_reply()
        del body["scope"]
        self.server.script(TOKEN, body=body)
        self.assertEqual(anthropic.refresh(signin(), self.client)["claudeAiOauth"]["scopes"], ["user:profile"])

    def test_refresh_keeps_a_rotated_pair_with_a_bad_lifetime_or_scope(self):
        fallback = anthropic.FALLBACK_LIFETIME_S * 1000
        scoped = ["user:inference", "user:profile", "user:sessions:claude_code"]
        cases = [
            (token_reply(expires_in="3600"), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            (token_reply(expires_in=0), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            (token_reply(expires_in=True), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            (token_reply(expires_in=-5), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            (token_reply(expires_in=10**9), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            (token_reply(expires_in=10**400), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            (token_reply(expires_in=None), NEW_ACCESS, NEW_RENEWAL, fallback, scoped),
            ('{"access_token": "a", "refresh_token": "b", "expires_in": NaN}', "a", "b", fallback, ["user:profile"]),
            ('{"access_token": "a", "refresh_token": "b", "expires_in": Infinity}', "a", "b", fallback, ["user:profile"]),
            ('{"access_token": "a", "refresh_token": "b", "expires_in": -Infinity}', "a", "b", fallback, ["user:profile"]),
            (token_reply(scope=["a"]), NEW_ACCESS, NEW_RENEWAL, 28800 * 1000, ["user:profile"]),
            (token_reply(scope="   "), NEW_ACCESS, NEW_RENEWAL, 28800 * 1000, ["user:profile"]),
            (token_reply(scope=7, expires_in="x"), NEW_ACCESS, NEW_RENEWAL, fallback, ["user:profile"]),
        ]
        for body, access, renewal, lifetime, scopes in cases:
            with self.subTest(body=body):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(TOKEN, body=body)
                client = Client(token_url=server.base + TOKEN, timeout_s=2)
                old = signin()
                before = int(time.time() * 1000)
                new = anthropic.refresh(old, client)
                after = int(time.time() * 1000)
                oauth = new["claudeAiOauth"]
                self.assertEqual(oauth["accessToken"], access)
                self.assertEqual(oauth["refreshToken"], renewal)
                self.assertGreaterEqual(oauth["expiresAt"], before + lifetime)
                self.assertLessEqual(oauth["expiresAt"], after + lifetime)
                self.assertEqual(oauth["scopes"], scopes)
                self.assertEqual(oauth["subscriptionType"], "max")
                self.assertEqual(new["mcpOAuth"], {"keep": "me"})
                self.assertEqual(old, signin())

    def test_refresh_takes_the_sign_in_end_from_the_reply_or_marks_it_unknown(self):
        old = signin()
        old["claudeAiOauth"]["refreshTokenExpiresAt"] = 1_900_000_000_000
        server = FakeAnthropic().start()
        self.addCleanup(server.stop)
        server.script(TOKEN, body=token_reply(refresh_token_expires_in=2592000))
        client = Client(token_url=server.base + TOKEN, timeout_s=2)
        before = int(time.time() * 1000)
        new = anthropic.refresh(old, client)
        after = int(time.time() * 1000)
        end = new["claudeAiOauth"]["refreshTokenExpiresAt"]
        self.assertIsInstance(end, int)
        self.assertGreaterEqual(end, before + 2592000 * 1000)
        self.assertLessEqual(end, after + 2592000 * 1000)
        self.assertEqual(old["claudeAiOauth"]["refreshTokenExpiresAt"], 1_900_000_000_000)
        for value in ("missing", None, "2592000", True, 0, -5, 10**12, 10**400):
            with self.subTest(value=value):
                body = token_reply() if value == "missing" else token_reply(refresh_token_expires_in=value)
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(TOKEN, body=body)
                new = anthropic.refresh(old, Client(token_url=server.base + TOKEN, timeout_s=2))
                self.assertNotIn("refreshTokenExpiresAt", new["claudeAiOauth"])
                self.assertEqual(new["claudeAiOauth"]["refreshToken"], NEW_RENEWAL)
        for text in (
            '{"access_token": "a", "refresh_token": "b", "refresh_token_expires_in": NaN}',
            '{"access_token": "a", "refresh_token": "b", "refresh_token_expires_in": Infinity}',
        ):
            with self.subTest(text=text):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(TOKEN, body=text)
                new = anthropic.refresh(old, Client(token_url=server.base + TOKEN, timeout_s=2))
                self.assertNotIn("refreshTokenExpiresAt", new["claudeAiOauth"])

    def test_refresh_bad_replies(self):
        bad = [
            token_reply(access_token=None),
            token_reply(refresh_token=""),
            token_reply(access_token=123),
            token_reply(refresh_token=["x"]),
            '{"access_token": "a", "expires_in": 3600}',
            {},
        ]
        for body in bad:
            with self.subTest(body=body):
                server = FakeAnthropic().start()
                self.addCleanup(server.stop)
                server.script(TOKEN, body=body)
                client = Client(token_url=server.base + TOKEN, timeout_s=2)
                old = signin()
                with self.assertRaises(UnexpectedReply) as ctx:
                    anthropic.refresh(old, client)
                self.assertNoSecrets(ctx.exception)
                self.assertEqual(old, signin())

    def test_refresh_without_renewal_key_sends_nothing(self):
        for bad in ({}, {"claudeAiOauth": {}}, {"claudeAiOauth": {"refreshToken": ""}}, {"claudeAiOauth": None}, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    anthropic.refresh(bad, self.client)
                self.assertNoSecrets(ctx.exception)
        self.assertEqual(self.server.requests, [])



class FailingOpener:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


class TlsTests(unittest.TestCase):
    def test_every_https_call_uses_the_shared_context(self):
        shared = ssl.create_default_context()
        saved = anthropic._built_opener
        self.addCleanup(setattr, anthropic, "_built_opener", saved)
        anthropic._built_opener = None
        with mock.patch.object(tls, "context", return_value=shared):
            opener = anthropic.opener()
            self.assertIs(anthropic.opener(), opener)
        found = [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler)]
        self.assertEqual(len(found), 1)
        self.assertIs(found[0]._context, shared)
        self.assertTrue(any(isinstance(handler, anthropic._NoRedirect) for handler in opener.handlers))

    def test_a_certificate_failure_is_its_own_error_and_never_an_internet_problem(self):
        bad = ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate")
        for error in (urllib.error.URLError(bad), bad):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(anthropic, "opener", return_value=FailingOpener(error)):
                    for call in (
                        lambda: anthropic.profile(ACCESS),
                        lambda: anthropic.usage(ACCESS),
                        lambda: anthropic.refresh(signin()),
                    ):
                        with self.assertRaises(anthropic.CertificateFailed) as caught:
                            call()
                        self.assertEqual(str(caught.exception), "Lifeline could not check Anthropic's certificate, so it did not connect.")
                        self.assertNotIn("reach", str(caught.exception))
                        self.assertIsInstance(caught.exception, Unavailable)
                        self.assertIsNone(caught.exception.__cause__)
                        self.assertTrue(caught.exception.__suppress_context__)

    def test_other_connection_failures_stay_unavailable(self):
        for error in (urllib.error.URLError(ConnectionRefusedError(61, "refused")), ssl.SSLError(1, "wrong version"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(anthropic, "opener", return_value=FailingOpener(error)):
                    with self.assertRaises(Unavailable) as caught:
                        anthropic.profile(ACCESS)
                    self.assertNotIsInstance(caught.exception, anthropic.CertificateFailed)


if __name__ == "__main__":
    unittest.main()
