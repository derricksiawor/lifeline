import copy
import email.utils
import http.client
import json
import math
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from lifeline import tls

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA = "oauth-2025-04-20"
USER_AGENT = "lifeline"
TIMEOUT_S = 20
REPLY_LIMIT = 4 * 1024 * 1024
DEFAULT_RETRY_AFTER_S = 300
FALLBACK_LIFETIME_S = 8 * 3600


class AnthropicError(Exception):
    pass


class RateLimited(AnthropicError):
    def __init__(self, retry_after_s):
        super().__init__(f"Rate limited, retry after {retry_after_s} seconds")
        self.retry_after_s = retry_after_s


class SignInDead(AnthropicError):
    def __init__(self):
        super().__init__("The sign-in was refused and needs to be replaced")


class NotAllowed(AnthropicError):
    def __init__(self):
        super().__init__("The sign-in may not read this address")


class Unavailable(AnthropicError):
    pass


class CertificateFailed(Unavailable):
    def __init__(self):
        super().__init__("Lifeline could not check Anthropic's certificate, so it did not connect.")


class UnexpectedReply(AnthropicError):
    pass


@dataclass(frozen=True)
class Profile:
    account_uuid: str
    email: str
    org_uuid: str
    rate_limit_tier: str | None


@dataclass(frozen=True)
class Window:
    utilization: float
    resets_at: datetime | None


@dataclass(frozen=True)
class Limit:
    kind: str
    group: str | None
    percent: float
    resets_at: datetime | None
    model: str | None
    surface: str | None
    severity: str | None
    active: bool | None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    five_hour: Window | None
    seven_day: Window | None
    extra: dict = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    limits: tuple = ()


@dataclass(frozen=True)
class Client:
    profile_url: str = PROFILE_URL
    usage_url: str = USAGE_URL
    token_url: str = TOKEN_URL
    timeout_s: float = TIMEOUT_S


DEFAULT_CLIENT = Client()


def _seconds_until(text):
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, math.ceil((when - datetime.now(timezone.utc)).total_seconds()))


def _retry_after(headers):
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return DEFAULT_RETRY_AFTER_S
    text = str(raw).strip()
    try:
        value = int(text)
    except ValueError:
        value = _seconds_until(text)
    if value is None or value < 0:
        return DEFAULT_RETRY_AFTER_S
    return value


def _is_invalid_grant(body):
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return isinstance(data, dict) and data.get("error") == "invalid_grant"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener_lock = threading.Lock()
_built_opener = None
MAX_LIFETIME_S = 10 * 365 * 86400


def opener():
    global _built_opener
    with _opener_lock:
        if _built_opener is None:
            _built_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=tls.context()))
        return _built_opener


def _certificate(error):
    return isinstance(error, ssl.SSLCertVerificationError) or isinstance(
        getattr(error, "reason", None), ssl.SSLCertVerificationError
    )


def _send(request, timeout_s):
    try:
        with opener().open(request, timeout=timeout_s) as response:
            body = response.read(REPLY_LIMIT + 1)
            if len(body) <= REPLY_LIMIT and response.length:
                raise http.client.IncompleteRead(body)
    except urllib.error.HTTPError as err:
        status = err.code
        headers = err.headers
        try:
            body = err.read(REPLY_LIMIT)
        except (OSError, http.client.HTTPException):
            body = b""
        err.close()
        if status == 429:
            raise RateLimited(_retry_after(headers)) from None
        if status == 401 or (status == 400 and _is_invalid_grant(body)):
            raise SignInDead() from None
        if status == 403:
            raise NotAllowed() from None
        if status >= 500:
            raise Unavailable(f"Server error {status}") from None
        raise UnexpectedReply(f"Unexpected status {status}") from None
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
        if _certificate(error):
            raise CertificateFailed() from None
        raise Unavailable("Could not reach the server") from None
    if len(body) > REPLY_LIMIT:
        raise UnexpectedReply("The reply was too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise UnexpectedReply("The reply was not JSON") from None
    if not isinstance(data, dict):
        raise UnexpectedReply("The reply was not an object")
    return data


def _get(url, access_token, timeout_s):
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": BETA,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    return _send(request, timeout_s)


def _text(data, key, what):
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise UnexpectedReply(f"The reply has no {what}")
    return value


def profile(access_token, client=DEFAULT_CLIENT):
    data = _get(client.profile_url, access_token, client.timeout_s)
    account = data.get("account")
    org = data.get("organization")
    tier = org.get("rate_limit_tier") if isinstance(org, dict) else None
    if tier is not None and not isinstance(tier, str):
        raise UnexpectedReply("The reply has an unexpected rate limit tier")
    return Profile(
        account_uuid=_text(account, "uuid", "account id"),
        email=_text(account, "email", "email"),
        org_uuid=_text(org, "uuid", "organization id"),
        rate_limit_tier=tier,
    )


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _window(value, name):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UnexpectedReply(f"The {name} window has an unexpected shape")
    used = value.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not _finite(used) or used < 0:
        raise UnexpectedReply(f"The {name} window has an unexpected utilization")
    raw = value.get("resets_at")
    if raw is None:
        resets = None
    elif isinstance(raw, str):
        try:
            resets = datetime.fromisoformat(raw)
        except ValueError:
            raise UnexpectedReply(f"The {name} window has an unexpected reset time") from None
        if resets.tzinfo is None:
            resets = resets.replace(tzinfo=timezone.utc)
    else:
        raise UnexpectedReply(f"The {name} window has an unexpected reset time")
    return Window(utilization=float(used), resets_at=resets)


LIMIT_KEYS = frozenset({"kind", "group", "percent", "resets_at", "scope", "severity", "is_active"})
LIMIT_TEXT = 128
LIMIT_EXTRAS = 16
LIMIT_ROWS = 64


def _label(value):
    return value if isinstance(value, str) and value and len(value) <= LIMIT_TEXT and value.isprintable() else None


def _scoped_name(scope, key):
    part = scope.get(key) if isinstance(scope, dict) else None
    return _label(part.get("display_name")) if isinstance(part, dict) else None


def _limit(value):
    if not isinstance(value, dict) or _label(value.get("kind")) is None:
        return None
    percent = value.get("percent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not _finite(percent) or percent < 0:
        return None
    raw = value.get("resets_at")
    resets = None
    if raw is not None:
        try:
            resets = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            resets = None
        if resets is None:
            return None
        if resets.tzinfo is None:
            resets = resets.replace(tzinfo=timezone.utc)
    scope = value.get("scope")
    active = value.get("is_active")
    extra = {}
    for key, item in value.items():
        if key in LIMIT_KEYS or len(extra) >= LIMIT_EXTRAS or not _label(key):
            continue
        if item is None or isinstance(item, bool) or (isinstance(item, (int, float)) and _finite(item)) or _label(item):
            extra[key] = item
    return Limit(
        kind=value["kind"],
        group=_label(value.get("group")),
        percent=float(percent),
        resets_at=resets,
        model=_scoped_name(scope, "model"),
        surface=_scoped_name(scope, "surface"),
        severity=_label(value.get("severity")),
        active=active if isinstance(active, bool) else None,
        extra=extra,
    )


def _limits(value):
    if not isinstance(value, list):
        return ()
    return tuple(limit for limit in (_limit(item) for item in value[:LIMIT_ROWS]) if limit is not None)


def _from_limits(limits, kind):
    found = next((limit for limit in limits if limit.kind == kind), None)
    return None if found is None else Window(utilization=found.percent, resets_at=found.resets_at)


def usage(access_token, client=DEFAULT_CLIENT):
    data = _get(client.usage_url, access_token, client.timeout_s)
    limits = _limits(data.get("limits"))
    five = _window(data.get("five_hour"), "five hour")
    week = _window(data.get("seven_day"), "seven day")
    return Usage(
        five_hour=five if five is not None else _from_limits(limits, "session"),
        seven_day=week if week is not None else _from_limits(limits, "weekly_all"),
        extra={k: v for k, v in data.items() if k not in ("five_hour", "seven_day")},
        fetched_at=datetime.now(timezone.utc),
        limits=limits,
    )


def refresh(signin, client=DEFAULT_CLIENT):
    try:
        old = signin["claudeAiOauth"]["refreshToken"]
    except (KeyError, TypeError):
        raise ValueError("The sign-in has no renewal key") from None
    if not isinstance(old, str) or not old:
        raise ValueError("The sign-in has no renewal key")
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": old, "client_id": CLIENT_ID}
    ).encode()
    request = urllib.request.Request(
        client.token_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    data = _send(request, client.timeout_s)
    access = _text(data, "access_token", "access key")
    renewal = _text(data, "refresh_token", "renewal key")
    expires_in = data.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not _finite(expires_in) or not 0 < expires_in <= MAX_LIFETIME_S:
        expires_in = FALLBACK_LIFETIME_S
    scope = data.get("scope")
    scopes = scope.split() if isinstance(scope, str) else []
    new = copy.deepcopy(signin)
    oauth = new["claudeAiOauth"]
    oauth["accessToken"] = access
    oauth["refreshToken"] = renewal
    moment = time.time() * 1000
    oauth["expiresAt"] = int(moment + expires_in * 1000)
    ends_in = data.get("refresh_token_expires_in")
    if isinstance(ends_in, bool) or not isinstance(ends_in, (int, float)) or not _finite(ends_in) or not 0 < ends_in <= MAX_LIFETIME_S:
        oauth.pop("refreshTokenExpiresAt", None)
    else:
        oauth["refreshTokenExpiresAt"] = int(moment + ends_in * 1000)
    if scopes:
        oauth["scopes"] = scopes
    return new
