import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import pickle
import stat
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import accountfile, ledger, locks, recovery
from lifeline import switch as switching
from lifeline.accountfile import AccountFileError
from lifeline.accounts import Account, Registry, signin_end_text
from lifeline.anthropic import Profile, SignInDead, Unavailable
from lifeline.locks import SessionLocks
from lifeline.slot import Slot, SlotError
from lifeline.store import FileStore, StoreError
from lifeline.switch import (
    RECORD_RETRY,
    SESSION_BUSY,
    SESSIONS_STAY,
    WRONG_NAME,
    IdentityUnknown,
    LifelineBusy,
    SealedSignin,
    SwitchError,
    SwitchFailed,
    SwitchRefused,
    SwitchResult,
    Watcher,
    default_backups,
    default_record_path,
    fingerprint,
    identify_live,
    read_record,
    read_record_entry,
    record_confirmed,
    record_path_for,
    switch,
    undo,
    write_record,
)
from lifeline.writelock import WriteLockTimeout, held

ALEX = "11111111-aaaa-4aaa-8aaa-000000000001"
WORK = "22222222-bbbb-4bbb-8bbb-000000000002"
PERSONAL = "33333333-cccc-4ccc-8ccc-000000000003"
ORG = "44444444-dddd-4ddd-8ddd-000000000004"
STRANGER = "55555555-eeee-4eee-8eee-000000000005"
TEAM_ORG = "66666666-ffff-4fff-8fff-000000000006"
TEAM = f"{PERSONAL}-{TEAM_ORG}"
ACCOUNTS = {
    ALEX: ("Alex's Account", "Alex", "Alex's Account", 1),
    WORK: ("Work Account", "Work", "Example Team's Account", 2),
    PERSONAL: ("Personal Account", "Personal", "Example Org Personal Account", 3),
    ORG: ("Team Account", "Team", "Example Org Team Account", 4),
}
SAME_LOGIN = {
    ALEX: ACCOUNTS[ALEX],
    WORK: ACCOUNTS[WORK],
    PERSONAL: ACCOUNTS[PERSONAL],
    TEAM: ACCOUNTS[ORG],
}
PAIRS = {TEAM: (PERSONAL, TEAM_ORG)}
TAGS = {TEAM: "teamorg0"}


def pair_of(uuid):
    return PAIRS.get(uuid, (uuid, f"org-{uuid[:8]}"))
FUTURE_MS = 4102444800000
PAST_MS = 946684800000


def details(uuid):
    login, org = pair_of(uuid)
    return {
        "accountUuid": login,
        "emailAddress": f"{login[:8]}@example.invalid",
        "organizationUuid": org,
        "organizationRateLimitTier": "default_claude_max_5x",
        "displayName": f"Person {login[:8]}",
        "hasExtraUsageEnabled": False,
    }


def mcp_signins():
    return {
        "github|1a2b": {
            "serverName": "github",
            "accessToken": "mcp-access-SECRET-one",
            "refreshToken": "mcp-refresh-SECRET-one",
            "expiresAt": FUTURE_MS,
        },
        "linear|3c4d": {
            "serverName": "linear",
            "accessToken": "mcp-access-SECRET-two",
            "refreshToken": "mcp-refresh-SECRET-two",
            "expiresAt": FUTURE_MS,
        },
    }


def config_data(oauth_account):
    return {
        "numStartups": 412,
        "installMethod": "native",
        "oauthAccount": oauth_account,
        "mcpServers": {
            f"server{i}": {
                "type": "http",
                "url": f"https://example.invalid/mcp/{i}",
                "headers": {"Authorization": f"Bearer header-{i}"},
            }
            for i in range(6)
        },
        "projects": {
            f"/home/test/project-{i}": {
                "allowedTools": [],
                "history": [{"display": f"note {i} é中"}],
                "lastCost": i / 7,
            }
            for i in range(300)
        },
        "cachedUsageUtilization": {"five_hour": {"utilization": 41.5}},
    }


def without(data, key):
    return {k: v for k, v in data.items() if k != key}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeProfiles:
    def __init__(self):
        self.owners = {}
        self.calls = []
        self.offline = False
        self.dead = set()
        self.hook = None

    def issue(self, uuid, n, expires=FUTURE_MS):
        tag = TAGS.get(uuid, uuid[:8])
        oauth = {
            "accessToken": f"access-{tag}-{n}-SECRET",
            "refreshToken": f"refresh-{tag}-{n}-SECRET",
            "expiresAt": expires,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
        self.owners[oauth["accessToken"]] = uuid
        self.owners[oauth["refreshToken"]] = uuid
        return oauth

    def owner(self, oauth):
        if not isinstance(oauth, dict):
            return None
        return self.owners.get(oauth.get("refreshToken"))

    def secrets(self):
        return list(self.owners)

    def __call__(self, token):
        self.calls.append(token)
        if self.hook is not None:
            self.hook(token)
        if self.offline:
            raise Unavailable("Could not reach the server")
        if token in self.dead or token not in self.owners:
            raise SignInDead()
        login, org = pair_of(self.owners[token])
        return Profile(
            account_uuid=login,
            email=f"{login[:8]}@example.invalid",
            org_uuid=org,
            rate_limit_tier="default_claude_max_5x",
        )


class FakeSlot(Slot):
    def __init__(self, item, log, owner):
        self.item = copy.deepcopy(item)
        self.log = log
        self.owner = owner
        self.writes = []
        self.write_plan = []
        self.read_plan = []
        self.pending = None

    def read(self):
        if self.read_plan:
            action = self.read_plan.pop(0)
            if action is not None:
                action(self)
        return copy.deepcopy(self.item)

    def write(self, item):
        if self.write_plan:
            action = self.write_plan.pop(0)
            if action is not None:
                return action(self, item)
        self.accept(item)

    def accept(self, item):
        self.item = copy.deepcopy(item)
        self.writes.append((time.monotonic(), copy.deepcopy(item)))
        self.log.append(("slot", self.owner(item.get("claudeAiOauth"))))


def refuse_write(slot, item):
    raise SlotError("The Keychain did not hold what was written. Another writer may have changed it.")


def land_then_fail(slot, item):
    slot.accept(item)
    raise SlotError("The Keychain did not hold what was written. Another writer may have changed it.")


def replace_then_fail(other):
    def action(slot, item):
        slot.item = copy.deepcopy(other)
        raise SlotError("The Keychain did not hold what was written. Another writer may have changed it.")

    return action


def hold_back(slot, item):
    slot.pending = copy.deepcopy(item)
    raise SlotError("security add-generic-password exited with status -1: timed out")


def land_late(slot):
    slot.accept(slot.pending)


def unreadable(slot):
    raise SlotError("The stored sign-in is not valid JSON.")


class FlakyStore:
    def __init__(self, inner, log):
        self.inner = inner
        self.log = log
        self.fail = {}
        self.after_put = None

    def _check(self, op, uuid, when):
        key = (op, uuid, when)
        if self.fail.get(key, 0) > 0:
            self.fail[key] -= 1
            raise StoreError("The new parked sign-in could not be saved.")

    def get(self, uuid):
        self._check("get", uuid, "before")
        return self.inner.get(uuid)

    def put(self, uuid, oauth):
        self._check("put", uuid, "before")
        self.inner.put(uuid, oauth)
        self.log.append(("put", uuid))
        if self.after_put is not None:
            self.after_put(uuid)
        self._check("put", uuid, "after")

    def remove(self, uuid):
        self._check("remove", uuid, "before")
        self.inner.remove(uuid)
        self.log.append(("remove", uuid))


class Env:
    def __init__(self, case, live=ALEX, parked=(WORK, PERSONAL, ORG), record=None, record_hash=True, accounts=ACCOUNTS):
        self.case = case
        tmp = TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        case.addCleanup(patcher.stop)
        self.lock_path = self.home / "lifeline-data" / "lifeline.lock"
        self.record_path = self.lock_path.parent / "last_loaded.json"
        self.backups = self.home / ".lifeline-backups"
        self.account_file = self.home / ".claude.json"
        self.log = []
        self.profiles = FakeProfiles()
        self.registry = Registry(self.home / "accounts.json")
        self.registry.save(
            [
                Account(
                    uuid=uuid,
                    email=f"{uuid[:8]}@example.invalid",
                    name=name,
                    short=short,
                    full=full,
                    plan="default_claude_max_5x",
                    key=key,
                    oauth_account=details(uuid),
                    login_uuid=pair_of(uuid)[0] if pair_of(uuid)[0] != uuid else None,
                )
                for uuid, (name, short, full, key) in accounts.items()
            ]
        )
        self.store_folder = self.home / "signins"
        self.store = FlakyStore(FileStore(self.store_folder), self.log)
        self.signins = {}
        for uuid in parked:
            self.signins[uuid] = self.profiles.issue(uuid, 1)
            self.store.inner.put(uuid, self.signins[uuid])
        self.signins[live] = self.profiles.issue(live, 1)
        self.slot = FakeSlot(
            {"claudeAiOauth": self.signins[live], "mcpOAuth": mcp_signins(), "otherKey": {"kept": True}},
            self.log,
            self.profiles.owner,
        )
        self.write_config(config_data(details(live)))
        if record is not None:
            signin = self.signins.get(record)
            if signin is not None and record_hash:
                record_confirmed(self.record_path, record, {"claudeAiOauth": signin}, datetime.now(timezone.utc))
            else:
                write_record(self.record_path, record)

    def write_config(self, data):
        self.account_file.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        os.chmod(self.account_file, 0o600)

    def config(self):
        return json.loads(self.account_file.read_text(encoding="utf-8"))

    def kwargs(self, **extra):
        options = dict(
            slot=self.slot,
            store=self.store,
            registry=self.registry,
            home=self.home,
            profile_fn=self.profiles,
            lock_path=self.lock_path,
            backups=self.backups,
            settle=0,
            session_timeout=5,
            lock_timeout=5,
        )
        options.update(extra)
        return options

    def switch(self, target, **extra):
        return switch(target, **self.kwargs(**extra))

    def watcher(self, chosen, clock, **extra):
        options = dict(
            home=self.home,
            lock_path=self.lock_path,
            backups=self.backups,
            settle=0,
            lock_timeout=2,
            session_timeout=2,
        )
        options.update(extra)
        minutes = options.pop("minutes", 10)
        return Watcher(self.slot, self.store, self.registry, chosen, self.profiles, minutes=minutes, clock=clock, **options)

    def live(self):
        item = self.slot.item or {}
        return item.get("claudeAiOauth")

    def live_owner(self):
        return self.profiles.owner(self.live())

    def parked(self, uuid):
        return self.store.inner.get(uuid)

    def store_files(self):
        return {p.name: p.read_bytes() for p in sorted(self.store_folder.iterdir())}

    def backup_names(self):
        return sorted(p.name for p in self.backups.iterdir()) if self.backups.exists() else []

    def snapshot(self):
        record = self.record_path.read_bytes() if self.record_path.exists() else None
        return (
            copy.deepcopy(self.slot.item),
            len(self.slot.writes),
            self.store_files(),
            self.account_file.read_bytes(),
            record,
            self.backup_names(),
        )

    def assert_one_copy(self):
        owner = self.live_owner()
        for uuid in list(ACCOUNTS) + [STRANGER]:
            if owner == uuid:
                self.case.assertIsNone(self.parked(uuid), f"{uuid} is both in the slot and parked")

    def assert_others_kept(self):
        self.case.assertEqual(self.slot.item["mcpOAuth"], mcp_signins())
        self.case.assertEqual(self.slot.item["otherKey"], {"kept": True})
        self.case.assertEqual(set(self.slot.item), {"claudeAiOauth", "mcpOAuth", "otherKey"})

    def assert_secret_free(self, *things):
        secrets = self.profiles.secrets() + [
            value for entry in mcp_signins().values() for key, value in entry.items() if "Token" in key
        ]
        for thing in things:
            texts = [str(thing), repr(thing)]
            if isinstance(thing, BaseException):
                self.case.assertIsNone(thing.__context__)
                self.case.assertIsNone(thing.__cause__)
                texts.extend(str(arg) for arg in thing.args)
            for text in texts:
                for secret in secrets:
                    self.case.assertNotIn(secret, text)


class SwitchTests(unittest.TestCase):
    def test_switch_parks_then_loads(self):
        env = Env(self)
        before = env.account_file.read_bytes()
        original = env.config()
        result = env.switch(WORK)
        self.assertEqual(result.from_uuid, ALEX)
        self.assertEqual(result.to_uuid, WORK)
        self.assertFalse(result.leftover)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(env.parked(PERSONAL), env.signins[PERSONAL])
        self.assertEqual(env.parked(ORG), env.signins[ORG])
        after = env.config()
        self.assertEqual(after["oauthAccount"], details(WORK))
        self.assertEqual(without(after, "oauthAccount"), without(original, "oauthAccount"))
        self.assertEqual(list(after), list(original))
        self.assertEqual(Path(result.backup).read_bytes(), before)
        self.assertEqual(Path(result.backup).parent, env.backups)
        self.assertEqual(read_record(env.record_path), WORK)
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(env.signins[WORK]))
        self.assertTrue(result.confirmed)
        self.assertEqual(
            env.profiles.calls, [env.signins[ALEX]["accessToken"], env.signins[WORK]["accessToken"]]
        )
        self.assertEqual(len(env.slot.writes), 1)
        env.assert_others_kept()
        env.assert_one_copy()
        env.assert_secret_free(result)

    def test_switch_reads_back_and_confirms(self):
        env = Env(self)
        result = env.switch(WORK)
        self.assertIs(result.confirmed, True)
        self.assertEqual(result.signin.open(), env.signins[WORK])

        env = Env(self)
        real = env.profiles.__call__
        calls = []

        def offline_after_first(token):
            calls.append(token)
            if len(calls) > 1:
                raise Unavailable("Could not reach the server")
            return real(token)

        result = switch(WORK, **env.kwargs(profile_fn=offline_after_first))
        self.assertIsNone(result.confirmed)
        self.assertEqual(result.signin.open(), env.signins[WORK])
        entry = read_record_entry(env.record_path)
        self.assertEqual(entry["uuid"], WORK)
        self.assertIsNone(entry["refresh_sha256"])

        env = Env(self)
        expired = env.profiles.issue(WORK, 4, expires=PAST_MS)
        env.store.inner.put(WORK, expired)
        result = env.switch(WORK)
        self.assertIsNone(result.confirmed)
        self.assertNotIn(expired["accessToken"], env.profiles.calls)

    def test_switch_reports_a_mislabelled_parked_signin(self):
        env = Env(self)
        stranger = env.profiles.issue(PERSONAL, 5)
        env.store.inner.put(WORK, stranger)
        result = env.switch(WORK)
        self.assertIs(result.confirmed, False)
        self.assertIsNone(result.signin)
        self.assertIsNone(read_record(env.record_path))
        self.assertEqual(env.live(), stranger)

    def test_switch_records_the_hash_only_if_the_slot_still_holds_the_loaded_signin(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2)

        def renew_during_readback(token):
            if token == env.signins[WORK]["accessToken"]:
                env.profiles.hook = None
                env.slot.item["claudeAiOauth"] = copy.deepcopy(renewed)

        env.profiles.hook = renew_during_readback
        result = env.switch(WORK)
        self.assertIs(result.confirmed, True)
        entry = read_record_entry(env.record_path)
        self.assertEqual(entry["uuid"], WORK)
        self.assertIsNone(entry["refresh_sha256"])

    def test_switch_result_keeps_the_signin_sealed(self):
        env = Env(self)
        result = env.switch(WORK)
        self.assertIsInstance(result.signin, SealedSignin)
        env.assert_secret_free(result, result.signin)
        flat = dataclasses.asdict(result)
        self.assertIs(flat["signin"], result.signin)
        with self.assertRaises(TypeError):
            json.dumps({"signin": flat["signin"]})
        with self.assertRaises(TypeError):
            pickle.dumps(result.signin)
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.assertRaises(TypeError):
                pickle.dumps(result, protocol=protocol)
        for method in (result.signin.__getstate__, result.signin.__reduce__):
            with self.assertRaises(TypeError):
                method()
        opened = result.signin.open()
        opened["refreshToken"] = "changed"
        self.assertEqual(result.signin.open(), env.signins[WORK])

    def test_record_confirmed_writes_the_hash_privately(self):
        env = Env(self)
        at = datetime(2026, 11, 19, 3, 0, 5, tzinfo=timezone.utc)
        record_confirmed(env.record_path, WORK, {"claudeAiOauth": env.signins[WORK], "mcpOAuth": mcp_signins()}, at)
        data = json.loads(env.record_path.read_text())
        expected = hashlib.sha256(env.signins[WORK]["refreshToken"].encode("utf-8")).hexdigest()
        self.assertEqual(data, {"uuid": WORK, "at": "2026-11-19T03:00:05+00:00", "refresh_sha256": expected, "proven": False})
        self.assertEqual(stat.S_IMODE(os.stat(env.record_path).st_mode), 0o600)
        record_confirmed(env.record_path, WORK, {"claudeAiOauth": env.signins[WORK]}, at, proven=True)
        self.assertTrue(json.loads(env.record_path.read_text())["proven"])
        record_confirmed(env.record_path, WORK, {"claudeAiOauth": env.signins[WORK]}, at)
        before = env.record_path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            record_confirmed(env.record_path, WORK, {"claudeAiOauth": {"accessToken": "access-x-SECRET"}}, at)
        self.assertEqual(env.record_path.read_bytes(), before)
        env.assert_secret_free(caught.exception)
        self.assertNotIn("access-x-SECRET", str(caught.exception))
        with self.assertRaises(TypeError):
            write_record(env.record_path, WORK, None, expected)
        write_record(env.record_path, WORK)
        self.assertIsNone(read_record_entry(env.record_path)["refresh_sha256"])

    def test_switch_order_parks_before_loading_and_unparks_last(self):
        env = Env(self)
        real = accountfile.replace_oauth_account

        def logged(path, new, backups, notes=None):
            env.log.append(("file", new.get("accountUuid")))
            return real(path, new, backups, notes=notes)

        with mock.patch.object(switching.accountfile, "replace_oauth_account", side_effect=logged):
            env.switch(WORK)
        self.assertEqual(env.log, [("put", ALEX), ("slot", WORK), ("file", WORK), ("remove", WORK)])

    def test_switch_keeps_mcp_written_during_the_profile_check(self):
        env = Env(self)
        newer = mcp_signins()
        newer["github|1a2b"]["refreshToken"] = "mcp-refresh-SECRET-renewed"

        def renew_mcp(token):
            if token == env.signins[ALEX]["accessToken"]:
                env.slot.item["mcpOAuth"] = copy.deepcopy(newer)

        env.profiles.hook = renew_mcp
        env.switch(WORK)
        self.assertEqual(len(env.slot.writes), 1)
        self.assertEqual(env.slot.writes[0][1]["mcpOAuth"], newer)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.slot.item["mcpOAuth"], newer)

    def test_switch_records_last_loaded_with_private_modes(self):
        env = Env(self)
        env.switch(PERSONAL)
        data = json.loads(env.record_path.read_text())
        self.assertEqual(data["uuid"], PERSONAL)
        self.assertIsInstance(data["at"], str)
        self.assertEqual(set(data), {"uuid", "at", "refresh_sha256", "proven"})
        self.assertEqual(data["refresh_sha256"], fingerprint(env.signins[PERSONAL]))
        self.assertIs(data["proven"], True)
        self.assertEqual(stat.S_IMODE(os.stat(env.record_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(env.lock_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(env.lock_path.parent).st_mode), 0o700)
        self.assertEqual(record_path_for(env.lock_path), env.record_path)

    def test_switch_uses_default_backups_and_record_beside_default_lock(self):
        env = Env(self)
        options = env.kwargs()
        del options["backups"]
        del options["lock_path"]
        with mock.patch.object(switching.writelock.ident.sys, "platform", "linux"):
            result = switch(WORK, **options)
            self.assertEqual(default_record_path(), env.home / ".local" / "share" / "com.example.lifeline" / "last_loaded.json")
            self.assertEqual(read_record(default_record_path()), WORK)
            self.assertEqual(default_backups(), env.home / ".local" / "share" / "com.example.lifeline" / "backups")
            self.assertEqual(Path(result.backup).parent, default_backups())

    def test_switch_parks_the_live_signin_over_any_other_saved_copy(self):
        env = Env(self)
        spare = env.profiles.issue(ALEX, 9, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, spare)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.live(), env.signins[WORK])

    def test_switch_replaces_older_parked_copy_of_live_account(self):
        env = Env(self)
        older = env.profiles.issue(ALEX, 0, expires=FUTURE_MS - 60_000)
        env.store.inner.put(ALEX, older)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])


def folder_of(env):
    return recovery.folder_for(env.lock_path)


def recovery_files(env, uuid):
    folder = folder_of(env)
    if not folder.exists():
        return []
    return sorted(p.name for p in folder.iterdir() if p.name.startswith(uuid) or p.name.startswith("." + uuid))


class RecoveryTests(unittest.TestCase):
    def test_switch_loads_a_recovered_signin_the_store_refuses(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS - 60_000)
        recovery.write(folder_of(env), WORK, renewed, spent=env.signins[WORK])
        env.store.fail[("put", WORK, "before")] = 5
        result = env.switch(WORK)
        self.assertEqual(env.live(), renewed)
        self.assertEqual(recovery_files(env, WORK), [])
        self.assertIsNone(env.parked(WORK))
        self.assertIsNone(result.note)
        env.assert_others_kept()
        env.assert_one_copy()

    def test_switch_imports_a_recovered_signin_the_store_accepts(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2)
        recovery.write(folder_of(env), WORK, renewed, spent=env.signins[WORK])
        env.switch(WORK)
        self.assertEqual(env.live(), renewed)
        self.assertEqual(recovery_files(env, WORK), [])
        self.assertIsNone(env.parked(WORK))
        env.assert_one_copy()

    def test_switch_refuses_in_one_sentence_when_the_store_cannot_be_read(self):
        env = Env(self)
        recovery.write(folder_of(env), WORK, env.profiles.issue(WORK, 2), spent=env.signins[WORK])
        env.store.fail[("get", WORK, "before")] = 3
        before = env.snapshot()
        files = recovery_files(env, WORK)
        with self.assertRaises(SwitchRefused) as caught:
            env.switch(WORK)
        self.assertEqual(
            str(caught.exception), "Lifeline could not read its saved sign-in for Work Account, so nothing was changed."
        )
        self.assertEqual(env.snapshot(), before)
        self.assertEqual(recovery_files(env, WORK), files)
        env.assert_secret_free(caught.exception)

    def test_switch_says_a_damaged_recovery_file_may_mean_a_spent_signin(self):
        env = Env(self)
        folder = folder_of(env)
        folder.mkdir(mode=0o700, parents=True)
        (folder / f"{WORK}.json").write_text("{broken")
        result = env.switch(WORK)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertIn("damaged", result.note)
        self.assertIn("Work Account", result.note)
        self.assertTrue(result.note.endswith("."))
        env.assert_secret_free(result)

    def test_switch_refuses_when_only_a_damaged_recovery_file_is_left(self):
        env = Env(self)
        env.store.inner.remove(WORK)
        folder = folder_of(env)
        folder.mkdir(mode=0o700, parents=True)
        (folder / f"{WORK}.json").write_text("{broken")
        before = env.snapshot()
        with self.assertRaises(SwitchRefused) as caught:
            env.switch(WORK)
        self.assertIn("damaged copy", str(caught.exception))
        self.assertEqual(env.snapshot(), before)

    def test_switch_back_parks_a_shorter_renewal_loaded_from_a_file(self):
        env = Env(self)
        old = env.signins[WORK]
        old_longer = dict(old, expiresAt=FUTURE_MS + 7_200_000)
        env.store.inner.put(WORK, old_longer)
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS)
        recovery.write(folder_of(env), WORK, renewed, spent=old_longer)
        env.store.fail[("put", WORK, "before")] = 1
        env.store.fail[("remove", WORK, "before")] = 2
        first = env.switch(WORK)
        self.assertTrue(first.leftover)
        self.assertEqual(env.live(), renewed)
        self.assertEqual(env.parked(WORK), old_longer)
        self.assertEqual(recovery_files(env, WORK), [])
        self.assertEqual(env.store.fail[("put", WORK, "before")], 0)
        env.switch(ALEX)
        self.assertEqual(env.parked(WORK), renewed)
        env.switch(WORK)
        self.assertEqual(env.live(), renewed)

    def test_rollback_keeps_the_recovery_file_when_the_slot_goes_back(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2)
        recovery.write(folder_of(env), WORK, renewed, spent=env.signins[WORK])
        env.store.fail[("put", WORK, "before")] = 5
        with failing_relabel():
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertEqual(recovery.pending(folder_of(env), WORK), renewed)

    def test_rollback_removes_the_recovery_file_when_the_slot_stays_on_the_target(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2)
        recovery.write(folder_of(env), WORK, renewed, spent=env.signins[WORK])
        env.store.fail[("put", WORK, "before")] = 5
        env.slot.write_plan = [None, refuse_write]
        with failing_relabel():
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(env.live(), renewed)
        self.assertEqual(recovery_files(env, WORK), [])

    def test_recovery_file_is_removed_inside_the_switch_not_after(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2)
        recovery.write(folder_of(env), WORK, renewed, spent=env.signins[WORK])
        env.store.fail[("put", WORK, "before")] = 5
        events = []
        real_remove = recovery.remove
        real_confirm = switching.confirm_switch

        def remove(folder, uuid, expected):
            events.append(("remove", env.lock_path.exists() and lock_is_held(env.lock_path)))
            return real_remove(folder, uuid, expected)

        def confirm(result, **options):
            events.append(("confirm", lock_is_held(env.lock_path)))
            return real_confirm(result, **options)

        with mock.patch.object(switching.recovery, "remove", side_effect=remove), mock.patch.object(
            switching, "confirm_switch", confirm
        ):
            env.switch(WORK)
        self.assertEqual(events, [("remove", True), ("confirm", False)])

    def test_failed_registry_read_is_not_called_an_unknown_account(self):
        env = Env(self)
        real = env.registry.by_uuid

        def flaky(uuid):
            if uuid == ALEX:
                raise OSError("busy")
            return real(uuid)

        with mock.patch.object(env.registry, "by_uuid", side_effect=flaky):
            with self.assertRaises(SwitchRefused) as caught:
                env.switch(WORK)
        self.assertEqual(str(caught.exception), "Lifeline could not read its list of accounts, so nothing was changed.")


def lock_is_held(path):
    try:
        with held(path, timeout=0):
            return False
    except Exception:
        return True


class Kill(BaseException):
    pass


def put_slot(env, oauth):
    item = copy.deepcopy(env.slot.item)
    item["claudeAiOauth"] = copy.deepcopy(oauth)
    env.slot.item = item


def holders(env, oauth):
    found = []
    if recovery.same_signin(env.live(), oauth):
        found.append("slot")
    for uuid in ACCOUNTS:
        if recovery.same_signin(env.parked(uuid), oauth):
            found.append(uuid)
    folder = folder_of(env)
    if folder.exists():
        for path in sorted(folder.iterdir()):
            loaded = recovery._load(path)
            if loaded is not None and recovery.same_signin(loaded[0], oauth):
                found.append("file")
    return found


def files_holding(env, uuid):
    folder = folder_of(env)
    if not folder.exists():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if path.name.startswith(uuid):
            loaded = recovery._load(path)
            if loaded is not None:
                found.append(loaded[0])
    return found


def held_lock(env):
    return held(env.lock_path, timeout=1)


def kept_outside_the_slot(env, oauth):
    return [place for place in holders(env, oauth) if place != "slot"]


def mark_spent(env, uuid, *pairs):
    with held(env.lock_path, timeout=1):
        ledger.add(env.lock_path.parent, uuid, list(pairs))


def spent_copy(env, oauth):
    mark_spent(env, WORK, oauth)
    return oauth


class LedgerTests(unittest.TestCase):
    def renewed_then_stale(self, env, clock, later_ms=3_600_000):
        result = env.switch(WORK)
        first = env.watcher(WORK, clock, seed=result, lifetime=None)
        g1 = env.signins[WORK]
        first.poll()
        g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + later_ms)
        put_slot(env, g2)
        first.poll()
        put_slot(env, g1)
        return first, g1, g2

    def test_a_proven_renewal_of_an_older_pair_never_replaces_a_copy_that_ends_later(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        g = env.signins[WORK]
        watcher.poll()
        v0 = env.profiles.issue(WORK, 10, expires=FUTURE_MS - 3_600_000)
        put_slot(env, v0)
        clock.now += 15
        watcher.poll()
        clock.now += 15
        watcher.poll()
        v1 = env.profiles.issue(WORK, 11, expires=FUTURE_MS - 1_800_000)
        put_slot(env, v1)
        clock.now += 15
        watcher.poll()
        clock.now += 15
        watcher.poll()
        env.switch(ALEX)
        self.assertTrue(kept_outside_the_slot(env, g), "the copy that ends later was dropped")
        self.assertTrue(kept_outside_the_slot(env, v1))
        env.switch(WORK)
        self.assertEqual(env.live(), g)
        self.assertTrue(kept_outside_the_slot(env, v1))

    def spoil_ledger(self, env, how):
        path = env.lock_path.parent / ledger.NAME
        if how == "damaged":
            path.write_text("{not json")
        else:
            target = env.lock_path.parent / "elsewhere.json"
            if path.exists():
                os.replace(path, target)
            else:
                target.write_text("{}")
            os.symlink(target, path)

    def unwritable_ledger_watch(self, how, lifetime):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        options = {} if lifetime == "default" else {"lifetime": lifetime}
        watcher = env.watcher(WORK, clock, seed=result, **options)
        g1 = env.signins[WORK]
        watcher.poll()
        self.spoil_ledger(env, how)
        calls = []
        real = watcher._locked
        watcher._locked = lambda action: (calls.append(1), real(action))[1]
        g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 3_600_000)
        put_slot(env, g2)
        return env, clock, watcher, g1, g2, calls

    def stop_sentence(self, env):
        path = env.lock_path.parent / ledger.NAME
        return (
            f"Lifeline stopped watching Work Account because it could not write its list of spent sign-ins at {path}. "
            "Every sign-in is kept. Make that file a plain file you can read and write. "
            "If it is damaged, rename it, and Lifeline starts a new one."
        )

    def test_a_watcher_that_cannot_write_the_spent_mark_ends_at_its_lifetime_and_reports_once(self):
        for how in ("damaged", "linked"):
            with self.subTest(how):
                env, clock, watcher, g1, g2, calls = self.unwritable_ledger_watch(how, "default")
                reports = []
                for _ in range(7):
                    clock.now += 300
                    reports += watcher.poll()
                self.assertFalse(watcher.active)
                self.assertEqual([r for r in reports if r.startswith("Lifeline stopped watching")], [self.stop_sentence(env)])
                self.assertEqual(holders(env, g1), ["file"])
                self.assertEqual(holders(env, g2), ["slot"])
                before = len(calls)
                clock.now += 300
                self.assertEqual(watcher.poll(), [])
                self.assertEqual(len(calls), before)
                self.assertLess(before, 7)

    def test_a_continuous_watcher_that_cannot_write_the_spent_mark_ends_after_one_window(self):
        env, clock, watcher, g1, g2, calls = self.unwritable_ledger_watch("damaged", None)
        reports = []
        for _ in range(36):
            clock.now += 15
            reports += watcher.poll()
        self.assertTrue(watcher.active)
        for _ in range(8):
            clock.now += 15
            reports += watcher.poll()
        self.assertFalse(watcher.active)
        self.assertEqual([r for r in reports if r.startswith("Lifeline stopped watching")], [self.stop_sentence(env)])
        self.assertEqual(holders(env, g1), ["file"])
        self.assertEqual(holders(env, g2), ["slot"])

    def test_a_watcher_never_ends_while_it_holds_the_only_copy_of_a_spent_pair(self):
        env, clock, watcher, g1, g2, calls = self.unwritable_ledger_watch("damaged", "default")
        with mock.patch.object(recovery, "set_aside", side_effect=recovery.RecoveryError("no")):
            for _ in range(7):
                clock.now += 300
                watcher.poll()
        self.assertTrue(watcher.active)
        self.assertNotIn("file", holders(env, g1))

    def test_stuck_restore_then_stop_keeps_the_renewal(self):
        for after in ("nothing", "rescue", "rescue then settle"):
            with self.subTest(after):
                env = Env(self)
                clock = FakeClock()
                first, g1, g2 = self.renewed_then_stale(env, clock)
                env.slot.write_plan = [refuse_write]
                first.poll()
                self.assertEqual(holders(env, g2), [WORK])
                self.assertEqual(env.live(), g1)
                if after.startswith("rescue"):
                    self.assertTrue(first.rescue())
                if after.endswith("settle"):
                    with held(env.lock_path, timeout=1):
                        recovery.settle(env.store.inner, folder_of(env), WORK)
                fresh = env.watcher(WORK, clock, lifetime=None)
                for _ in range(2):
                    clock.now += 15
                    fresh.poll()
                self.assertEqual(env.live(), g2)
                env.switch(ALEX)
                self.assertEqual(env.parked(WORK), g2)
                env.switch(WORK)
                self.assertEqual(env.live(), g2)
                self.assertEqual(env.slot.item["mcpOAuth"], mcp_signins())

    def killed_mid_restore(self, env, clock, record_g2):
        result = env.switch(WORK)
        first = env.watcher(WORK, clock, seed=result, lifetime=None)
        g1 = env.signins[WORK]
        first.poll()
        g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        put_slot(env, g2)
        if record_g2:
            first.poll()
        else:
            with mock.patch.object(switching, "record_confirmed", side_effect=OSError("disk")):
                first.poll()
        put_slot(env, g1)

        def kill(uuid):
            if uuid == WORK:
                env.store.after_put = None
                raise Kill()

        env.store.after_put = kill
        with self.assertRaises(Kill):
            first.poll()
        self.assertEqual(holders(env, g2), [WORK])
        self.assertEqual(env.live(), g1)
        return g1, g2

    def test_kill_mid_restore_then_switch_away_and_back_keeps_the_renewal(self):
        for record_g2 in (False, True):
            with self.subTest(record_g2=record_g2):
                env = Env(self)
                g1, g2 = self.killed_mid_restore(env, FakeClock(), record_g2)
                env.switch(ALEX)
                self.assertEqual(env.parked(WORK), g2)
                self.assertNotIn(WORK, holders(env, g1))
                env.switch(WORK)
                self.assertEqual(env.live(), g2)
                env.assert_others_kept()

    def test_kill_mid_restore_then_a_fresh_watcher_puts_the_renewal_back(self):
        for record_g2, offline in ((False, False), (True, False), (True, True)):
            with self.subTest(record_g2=record_g2, offline=offline):
                env = Env(self)
                clock = FakeClock()
                g1, g2 = self.killed_mid_restore(env, clock, record_g2)
                env.profiles.offline = offline
                fresh = env.watcher(WORK, clock, lifetime=None)
                for _ in range(3):
                    clock.now += 15
                    fresh.poll()
                self.assertEqual(env.live(), g2)
                self.assertNotEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(g1) if record_g2 else None)
                env.profiles.offline = False
                env.switch(ALEX)
                self.assertEqual(env.parked(WORK), g2)
                env.switch(WORK)
                self.assertEqual(env.live(), g2)

    def test_rescue_import_then_switch_away_and_back_keeps_the_renewal(self):
        for imported in (False, True):
            with self.subTest(imported=imported):
                env = Env(self)
                clock = FakeClock()
                result = env.switch(WORK)
                first = env.watcher(WORK, clock, seed=result, lifetime=None)
                g1 = env.signins[WORK]
                first.poll()
                g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
                put_slot(env, g2)
                first.poll()
                put_slot(env, g1)
                self.assertTrue(first.rescue())
                if imported:
                    with held(env.lock_path, timeout=1):
                        self.assertEqual(recovery.settle(env.store.inner, folder_of(env), WORK), recovery.SAVED)
                fresh = env.watcher(WORK, clock, lifetime=None)
                for _ in range(2):
                    clock.now += 15
                    fresh.poll()
                self.assertEqual(env.live(), g2)
                env.switch(ALEX)
                self.assertEqual(env.parked(WORK), g2)
                env.switch(WORK)
                self.assertEqual(env.live(), g2)

    def test_switch_while_a_stuck_watcher_lives_keeps_the_renewal(self):
        env = Env(self)
        clock = FakeClock()
        first, g1, g2 = self.renewed_then_stale(env, clock)
        env.slot.write_plan = [refuse_write]
        first.poll()
        env.switch(ALEX)
        self.assertEqual(env.parked(WORK), g2)
        clock.now += 15
        first.poll()
        self.assertFalse(first.active)
        self.assertEqual(env.parked(WORK), g2)
        env.switch(WORK)
        self.assertEqual(env.live(), g2)

    def test_switch_keeps_a_saved_copy_the_ledger_proves_newer(self):
        env = Env(self)
        newer = env.profiles.issue(ALEX, 2, expires=FUTURE_MS - 60_000)
        env.store.inner.put(ALEX, newer)
        mark_spent(env, ALEX, env.signins[ALEX])
        result = env.switch(WORK)
        self.assertEqual(result.to_uuid, WORK)
        self.assertEqual(env.parked(ALEX), newer)
        self.assertEqual(files_holding(env, ALEX), [])
        self.assertEqual(env.live(), env.signins[WORK])

    def test_switch_parks_over_a_saved_copy_the_ledger_marks_spent(self):
        env = Env(self)
        spent = env.profiles.issue(ALEX, 0, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, spent)
        mark_spent(env, ALEX, spent)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(files_holding(env, ALEX), [])

    def test_switch_record_decides_only_for_the_live_pair(self):
        env = Env(self)
        record_confirmed(env.record_path, ALEX, env.slot.item, datetime.now(timezone.utc), proven=True)
        older = env.profiles.issue(ALEX, 0, expires=FUTURE_MS - 60_000)
        env.store.inner.put(ALEX, older)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(files_holding(env, ALEX), [])
        env = Env(self)
        record_confirmed(env.record_path, ALEX, env.slot.item, datetime.now(timezone.utc), proven=True)
        later = env.profiles.issue(ALEX, 0, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, later)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(files_holding(env, ALEX), [later])
        env = Env(self)
        confirmed = env.profiles.issue(ALEX, 2, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, confirmed)
        record_confirmed(env.record_path, ALEX, {"claudeAiOauth": confirmed}, datetime.now(timezone.utc), proven=True)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(files_holding(env, ALEX), [confirmed])
        env = Env(self, record=ALEX)
        older = env.profiles.issue(ALEX, 0, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, older)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(files_holding(env, ALEX), [older])

    def leftover_then_unwatched_renewal(self, later_ms=3_600_000):
        env = Env(self)
        env.store.fail[("remove", WORK, "before")] = 2
        result = env.switch(WORK)
        self.assertTrue(result.leftover)
        h = env.signins[WORK]
        h2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + later_ms)
        put_slot(env, h2)
        self.assertEqual(env.parked(WORK), h)
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(h))
        return env, h, h2

    def test_switch_away_keeps_an_unwatched_renewal_when_the_record_names_a_saved_copy(self):
        env, h, h2 = self.leftover_then_unwatched_renewal()
        env.switch(ALEX)
        self.assertEqual(env.parked(WORK), h2)
        self.assertEqual(files_holding(env, WORK), [h])
        env.switch(WORK)
        self.assertEqual(env.live(), h2)

    def test_fresh_watcher_leaves_an_unwatched_renewal_when_the_record_names_a_saved_copy(self):
        for later_ms in (3_600_000, -3_600_000):
            with self.subTest(later_ms=later_ms):
                env, h, h2 = self.leftover_then_unwatched_renewal(later_ms)
                fresh = env.watcher(WORK, FakeClock(), lifetime=None)
                writes = len(env.slot.writes)
                self.assertEqual(fresh.poll(), [])
                self.assertEqual(fresh.poll(), [])
                self.assertEqual(env.live(), h2)
                self.assertEqual(len(env.slot.writes), writes)
                self.assertEqual(env.parked(WORK), h)

    def test_rollback_after_the_ledger_kept_the_saved_copy(self):
        env = Env(self)
        newer = env.profiles.issue(ALEX, 2, expires=FUTURE_MS - 60_000)
        env.store.inner.put(ALEX, newer)
        mark_spent(env, ALEX, env.signins[ALEX])
        with failing_relabel():
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertEqual(env.parked(ALEX), newer)
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_switch_sets_an_undecided_saved_copy_aside_before_parking(self):
        env = Env(self)
        spare = env.profiles.issue(ALEX, 9, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, spare)
        env.switch(WORK)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(files_holding(env, ALEX), [spare])

    def test_switch_refuses_when_an_undecided_saved_copy_cannot_be_set_aside(self):
        env = Env(self)
        spare = env.profiles.issue(ALEX, 9, expires=FUTURE_MS + 60_000)
        env.store.inner.put(ALEX, spare)
        before = env.snapshot()
        with mock.patch.object(switching.recovery, "set_aside", side_effect=recovery.RecoveryError("no")):
            with self.assertRaises(SwitchRefused) as caught:
                env.switch(WORK)
        self.assertEqual(env.snapshot(), before)
        self.assertIn("Alex's Account", str(caught.exception))
        env.assert_secret_free(caught.exception)

    def test_switch_keeps_an_undecided_saved_copy_of_the_target_when_it_loads_a_file_pair(self):
        env = Env(self)
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        recovery.write(folder_of(env), WORK, renewed)
        env.store.fail[("put", WORK, "before")] = 5
        real = recovery.set_aside
        calls = []

        def first_fails(folder, uuid, oauth):
            calls.append(uuid)
            if len(calls) == 1:
                raise recovery.RecoveryError("no")
            return real(folder, uuid, oauth)

        with mock.patch.object(recovery, "set_aside", side_effect=first_fails):
            env.switch(WORK)
        self.assertEqual(env.live(), renewed)
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(files_holding(env, WORK), [env.signins[WORK]])

    def fought(self, env, clock):
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result)
        watcher.poll()
        return watcher

    def test_fight_keeps_a_saved_copy_the_ledger_proves_newer(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        stale = env.signins[PERSONAL]
        newer = env.profiles.issue(PERSONAL, 2, expires=FUTURE_MS - 60_000)
        env.store.inner.put(PERSONAL, newer)
        mark_spent(env, PERSONAL, stale)
        put_slot(env, stale)
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(PERSONAL), newer)
        self.assertEqual(files_holding(env, PERSONAL), [])

    def test_fight_parks_over_a_saved_copy_the_ledger_marks_spent(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        spent = env.profiles.issue(PERSONAL, 0, expires=FUTURE_MS + 60_000)
        env.store.inner.put(PERSONAL, spent)
        mark_spent(env, PERSONAL, spent)
        foreign = env.profiles.issue(PERSONAL, 2)
        put_slot(env, foreign)
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.parked(PERSONAL), foreign)
        self.assertEqual(files_holding(env, PERSONAL), [])

    def test_fight_keeps_both_copies_when_nothing_decides(self):
        cases = {
            "the saved copy is not newer": (FUTURE_MS, FUTURE_MS),
            "the saved copy expires later": (FUTURE_MS + 60_000, FUTURE_MS),
        }
        for label, (saved_at, foreign_at) in cases.items():
            with self.subTest(label):
                env = Env(self)
                watcher = self.fought(env, FakeClock())
                saved = env.profiles.issue(PERSONAL, 7, expires=saved_at)
                env.store.inner.put(PERSONAL, saved)
                foreign = env.profiles.issue(PERSONAL, 2, expires=foreign_at)
                put_slot(env, foreign)
                self.assertEqual(len(watcher.poll()), 1)
                self.assertEqual(env.live(), env.signins[WORK])
                kept = [env.parked(PERSONAL)] + files_holding(env, PERSONAL)
                self.assertEqual(sorted(p["accessToken"] for p in kept), sorted([saved["accessToken"], foreign["accessToken"]]))
                self.assertEqual(env.parked(PERSONAL), saved if saved_at > foreign_at else foreign)

    def test_watcher_writes_the_pair_a_renewal_replaced_to_the_ledger(self):
        env = Env(self)
        clock = FakeClock()
        watcher = self.fought(env, clock)
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        put_slot(env, renewed)
        with mock.patch.object(switching.ledger, "add", side_effect=ledger.LedgerError("no")):
            self.assertEqual(watcher.poll(), [RECORD_RETRY])
        self.assertEqual(ledger.spent(env.lock_path.parent, WORK), frozenset())
        clock.now += 15
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(ledger.spent(env.lock_path.parent, WORK), frozenset({fingerprint(env.signins[WORK])}))
        self.assertNotIn("SECRET", (env.lock_path.parent / ledger.NAME).read_text())
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(renewed))

    def test_watcher_adopts_a_shorter_renewal_and_never_fights_it(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        renewal = env.profiles.issue(WORK, 2, expires=FUTURE_MS - 3_600_000)
        put_slot(env, renewal)
        writes = len(env.slot.writes)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), renewal)
        self.assertEqual(len(env.slot.writes), writes)
        self.assertEqual(ledger.spent(env.lock_path.parent, WORK), frozenset())
        self.assertNotEqual(kept_outside_the_slot(env, env.signins[WORK]), [])
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(renewal))

    def test_watcher_keeps_the_held_copy_when_it_cannot_record_the_change(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        changed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        put_slot(env, changed)
        writes = len(env.slot.writes)
        with mock.patch.object(switching.ledger, "add", side_effect=ledger.LedgerError("no")):
            self.assertEqual(watcher.poll(), [RECORD_RETRY])
        self.assertEqual(env.live(), changed)
        self.assertEqual(len(env.slot.writes), writes)
        self.assertEqual(files_holding(env, WORK), [env.signins[WORK]])

    def test_watcher_secures_its_choice_beside_an_undecided_saved_copy(self):
        for confirmed in (True, False):
            with self.subTest(record_confirms_the_choice=confirmed):
                env = Env(self)
                watcher = self.fought(env, FakeClock())
                if not confirmed:
                    write_record(env.record_path, WORK)
                other = env.profiles.issue(WORK, 7)
                env.store.inner.put(WORK, other)
                put_slot(env, env.profiles.issue(PERSONAL, 2))
                events = []
                real_set_aside = recovery.set_aside

                def logged(folder, uuid, oauth):
                    events.append(("aside", uuid, oauth["accessToken"]))
                    return real_set_aside(folder, uuid, oauth)

                env.store.after_put = lambda uuid: events.append(("put", uuid, env.parked(uuid)["accessToken"]))
                with mock.patch.object(recovery, "set_aside", side_effect=logged):
                    self.assertEqual(len(watcher.poll()), 1)
                self.assertEqual(env.live(), env.signins[WORK])
                self.assertIsNone(env.parked(WORK))
                chosen = [event for event in events if event[1] == WORK]
                held_copy = ("put", WORK, env.signins[WORK]["accessToken"])
                self.assertEqual(chosen, [("aside", WORK, other["accessToken"]), held_copy])

    def test_watcher_standing_down_keeps_its_choice_beside_a_newer_saved_copy(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        newer = env.profiles.issue(WORK, 7, expires=FUTURE_MS + 60_000)
        env.store.inner.put(WORK, newer)
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        record_confirmed(env.record_path, PERSONAL, env.slot.item, datetime.now(timezone.utc))
        self.assertEqual(watcher.poll(), [])
        self.assertFalse(watcher.active)
        self.assertEqual(env.parked(WORK), newer)
        self.assertEqual(files_holding(env, WORK), [env.signins[WORK]])

    def test_seeded_watcher_restores_a_saved_copy_the_ledger_proves_newer(self):
        env = Env(self)
        result = env.switch(WORK)
        g2 = env.profiles.issue(WORK, 2)
        env.store.inner.put(WORK, g2)
        mark_spent(env, WORK, env.signins[WORK])
        watcher = env.watcher(WORK, FakeClock(), seed=result, lifetime=None)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), g2)
        self.assertIsNone(env.parked(WORK))

    def test_watcher_standing_down_sets_an_older_saved_copy_aside(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        older = env.profiles.issue(WORK, 7, expires=FUTURE_MS - 60_000)
        env.store.inner.put(WORK, older)
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        record_confirmed(env.record_path, PERSONAL, env.slot.item, datetime.now(timezone.utc))
        self.assertEqual(len(watcher.poll()), 1)
        self.assertFalse(watcher.active)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(files_holding(env, WORK), [older])

    def test_watcher_sets_its_held_copy_aside_before_adopting_a_newer_saved_copy(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        write_record(env.record_path, WORK)
        newer = env.profiles.issue(WORK, 7, expires=FUTURE_MS + 60_000)
        env.store.inner.put(WORK, newer)
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        writes = len(env.slot.writes)
        seen = []
        real = recovery.set_aside

        def logged(folder, uuid, oauth):
            seen.append((uuid, oauth["accessToken"], len(env.slot.writes)))
            return real(folder, uuid, oauth)

        with mock.patch.object(recovery, "set_aside", side_effect=logged):
            self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.live(), newer)
        self.assertIn((WORK, env.signins[WORK]["accessToken"], writes), seen)

    def test_hand_over_keeps_the_saved_copy_it_cannot_judge(self):
        env = Env(self)
        clock = FakeClock()
        watcher = self.fought(env, clock)
        first = env.profiles.issue(PERSONAL, 2)
        put_slot(env, first)
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.parked(PERSONAL), first)
        clock.now += 60
        again = env.profiles.issue(PERSONAL, 3)
        put_slot(env, again)
        reports = watcher.poll()
        self.assertIn("again", reports[0])
        self.assertIsNone(env.parked(PERSONAL))
        self.assertIn(first["accessToken"], [p["accessToken"] for p in files_holding(env, PERSONAL)])

    def test_restore_never_replaces_a_record_that_names_a_newer_saved_copy(self):
        env = Env(self)
        env.switch(WORK)
        clock = FakeClock()
        watcher = env.watcher(WORK, clock, lifetime=None)
        self.assertEqual(watcher.poll(), [])
        env.profiles.offline = True
        put_slot(env, env.profiles.issue(STRANGER, 1))
        self.assertEqual(len(watcher.poll()), 1)
        g2 = env.profiles.issue(WORK, 2)
        env.store.inner.put(WORK, g2)
        record_confirmed(env.record_path, WORK, {"claudeAiOauth": g2}, datetime.now(timezone.utc))
        env.profiles.offline = False
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        clock.now += 121
        env.store.fail[("get", WORK, "before")] = 1
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(g2))
        clock.now += 15
        watcher.poll()
        self.assertEqual(env.live(), g2)
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(g2))

    def test_watcher_never_adopts_the_copy_a_proven_newer_saved_copy_replaced(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        watcher.poll()
        env.profiles.offline = True
        put_slot(env, env.profiles.issue(STRANGER, 1))
        watcher.poll()
        renewal = env.profiles.issue(WORK, 2, expires=FUTURE_MS - 60_000)
        env.store.inner.put(WORK, renewal)
        mark_spent(env, WORK, env.signins[WORK])
        env.profiles.offline = False
        put_slot(env, env.signins[WORK])
        clock.now += 15
        watcher.poll()
        self.assertEqual(env.live(), renewal)
        put_slot(env, env.signins[WORK])
        clock.now += 15
        watcher.poll()
        self.assertEqual(env.live(), renewal)
        self.assertNotIn(fingerprint(renewal), ledger.spent(env.lock_path.parent, WORK))

    def test_watcher_records_the_held_copy_only_when_the_new_pair_ends_later(self):
        for label, delta in {"later": 60_000, "equal": 0, "earlier": -60_000}.items():
            with self.subTest(label):
                env = Env(self)
                watcher = self.fought(env, FakeClock())
                held = env.signins[WORK]
                changed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + delta)
                put_slot(env, changed)
                writes = len(env.slot.writes)
                self.assertEqual(watcher.poll(), [])
                self.assertEqual(env.live(), changed)
                self.assertEqual(len(env.slot.writes), writes)
                spent = ledger.spent(env.lock_path.parent, WORK)
                if delta > 0:
                    self.assertEqual(spent, frozenset({fingerprint(held)}))
                    self.assertEqual(kept_outside_the_slot(env, held), [])
                else:
                    self.assertEqual(spent, frozenset())
                    self.assertNotEqual(kept_outside_the_slot(env, held), [])

    def test_equal_expiry_stale_write_back_keeps_both_and_loads_the_newest(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        first = env.watcher(WORK, clock, seed=result, lifetime=None)
        g1 = env.signins[WORK]
        first.poll()
        g2 = env.profiles.issue(WORK, 2)
        put_slot(env, g2)
        first.poll()
        put_slot(env, g1)
        clock.now += 15
        writes = len(env.slot.writes)
        first.poll()
        self.assertEqual(env.live(), g1)
        self.assertEqual(len(env.slot.writes), writes)
        self.assertEqual(ledger.spent(env.lock_path.parent, WORK), frozenset())
        self.assertNotEqual(kept_outside_the_slot(env, g2), [])
        fresh = env.watcher(WORK, clock, lifetime=None)
        clock.now += 15
        fresh.poll()
        env.switch(ALEX)
        env.switch(WORK)
        self.assertEqual(env.live(), g2)
        env.assert_others_kept()

    def test_switch_records_when_each_pair_was_first_seen_live(self):
        env = Env(self)
        env.switch(WORK)
        base = env.lock_path.parent
        cmd_seen = ledger.seen(base, WORK)
        self.assertIn(fingerprint(env.signins[WORK]), cmd_seen)
        self.assertIn(fingerprint(env.signins[ALEX]), ledger.seen(base, ALEX))
        first = cmd_seen[fingerprint(env.signins[WORK])]
        env.switch(ALEX)
        env.switch(WORK)
        self.assertEqual(ledger.seen(base, WORK)[fingerprint(env.signins[WORK])], first)
        self.assertNotIn("SECRET", (base / ledger.NAME).read_text())

    def test_a_fight_loads_the_copy_first_seen_live_later_on_an_expiry_tie(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        held = env.signins[WORK]
        later = env.profiles.issue(WORK, 2)
        env.store.inner.put(WORK, later)
        with held_lock(env):
            ledger.see(env.lock_path.parent, WORK, [later])
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.live(), later)
        self.assertNotEqual(kept_outside_the_slot(env, held), [])

    def test_standing_down_keeps_the_copy_first_seen_live_later_in_the_store(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        held = env.signins[WORK]
        later = env.profiles.issue(WORK, 2)
        env.store.inner.put(WORK, later)
        with held_lock(env):
            ledger.see(env.lock_path.parent, WORK, [later])
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        record_confirmed(env.record_path, PERSONAL, env.slot.item, datetime.now(timezone.utc))
        self.assertEqual(watcher.poll(), [])
        self.assertFalse(watcher.active)
        self.assertEqual(env.parked(WORK), later)
        self.assertEqual(files_holding(env, WORK), [held])

    def test_switch_names_accounts_from_its_first_read_of_the_list(self):
        env = Env(self)
        real = env.registry.by_uuid
        calls = []

        def flaky(uuid):
            calls.append(uuid)
            if len(calls) > 2:
                raise OSError("busy")
            return real(uuid)

        with mock.patch.object(env.registry, "by_uuid", side_effect=flaky), failing_relabel():
            with self.assertRaises(SwitchFailed) as caught:
                env.switch(WORK)
        self.assertIn("Alex's Account", str(caught.exception))
        self.assertNotIn(switching.OUTSIDER, str(caught.exception))

    def test_watcher_waits_to_report_until_it_can_read_the_names(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        real = env.registry.by_uuid
        busy = [True]

        def flaky(uuid):
            if uuid == WORK and busy[0]:
                raise OSError("busy")
            return real(uuid)

        with mock.patch.object(env.registry, "by_uuid", side_effect=flaky):
            self.assertEqual(watcher.poll(), [])
            self.assertEqual(env.live(), env.signins[WORK])
            busy[0] = False
            reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Personal Account", reports[0])
        self.assertIn("Work Account", reports[0])
        self.assertNotIn(switching.OUTSIDER, reports[0])

    def test_stale_note_names_what_the_slot_holds_and_returns_when_it_moves_on(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        stale = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)
        mark_spent(env, WORK, stale)
        put_slot(env, stale)
        env.slot.write_plan = [refuse_write]
        first = watcher.poll()
        self.assertEqual(len(first), 1)
        self.assertIn("an older sign-in", first[0])
        env.slot.write_plan = [refuse_write]
        self.assertEqual(watcher.poll(), [])
        other = env.profiles.issue(WORK, 9, expires=FUTURE_MS - 30_000)
        mark_spent(env, WORK, other)
        put_slot(env, other)
        env.slot.write_plan = [replace_then_fail({**env.slot.item, "claudeAiOauth": env.profiles.issue(STRANGER, 1)})]
        second = watcher.poll()
        self.assertEqual(len(second), 1)
        self.assertIn("a different sign-in", second[0])
        self.assertIn("Work Account", second[0])
        env.assert_secret_free(*first, *second)

    def test_stale_note_says_when_the_slot_could_not_be_read(self):
        env = Env(self)
        watcher = self.fought(env, FakeClock())
        put_slot(env, spent_copy(env, env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)))
        env.slot.write_plan = [refuse_write]
        env.slot.read_plan = [None, None, None, unreadable]
        reports = watcher.poll()
        self.assertEqual(
            reports,
            [
                "Lifeline could not read the sign-in in Claude Code, so it could not put back "
                "the newer sign-in for Work Account yet. It will keep trying."
            ],
        )
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), env.signins[WORK])

    def test_rollback_that_stays_on_the_target_still_says_the_recovery_file_was_damaged(self):
        env = Env(self)
        folder = folder_of(env)
        folder.mkdir(mode=0o700, parents=True)
        (folder / f"{WORK}.json").write_text("{broken")
        env.slot.write_plan = [None, refuse_write]
        with failing_relabel():
            with self.assertRaises(SwitchFailed) as caught:
                env.switch(WORK)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertIn("damaged", str(caught.exception))
        self.assertIn(WRONG_NAME, str(caught.exception))


    def test_the_switch_proves_its_record_only_after_a_choose_by_proof(self):
        env = Env(self)
        env.switch(WORK)
        entry = read_record_entry(env.record_path)
        self.assertEqual((entry["uuid"], entry["refresh_sha256"], entry["proven"]), (WORK, fingerprint(env.signins[WORK]), True))
        env.switch(ALEX)
        recovery.set_aside(folder_of(env), WORK, env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000))
        env.switch(WORK)
        entry = read_record_entry(env.record_path)
        self.assertEqual((entry["uuid"], entry["refresh_sha256"], entry["proven"]), (WORK, fingerprint(env.signins[WORK]), False))
        expire(env)
        env.profiles.offline = True
        self.assertEqual(env.switch(ALEX).from_uuid, WORK)

    def test_a_failed_switch_puts_back_the_record_it_found(self):
        for proven in (False, True):
            with self.subTest(proven=proven):
                env = Env(self)
                record_confirmed(env.record_path, ALEX, env.slot.item, datetime.now(timezone.utc), proven=proven)
                env.slot.write_plan = [refuse_write]
                with self.assertRaises(SwitchFailed):
                    env.switch(WORK)
                entry = read_record_entry(env.record_path)
                self.assertEqual(
                    (entry["uuid"], entry["refresh_sha256"], entry["proven"]),
                    (ALEX, fingerprint(env.signins[ALEX]), proven),
                )

    def test_a_renewal_proves_the_record_and_a_first_sight_does_not(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        watcher.poll()
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        put_slot(env, renewed)
        clock.now += 15
        watcher.poll()
        entry = read_record_entry(env.record_path)
        self.assertEqual((entry["refresh_sha256"], entry["proven"]), (fingerprint(renewed), True))
        fresh = env.watcher(WORK, FakeClock(), lifetime=None)
        fresh.poll()
        entry = read_record_entry(env.record_path)
        self.assertEqual((entry["uuid"], entry["refresh_sha256"], entry["proven"]), (WORK, fingerprint(renewed), False))

    def test_an_older_unknown_write_back_is_adopted_without_proof_and_the_held_copy_is_loaded_later(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        held = env.signins[WORK]
        watcher.poll()
        older = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 3_600_000)
        put_slot(env, older)
        clock.now += 15
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), older)
        self.assertNotEqual(kept_outside_the_slot(env, held), [])
        self.assertNotIn(fingerprint(held), ledger.spent(env.lock_path.parent, WORK))
        entry = read_record_entry(env.record_path)
        self.assertFalse(entry["proven"])
        env.switch(ALEX)
        self.assertNotEqual(kept_outside_the_slot(env, held), [])
        self.assertNotEqual(kept_outside_the_slot(env, older), [])
        env.switch(WORK)
        self.assertEqual(env.live(), held)
        self.assertNotEqual(kept_outside_the_slot(env, older), [])
        env.assert_others_kept()

    def older_after(self, env, clock, watcher, held):
        older = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 3_600_000)
        put_slot(env, older)
        clock.now += 15
        self.assertEqual(watcher.poll(), [])
        self.assertIsNone(watcher._parked)
        self.assertNotEqual(kept_outside_the_slot(env, held), [])
        clock.now += 15
        watcher.poll()
        self.assertTrue(recovery.settle(env.store, folder_of(env), WORK))
        self.assertNotEqual(kept_outside_the_slot(env, held), [])
        env.switch(ALEX)
        self.assertNotEqual(kept_outside_the_slot(env, held), [])
        env.switch(WORK)
        self.assertEqual(env.live(), held)
        self.assertNotEqual(kept_outside_the_slot(env, older), [])
        env.assert_others_kept()

    def test_an_older_pair_after_a_stuck_restore_never_removes_the_parked_copy(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        g1 = env.signins[WORK]
        watcher.poll()
        g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 3_600_000)
        put_slot(env, g2)
        clock.now += 15
        watcher.poll()
        put_slot(env, g1)
        env.slot.write_plan = [refuse_write]
        clock.now += 15
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(watcher._parked, g2)
        self.older_after(env, clock, watcher, g2)

    def test_an_older_pair_after_an_unknown_sign_in_never_removes_the_saved_copy(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        g1 = env.signins[WORK]
        watcher.poll()
        env.profiles.offline = True
        put_slot(env, env.profiles.issue(PERSONAL, 5))
        clock.now += 15
        self.assertEqual(len(watcher.poll()), 1)
        env.profiles.offline = False
        self.assertEqual(watcher._parked, g1)
        self.older_after(env, clock, watcher, g1)

    def test_a_ledger_that_cannot_be_read_never_lets_the_record_decide(self):
        def damaged(path):
            path.write_text("{not json")

        def linked(path):
            target = path.parent / "elsewhere.json"
            os.replace(path, target)
            os.symlink(target, path)

        def unreadable(path):
            os.chmod(path, 0)
            self.addCleanup(lambda: os.chmod(path, 0o600) if path.exists() and not path.is_symlink() else None)

        for spoil in (damaged, linked, unreadable):
            for record_g2 in (False, True):
                with self.subTest(state=spoil.__name__, record_g2=record_g2):
                    env = Env(self)
                    clock = FakeClock()
                    g1, g2 = self.killed_mid_restore(env, clock, record_g2)
                    path = env.lock_path.parent / ledger.NAME
                    spoil(path)
                    self.assertTrue(ledger.problem(env.lock_path.parent))
                    fresh = env.watcher(WORK, clock, lifetime=None)
                    for _ in range(3):
                        clock.now += 15
                        fresh.poll()
                    self.assertNotEqual(kept_outside_the_slot(env, g2), [])
                    env.switch(ALEX)
                    self.assertNotEqual(kept_outside_the_slot(env, g2), [])
                    env.switch(WORK)
                    self.assertEqual(env.live(), g2)
                    self.assertNotEqual(kept_outside_the_slot(env, g1), [])
                    env.assert_others_kept()

    def test_a_first_sight_never_lets_the_record_prove_the_live_pair(self):
        env = Env(self)
        env.switch(WORK)
        g1 = env.signins[WORK]
        self.assertTrue(read_record_entry(env.record_path)["proven"])
        g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        env.store.inner.put(WORK, g2)
        clock = FakeClock()
        watcher = env.watcher(WORK, clock, lifetime=None)
        watcher.poll()
        self.assertFalse(read_record_entry(env.record_path)["proven"])
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        clock.now += 15
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.live(), g2)
        self.assertNotEqual(kept_outside_the_slot(env, g1), [])

    def test_a_restore_without_proof_clears_the_record_proof(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        watcher.poll()
        g2 = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        put_slot(env, g2)
        clock.now += 15
        watcher.poll()
        self.assertEqual(
            (read_record_entry(env.record_path)["refresh_sha256"], read_record_entry(env.record_path)["proven"]),
            (fingerprint(g2), True),
        )
        later = env.profiles.issue(WORK, 3, expires=FUTURE_MS + 120_000)
        env.store.inner.put(WORK, later)
        put_slot(env, env.profiles.issue(PERSONAL, 2))
        clock.now += 15
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.live(), later)
        self.assertNotEqual(kept_outside_the_slot(env, g2), [])
        self.assertFalse(read_record_entry(env.record_path)["proven"])

    def test_a_change_the_ledger_proves_is_adopted_with_proof(self):
        env = Env(self)
        clock = FakeClock()
        result = env.switch(WORK)
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        held = env.signins[WORK]
        watcher.poll()
        mark_spent(env, WORK, held)
        other = env.profiles.issue(WORK, 2, expires=FUTURE_MS - 60_000)
        put_slot(env, other)
        clock.now += 15
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), other)
        self.assertEqual(kept_outside_the_slot(env, held), [])
        entry = read_record_entry(env.record_path)
        self.assertEqual((entry["refresh_sha256"], entry["proven"]), (fingerprint(other), True))

    def test_sign_in_ends_are_refreshed_only_where_one_is_recorded_and_known(self):
        env = Env(self)
        base = int(time.time() * 1000)
        day = 86400 * 1000

        class Filtered:
            def __init__(self, inner):
                self.inner = inner

            def load(self):
                return [account for account in self.inner.load() if account.uuid != ORG]

            def __getattr__(self, name):
                return getattr(self.inner, name)

        env.registry.upsert(dataclasses.replace(env.registry.by_uuid(WORK), signin_ends="2026-11-30T00:00:00Z"))
        known = env.profiles.issue(WORK, 3, expires=base + 8 * 3600 * 1000)
        known["refreshTokenExpiresAt"] = base + 20 * day
        registry = Filtered(env.registry)
        self.assertTrue(switching.note_signin_end(registry, WORK, known))
        self.assertEqual(env.registry.by_uuid(WORK).signin_ends, signin_end_text(known))
        self.assertEqual(len(env.registry.load()), len(ACCOUNTS))
        self.assertFalse(switching.note_signin_end(registry, WORK, known))
        self.assertFalse(switching.note_signin_end(registry, WORK, env.signins[WORK]))
        self.assertEqual(env.registry.by_uuid(WORK).signin_ends, signin_end_text(known))
        self.assertFalse(switching.note_signin_end(registry, PERSONAL, known))
        self.assertIsNone(env.registry.by_uuid(PERSONAL).signin_ends)
        self.assertFalse(switching.note_signin_end(registry, STRANGER, known))

    def test_the_switch_and_the_watcher_keep_sign_in_ends_current(self):
        env = Env(self)
        for uuid in (ALEX, WORK):
            env.registry.upsert(dataclasses.replace(env.registry.by_uuid(uuid), signin_ends="2026-11-30T00:00:00Z"))
        base = int(time.time() * 1000)
        day = 86400 * 1000
        live = env.profiles.issue(ALEX, 3, expires=base + 8 * 3600 * 1000)
        live["refreshTokenExpiresAt"] = base + 20 * day
        put_slot(env, live)
        parked = env.profiles.issue(WORK, 3, expires=base + 8 * 3600 * 1000)
        parked["refreshTokenExpiresAt"] = base + 25 * day
        env.store.inner.put(WORK, parked)
        result = env.switch(WORK)
        ends = {account.uuid: account.signin_ends for account in env.registry.load()}
        self.assertIsNotNone(ends[ALEX])
        self.assertEqual(ends[ALEX], signin_end_text(live))
        self.assertEqual(ends[WORK], signin_end_text(parked))
        self.assertIsNone(ends[PERSONAL])
        clock = FakeClock()
        watcher = env.watcher(WORK, clock, seed=result, lifetime=None)
        watcher.poll()
        renewed = env.profiles.issue(WORK, 4, expires=base + 9 * 3600 * 1000)
        renewed["refreshTokenExpiresAt"] = base + 30 * day
        put_slot(env, renewed)
        clock.now += 15
        locked = []
        real = env.registry.save

        def spy(accounts):
            with self.assertRaises(WriteLockTimeout):
                with held(env.lock_path, timeout=0):
                    pass
            locked.append(True)
            return real(accounts)

        with mock.patch.object(env.registry, "save", side_effect=spy):
            watcher.poll()
            clock.now += 15
            watcher.poll()
        self.assertEqual(locked, [True])
        self.assertEqual(env.registry.by_uuid(WORK).signin_ends, signin_end_text(renewed))

class RefusalTests(unittest.TestCase):
    def assert_refused(self, env, target, kind=SwitchRefused, **extra):
        before = env.snapshot()
        with self.assertRaises(kind) as caught:
            env.switch(target, **extra)
        self.assertEqual(env.snapshot(), before)
        self.assertTrue(str(caught.exception).endswith("."))
        env.assert_secret_free(caught.exception)
        return caught.exception

    def test_refuses_the_account_already_in_use(self):
        env = Env(self)
        error = self.assert_refused(env, ALEX)
        self.assertIn("Alex's Account", str(error))

    def test_refuses_an_account_not_in_lifeline(self):
        env = Env(self)
        self.assert_refused(env, STRANGER)

    def test_refuses_a_target_without_a_parked_signin(self):
        env = Env(self)
        env.store.inner.remove(WORK)
        error = self.assert_refused(env, WORK)
        self.assertIn("Work Account", str(error))

    def test_refuses_a_damaged_parked_signin(self):
        env = Env(self)
        env.store.inner.put(WORK, {"accessToken": "access-22222222-7-SECRET", "expiresAt": FUTURE_MS})
        self.assert_refused(env, WORK)

    def test_refuses_when_target_details_are_missing(self):
        env = Env(self)
        accounts = env.registry.load()
        for account in accounts:
            if account.uuid == WORK:
                account.oauth_account = {}
        env.registry.save(accounts)
        self.assert_refused(env, WORK)

    def test_refuses_when_live_account_is_not_in_lifeline(self):
        env = Env(self)
        env.slot.item["claudeAiOauth"] = env.profiles.issue(STRANGER, 1)
        self.assert_refused(env, WORK)

    def test_refuses_when_claude_code_is_signed_out(self):
        env = Env(self)
        env.slot.item = None
        self.assert_refused(env, WORK)
        env.slot.item = {"mcpOAuth": mcp_signins()}
        self.assert_refused(env, WORK)

    def test_refuses_when_the_slot_cannot_be_read(self):
        env = Env(self)
        env.slot.read_plan = [unreadable]
        self.assert_refused(env, WORK)

    def test_refuses_when_the_account_file_cannot_be_read(self):
        env = Env(self)
        env.account_file.write_text("{not json")
        self.assert_refused(env, WORK)

    def test_refuses_when_the_target_parked_copy_cannot_be_read(self):
        env = Env(self)
        env.store.fail[("get", WORK, "before")] = 1
        error = self.assert_refused(env, WORK)
        self.assertIn("Work Account", str(error))

    def test_refuses_when_the_live_account_parked_copy_cannot_be_read(self):
        env = Env(self)
        env.store.fail[("get", ALEX, "before")] = 1
        error = self.assert_refused(env, WORK)
        self.assertIn("Alex's Account", str(error))

    def test_refuses_when_the_list_of_accounts_cannot_be_read(self):
        env = Env(self)
        (env.home / "accounts.json").write_text("{broken")
        self.assert_refused(env, WORK)


def expire(env):
    env.slot.item["claudeAiOauth"]["expiresAt"] = PAST_MS


class IdentityTests(unittest.TestCase):
    def test_identity_refusal_changes_nothing(self):
        cases = {
            "record names another account": PERSONAL,
            "no record at all": None,
            "record matches but the key has not expired": ALEX,
        }
        for label, record in cases.items():
            with self.subTest(label):
                env = Env(self, record=record)
                env.profiles.offline = True
                before = env.snapshot()
                with self.assertRaises(IdentityUnknown) as caught:
                    env.switch(WORK)
                self.assertIsInstance(caught.exception, SwitchRefused)
                self.assertEqual(env.snapshot(), before)
                self.assertEqual(env.profiles.calls, [env.signins[ALEX]["accessToken"]])
                env.assert_secret_free(caught.exception)

    def test_profile_failure_on_an_unexpired_key_is_refused(self):
        env = Env(self, record=ALEX)
        env.profiles.offline = True
        before = env.snapshot()
        with self.assertRaises(IdentityUnknown):
            env.switch(WORK)
        self.assertEqual(env.snapshot(), before)

    def test_refused_unexpired_key_is_refused(self):
        env = Env(self, record=ALEX)
        env.profiles.dead.add(env.signins[ALEX]["accessToken"])
        before = env.snapshot()
        with self.assertRaises(IdentityUnknown):
            env.switch(WORK)
        self.assertEqual(env.snapshot(), before)

    def test_identity_from_record_when_key_expired_and_hash_matches(self):
        env = Env(self, record=ALEX)
        expire(env)
        result = env.switch(WORK)
        self.assertEqual(result.from_uuid, ALEX)
        self.assertNotIn(env.signins[ALEX]["accessToken"], env.profiles.calls)
        self.assertEqual(env.parked(ALEX)["refreshToken"], env.signins[ALEX]["refreshToken"])

    def test_identity_refusal_when_hash_is_missing_or_different(self):
        cases = {
            "record has no hash": dict(record=ALEX, record_hash=False),
            "record hash is another sign-in's": dict(record=ALEX),
        }
        for label, options in cases.items():
            with self.subTest(label):
                env = Env(self, **options)
                if label == "record hash is another sign-in's":
                    older = env.profiles.issue(ALEX, 0)
                    record_confirmed(env.record_path, ALEX, {"claudeAiOauth": older}, datetime.now(timezone.utc))
                expire(env)
                before = env.snapshot()
                with self.assertRaises(IdentityUnknown):
                    env.switch(WORK)
                self.assertEqual(env.snapshot(), before)
                self.assertEqual(env.profiles.calls, [])

    def test_identity_refusal_when_key_expired_and_record_disagrees(self):
        env = Env(self, record=ORG)
        expire(env)
        before = env.snapshot()
        with self.assertRaises(IdentityUnknown):
            env.switch(WORK)
        self.assertEqual(env.snapshot(), before)
        self.assertEqual(env.profiles.calls, [])

    def test_identity_refusal_when_account_file_disagrees_with_record(self):
        env = Env(self, record=ALEX)
        data = env.config()
        data["oauthAccount"] = details(PERSONAL)
        env.write_config(data)
        expire(env)
        before = env.snapshot()
        with self.assertRaises(IdentityUnknown):
            env.switch(WORK)
        self.assertEqual(env.snapshot(), before)
        self.assertEqual(env.profiles.calls, [])

    def test_identity_trusts_profile_over_relabelled_account_file(self):
        env = Env(self, record=PERSONAL)
        data = env.config()
        data["oauthAccount"] = details(PERSONAL)
        env.write_config(data)
        result = env.switch(WORK)
        self.assertEqual(result.from_uuid, ALEX)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.parked(PERSONAL), env.signins[PERSONAL])

    def test_identify_live_is_public_and_follows_the_same_rule(self):
        env = Env(self, record=ALEX)
        item = copy.deepcopy(env.slot.item)
        common = dict(profile_fn=env.profiles, home=env.home, record_path=env.record_path, registry=env.registry)
        self.assertEqual(identify_live(item, **common), ALEX)
        env.profiles.offline = True
        with self.assertRaises(IdentityUnknown) as caught:
            identify_live(item, **common)
        env.assert_secret_free(caught.exception)
        stale = copy.deepcopy(item)
        stale["claudeAiOauth"]["expiresAt"] = PAST_MS
        self.assertEqual(identify_live(stale, **common), ALEX)
        record_confirmed(env.record_path, ORG, item, datetime.now(timezone.utc))
        with self.assertRaises(IdentityUnknown):
            identify_live(stale, **common)
        write_record(env.record_path, ALEX)
        with self.assertRaises(IdentityUnknown):
            identify_live(stale, **common)
        with self.assertRaises(IdentityUnknown):
            identify_live({"mcpOAuth": mcp_signins()}, **common)
        with self.assertRaises(IdentityUnknown):
            identify_live(None, **common)

    def test_an_identity_failure_says_what_failed(self):
        from lifeline.anthropic import CertificateFailed, UnexpectedReply

        cases = (
            (CertificateFailed(), switching.CERTIFICATE_UNCHECKED),
            (SignInDead(), switching.LIVE_REFUSED),
            (UnexpectedReply("odd"), switching.LIVE_UNCHECKED),
            (Unavailable("Could not reach the server"), switching.UNCONFIRMED),
        )
        for error, sentence in cases:
            with self.subTest(error=type(error).__name__):
                env = Env(self)
                item = copy.deepcopy(env.slot.item)

                def failing(token, error=error):
                    raise error

                with self.assertRaises(IdentityUnknown) as caught:
                    identify_live(item, profile_fn=failing, home=env.home, record_path=env.record_path, registry=env.registry)
                self.assertEqual(str(caught.exception), sentence)
                before = env.snapshot()
                with self.assertRaises(IdentityUnknown) as caught:
                    env.switch(WORK, profile_fn=failing)
                self.assertEqual(str(caught.exception), sentence)
                self.assertEqual(env.snapshot(), before)
                env.assert_secret_free(caught.exception)
        env = Env(self)
        stale = copy.deepcopy(env.slot.item)
        stale["claudeAiOauth"]["expiresAt"] = PAST_MS
        with self.assertRaises(IdentityUnknown) as caught:
            identify_live(stale, profile_fn=env.profiles, home=env.home, record_path=env.record_path, registry=env.registry)
        self.assertEqual(str(caught.exception), switching.LIVE_NOT_RENEWED)
        record_confirmed(env.record_path, ORG, stale, datetime.now(timezone.utc))
        with self.assertRaises(IdentityUnknown) as caught:
            identify_live(stale, profile_fn=env.profiles, home=env.home, record_path=env.record_path, registry=env.registry)
        self.assertEqual(str(caught.exception), switching.LIVE_MISMATCHED)
        for sentence in (switching.CERTIFICATE_UNCHECKED, switching.LIVE_REFUSED, switching.LIVE_UNCHECKED, switching.LIVE_NOT_RENEWED, switching.LIVE_MISMATCHED):
            self.assertNotIn("internet", sentence)

    def test_identify_live_works_while_caller_holds_both_locks(self):
        env = Env(self)
        item = copy.deepcopy(env.slot.item)
        with held(env.lock_path, timeout=1), SessionLocks(env.home, timeout=1):
            found = identify_live(item, profile_fn=env.profiles, home=env.home, record_path=env.record_path, registry=env.registry)
        self.assertEqual(found, ALEX)

    def test_crash_after_load_cannot_fool_the_record(self):
        env = Env(self, record=ALEX)

        def crash(path, new, backups, notes=None):
            raise KeyboardInterrupt

        with mock.patch.object(switching.accountfile, "replace_oauth_account", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                env.switch(WORK)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.config()["oauthAccount"], details(ALEX))
        self.assertIsNone(read_record(env.record_path))
        env.profiles.offline = True
        expire(env)
        before = env.snapshot()
        with self.assertRaises(IdentityUnknown):
            env.switch(ORG)
        self.assertEqual(env.snapshot(), before)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])


@contextlib.contextmanager
def failing_relabel(also=None, real_first=False):
    real = accountfile.replace_oauth_account
    calls = []

    def first_fails(path, new, backups, notes=None):
        calls.append(new.get("accountUuid"))
        if len(calls) == 1:
            if real_first:
                real(path, new, backups, notes=notes)
            if also is not None:
                also()
            raise AccountFileError(f"could not update {path}: disk full")
        if real_first:
            raise AccountFileError(f"could not update {path}: disk full")
        return real(path, new, backups, notes=notes)

    with mock.patch.object(switching.accountfile, "replace_oauth_account", side_effect=first_fails):
        yield calls


def arm_a(env):
    env.store.fail[("put", ALEX, "after")] = 1
    return contextlib.nullcontext()


def arm_b(env):
    env.slot.write_plan = [refuse_write]
    return contextlib.nullcontext()


def arm_c(env):
    return failing_relabel()


def arm_d(env):
    env.store.fail[("remove", WORK, "before")] = 2
    return contextlib.nullcontext()


FAILURE_POINTS = {"a": arm_a, "b": arm_b, "c": arm_c, "d": arm_d}


class FailureTests(unittest.TestCase):
    def run_point(self, point):
        env = Env(self)
        original = env.config()
        with FAILURE_POINTS[point](env):
            if point == "d":
                result = env.switch(WORK)
                error = None
            else:
                with self.assertRaises(SwitchFailed) as caught:
                    env.switch(WORK)
                result = None
                error = caught.exception
        return env, original, result, error

    def test_switch_rolls_back_on_failure(self):
        for point in FAILURE_POINTS:
            with self.subTest(point=point):
                env, original, result, error = self.run_point(point)
                env.assert_others_kept()
                if point == "d":
                    self.assertTrue(result.leftover)
                    self.assertEqual(env.live(), env.signins[WORK])
                    self.assertEqual(env.parked(ALEX), env.signins[ALEX])
                    self.assertEqual(env.config()["oauthAccount"], details(WORK))
                    self.assertEqual(read_record(env.record_path), WORK)
                    env.assert_secret_free(result)
                    continue
                self.assertEqual(env.live(), env.signins[ALEX])
                self.assertIsNone(env.parked(ALEX))
                self.assertEqual(env.parked(WORK), env.signins[WORK])
                self.assertEqual(env.config(), original)
                self.assertEqual(read_record(env.record_path), ALEX)
                self.assertTrue(str(error).endswith("."))
                env.assert_secret_free(error)
                env.assert_one_copy()

    def test_mcp_survives_every_path(self):
        for point in FAILURE_POINTS:
            with self.subTest(point=point):
                env, _, _, _ = self.run_point(point)
                self.assertEqual(env.slot.item["mcpOAuth"], mcp_signins())
                for _, written in env.slot.writes:
                    self.assertEqual(written["mcpOAuth"], mcp_signins())
                    self.assertEqual(written["otherKey"], {"kept": True})

    def test_no_path_leaves_a_parked_and_a_live_copy(self):
        for point in ("a", "b", "c"):
            with self.subTest(point=point):
                env, _, _, _ = self.run_point(point)
                env.assert_one_copy()
        env = Env(self)
        env.switch(WORK)
        env.assert_one_copy()
        result = env.switch(PERSONAL)
        env.assert_one_copy()
        undo(result, **env.kwargs())
        env.assert_one_copy()

    def test_failure_a_park_fails_before_writing(self):
        env = Env(self)
        env.store.fail[("put", ALEX, "before")] = 1
        with self.assertRaises(SwitchFailed) as caught:
            env.switch(WORK)
        self.assertEqual(env.slot.writes, [])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        env.assert_secret_free(caught.exception)

    def test_failure_a_record_cannot_be_cleared(self):
        env = Env(self)
        real = switching.write_record

        def no_clear(path, uuid, now=None):
            if uuid is None:
                raise OSError("read-only")
            return real(path, uuid, now)

        with mock.patch.object(switching, "write_record", side_effect=no_clear):
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(env.slot.writes, [])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_failure_a_compromised_lock_stops_before_writing(self):
        env = Env(self)
        json_lock = env.home / ".claude.json.lock"

        def steal(token):
            os.rmdir(json_lock)
            os.mkdir(json_lock)
            time.sleep(0.3)

        env.profiles.hook = steal
        with mock.patch.object(SessionLocks, "touch_interval", 0.02):
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(env.slot.writes, [])
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_slot_changed_before_write_keeps_parked_copy(self):
        env = Env(self)
        foreign = env.profiles.issue(PERSONAL, 2)

        def other_app(token):
            env.slot.item["claudeAiOauth"] = copy.deepcopy(foreign)

        env.profiles.hook = other_app
        with self.assertRaises(SwitchFailed) as caught:
            env.switch(WORK)
        self.assertEqual(env.slot.writes, [])
        self.assertEqual(env.live(), foreign)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(read_record(env.record_path))
        env.assert_secret_free(caught.exception)

    def test_failure_b_write_landed_despite_error_continues(self):
        env = Env(self)
        env.slot.write_plan = [land_then_fail]
        result = env.switch(WORK)
        self.assertEqual(result.to_uuid, WORK)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(env.config()["oauthAccount"], details(WORK))
        env.assert_others_kept()
        env.assert_one_copy()

    def test_failure_b_rechecks_before_dropping_the_parked_copy(self):
        env = Env(self)
        env.slot.write_plan = [hold_back]
        env.slot.read_plan = [None, None, None, land_late]
        result = env.switch(WORK, settle=0.01)
        self.assertEqual(result.to_uuid, WORK)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        env.assert_one_copy()

    def test_failure_b_slot_holds_something_else(self):
        env = Env(self)
        stranger = {"claudeAiOauth": env.profiles.issue(STRANGER, 1), "mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}
        env.slot.write_plan = [replace_then_fail(stranger)]
        original = env.account_file.read_bytes()
        with self.assertRaises(SwitchFailed) as caught:
            env.switch(WORK)
        self.assertEqual(env.slot.item, stranger)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(env.account_file.read_bytes(), original)
        self.assertIsNone(read_record(env.record_path))
        self.assertIn("Alex's Account", str(caught.exception))
        self.assertIn("Work Account", str(caught.exception))
        env.assert_secret_free(caught.exception)

    def test_failure_b_slot_cannot_be_read_back(self):
        env = Env(self)
        env.slot.write_plan = [refuse_write]
        env.slot.read_plan = [None, None, unreadable, unreadable, unreadable]
        with self.assertRaises(SwitchFailed):
            env.switch(WORK, settle=0.01)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(read_record(env.record_path))

    def test_failure_c_after_the_file_was_replaced_restores_it(self):
        env = Env(self)
        original = env.config()
        real_read_back = accountfile._read_back
        calls = []

        def mismatch_once(path):
            calls.append(1)
            if len(calls) == 1:
                return b"{}"
            return real_read_back(path)

        with mock.patch.object(accountfile, "_read_back", side_effect=mismatch_once):
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(env.config(), original)
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(len(env.backup_names()), 2)
        env.assert_others_kept()
        env.assert_one_copy()

    def test_failure_c_when_switching_back_also_fails(self):
        env = Env(self)
        env.slot.write_plan = [None, refuse_write]
        with failing_relabel():
            with self.assertRaises(SwitchFailed) as caught:
                env.switch(WORK)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertIsNone(env.parked(WORK))
        self.assertIn("Work Account", str(caught.exception))
        self.assertIn(WRONG_NAME, str(caught.exception))
        env.assert_others_kept()
        env.assert_one_copy()
        env.assert_secret_free(caught.exception)

    def test_failure_c_when_the_slot_changes_during_the_rollback(self):
        stranger_item = {"claudeAiOauth": None, "mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}
        outcomes = {
            "other": lambda env: setattr(env.slot, "item", dict(stranger_item, claudeAiOauth=env.profiles.issue(STRANGER, 1))),
            "empty": lambda env: setattr(env.slot, "item", {"mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}),
            "unreadable": lambda env: setattr(env.slot, "read_plan", [unreadable, unreadable, unreadable]),
        }
        for label, change in outcomes.items():
            with self.subTest(label):
                env = Env(self)
                with failing_relabel(also=lambda env=env, change=change: change(env)):
                    with self.assertRaises(SwitchFailed) as caught:
                        env.switch(WORK, settle=0.01)
                self.assertEqual(env.parked(ALEX), env.signins[ALEX])
                self.assertEqual(env.parked(WORK), env.signins[WORK])
                self.assertIsNone(read_record(env.record_path))
                self.assertIn("Alex's Account", str(caught.exception))
                self.assertIn("Work Account", str(caught.exception))
                env.assert_secret_free(caught.exception)

    def test_failure_c_names_a_wrong_account_name_it_could_not_restore(self):
        env = Env(self)
        with failing_relabel(real_first=True) as calls:
            with self.assertRaises(SwitchFailed) as caught:
                env.switch(WORK)
        self.assertEqual(calls, [WORK, ALEX])
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(env.config()["oauthAccount"], details(WORK))
        self.assertIn(WRONG_NAME, str(caught.exception))
        self.assertEqual(read_record(env.record_path), ALEX)

    def test_failure_a_fresh_read_fails_but_slot_still_holds_live(self):
        env = Env(self)
        env.slot.read_plan = [None, unreadable, None, None]
        with self.assertRaises(SwitchFailed):
            env.switch(WORK, settle=0.01)
        self.assertEqual(env.slot.writes, [])
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(read_record(env.record_path), ALEX)
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(env.signins[ALEX]))

    def test_failure_b_slot_left_empty(self):
        env = Env(self)
        emptied = {"mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}
        env.slot.write_plan = [replace_then_fail(emptied)]
        with self.assertRaises(SwitchFailed) as caught:
            env.switch(WORK)
        self.assertIn("no sign-in", str(caught.exception))
        self.assertEqual(env.slot.item, emptied)
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_failure_a_failed_put_keeps_an_older_parked_copy(self):
        env = Env(self)
        older = env.profiles.issue(ALEX, 0, expires=FUTURE_MS - 60_000)
        env.store.inner.put(ALEX, older)
        env.store.fail[("put", ALEX, "before")] = 1
        with self.assertRaises(SwitchFailed):
            env.switch(WORK)
        self.assertEqual(env.parked(ALEX), older)
        self.assertEqual(env.live(), env.signins[ALEX])

    def test_failure_c_rechecks_before_dropping_the_target_copy(self):
        env = Env(self)
        env.slot.write_plan = [None, hold_back]
        env.slot.read_plan = [None, None, None, None, land_late]
        with failing_relabel():
            with self.assertRaises(SwitchFailed):
                env.switch(WORK, settle=0.01)
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(env.parked(ALEX))
        env.assert_others_kept()
        env.assert_one_copy()

    def test_failure_d_leftover_is_reported_not_undone(self):
        env = Env(self)
        env.store.fail[("remove", WORK, "before")] = 1
        result = env.switch(WORK)
        self.assertFalse(result.leftover)
        self.assertIsNone(env.parked(WORK))
        env = Env(self)
        env.store.fail[("remove", WORK, "before")] = 2
        result = env.switch(WORK)
        self.assertTrue(result.leftover)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.config()["oauthAccount"], details(WORK))


class ConcurrencyTests(unittest.TestCase):
    def run_in_thread(self, env, target, **extra):
        outcome = {}

        def run():
            try:
                outcome["result"] = env.switch(target, **extra)
            except BaseException as error:
                outcome["error"] = error

        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(worker.join, 10)
        return worker, outcome

    def test_switch_survives_concurrent_claude_json_write(self):
        env = Env(self)
        json_lock = env.home / ".claude.json.lock"
        json_lock.mkdir()
        worker, outcome = self.run_in_thread(env, WORK)
        time.sleep(0.3)
        self.assertTrue(worker.is_alive())
        self.assertEqual(env.slot.writes, [])
        data = env.config()
        data["writtenWhileLocked"] = 1
        env.write_config(data)
        json_lock.rmdir()
        worker.join(10)
        self.assertNotIn("error", outcome)
        after = env.config()
        self.assertEqual(after["oauthAccount"], details(WORK))
        self.assertEqual(after["writtenWhileLocked"], 1)

    def test_switch_survives_a_racing_writer_that_honours_the_lock(self):
        env = Env(self)
        json_lock = env.home / ".claude.json.lock"
        rounds = 25
        stop = threading.Event()

        def writer():
            for i in range(rounds):
                while not stop.is_set():
                    try:
                        os.mkdir(json_lock)
                        break
                    except FileExistsError:
                        time.sleep(0.001)
                try:
                    data = env.config()
                    data[f"writer{i}"] = i
                    temp = env.account_file.with_name(".claude.json.writer")
                    temp.write_text(json.dumps(data, indent=2) + "\n")
                    os.replace(temp, env.account_file)
                finally:
                    os.rmdir(json_lock)
                time.sleep(0.003)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = env.switch(WORK, session_timeout=10)
        finally:
            thread.join(20)
            stop.set()
        self.assertEqual(result.to_uuid, WORK)
        after = env.config()
        self.assertEqual(after["oauthAccount"], details(WORK))
        for i in range(rounds):
            self.assertEqual(after[f"writer{i}"], i)

    def test_switch_survives_a_writer_that_ignores_the_lock(self):
        env = Env(self)
        real_backup = accountfile._write_backup

        def sneak(backups, raw):
            target = real_backup(backups, raw)
            data = env.config()
            data["sneakedIn"] = True
            env.write_config(data)
            return target

        with mock.patch.object(accountfile, "_write_backup", side_effect=sneak):
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        after = env.config()
        self.assertTrue(after["sneakedIn"])
        self.assertEqual(after["oauthAccount"], details(ALEX))
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        env.assert_others_kept()
        env.assert_one_copy()

    def test_switch_waits_for_refresh_lock(self):
        env = Env(self)
        refresh_lock = env.home / ".claude" / ".oauth_refresh.lock"
        refresh_lock.mkdir()
        worker, outcome = self.run_in_thread(env, WORK, session_timeout=10)
        time.sleep(0.4)
        self.assertTrue(worker.is_alive())
        self.assertEqual(env.slot.writes, [])
        self.assertEqual(env.profiles.calls, [])
        self.assertIsNone(env.parked(ALEX))
        renewed = env.profiles.issue(ALEX, 2)
        env.slot.item["claudeAiOauth"] = copy.deepcopy(renewed)
        released = time.monotonic()
        refresh_lock.rmdir()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"].to_uuid, WORK)
        self.assertGreater(env.slot.writes[0][0], released)
        self.assertEqual(env.parked(ALEX), renewed)
        self.assertEqual(env.profiles.calls, [renewed["accessToken"], env.signins[WORK]["accessToken"]])
        self.assertFalse(refresh_lock.exists())

    def test_switch_gives_up_when_refresh_lock_stays_held(self):
        env = Env(self)
        (env.home / ".claude" / ".oauth_refresh.lock").mkdir()
        before = env.snapshot()
        with self.assertRaises(SwitchRefused):
            env.switch(WORK, session_timeout=0.3)
        self.assertEqual(env.snapshot(), before)
        self.assertEqual(env.profiles.calls, [])

    def test_switch_waits_for_lifeline_lock(self):
        env = Env(self)
        release = threading.Event()
        holding = threading.Event()

        def hold():
            with held(env.lock_path, timeout=1):
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(holding.wait(5))
        worker, outcome = self.run_in_thread(env, WORK)
        time.sleep(0.3)
        self.assertTrue(worker.is_alive())
        self.assertEqual(env.slot.writes, [])
        self.assertEqual(env.profiles.calls, [])
        release.set()
        holder.join(5)
        worker.join(10)
        self.assertNotIn("error", outcome)
        self.assertEqual(env.live(), env.signins[WORK])

    def test_switch_refuses_when_lifeline_lock_stays_busy(self):
        env = Env(self)
        before = env.snapshot()
        with held(env.lock_path, timeout=1):
            with self.assertRaises(LifelineBusy) as caught:
                env.switch(WORK, lock_timeout=0.2)
        self.assertIsInstance(caught.exception, SwitchRefused)
        self.assertEqual(env.snapshot(), before)
        self.assertFalse((env.home / ".claude" / ".oauth_refresh.lock").exists())


class LaterCheckTests(unittest.TestCase):
    def test_a_switch_can_leave_its_check_for_later(self):
        env = Env(self)
        before = len(env.profiles.calls)
        result = env.switch(WORK, confirm=False)
        self.assertEqual(env.live_owner(), WORK)
        self.assertIsNone(result.confirmed)
        self.assertEqual(len(env.profiles.calls) - before, 1)
        self.assertIsNone(read_record_entry(env.record_path)["refresh_sha256"])
        self.assertEqual(result.signin.open(), env.signins[WORK])
        checked = switching.confirm_switch(
            result, slot=env.slot, profile_fn=env.profiles, registry=env.registry, lock_path=env.lock_path, lock_timeout=5
        )
        self.assertTrue(checked.confirmed)
        self.assertEqual(len(env.profiles.calls) - before, 2)
        entry = read_record_entry(env.record_path)
        self.assertEqual((entry["uuid"], entry["refresh_sha256"]), (WORK, fingerprint(env.signins[WORK])))

    def test_a_later_check_after_another_switch_writes_nothing(self):
        env = Env(self)
        first = env.switch(WORK, confirm=False)
        env.switch(ORG)
        written = env.record_path.read_bytes()
        checked = switching.confirm_switch(first, slot=env.slot, profile_fn=env.profiles, registry=env.registry, lock_path=env.lock_path, lock_timeout=5)
        self.assertIsNone(checked.confirmed)
        self.assertEqual(env.record_path.read_bytes(), written)


class SettingsCopyTests(unittest.TestCase):
    def test_a_failed_prune_of_old_settings_copies_never_rolls_back_a_switch(self):
        env = Env(self)
        with mock.patch.object(accountfile, "prune_backups", side_effect=PermissionError(13, "denied")):
            result = env.switch(WORK)
        self.assertEqual(env.live_owner(), WORK)
        self.assertEqual(env.config()["oauthAccount"], details(WORK))
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])
        self.assertEqual(
            result.note, f"Lifeline could not remove old copies of Claude Code's settings file from {env.backups}."
        )
        env.assert_others_kept()

    def test_a_read_back_failure_never_rolls_back_a_switch_and_joins_any_other_note(self):
        env = Env(self)
        with mock.patch.object(accountfile, "_read_back", side_effect=OSError(5, "io")):
            result = env.switch(WORK)
        self.assertEqual(env.live_owner(), WORK)
        self.assertEqual(env.config()["oauthAccount"], details(WORK))
        self.assertEqual(result.note, "Lifeline could not read Claude Code's settings file back to check it.")


class UndoTests(unittest.TestCase):
    def test_undo_restores_previous(self):
        env = Env(self)
        result = env.switch(WORK)
        renewed = env.profiles.issue(WORK, 2)
        env.slot.item["claudeAiOauth"] = copy.deepcopy(renewed)
        back = undo(result, **env.kwargs())
        self.assertEqual(back.from_uuid, WORK)
        self.assertEqual(back.to_uuid, ALEX)
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertEqual(env.parked(WORK), renewed)
        self.assertIsNone(env.parked(ALEX))
        self.assertEqual(env.config()["oauthAccount"], details(ALEX))
        self.assertEqual(read_record(env.record_path), ALEX)
        env.assert_others_kept()
        env.assert_one_copy()

    def test_undo_works_offline_with_an_expired_key(self):
        env = Env(self)
        result = env.switch(WORK)
        env.slot.item["claudeAiOauth"]["expiresAt"] = PAST_MS
        env.profiles.offline = True
        back = undo(result, **env.kwargs())
        self.assertEqual(back.to_uuid, ALEX)
        self.assertEqual(env.live(), env.signins[ALEX])
        self.assertEqual(env.parked(WORK)["refreshToken"], env.signins[WORK]["refreshToken"])

    def test_undo_without_a_previous_account_is_refused(self):
        env = Env(self)
        with self.assertRaises(SwitchRefused):
            undo(SwitchResult(None, WORK, None), **env.kwargs())



CLAUDE_LOCKS = (".oauth_refresh.lock", ".claude.lock", ".claude.json.lock", ".storage-write.lock")


def stale_signin_file(folder):
    path = folder / ".credentials.json"
    path.write_bytes(b"stale copy")
    os.chmod(path, 0o600)
    old = time.time() - 86400
    os.utime(path, (old, old))
    return path


def moved(path):
    return time.time() - path.stat().st_mtime < 5


@contextlib.contextmanager
def lock_log():
    taken, freed = [], []
    real_mkdir, real_rmdir = os.mkdir, os.rmdir

    def mkdir(path, *args, **kwargs):
        if Path(path).name in CLAUDE_LOCKS:
            taken.append(Path(path))
        return real_mkdir(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        if Path(path).name in CLAUDE_LOCKS:
            freed.append(Path(path))
        return real_rmdir(path, *args, **kwargs)

    with mock.patch.object(locks.os, "mkdir", mkdir), mock.patch.object(locks.os, "rmdir", rmdir):
        yield taken, freed


class SessionMoveTests(unittest.TestCase):
    def test_a_switch_writes_the_keychain_then_the_file_time_then_the_name_under_all_four_locks(self):
        env = Env(self)
        claude = env.home / ".claude"
        path = stale_signin_file(claude)
        storage = claude / ".storage-write.lock"
        seen = []

        def at_write(slot, item):
            seen.append(("keychain", storage.is_dir(), moved(path), env.config()["oauthAccount"] == details(ALEX)))
            slot.accept(item)

        env.slot.write_plan = [at_write]
        real = accountfile.replace_oauth_account

        def relabel(*args, **kwargs):
            seen.append(("name", storage.is_dir(), moved(path)))
            return real(*args, **kwargs)

        with lock_log() as (taken, freed), mock.patch.object(accountfile, "replace_oauth_account", relabel):
            result = env.switch(WORK)
        self.assertEqual(seen, [("keychain", True, False, True), ("name", True, True)])
        self.assertEqual([p.name for p in taken], list(CLAUDE_LOCKS))
        self.assertEqual([p.name for p in freed], list(reversed(CLAUDE_LOCKS)))
        self.assertEqual(taken[-1], storage)
        self.assertFalse(storage.exists())
        self.assertEqual(path.read_bytes(), b"stale copy")
        self.assertTrue(moved(path))
        self.assertEqual(env.live_owner(), WORK)
        self.assertEqual(env.config()["oauthAccount"], details(WORK))
        self.assertIsNone(result.note)

    def test_a_storage_lock_held_by_claude_code_stops_the_switch_with_nothing_changed(self):
        env = Env(self, record=ALEX)
        claude = env.home / ".claude"
        path = stale_signin_file(claude)
        storage = claude / ".storage-write.lock"
        storage.mkdir()
        before = env.snapshot()
        stamp = path.stat().st_mtime
        with self.assertRaises(SwitchFailed) as caught:
            env.switch(WORK, storage_timeout=0.3)
        self.assertEqual(
            str(caught.exception),
            "Claude Code was saving its sign-in. Lifeline stopped before changing Claude Code, which is still on Alex's Account.",
        )
        after = env.snapshot()
        self.assertEqual((after[0], after[1], after[2], after[3], after[5]), (before[0], before[1], before[2], before[3], before[5]))
        self.assertEqual(read_record(env.record_path), ALEX)
        self.assertEqual(path.stat().st_mtime, stamp)
        self.assertTrue(storage.is_dir())
        for left in (claude / ".oauth_refresh.lock", env.home / ".claude.lock", env.home / ".claude.json.lock"):
            self.assertFalse(left.exists(), left)
        storage.rmdir()
        self.assertEqual(env.switch(WORK).to_uuid, WORK)

    def test_the_storage_lock_is_taken_only_after_lifeline_has_parked_the_old_sign_in(self):
        env = Env(self)
        seen = []
        real = switching.StorageLock

        class Watching(real):
            def __enter__(self):
                seen.append(env.parked(ALEX) is not None)
                return super().__enter__()

        with mock.patch.object(switching, "StorageLock", Watching):
            env.switch(WORK)
        self.assertEqual(seen, [True])

    def test_a_lock_lost_during_the_switch_is_reported(self):
        env = Env(self)
        real = switching.StorageLock
        lost = []

        class Losing(real):
            @property
            def compromised(self):
                return bool(lost) or super().compromised

        def at_write(slot, item):
            slot.accept(item)
            lost.append(True)

        env.slot.write_plan = [at_write]
        with mock.patch.object(switching, "StorageLock", Losing):
            result = env.switch(WORK)
        self.assertEqual(env.live_owner(), WORK)
        self.assertEqual(result.note, switching.LOCK_LOST.format(name="Work Account"))

    def test_no_sign_in_file_is_made_when_claude_code_has_none(self):
        env = Env(self)
        claude = env.home / ".claude"
        before = sorted(p.name for p in claude.iterdir())
        env.switch(WORK)
        self.assertEqual(env.live_owner(), WORK)
        self.assertFalse((claude / ".credentials.json").exists())
        self.assertEqual(sorted(p.name for p in claude.iterdir()), before)

    def test_the_lock_and_file_time_stay_with_the_plain_item_whatever_the_environment(self):
        env = Env(self)
        other = env.root / "config"
        other.mkdir()
        secure = env.root / "secure"
        secure.mkdir()
        elsewhere = [stale_signin_file(other), stale_signin_file(secure)]
        usual = stale_signin_file(env.home / ".claude")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other), "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(secure)}):
            with lock_log() as (taken, freed):
                env.switch(WORK)
        self.assertEqual([p for p in taken if p.name == ".storage-write.lock"], [env.home / ".claude" / ".storage-write.lock"])
        self.assertTrue(moved(usual))
        self.assertFalse(any(moved(path) for path in elsewhere))

    def test_a_file_time_that_cannot_be_moved_says_open_sessions_may_stay(self):
        env = Env(self)
        path = stale_signin_file(env.home / ".claude")
        real = os.utime

        def utime(target, *args, **kwargs):
            if Path(target).name == ".credentials.json":
                raise PermissionError()
            return real(target, *args, **kwargs)

        with mock.patch.object(locks.os, "utime", utime):
            result = env.switch(WORK)
        self.assertEqual(env.live_owner(), WORK)
        self.assertFalse(moved(path))
        self.assertEqual(result.note, SESSIONS_STAY.format(name="Alex's Account"))

    def test_undo_moves_the_file_time_too(self):
        env = Env(self)
        path = stale_signin_file(env.home / ".claude")
        result = env.switch(WORK)
        old = time.time() - 86400
        os.utime(path, (old, old))
        undo(result, **env.kwargs())
        self.assertEqual(env.live_owner(), ALEX)
        self.assertTrue(moved(path))

    def test_a_switch_back_after_a_failed_relabel_moves_the_file_time_again(self):
        env = Env(self)
        path = stale_signin_file(env.home / ".claude")
        stamps = []
        real = locks.nudge_sessions

        def nudge(folder):
            stamps.append(env.live_owner())
            return real(folder)

        with mock.patch.object(switching, "nudge_sessions", nudge), mock.patch.object(
            accountfile, "replace_oauth_account", side_effect=AccountFileError("no")
        ):
            with self.assertRaises(SwitchFailed):
                env.switch(WORK)
        self.assertEqual(stamps, [WORK, ALEX])
        self.assertEqual(env.live_owner(), ALEX)
        self.assertTrue(moved(path))

    def test_the_watcher_moves_the_file_time_when_it_switches_back(self):
        env = Env(self)
        env.switch(WORK)
        path = stale_signin_file(env.home / ".claude")
        clock = FakeClock()
        watcher = env.watcher(WORK, clock)
        self.assertEqual(watcher.poll(), [])
        item = copy.deepcopy(env.slot.item)
        item["claudeAiOauth"] = env.profiles.issue(PERSONAL, 2)
        env.slot.item = item
        clock.now += 60
        storage = env.home / ".claude" / ".storage-write.lock"
        held_at_write = []

        def at_write(slot, item):
            held_at_write.append(storage.is_dir())
            slot.accept(item)

        env.slot.write_plan = [at_write]
        with lock_log() as (taken, freed):
            reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live_owner(), WORK)
        self.assertEqual(held_at_write, [True])
        self.assertIn(storage, taken)
        self.assertTrue(moved(path))

    def test_the_watcher_says_when_it_could_not_move_open_sessions(self):
        env = Env(self)
        env.switch(WORK)
        stale_signin_file(env.home / ".claude")
        clock = FakeClock()
        watcher = env.watcher(WORK, clock)
        self.assertEqual(watcher.poll(), [])
        item = copy.deepcopy(env.slot.item)
        item["claudeAiOauth"] = env.profiles.issue(PERSONAL, 2)
        env.slot.item = item
        clock.now += 60
        real = os.utime

        def utime(target, *args, **kwargs):
            if Path(target).name == ".credentials.json":
                raise PermissionError()
            return real(target, *args, **kwargs)

        with mock.patch.object(locks.os, "utime", utime):
            reports = watcher.poll()
        self.assertEqual(env.live_owner(), WORK)
        self.assertTrue(any(SESSIONS_STAY.format(name="Personal Account") in report for report in reports), reports)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.result = self.env.switch(WORK)
        self.clock = FakeClock()

    def foreign_write(self, oauth, mcp=None):
        item = copy.deepcopy(self.env.slot.item)
        item["claudeAiOauth"] = copy.deepcopy(oauth)
        if mcp is not None:
            item["mcpOAuth"] = copy.deepcopy(mcp)
        self.env.slot.item = item

    def test_watcher_keeps_choice_and_saves_foreign_signin(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        self.assertEqual(watcher.poll(), [])
        foreign = env.profiles.issue(PERSONAL, 2)
        changed_mcp = mcp_signins()
        changed_mcp["linear|3c4d"]["refreshToken"] = "mcp-refresh-SECRET-desktop"
        self.foreign_write(foreign, changed_mcp)
        self.clock.now += 60
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Personal Account", reports[0])
        self.assertIn("Work Account", reports[0])
        self.assertEqual(env.parked(PERSONAL), foreign)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.slot.item["mcpOAuth"], changed_mcp)
        self.assertEqual(env.slot.item["otherKey"], {"kept": True})
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(read_record(env.record_path), WORK)
        self.assertEqual(watcher.poll(), [])
        self.clock.now += 120
        self.foreign_write(foreign, changed_mcp)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Personal Account", reports[0])
        self.assertIn("again", reports[0])
        self.assertEqual(env.live(), foreign)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(env.parked(PERSONAL))
        self.assertIsNone(read_record(env.record_path))
        self.assertFalse(watcher.active)
        self.foreign_write(foreign, changed_mcp)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), foreign)
        env.assert_one_copy()
        for report in reports:
            env.assert_secret_free(report)

    def test_watcher_remembers_renewal_in_memory_only(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        self.foreign_write(renewed)
        files = env.store_files()
        writes = len(env.slot.writes)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.store_files(), files)
        self.assertEqual(len(env.slot.writes), writes)
        self.assertIsNone(env.parked(WORK))
        foreign = env.profiles.issue(ORG, 2)
        self.foreign_write(foreign)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), renewed)
        self.assertEqual(env.parked(ORG), foreign)
        self.assertIsNone(env.parked(WORK))

    def test_watcher_identifies_offline_by_parked_copy(self):
        env = self.env
        env.profiles.offline = True
        watcher = env.watcher(WORK, self.clock)
        self.assertEqual(watcher.poll(), [])
        self.foreign_write(env.signins[ALEX])
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Alex's Account", reports[0])
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), env.signins[ALEX])

    def test_watcher_keeps_newer_parked_copy_of_foreign_account(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        stale = env.profiles.issue(PERSONAL, 0, expires=FUTURE_MS - 60_000)
        self.foreign_write(stale)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.parked(PERSONAL), env.signins[PERSONAL])
        self.assertEqual(env.live(), env.signins[WORK])

    def test_watcher_fixes_a_relabelled_account_file(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        data = env.config()
        data["oauthAccount"] = details(ORG)
        env.write_config(data)
        self.foreign_write(env.profiles.issue(ORG, 2))
        watcher.poll()
        after = env.config()
        self.assertEqual(after["oauthAccount"], details(WORK))
        self.assertEqual(without(after, "oauthAccount"), without(data, "oauthAccount"))

    def test_watcher_stops_after_minutes(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, minutes=10)
        watcher.poll()
        self.clock.now += 599
        self.assertEqual(watcher.poll(), [])
        self.assertTrue(watcher.active)
        self.clock.now += 1
        foreign = env.profiles.issue(ORG, 2)
        self.foreign_write(foreign)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Team Account", reports[0])
        self.assertEqual(env.live(), foreign)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(env.parked(ORG))
        self.assertFalse(watcher.active)
        self.assertEqual(watcher.poll(), [])
        env.assert_one_copy()

    def test_watcher_ends_quietly_when_choice_still_holds(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, minutes=10)
        watcher.poll()
        self.clock.now += 601
        files = env.store_files()
        self.assertEqual(watcher.poll(), [])
        self.assertFalse(watcher.active)
        self.assertEqual(env.store_files(), files)
        self.assertEqual(read_record(env.record_path), WORK)

    def test_watcher_reports_unknown_signin_once_then_saves_choice_at_end(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        env.profiles.offline = True
        unknown = env.profiles.issue(STRANGER, 1)
        self.foreign_write(unknown)
        first = watcher.poll()
        self.assertEqual(len(first), 1)
        self.assertIn("Work Account", first[0])
        self.assertEqual(env.live(), unknown)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(read_record(env.record_path), WORK)
        self.clock.now += 600
        last = watcher.poll()
        self.assertEqual(len(last), 1)
        self.assertIn("Work Account", last[0])
        self.assertEqual(env.live(), unknown)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(env.parked(STRANGER))
        self.assertIsNone(read_record(env.record_path))
        self.assertFalse(watcher.active)
        env.assert_secret_free(*first, *last)

    def test_watcher_stands_down_after_a_new_switch(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        env.switch(ORG)
        files = env.store_files()
        self.assertEqual(watcher.poll(), [])
        self.assertFalse(watcher.active)
        self.assertEqual(env.live(), env.signins[ORG])
        self.assertEqual(env.store_files(), files)
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_watcher_saves_choice_when_claude_code_is_signed_out(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        env.slot.item = {"mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("signed out", reports[0])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertNotIn("claudeAiOauth", env.slot.item)
        self.assertFalse(watcher.active)

    def test_watcher_hands_over_when_switching_back_fails(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        foreign = env.profiles.issue(PERSONAL, 2)
        self.foreign_write(foreign)
        env.slot.write_plan = [refuse_write]
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), foreign)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertIsNone(env.parked(PERSONAL))
        self.assertFalse(watcher.active)
        env.assert_one_copy()

    def test_watcher_waits_for_the_lifeline_lock(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, lock_timeout=0.1)
        watcher.poll()
        foreign = env.profiles.issue(ORG, 2)
        self.foreign_write(foreign)
        with held(env.lock_path, timeout=1):
            self.assertEqual(watcher.poll(), [])
            self.assertEqual(env.live(), foreign)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ORG), foreign)

    def test_watcher_waits_for_claude_codes_refresh_lock(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, session_timeout=0.2)
        watcher.poll()
        foreign = env.profiles.issue(ORG, 2)
        self.foreign_write(foreign)
        refresh_lock = env.home / ".claude" / ".oauth_refresh.lock"
        refresh_lock.mkdir()
        files = env.store_files()
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), foreign)
        self.assertEqual(env.store_files(), files)
        refresh_lock.rmdir()
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ORG), foreign)
        self.assertFalse(refresh_lock.exists())

    def test_watcher_first_sight_of_another_account_is_left_and_reported(self):
        env = self.env
        foreign = env.profiles.issue(PERSONAL, 2)
        self.foreign_write(foreign)
        watcher = env.watcher(WORK, self.clock)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("no checked copy", reports[0])
        self.assertEqual(env.live(), foreign)
        self.assertIsNone(env.parked(PERSONAL))
        self.assertIsNone(env.parked(WORK))
        self.assertIsNone(read_record(env.record_path))
        self.assertFalse(watcher.active)
        env.assert_one_copy()

    def other_login(self, uuid, n):
        signin = self.env.profiles.issue(uuid, n)
        self.foreign_write(signin)
        return signin

    def test_watcher_saves_choice_when_a_switch_runs_while_another_account_holds_the_slot(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        self.other_login(PERSONAL, 2)
        env.switch(ORG)
        reports = watcher.poll()
        self.assertEqual(
            reports,
            [
                "Lifeline stopped watching Work Account because Claude Code was switched to Team Account. "
                "Work Account is saved in Lifeline, ready to switch back to."
            ],
        )
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(env.live(), env.signins[ORG])
        self.assertFalse(watcher.active)
        self.assertEqual(watcher.poll(), [])
        env.assert_one_copy()
        env.assert_secret_free(*reports)

    def test_watcher_secures_choice_at_once_offline_and_the_next_switch_refuses(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        env.profiles.offline = True
        foreign = self.other_login(PERSONAL, 2)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        with self.assertRaises(IdentityUnknown):
            env.switch(ORG)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(env.parked(PERSONAL), env.signins[PERSONAL])
        self.assertEqual(env.live(), foreign)
        foreign_expired = copy.deepcopy(foreign)
        foreign_expired["expiresAt"] = PAST_MS
        self.foreign_write(foreign_expired)
        with self.assertRaises(IdentityUnknown):
            env.switch(ORG)
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_watcher_saves_choice_when_the_final_record_write_failed(self):
        env = Env(self)
        real = switching.write_record

        def no_final(path, uuid, now=None):
            if uuid == WORK:
                raise OSError("read-only")
            return real(path, uuid, now)

        with mock.patch.object(switching, "write_record", side_effect=no_final):
            result = env.switch(WORK)
        self.assertIsNone(read_record(env.record_path))
        watcher = env.watcher(WORK, self.clock, seed=result)
        self.assertEqual(watcher.poll(), [])
        self.foreign_write_into(env, env.profiles.issue(PERSONAL, 2))
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("because Lifeline's record of the last switch was cleared", reports[0])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertFalse(watcher.active)

    def foreign_write_into(self, env, oauth):
        item = copy.deepcopy(env.slot.item)
        item["claudeAiOauth"] = copy.deepcopy(oauth)
        env.slot.item = item

    def test_watcher_parks_choice_when_another_login_takes_slot_and_record(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        self.other_login(PERSONAL, 2)
        record_confirmed(env.record_path, PERSONAL, env.slot.item, datetime.now(timezone.utc))
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("because Claude Code was switched to Personal Account", reports[0])
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(read_record(env.record_path), PERSONAL)
        self.assertFalse(watcher.active)
        self.assertEqual(watcher.poll(), [])

    def test_watcher_keeps_a_newer_parked_copy_of_the_choice_when_standing_down(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        newer = env.profiles.issue(WORK, 7, expires=FUTURE_MS + 60_000)
        env.store.inner.put(WORK, newer)
        self.other_login(PERSONAL, 2)
        record_confirmed(env.record_path, PERSONAL, env.slot.item, datetime.now(timezone.utc))
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.parked(WORK), newer)
        self.assertFalse(watcher.active)

    def test_watcher_runs_continuously_and_forgets_old_repeats(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, lifetime=None)
        watcher.poll()
        self.other_login(PERSONAL, 2)
        self.clock.now += 60
        first = watcher.poll()
        self.assertEqual(len(first), 1)
        self.assertEqual(env.live(), env.signins[WORK])
        self.clock.now += 3 * 3600
        later = self.other_login(PERSONAL, 3)
        second = watcher.poll()
        self.assertEqual(len(second), 1)
        self.assertNotIn("again", second[0])
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(PERSONAL), later)
        self.assertTrue(watcher.active)
        self.assertEqual(read_record(env.record_path), WORK)
        self.clock.now += 5 * 60
        repeat = self.other_login(PERSONAL, 4)
        third = watcher.poll()
        self.assertEqual(len(third), 1)
        self.assertIn("again", third[0])
        self.assertEqual(env.live(), repeat)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertFalse(watcher.active)

    def test_watcher_seeded_by_the_switch_keeps_choice_before_its_first_poll(self):
        env = self.env
        env.profiles.offline = True
        foreign = self.other_login(ALEX, 1)
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Alex's Account", reports[0])
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(ALEX), foreign)
        self.assertIsNone(env.parked(WORK))

    def test_watcher_ignores_a_seed_for_another_account(self):
        env = self.env
        self.other_login(PERSONAL, 2)
        watcher = env.watcher(ORG, self.clock, seed=self.result)
        write_record(env.record_path, ORG)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("no checked copy", reports[0])
        self.assertIsNone(env.parked(WORK))

    def test_watcher_keeps_trying_when_the_record_cannot_be_cleared(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        foreign = self.other_login(PERSONAL, 2)
        watcher.poll()
        self.foreign_write(foreign)
        real = switching.write_record
        attempts = []

        def first_clear_fails(path, uuid, now=None):
            if uuid is None and not attempts:
                attempts.append(1)
                raise OSError("read-only")
            return real(path, uuid, now)

        with mock.patch.object(switching, "write_record", side_effect=first_clear_fails):
            first = watcher.poll()
            self.assertEqual(first, ["Lifeline could not update its own record. It will try again."])
            self.assertTrue(watcher.active)
            self.assertEqual(read_record(env.record_path), WORK)
            self.assertEqual(env.parked(WORK), env.signins[WORK])
            second = watcher.poll()
        self.assertEqual(len(second), 1)
        self.assertIn("again", second[0])
        self.assertIsNone(read_record(env.record_path))
        self.assertFalse(watcher.active)

    def test_watcher_stopped_mid_fight_leaves_choice_parked(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        foreign = self.other_login(PERSONAL, 2)

        def killed(slot, item):
            raise KeyboardInterrupt

        env.slot.write_plan = [killed]
        with self.assertRaises(KeyboardInterrupt):
            watcher.poll()
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(env.parked(PERSONAL), foreign)
        self.assertEqual(env.live(), foreign)
        self.assertFalse((env.home / ".claude.json.lock").exists())

    def test_watcher_retries_the_fight_when_a_lock_is_taken_over(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        foreign = self.other_login(PERSONAL, 2)
        json_lock = env.home / ".claude.json.lock"

        def steal(uuid):
            if uuid == PERSONAL:
                env.store.after_put = None
                os.rmdir(json_lock)
                os.mkdir(json_lock)
                time.sleep(0.3)

        env.store.after_put = steal
        with mock.patch.object(SessionLocks, "touch_interval", 0.02):
            self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), foreign)
        self.assertIsNone(env.parked(PERSONAL))
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertTrue(watcher.active)
        os.rmdir(json_lock)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertEqual(env.parked(PERSONAL), foreign)
        self.assertIsNone(env.parked(WORK))

    def test_watcher_hands_over_when_the_store_fails_during_the_fight(self):
        for op in ("get", "put"):
            with self.subTest(op):
                self.setUp()
                env = self.env
                watcher = env.watcher(WORK, self.clock)
                watcher.poll()
                foreign = self.other_login(PERSONAL, 2)
                env.store.fail[(op, PERSONAL, "before")] = 1
                reports = watcher.poll()
                self.assertEqual(len(reports), 1)
                self.assertIn("could not switch back", reports[0])
                self.assertEqual(env.live(), foreign)
                self.assertEqual(env.parked(WORK), env.signins[WORK])
                self.assertFalse(watcher.active)
                env.assert_one_copy()

    def test_watcher_holds_an_unidentified_value_for_two_minutes(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, lifetime=None)
        watcher.poll()
        env.profiles.offline = True
        unknown = self.other_login(STRANGER, 1)
        watcher.poll()
        asked = env.profiles.calls.count(unknown["accessToken"])
        self.assertEqual(asked, 1)
        for _ in range(4):
            self.clock.now += 15
            self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.profiles.calls.count(unknown["accessToken"]), 1)
        self.clock.now += 61
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.profiles.calls.count(unknown["accessToken"]), 2)

    def test_watcher_puts_back_its_newer_copy_over_a_stale_one(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        stale = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)
        mark_spent(env, WORK, stale)
        self.foreign_write(stale)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(watcher.poll(), [])

    def test_watcher_never_writes_back_over_a_change_it_cannot_prove_stale(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        older = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)
        self.foreign_write(older)
        writes = len(env.slot.writes)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), older)
        self.assertEqual(len(env.slot.writes), writes)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), older)

    def test_watcher_drops_its_spare_copy_once_a_renewal_is_confirmed(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        self.foreign_write(renewed)
        env.profiles.offline = True
        first = watcher.poll()
        self.assertEqual(len(first), 1)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        env.profiles.offline = False
        self.clock.now += 121
        self.assertEqual(watcher.poll(), [])
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(env.live(), renewed)
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(renewed))
        self.assertTrue(watcher.active)

    def test_watcher_confirmed_renewal_keeps_the_record_current(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        self.foreign_write(renewed)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(renewed))
        env.slot.item["claudeAiOauth"]["expiresAt"] = PAST_MS
        env.profiles.offline = True
        self.assertEqual(env.switch(ORG).from_uuid, WORK)

    def test_watcher_reports_a_failed_relabel(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        self.other_login(ORG, 2)
        data = env.config()
        data["oauthAccount"] = details(ORG)
        env.write_config(data)
        with mock.patch.object(switching.accountfile, "replace_oauth_account", side_effect=AccountFileError("disk full")):
            reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].endswith(WRONG_NAME))
        self.assertEqual(env.live(), env.signins[WORK])

    def renew_then_stale(self, watcher):
        env = self.env
        spent = env.signins[WORK]
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        self.foreign_write(renewed)
        return spent, renewed

    def assert_renewal_kept(self, renewed, spent):
        env = self.env
        self.assertEqual(env.live(), renewed)
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(read_record_entry(env.record_path)["refresh_sha256"], fingerprint(renewed))
        self.assertNotEqual(env.live(), spent)

    def test_watcher_restores_a_renewal_when_the_spent_copy_returns_during_the_check(self):
        for lifetime in (10, None):
            with self.subTest(lifetime=lifetime):
                self.setUp()
                env = self.env
                watcher = env.watcher(WORK, self.clock, seed=self.result, lifetime=lifetime)
                watcher.poll()
                spent, renewed = self.renew_then_stale(watcher)

                def stale_write_back(token):
                    if token == renewed["accessToken"]:
                        env.profiles.hook = None
                        self.foreign_write(spent)

                env.profiles.hook = stale_write_back
                self.assertEqual(watcher.poll(), [])
                self.assertEqual(env.live(), renewed)
                self.clock.now += 15
                self.assertEqual(watcher.poll(), [])
                self.assert_renewal_kept(renewed, spent)
                self.clock.now += 600
                self.assertEqual(watcher.poll(), [])
                self.assertEqual(env.live(), renewed)

    def test_watcher_restores_a_renewal_after_a_busy_lock(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result, lock_timeout=0.1)
        watcher.poll()
        spent, renewed = self.renew_then_stale(watcher)
        with held(env.lock_path, timeout=1):
            self.assertEqual(watcher.poll(), [])
        self.foreign_write(spent)
        self.assertEqual(watcher.poll(), [])
        self.assert_renewal_kept(renewed, spent)

    def test_watcher_restores_a_renewal_after_a_failed_record_write(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result, lifetime=None)
        watcher.poll()
        spent, renewed = self.renew_then_stale(watcher)
        with mock.patch.object(switching, "record_confirmed", side_effect=OSError("disk")):
            self.assertEqual(watcher.poll(), [RECORD_RETRY])
        self.foreign_write(spent)
        self.clock.now += 15
        self.assertEqual(watcher.poll(), [])
        self.assert_renewal_kept(renewed, spent)
        self.assertTrue(watcher.active)

    def test_watcher_keeps_mcp_renewed_during_its_own_store_writes(self):
        cases = {
            "fight": lambda env: env.profiles.issue(PERSONAL, 2),
            "restore": lambda env: spent_copy(env, env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)),
        }
        for label, make in cases.items():
            with self.subTest(label):
                self.setUp()
                env = self.env
                watcher = env.watcher(WORK, self.clock, seed=self.result)
                watcher.poll()
                self.foreign_write(make(env))
                newer = mcp_signins()
                newer["github|1a2b"]["refreshToken"] = "mcp-refresh-SECRET-renewed"

                def renew_mcp(uuid):
                    if uuid == WORK:
                        env.store.after_put = None
                        env.slot.item["mcpOAuth"] = copy.deepcopy(newer)

                env.store.after_put = renew_mcp
                watcher.poll()
                self.assertEqual(env.live(), env.signins[WORK])
                self.assertEqual(env.slot.item["mcpOAuth"], newer)
                self.assertEqual(env.slot.writes[-1][1]["mcpOAuth"], newer)

    def test_watcher_puts_back_a_newer_parked_copy_of_the_choice(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result, lifetime=None)
        watcher.poll()
        env.profiles.offline = True
        foreign = self.other_login(PERSONAL, 2)
        watcher.poll()
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        newer = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 3_600_000)
        env.store.inner.put(WORK, newer)
        env.profiles.offline = False
        self.clock.now += 121
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), newer)
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(env.parked(PERSONAL), foreign)
        env.assert_one_copy()

    def test_watcher_retries_a_failed_unpark_and_leaves_no_duplicate(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        self.other_login(PERSONAL, 2)
        env.store.fail[("remove", WORK, "before")] = 1
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertTrue(watcher.active)
        self.clock.now += 600
        self.assertEqual(watcher.poll(), [])
        self.assertIsNone(env.parked(WORK))
        self.assertFalse(watcher.active)
        env.assert_one_copy()

    def test_watcher_drops_its_spare_when_the_choice_comes_back(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        env.profiles.offline = True
        self.other_login(STRANGER, 1)
        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.foreign_write(env.signins[WORK])
        self.assertEqual(watcher.poll(), [])
        self.assertIsNone(env.parked(WORK))
        self.clock.now += 600
        self.assertEqual(watcher.poll(), [])
        self.assertFalse(watcher.active)
        env.assert_one_copy()

    def test_watcher_never_parks_under_an_account_not_in_lifeline(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        stranger = self.other_login(STRANGER, 1)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("not in Lifeline", reports[0])
        self.assertIsNone(env.parked(STRANGER))
        self.assertEqual(env.live(), stranger)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertTrue(watcher.active)
        self.assertEqual(watcher.poll(), [])
        self.assertIsNone(env.parked(STRANGER))

    def test_watcher_names_a_sign_out_as_the_reason_it_stopped(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        write_record(env.record_path, None)
        env.slot.item = {"mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("because Claude Code was signed out", reports[0])
        self.assertEqual(env.parked(WORK), env.signins[WORK])

    def rescue_files(self):
        folder = folder_of(self.env)
        if not folder.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(folder.glob(f"{WORK}.rescue.*.json"))]

    def test_watcher_rescue_writes_the_held_signin_without_the_lock(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        with held(env.lock_path, timeout=1):
            self.assertTrue(watcher.rescue())
        files = self.rescue_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["claudeAiOauth"], env.signins[WORK])
        self.assertNotIn("spent", files[0])
        renewed = env.profiles.issue(WORK, 2, expires=FUTURE_MS + 60_000)
        self.foreign_write(renewed)
        watcher.poll()
        self.assertEqual(self.rescue_files(), [])
        self.assertIsNone(env.parked(WORK))
        self.assertTrue(watcher.rescue())
        files = self.rescue_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["claudeAiOauth"], renewed)
        self.assertEqual(files[0]["spent"], fingerprint(env.signins[WORK]))

    def test_watcher_rescue_never_raises(self):
        env = self.env
        empty = env.watcher(WORK, self.clock)
        self.assertFalse(empty.rescue())
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        with mock.patch.object(switching.recovery, "rescue", side_effect=recovery.RecoveryError("no")):
            self.assertFalse(watcher.rescue())
        with mock.patch.object(switching.recovery, "rescue", side_effect=OSError("disk")):
            self.assertFalse(watcher.rescue())

    def test_watcher_never_adopts_a_spare_it_saw_while_the_choice_was_live(self):
        env = self.env
        spare = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)
        env.store.inner.put(WORK, spare)
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        self.other_login(PERSONAL, 2)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertIsNone(env.parked(WORK))

    def test_watcher_stand_down_parks_over_a_spare_it_saw(self):
        env = self.env
        spare = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)
        env.store.inner.put(WORK, spare)
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        self.other_login(PERSONAL, 2)
        env.switch(ORG)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertFalse(watcher.active)

    def test_watcher_treats_an_unusable_saved_copy_as_absent(self):
        for path in ("secure", "stand-down"):
            with self.subTest(path):
                self.setUp()
                env = self.env
                watcher = env.watcher(WORK, self.clock, seed=self.result)
                watcher.poll()
                env.store.inner.put(WORK, {"expiresAt": FUTURE_MS + 60_000, "scopes": []})
                self.other_login(PERSONAL, 2)
                if path == "stand-down":
                    record_confirmed(env.record_path, PERSONAL, env.slot.item, datetime.now(timezone.utc))
                    watcher.poll()
                    self.assertEqual(env.parked(WORK), env.signins[WORK])
                else:
                    env.slot.write_plan = [refuse_write]
                    watcher.poll()
                    self.assertEqual(env.parked(WORK), env.signins[WORK])

    def test_watcher_honours_a_recovery_record_over_expiry(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result, lifetime=None)
        watcher.poll()
        env.profiles.offline = True
        self.other_login(STRANGER, 1)
        watcher.poll()
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        shorter = env.profiles.issue(WORK, 2, expires=FUTURE_MS - 60_000)
        with held(env.lock_path, timeout=1):
            recovery.write(folder_of(env), WORK, shorter, spent=env.signins[WORK])
        self.foreign_write(env.signins[WORK])
        self.clock.now += 121
        env.profiles.offline = False
        watcher.poll()
        self.assertEqual(env.live(), shorter)
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(recovery_files(env, WORK), [])

    def test_watcher_restores_the_reader_renewal_of_its_parked_copy(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result, lifetime=None)
        watcher.poll()
        env.profiles.offline = True
        self.other_login(STRANGER, 1)
        watcher.poll()
        renewal = env.profiles.issue(WORK, 2, expires=FUTURE_MS - 60_000)
        env.store.inner.put(WORK, renewal)
        self.foreign_write(env.signins[WORK])
        self.clock.now += 15
        watcher.poll()
        self.assertEqual(env.live(), renewal)
        self.assertIsNone(env.parked(WORK))

    def test_watcher_tries_again_when_the_list_of_accounts_cannot_be_read(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        stranger = self.other_login(STRANGER, 1)
        real = env.registry.by_uuid

        def flaky(uuid):
            if uuid == STRANGER:
                raise OSError("busy")
            return real(uuid)

        with mock.patch.object(env.registry, "by_uuid", side_effect=flaky):
            self.assertEqual(watcher.poll(), [])
        self.assertIsNone(env.parked(WORK))
        self.assertEqual(env.live(), stranger)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("not in Lifeline", reports[0])

    def test_watcher_reports_once_when_a_restore_keeps_getting_stuck(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock, seed=self.result)
        watcher.poll()
        stale = env.profiles.issue(WORK, 0, expires=FUTURE_MS - 60_000)
        mark_spent(env, WORK, stale)
        self.foreign_write(stale)
        env.slot.write_plan = [refuse_write]
        first = watcher.poll()
        self.assertEqual(len(first), 1)
        self.assertIn("older sign-in", first[0])
        self.assertIn("Work Account", first[0])
        env.slot.write_plan = [refuse_write]
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), stale)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(env.live(), env.signins[WORK])
        self.assertIsNone(env.parked(WORK))
        env.assert_secret_free(*first)

    def test_watcher_retries_saving_the_choice(self):
        env = self.env
        watcher = env.watcher(WORK, self.clock)
        watcher.poll()
        env.slot.item = {"mcpOAuth": mcp_signins(), "otherKey": {"kept": True}}
        env.store.fail[("put", WORK, "before")] = 1
        first = watcher.poll()
        self.assertEqual(len(first), 1)
        self.assertTrue(watcher.active)
        self.assertIsNone(env.parked(WORK))
        second = watcher.poll()
        self.assertEqual(len(second), 1)
        self.assertEqual(env.parked(WORK), env.signins[WORK])
        self.assertFalse(watcher.active)


class OrganizationTests(unittest.TestCase):
    def common(self, env):
        return dict(profile_fn=env.profiles, home=env.home, record_path=env.record_path, registry=env.registry)

    def foreign_write(self, env, oauth):
        item = copy.deepcopy(env.slot.item)
        item["claudeAiOauth"] = copy.deepcopy(oauth)
        env.slot.item = item

    def test_a_switch_between_two_organizations_of_one_login_moves_the_right_signin_each_way(self):
        env = Env(self, live=PERSONAL, parked=(WORK, TEAM), accounts=SAME_LOGIN)
        personal, team = env.signins[PERSONAL], env.signins[TEAM]
        self.assertEqual(identify_live(env.slot.item, **self.common(env)), PERSONAL)
        result = env.switch(TEAM)
        self.assertEqual((result.from_uuid, result.to_uuid, result.confirmed), (PERSONAL, TEAM, True))
        self.assertEqual(env.live(), team)
        self.assertEqual(env.parked(PERSONAL), personal)
        self.assertIsNone(env.parked(TEAM))
        self.assertEqual(env.config()["oauthAccount"], details(TEAM))
        self.assertEqual(identify_live(env.slot.item, **self.common(env)), TEAM)
        self.assertEqual(read_record(env.record_path), TEAM)
        back = env.switch(PERSONAL)
        self.assertEqual((back.from_uuid, back.to_uuid, back.confirmed), (TEAM, PERSONAL, True))
        self.assertEqual(env.live(), personal)
        self.assertEqual(env.parked(TEAM), team)
        self.assertIsNone(env.parked(PERSONAL))
        self.assertEqual(env.config()["oauthAccount"], details(PERSONAL))
        self.assertEqual(identify_live(env.slot.item, **self.common(env)), PERSONAL)
        account = env.registry.by_uuid(TEAM)
        env.registry.upsert(dataclasses.replace(account, oauth_account=details(PERSONAL)))
        before = env.snapshot()
        with self.assertRaises(SwitchRefused):
            env.switch(TEAM)
        self.assertEqual(env.snapshot(), before)

    def test_an_expired_live_signin_is_named_by_the_record_and_its_organization(self):
        env = Env(self, live=TEAM, parked=(WORK, PERSONAL), accounts=SAME_LOGIN, record=TEAM)
        stale = copy.deepcopy(env.slot.item)
        stale["claudeAiOauth"]["expiresAt"] = PAST_MS
        self.assertEqual(identify_live(stale, **self.common(env)), TEAM)
        data = env.config()
        data["oauthAccount"] = details(PERSONAL)
        env.write_config(data)
        with self.assertRaises(IdentityUnknown):
            identify_live(stale, **self.common(env))

    def test_identify_live_never_names_the_other_organization_of_a_login(self):
        env = Env(self, live=TEAM, parked=(WORK,), accounts=ACCOUNTS)
        self.assertEqual(identify_live(env.slot.item, **self.common(env)), TEAM)
        self.assertIsNone(env.registry.by_uuid(TEAM))
        with self.assertRaises(SwitchRefused) as caught:
            env.switch(WORK)
        self.assertIn("not in Lifeline", str(caught.exception))
        self.assertEqual(env.live(), env.signins[TEAM])
        self.assertIsNone(env.parked(PERSONAL))

    def test_the_watcher_never_files_the_other_organization_under_the_chosen_account(self):
        env = Env(self, live=ALEX, parked=(WORK, PERSONAL), accounts=ACCOUNTS)
        result = env.switch(PERSONAL)
        clock = FakeClock()
        watcher = env.watcher(PERSONAL, clock, seed=result)
        watcher.poll()
        team = env.profiles.issue(TEAM, 2)
        self.foreign_write(env, team)
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("not in Lifeline", reports[0])
        self.assertEqual(env.parked(PERSONAL), env.signins[PERSONAL])
        self.assertEqual(env.live(), team)
        self.assertEqual(read_record(env.record_path), PERSONAL)
        env.assert_secret_free(*reports)

    def test_the_watcher_keeps_each_organizations_signin_under_its_own_account(self):
        env = Env(self, live=ALEX, parked=(WORK, PERSONAL, TEAM), accounts=SAME_LOGIN)
        result = env.switch(PERSONAL)
        clock = FakeClock()
        watcher = env.watcher(PERSONAL, clock, seed=result)
        watcher.poll()
        team = env.profiles.issue(TEAM, 2)
        data = env.config()
        data["oauthAccount"] = details(TEAM)
        env.write_config(data)
        self.foreign_write(env, team)
        clock.now += 60
        reports = watcher.poll()
        self.assertEqual(len(reports), 1)
        self.assertIn("Team Account", reports[0])
        self.assertIn("Personal Account", reports[0])
        self.assertEqual(env.parked(TEAM), team)
        self.assertEqual(env.live(), env.signins[PERSONAL])
        self.assertIsNone(env.parked(PERSONAL))
        self.assertEqual(env.config()["oauthAccount"], details(PERSONAL))


if __name__ == "__main__":
    unittest.main()
