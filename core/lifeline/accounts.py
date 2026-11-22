import json
import math
import os
import platform
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lifeline import ident

TOKEN_KEYS = frozenset({"accesstoken", "refreshtoken", "access_token", "refresh_token"})
END_KEY = "refreshTokenExpiresAt"
ACCESS_END_KEY = "expiresAt"
END_HORIZON = timedelta(days=3650)
STALE_AFTER = timedelta(days=3)
LOGIN_KEY = "accountUuid"
ORG_KEY = "organizationUuid"
ORG_NAME_KEY = "organizationName"
KNOWN_NAME = "known_accounts.json"
KNOWN_KEYS = ("name", "short", "full", "email", "organization")
PERSONAL = "personal"
PERSONAL_SUFFIX = "'s Organization"

PLAN_LABELS = {
    "default_claude_max_5x": "Max 5x",
    "default_claude_max_20x": "Max 20x",
}


class RegistryError(ValueError):
    pass


@dataclass
class Account:
    uuid: str
    email: str
    name: str
    short: str
    full: str
    plan: str
    key: int
    oauth_account: dict = field(default_factory=dict)
    signed_in_at: str | None = None
    signin_ends: str | None = None
    org_uuid: str | None = None
    login_uuid: str | None = None

    @property
    def login(self):
        return self.login_uuid or self.uuid

    @property
    def organization(self):
        return _text(self.oauth_account.get(ORG_NAME_KEY)) if isinstance(self.oauth_account, dict) else None


def _text(value):
    return value if isinstance(value, str) and value else None


def stated(details):
    if not isinstance(details, dict):
        return None, None
    return _text(details.get(LOGIN_KEY)), _text(details.get(ORG_KEY))


def with_org(account):
    if account.org_uuid is not None:
        return account
    org = stated(account.oauth_account)[1]
    return replace(account, org_uuid=org) if org is not None else account


def resolve(accounts, login, org):
    if not isinstance(login, str) or not login:
        return None
    same = [account for account in accounts if account.login == login]
    if org is None:
        return same[0] if len(same) == 1 and same[0].org_uuid is None else None
    exact = [account for account in same if account.org_uuid == org]
    if exact:
        return exact[0]
    unknown = [account for account in same if account.org_uuid is None]
    return unknown[0] if len(unknown) == 1 else None


def new_id(accounts, login, org):
    if not isinstance(login, str) or not login:
        return None
    if not any(account.uuid == login or account.login == login for account in accounts):
        return login
    if org is None:
        return None
    return f"{login}-{org}"


def identity(accounts, login, org):
    found = resolve(accounts, login, org)
    if found is not None:
        return found.uuid
    if org is None and any(account.login == login for account in accounts):
        return None
    return new_id(accounts, login, org)


def labels(details, account):
    login, org = stated(details)
    return account is not None and resolve([account], login, org) is account


def agrees(details, account):
    login, org = stated(details)
    if account is None or login != account.login:
        return False
    return org is None or account.org_uuid is None or org == account.org_uuid


def organization_kind(details):
    name = _text(details.get(ORG_NAME_KEY)) if isinstance(details, dict) else None
    if name is None or not name.strip():
        return None
    return PERSONAL if name.strip().endswith(PERSONAL_SUFFIX) else name.strip()


def load_known(path):
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return []
    if not isinstance(items, list):
        return []
    found = []
    for item in items:
        if isinstance(item, dict) and all(isinstance(item.get(key), str) and item[key].strip() for key in KNOWN_KEYS):
            found.append({key: item[key].strip() for key in KNOWN_KEYS})
    return found


def known_entry(known, email, kind):
    email = email.strip().lower() if isinstance(email, str) else ""
    same = [entry for entry in known if entry["email"].lower() == email]
    if kind is None:
        return same[0] if len(same) == 1 else None
    return next((entry for entry in same if entry["organization"].lower() == kind.lower()), None)


def known_of(account, known):
    return known_entry(known, account.email, organization_kind(account.oauth_account))


def _millis(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def signin_end(oauth, now=None):
    if not isinstance(oauth, dict):
        return None
    end = _millis(oauth.get(END_KEY))
    now = now if now is not None else datetime.now(timezone.utc)
    horizon = (now + END_HORIZON).timestamp() * 1000
    if end is None or end <= 0 or end > horizon:
        return None
    access = _millis(oauth.get(ACCESS_END_KEY))
    if access is not None and access > end + STALE_AFTER.total_seconds() * 1000:
        return None
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=end)


def signin_end_text(oauth, now=None):
    end = signin_end(oauth, now)
    return None if end is None else end.strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_label(tier):
    return PLAN_LABELS.get(tier) or (tier if tier in PLAN_LABELS.values() else "")


def default_registry_path(home=None, system=None):
    return ident.data_folder(home, system or platform.system()) / "accounts.json"


def _write_private(path, data, prefix=".lifeline-"):
    path = Path(path)
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=prefix, suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def strip_tokens(value):
    if isinstance(value, dict):
        return {k: strip_tokens(v) for k, v in value.items() if str(k).lower() not in TOKEN_KEYS}
    if isinstance(value, list):
        return [strip_tokens(v) for v in value]
    return value


class Registry:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_registry_path()

    def load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            items = json.loads(raw)
        except ValueError as error:
            raise RegistryError(f"Registry file is not valid JSON: {error}") from error
        if not isinstance(items, list):
            raise RegistryError("Registry file must hold a list of accounts.")
        accounts = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise RegistryError(f"Account {index} is not an object.")
            try:
                accounts.append(with_org(Account(**item)))
            except TypeError as error:
                raise RegistryError(f"Account {index} is malformed: {error}") from error
        return accounts

    def save(self, accounts):
        accounts = [with_org(account) for account in accounts]
        seen_uuids = set()
        seen_keys = set()
        seen_pairs = set()
        for account in accounts:
            if isinstance(account.key, bool) or not isinstance(account.key, int) or account.key < 1:
                raise RegistryError(f"Account number {account.key!r} must be a whole number, 1 or more.")
            if account.uuid in seen_uuids:
                raise RegistryError(f"The list of accounts holds {account.name} twice.")
            if account.key in seen_keys:
                raise RegistryError(f"Two accounts have the number {account.key}.")
            pair = (account.login, account.org_uuid)
            if pair in seen_pairs:
                raise RegistryError(f"Two accounts have the same login and organization, {account.login}.")
            seen_uuids.add(account.uuid)
            seen_keys.add(account.key)
            seen_pairs.add(pair)
        records = []
        for account in accounts:
            record = asdict(account)
            record["oauth_account"] = strip_tokens(record["oauth_account"])
            records.append(record)
        text = json.dumps(records, indent=2, sort_keys=True) + "\n"
        _write_private(self.path, text.encode("utf-8"), prefix=".accounts-")

    def by_uuid(self, uuid):
        return next((a for a in self.load() if a.uuid == uuid), None)

    def find(self, login, org):
        return resolve(self.load(), login, org)

    def by_key(self, n):
        return next((a for a in self.load() if a.key == n), None)

    def upsert(self, account):
        accounts = self.load()
        for index, existing in enumerate(accounts):
            if existing.uuid == account.uuid:
                accounts[index] = account
                break
        else:
            accounts.append(account)
        self.save(accounts)
