import hashlib
import os
import pwd
import re
import shutil
import tempfile
import time
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from lifeline import ident, recovery, switch, writelock
from lifeline.accountfile import AccountFileError, read_oauth_account
from lifeline.accounts import Account, identity, new_id, plan_label, resolve, signin_end_text, stated
from lifeline.anthropic import CertificateFailed
from lifeline.locks import STORAGE_TIMEOUT, LockTimeout, SessionLocks, StorageLock, nudge_sessions
from lifeline.locks import claude_folder as find_claude_folder
from lifeline.slot import ITEM_NOT_FOUND, FileSlot, MacKeychainSlot, SecurityError, SlotError, login_keychain, run_security

SERVICE_PREFIX = "Claude Code-credentials-"
FOLDER_PREFIX = "lifeline-login-"
STALE_LOGIN_SECONDS = 3600.0
DELETE_REPEAT_LIMIT = 20
OAUTH_KEY = "claudeAiOauth"
KEYCHAIN_ACCOUNT = re.compile(r"[A-Za-z0-9._-]+")
FALLBACK_ACCOUNT = "claude-code-user"
CONFIG_VARIABLE = "CLAUDE_CONFIG_DIR"
LOGIN_DROPPED_PREFIXES = ("CLAUDE_", "ANTHROPIC_")
LOGIN_DROPPED_NAMES = frozenset({"CLAUDECODE"})
SAVED_IN_KEYCHAIN = "keychain"
SAVED_IN_FILE = "file"
SIGNIN_FILE = ".credentials.json"
YOUR_BROWSER = "Your browser"
ALREADY_HAS = "Lifeline already has {name}. {browser} signed in as {email}, so nothing changed."
ALREADY_HAS_PLAIN = "Lifeline already has {name}, so nothing changed."
ORG_NOT_KNOWN = (
    "Your browser signed in as {email}. Lifeline has an account for that login whose organization it does not "
    "know yet, so nothing changed. Sign in again to {name} first, so Lifeline learns its organization."
)
NEW_CERTIFICATE = "Lifeline could not check Anthropic's certificate, so it could not check the new sign-in. Nothing was added."
ORG_UNTOLD = (
    "Anthropic did not say which organization the new sign-in belongs to, and Lifeline already has an account "
    "with this login, so nothing was added."
)
OVERWRITTEN = "The new sign-in was saved, but another app changed Claude Code's account right after it was written."
KEPT_NEW = "Lifeline kept the new sign-in for this account."
KEPT_BOTH = "Lifeline kept its newer saved sign-in for this account and put the new one in its recovery folder."
NOT_KEPT = (
    "Another app changed Claude Code's account right after the new sign-in was written, "
    "and Lifeline could not keep the new sign-in. Sign in again."
)


class EnrollError(Exception):
    reason = None


class LoginFailure(Exception):
    pass


LOGIN_FAILED = "The browser sign-in did not finish, so nothing was added."
LIVE_NOT_ASKED = "Claude Code now uses a different account, so nothing was changed."


def hashed_service(folder):
    digest = hashlib.sha256(str(folder).encode("utf-8")).hexdigest()
    return SERVICE_PREFIX + digest[:8]


def login_root(home=None, system=None):
    return ident.data_folder(home, system) / "login"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def keychain_account(user):
    if isinstance(user, str) and KEYCHAIN_ACCOUNT.fullmatch(user):
        return user
    return FALLBACK_ACCOUNT


def login_user():
    name = os.environ.get("USER")
    if name:
        return name
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return None


def login_environment(folder, user):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(LOGIN_DROPPED_PREFIXES) and key not in LOGIN_DROPPED_NAMES
    }
    env[CONFIG_VARIABLE] = str(folder)
    env["USER"] = keychain_account(user)
    return env


def _user(user):
    return user if user is not None else login_user()


def _delete_once(runner, service):
    for _ in range(DELETE_REPEAT_LIMIT):
        try:
            runner(["delete-generic-password", "-s", service, login_keychain()])
        except SecurityError as error:
            return error.returncode == ITEM_NOT_FOUND
        except (SlotError, OSError):
            return False
    return False


def _delete_item(runner, service):
    return _delete_once(runner, service) or _delete_once(runner, service)


def _remove_folder(folder):
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _checked_place(saved_in):
    if saved_in not in (SAVED_IN_KEYCHAIN, SAVED_IN_FILE):
        raise ValueError("A new sign-in is saved in the Keychain or in a file.")
    return saved_in


def _drop_file(folder):
    try:
        os.unlink(folder / SIGNIN_FILE)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _cleanup(runner, service, folder, saved_in=SAVED_IN_KEYCHAIN):
    left = []
    if saved_in == SAVED_IN_KEYCHAIN and not _delete_item(runner, service):
        left.append(f"the temporary Keychain item named {service}")
    if not _remove_folder(folder):
        left.append(f"the temporary folder {folder}")
    return left


def _sweep(root, runner, clock=time.time, saved_in=SAVED_IN_KEYCHAIN):
    try:
        entries = list(root.glob(FOLDER_PREFIX + "*"))
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_dir() or entry.is_symlink() or clock() - entry.stat().st_mtime < STALE_LOGIN_SECONDS:
                continue
            service = hashed_service(entry.resolve())
        except OSError:
            continue
        if saved_in == SAVED_IN_KEYCHAIN:
            _delete_item(runner, service)
        _remove_folder(entry)


def _listing(left):
    return " and ".join(left)


def _read_new(folder, service, runner, user, saved_in):
    if saved_in == SAVED_IN_FILE:
        try:
            return FileSlot(folder / SIGNIN_FILE).read(), None
        except (SlotError, OSError, UnicodeDecodeError):
            return None, "The new sign-in could not be read from the login folder."
    try:
        return MacKeychainSlot(service=service, account=keychain_account(user), runner=runner).read(), None
    except (SlotError, OSError):
        return None, "The new sign-in could not be read from the Keychain."


def _collect(folder, service, run_login, runner, profile_fn, user, saved_in=SAVED_IN_KEYCHAIN):
    failed, reason = False, None
    try:
        run_login(folder)
    except LoginFailure as error:
        failed, reason = True, str(error)
    except Exception:
        failed = True
    if failed:
        error = EnrollError(LOGIN_FAILED)
        error.reason = reason
        raise error
    signin, problem = _read_new(folder, service, runner, user, saved_in)
    if problem:
        raise EnrollError(problem)
    oauth = signin.get(OAUTH_KEY) if isinstance(signin, dict) else None
    if (
        not isinstance(oauth, dict)
        or not isinstance(oauth.get("accessToken"), str)
        or not isinstance(oauth.get("refreshToken"), str)
        or not oauth["refreshToken"]
    ):
        raise EnrollError("The browser sign-in left no complete sign-in behind, so nothing was added.")
    details = None
    try:
        details = read_oauth_account(folder / ".claude.json")
    except AccountFileError:
        problem = "The new account details could not be read."
    if problem:
        raise EnrollError(problem)
    login, org = stated(details)
    if login is None:
        raise EnrollError("Claude did not say which account signed in, so nothing was added.")
    found = None
    try:
        found = profile_fn(oauth["accessToken"])
    except CertificateFailed:
        problem = NEW_CERTIFICATE
    except Exception:
        problem = "The new sign-in could not be checked with Anthropic, so nothing was added."
    if problem:
        raise EnrollError(problem)
    said = _org_said(found)
    if found.account_uuid != login or (org is not None and said is not None and org != said):
        raise EnrollError("The new sign-in does not belong to the account it was said to be, so nothing was added.")
    return oauth, details, found


def _org_said(found):
    value = getattr(found, "org_uuid", None)
    return value if isinstance(value, str) and value else None


def _org_of(found, details):
    return _org_said(found) or stated(details)[1]


def _enter(stack, manager):
    kind = None
    try:
        stack.enter_context(manager)
    except (writelock.WriteLockTimeout, LockTimeout):
        kind = "busy"
    except FileNotFoundError:
        kind = "missing"
    except OSError:
        kind = "broken"
    return kind


def _under_write_lock(lock_path, timeout, action):
    with ExitStack() as stack:
        kind = _enter(stack, writelock.held(lock_path, timeout=timeout))
        if kind == "busy":
            raise EnrollError(switch.LIFELINE_BUSY)
        if kind is not None:
            raise EnrollError(switch.NO_LOCK_FILE)
        return action()


def _session(run_login, runner, profile_fn, user, root, body, saved_in=SAVED_IN_KEYCHAIN):
    saved_in = _checked_place(saved_in)
    user = _user(user)
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sweep(root, runner, saved_in=saved_in)
    folder = Path(tempfile.mkdtemp(prefix=FOLDER_PREFIX, dir=root)).resolve()
    service = hashed_service(folder)

    def drop():
        if saved_in == SAVED_IN_FILE:
            return _drop_file(folder)
        return _delete_item(runner, service)

    try:
        os.chmod(folder, 0o700)
        fresh = _collect(folder, service, run_login, runner, profile_fn, user, saved_in)
        result = body(*fresh, drop)
    except BaseException as error:
        left = _cleanup(runner, service, folder, saved_in)
        if left:
            error.add_note(f"Lifeline could not remove {_listing(left)}. Remove it before going on.")
        raise
    left = _cleanup(runner, service, folder, saved_in)
    if left:
        raise EnrollError(
            f"The account was saved, but Lifeline could not remove {_listing(left)}. Remove it before going on."
        )
    return result


def _first_word(text):
    words = text.split()
    return words[0] if words else text


def _free_key(accounts, wanted):
    taken = {account.key for account in accounts}
    if wanted is not None:
        if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
            raise EnrollError("An account number must be a whole number, 1 or more.")
        if wanted in taken:
            raise EnrollError(f"Account number {wanted} is already in use.")
        return wanted
    number = 1
    while number in taken:
        number += 1
    return number


def _plan(details, fallback=None):
    tier = details.get("organizationRateLimitTier") or details.get("userRateLimitTier") or fallback
    return plan_label(tier) if tier else ""


def _new_account(
    accounts, uuid, details, email, found_plan, key, name, short, signed_in_at, signin_ends=None, login=None, org=None,
    names_for=None,
):
    chosen = _free_key(accounts, key)
    known = names_for(dict(details, emailAddress=email)) if names_for is not None and not name else None
    if known is not None:
        name, short, full = known[0], short or known[1], known[2]
    else:
        full = None
    shown = details.get("displayName") or email.split("@")[0]
    label = name or shown
    return Account(
        uuid=uuid,
        email=email,
        name=label,
        short=short or _first_word(label),
        full=full or label,
        plan=found_plan,
        key=chosen,
        oauth_account=details,
        signed_in_at=signed_in_at,
        signin_ends=signin_ends,
        org_uuid=org,
        login_uuid=login if login is not None and login != uuid else None,
    )


def already_has(account, browser=YOUR_BROWSER):
    if account.email:
        return ALREADY_HAS.format(name=account.name, browser=browser, email=account.email)
    return ALREADY_HAS_PLAIN.format(name=account.name)


class AlreadyHas(EnrollError):
    def __init__(self, account):
        self.name = account.name
        self.email = account.email
        super().__init__(already_has(account))


def enroll(
    *,
    run_login,
    runner=run_security,
    store,
    registry,
    profile_fn,
    user=None,
    temp_root=None,
    name=None,
    short=None,
    key=None,
    live_slot=None,
    home=None,
    lock_path=None,
    lock_timeout=writelock.DEFAULT_TIMEOUT,
    now=None,
    saved_in=SAVED_IN_KEYCHAIN,
    names_for=None,
    claude_folder=None,
    storage_timeout=STORAGE_TIMEOUT,
):
    _checked_place(saved_in)
    _free_key(registry.load(), key)
    home = Path(home) if home is not None else Path.home()
    live_slot = live_slot if live_slot is not None else MacKeychainSlot(account=keychain_account(_user(user)), runner=runner)
    lock_path = Path(lock_path) if lock_path is not None else writelock.default_lock_path()
    record_path = switch.record_path_for(lock_path)

    def body(oauth, details, found, drop):
        def locked():
            accounts = registry.load()
            org = _org_of(found, details)
            existing = resolve(accounts, found.account_uuid, org)
            if existing is not None and existing.org_uuid is None:
                email = details.get("emailAddress") or found.email
                raise EnrollError(ORG_NOT_KNOWN.format(email=email, name=existing.name))
            if existing is not None:
                raise AlreadyHas(existing)
            uuid = new_id(accounts, found.account_uuid, org)
            if uuid is None:
                raise EnrollError(ORG_UNTOLD)
            email = details.get("emailAddress") or found.email
            account = _new_account(
                accounts,
                uuid,
                details,
                email,
                _plan(details, found.rate_limit_tier),
                key,
                name,
                short,
                (now or _now)(),
                signin_end_text(oauth),
                login=found.account_uuid,
                org=org,
                names_for=names_for,
            )
            _, problem = _place(
                account.uuid, oauth, live_slot, store, profile_fn, home, record_path, registry, empty_ok=True,
                claude_folder=claude_folder, storage_timeout=storage_timeout,
            )
            drop()
            registry.upsert(account)
            if problem:
                raise EnrollError(problem)
            return account

        return _under_write_lock(lock_path, lock_timeout, locked)

    root = Path(temp_root) if temp_root is not None else login_root()
    return _session(run_login, runner, profile_fn, user, root, body, saved_in)


def _has_signin(item):
    oauth = item.get(OAUTH_KEY) if isinstance(item, dict) else None
    return isinstance(oauth, dict) and bool(oauth)


def _identify(slot, profile_fn, home, record_path, registry, empty_ok=False):
    problem = None
    item = None
    try:
        item = slot.read()
    except (SlotError, OSError):
        problem = "The sign-in in use could not be read, so it was left as it was."
    if problem:
        raise EnrollError(problem)
    if empty_ok and not _has_signin(item):
        return item, None
    found = None
    try:
        found = switch.identify_live(item, profile_fn=profile_fn, home=home, record_path=record_path, registry=registry)
    except switch.SwitchError as error:
        problem = str(error)
    if problem:
        raise EnrollError(problem)
    return item, found


def _oauth_value(item):
    value = item.get(OAUTH_KEY) if isinstance(item, dict) else None
    return value if isinstance(value, dict) and value else None


def _fresh(slot):
    problem = None
    item = None
    try:
        item = slot.read()
    except (SlotError, OSError):
        problem = "The sign-in in use could not be read, so it was left as it was."
    if problem:
        raise EnrollError(problem)
    return item


def _slot_state(slot, new, before):
    try:
        item = slot.read()
    except (SlotError, OSError):
        return "unreadable"
    token = (item.get(OAUTH_KEY) or {}).get("accessToken") if isinstance(item, dict) else None
    if not isinstance(token, str):
        return "nothing"
    if token == new.get("accessToken"):
        return "new"
    if token == before.get("accessToken"):
        return "before"
    return "different"


STATE_WORDS = {
    "unreadable": "could not be read",
    "nothing": "holds nothing",
    "new": "holds the new sign-in",
    "before": "holds the sign-in it held before",
    "different": "holds a different sign-in",
}


def _newer_parked(store, uuid, oauth):
    try:
        existing = store.get(uuid)
    except Exception:
        return False
    old, new = _expiry(existing), _expiry(oauth)
    return old is not None and new is not None and old > new


def _expiry(oauth):
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _put(store, uuid, oauth):
    try:
        store.put(uuid, oauth)
    except Exception:
        try:
            held = store.get(uuid)
        except Exception:
            return False
        return held == oauth
    return True


def _park_after_overwrite(store, uuid, oauth, folder):
    if _newer_parked(store, uuid, oauth):
        try:
            recovery.set_aside(folder, uuid, oauth)
        except recovery.RecoveryError:
            raise EnrollError(NOT_KEPT) from None
        return f"{OVERWRITTEN} {KEPT_BOTH}"
    if not _put(store, uuid, oauth):
        raise EnrollError(NOT_KEPT)
    return f"{OVERWRITTEN} {KEPT_NEW}"


OLD_SIGNIN_STAYS = "Open Claude Code sessions may keep the old sign-in until you restart them."


def _write_live(slot, store, uuid, fresh, oauth, folder, claude_folder):
    merged = dict(fresh)
    merged[OAUTH_KEY] = oauth
    failed = False
    try:
        slot.write(merged)
    except (SlotError, OSError):
        failed = True
    stays = OLD_SIGNIN_STAYS if nudge_sessions(claude_folder) == "failed" else None
    if not failed:
        state = _slot_state(slot, oauth, fresh.get(OAUTH_KEY) or {})
        if state in ("new", "unreadable"):
            return stays, merged
    else:
        state = _slot_state(slot, oauth, fresh.get(OAUTH_KEY) or {})
        if state == "new":
            unconfirmed = (
                "The new sign-in was written, but Lifeline could not confirm it. "
                "Claude Code's saved sign-in is now the new one."
            )
            return (f"{unconfirmed} {stays}" if stays else unconfirmed), merged
    if state in ("different", "nothing"):
        return _park_after_overwrite(store, uuid, oauth, folder), None
    raise EnrollError(
        f"The sign-in in use could not be replaced. Claude Code's saved sign-in now {STATE_WORDS[state]}."
    )


def _record_live(record_path, uuid, item):
    saved = False
    try:
        switch.record_confirmed(record_path, uuid, item, datetime.now(timezone.utc))
        saved = True
    except Exception:
        pass
    return None if saved else "The sign-in was saved, but Lifeline could not record which account is in use."


def _store(store, uuid, oauth):
    try:
        store.put(uuid, oauth)
    except Exception:
        try:
            held = store.get(uuid)
        except Exception:
            held = None
        if held != oauth:
            raise


def _place(
    uuid, oauth, slot, store, profile_fn, home, record_path, registry, empty_ok=False, claude_folder=None,
    storage_timeout=STORAGE_TIMEOUT,
):
    claude_folder = Path(claude_folder) if claude_folder is not None else find_claude_folder(home)
    for _ in range(2):
        identified, first = _identify(slot, profile_fn, home, record_path, registry, empty_ok)
        if first is None:
            _store(store, uuid, oauth)
            return "store", None
        session = SessionLocks(home)
        changed = False
        problem = None
        where = None
        with ExitStack() as stack:
            kind = _enter(stack, session)
            if kind == "busy":
                raise EnrollError(switch.SESSION_BUSY)
            if kind == "missing":
                raise EnrollError(switch.NO_SETTINGS_FOLDER)
            if kind is not None:
                raise EnrollError(switch.NO_SESSION_LOCKS)
            confirmed, second = _identify(slot, profile_fn, home, record_path, registry, empty_ok)
            storage = None
            fresh = _fresh(slot)
            if second != first or _oauth_value(fresh) != _oauth_value(confirmed) or _oauth_value(confirmed) != _oauth_value(identified):
                changed = True
            elif second == uuid:
                storage = StorageLock(claude_folder, timeout=storage_timeout)
                kind = _enter(stack, storage)
                if kind == "busy":
                    raise EnrollError(switch.SESSION_BUSY)
                if kind == "missing":
                    raise EnrollError(switch.NO_SETTINGS_FOLDER)
                if kind is not None:
                    raise EnrollError(switch.NO_SESSION_LOCKS)
                fresh = _fresh(slot)
                if _oauth_value(fresh) != _oauth_value(confirmed):
                    changed = True
            if not changed and second == uuid:
                folder = recovery.folder_for(record_path)
                problem, written = _write_live(slot, store, uuid, fresh, oauth, folder, claude_folder)
                recorded = _record_live(record_path, uuid, written) if written is not None else None
                problem = problem or recorded
                where = "slot"
            elif not changed:
                _store(store, uuid, oauth)
                where = "store"
        if changed:
            continue
        if session.compromised or (storage is not None and storage.compromised):
            if where == "slot":
                disturbed = "Claude Code changed its files while Lifeline saved the sign-in. Check Claude Code is still signed in."
            else:
                disturbed = "Claude Code changed its files while Lifeline saved the sign-in. Check that Lifeline still lists the account."
            problem = f"{problem} {disturbed}" if problem else disturbed
        return where, problem
    raise EnrollError("The sign-in in Claude Code kept changing, so nothing was changed. Try again in a moment.")


def resign(
    uuid,
    *,
    run_login,
    runner=run_security,
    store,
    registry,
    profile_fn,
    user=None,
    temp_root=None,
    live_slot=None,
    home=None,
    lock_path=None,
    lock_timeout=writelock.DEFAULT_TIMEOUT,
    now=None,
    saved_in=SAVED_IN_KEYCHAIN,
    claude_folder=None,
    storage_timeout=STORAGE_TIMEOUT,
):
    _checked_place(saved_in)
    if registry.by_uuid(uuid) is None:
        raise EnrollError("That account is not added yet.")
    home = Path(home) if home is not None else Path.home()
    user = _user(user)
    live_slot = live_slot if live_slot is not None else MacKeychainSlot(account=keychain_account(user), runner=runner)
    lock_path = Path(lock_path) if lock_path is not None else writelock.default_lock_path()
    record_path = switch.record_path_for(lock_path)

    def body(oauth, details, found, drop):
        org = _org_of(found, details)
        _same_account(registry, uuid, found, org)

        def locked():
            existing = registry.by_uuid(uuid)
            if existing is None:
                raise EnrollError("That account is no longer in Lifeline, so nothing was changed.")
            _same_account(registry, uuid, found, org)
            _, problem = _place(
                uuid, oauth, live_slot, store, profile_fn, home, record_path, registry, empty_ok=True,
                claude_folder=claude_folder, storage_timeout=storage_timeout,
            )
            drop()
            updated = replace(
                existing,
                email=details.get("emailAddress") or existing.email,
                plan=_plan(details, found.rate_limit_tier) or existing.plan,
                oauth_account=details,
                signed_in_at=(now or _now)(),
                signin_ends=signin_end_text(oauth),
                org_uuid=existing.org_uuid or org,
            )
            registry.upsert(updated)
            if problem:
                raise EnrollError(problem)
            return updated

        return _under_write_lock(lock_path, lock_timeout, locked)

    root = Path(temp_root) if temp_root is not None else login_root()
    return _session(run_login, runner, profile_fn, user, root, body, saved_in)


def _same_account(registry, uuid, found, org):
    match = resolve(registry.load(), found.account_uuid, org)
    if match is None or match.uuid != uuid:
        raise EnrollError("This sign-in belongs to a different account, so nothing was changed.")


def adopt_live(
    *,
    slot,
    registry,
    profile_fn,
    home,
    record_path=None,
    lock_path=None,
    lock_timeout=writelock.DEFAULT_TIMEOUT,
    name=None,
    short=None,
    key=None,
    signed_in_at=None,
    names_for=None,
    expected=None,
    claude_folder=None,
):
    home = Path(home)
    claude_folder = Path(claude_folder) if claude_folder is not None else find_claude_folder(home)
    lock_path = Path(lock_path) if lock_path is not None else writelock.default_lock_path()
    record_path = Path(record_path) if record_path is not None else switch.record_path_for(lock_path)

    def locked():
        item, uuid = _identify(slot, profile_fn, home, record_path, registry)
        if expected is not None and uuid != expected:
            raise EnrollError(LIVE_NOT_ASKED)
        problem = None
        details = None
        try:
            details = read_oauth_account(home / ".claude.json")
        except AccountFileError:
            problem = "Claude Code's account details could not be read, so nothing was added."
        if problem:
            raise EnrollError(problem)
        accounts = registry.load()
        login, org = stated(details)
        if login is None or identity(accounts, login, org) != uuid:
            raise EnrollError("Claude Code's account details do not match its sign-in, so nothing was added.")
        email = details.get("emailAddress")
        if not isinstance(email, str) or not email:
            raise EnrollError("Claude Code's account details hold no email, so nothing was added.")
        existing = next((a for a in accounts if a.uuid == uuid), None)
        plan = _plan(details)
        ends = signin_end_text(_oauth_value(item))
        if existing is not None:
            account = replace(
                existing,
                email=email,
                plan=plan or existing.plan,
                oauth_account=details,
                signin_ends=ends,
                org_uuid=existing.org_uuid or org,
            )
        else:
            account = _new_account(
                accounts, uuid, details, email, plan, key, name, short, signed_in_at, ends, login=login, org=org,
                names_for=names_for,
            )
        wrote = False
        try:
            switch.record_confirmed(record_path, uuid, item, datetime.now(timezone.utc))
            wrote = True
        except Exception:
            pass
        if not wrote:
            raise EnrollError("Lifeline could not save which account is in use, so nothing was added.")
        registry.upsert(account)
        nudge_sessions(claude_folder)
        return account

    return _under_write_lock(lock_path, lock_timeout, locked)
