import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from datetime import datetime, timedelta, timezone

from lifeline import accounts
from lifeline.accounts import (
    Account,
    Registry,
    RegistryError,
    default_registry_path,
    identity,
    new_id,
    plan_label,
    resolve,
    signin_end,
    signin_end_text,
)


def make(n, uuid=None, oauth=None):
    return Account(
        uuid=uuid or f"uuid-{n}",
        email=f"a{n}@example.com",
        name=f"Name {n}",
        short=f"S{n}",
        full=f"Full {n}",
        plan="Max 5x",
        key=n,
        oauth_account=oauth if oauth is not None else {"emailAddress": f"a{n}@example.com"},
    )


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "accounts.json"
        self.registry = Registry(self.path)

    def test_registry_round_trip(self):
        accounts = [make(1), make(2), make(3), make(4)]
        self.registry.save(accounts)
        self.assertEqual(Registry(self.path).load(), accounts)

    def test_load_missing_file_is_empty(self):
        self.assertEqual(self.registry.load(), [])

    def test_plan_label_maps_tiers(self):
        self.assertEqual(plan_label("default_claude_max_5x"), "Max 5x")
        self.assertEqual(plan_label("default_claude_max_20x"), "Max 20x")

    def test_a_tier_lifeline_does_not_know_gives_no_label(self):
        for tier in ("something_new", "raven", "default_raven", "Raven", "default_claude_pro"):
            with self.subTest(tier=tier):
                self.assertEqual(plan_label(tier), "")

    def test_registry_never_stores_tokens(self):
        oauth = {
            "emailAddress": "a@example.com",
            "accessToken": "SECRET-A",
            "refreshToken": "SECRET-R",
            "nested": {"accessToken": "SECRET-N", "list": [{"refreshToken": "SECRET-L"}]},
        }
        self.registry.save([make(1, oauth=oauth)])
        text = self.path.read_text()
        for word in ("accessToken", "refreshToken", "SECRET-A", "SECRET-R", "SECRET-N", "SECRET-L"):
            self.assertNotIn(word, text)
        self.assertEqual(self.registry.load()[0].oauth_account["emailAddress"], "a@example.com")

    def test_save_does_not_mutate_input(self):
        oauth = {"accessToken": "x", "emailAddress": "e"}
        account = make(1, oauth=oauth)
        self.registry.save([account])
        self.assertEqual(account.oauth_account, {"accessToken": "x", "emailAddress": "e"})

    def test_file_mode_is_600(self):
        self.registry.save([make(1)])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.registry.save([make(1), make(2)])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_mode_is_600_even_when_file_existed_wider(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[]")
        os.chmod(self.path, 0o644)
        self.registry.save([make(1)])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_lookups(self):
        self.registry.save([make(1), make(2)])
        self.assertEqual(self.registry.by_uuid("uuid-2").key, 2)
        self.assertEqual(self.registry.by_key(1).uuid, "uuid-1")
        self.assertIsNone(self.registry.by_uuid("nope"))
        self.assertIsNone(self.registry.by_key(9))

    def test_upsert_adds_then_replaces_without_duplicating(self):
        self.registry.upsert(make(1))
        self.registry.upsert(make(2))
        changed = make(1)
        changed.plan = "Max 20x"
        self.registry.upsert(changed)
        loaded = self.registry.load()
        self.assertEqual([a.uuid for a in loaded], ["uuid-1", "uuid-2"])
        self.assertEqual(loaded[0].plan, "Max 20x")

    def test_upsert_repeated_is_stable(self):
        for _ in range(5):
            self.registry.upsert(make(1))
        self.assertEqual(len(self.registry.load()), 1)

    def test_failed_save_leaves_old_file_and_no_temp_file(self):
        self.registry.save([make(1)])
        before = self.path.read_text()
        with mock.patch("lifeline.accounts.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.registry.save([make(1), make(2)])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(os.listdir(self.path.parent), ["accounts.json"])

    def test_save_rejects_duplicate_uuid(self):
        with self.assertRaises(ValueError):
            self.registry.save([make(1), make(2, uuid="uuid-1")])
        self.assertFalse(self.path.exists())

    def test_save_rejects_duplicate_key(self):
        other = make(2)
        other.key = 1
        with self.assertRaises(ValueError):
            self.registry.save([make(1), other])
        self.assertFalse(self.path.exists())

    def test_save_rejects_key_out_of_range(self):
        account = make(1)
        account.key = 12
        self.registry.save([account])
        self.assertEqual(self.registry.load()[0].key, 12)
        for bad in (0, -1, True, "2", 2.0):
            account = make(1)
            account.key = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.registry.save([account])

    def test_upsert_rejects_key_taken_by_other_account(self):
        self.registry.upsert(make(1))
        clash = make(2)
        clash.key = 1
        with self.assertRaises(ValueError):
            self.registry.upsert(clash)
        self.assertEqual([a.uuid for a in self.registry.load()], ["uuid-1"])

    def write_raw(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)

    def test_load_rejects_corrupt_json(self):
        self.write_raw("{not json")
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_load_rejects_truncated_json(self):
        self.registry.save([make(1), make(2)])
        full = self.path.read_text()
        self.write_raw(full[: len(full) // 2])
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_load_rejects_empty_file(self):
        self.write_raw("")
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_load_rejects_non_list_top_level(self):
        self.write_raw('{"uuid": "x"}')
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_load_rejects_non_object_item(self):
        self.write_raw("[1]")
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_load_rejects_missing_field(self):
        self.registry.save([make(1)])
        items = json.loads(self.path.read_text())
        del items[0]["email"]
        self.write_raw(json.dumps(items))
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_load_rejects_unknown_field(self):
        self.registry.save([make(1)])
        items = json.loads(self.path.read_text())
        items[0]["surprise"] = 1
        self.write_raw(json.dumps(items))
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_signed_in_at_defaults_to_none_and_round_trips(self):
        self.assertIsNone(make(1).signed_in_at)
        account = make(1)
        account.signed_in_at = "2026-11-19T10:00:00Z"
        self.registry.save([account, make(2)])
        loaded = self.registry.load()
        self.assertEqual(loaded[0].signed_in_at, "2026-11-19T10:00:00Z")
        self.assertIsNone(loaded[1].signed_in_at)

    def test_load_file_written_before_the_field_existed(self):
        self.registry.save([make(1)])
        items = json.loads(self.path.read_text())
        del items[0]["signed_in_at"]
        self.write_raw(json.dumps(items))
        self.assertIsNone(self.registry.load()[0].signed_in_at)

    def test_file_is_valid_json_list(self):
        self.registry.save([make(1)])
        self.assertIsInstance(json.loads(self.path.read_text()), list)

    def test_default_path_derives_from_home(self):
        home = Path("/somewhere/else")
        path = default_registry_path(home=home, system="Darwin")
        self.assertEqual(path, home / "Library/Application Support/com.example.lifeline/accounts.json")
        path = default_registry_path(home=home, system="Linux")
        self.assertEqual(path, home / ".local/share/com.example.lifeline/accounts.json")


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def ms(moment):
    return int(moment.timestamp() * 1000)


class SigninEndTests(unittest.TestCase):
    def test_the_end_is_the_renewal_key_expiry_claude_code_stores(self):
        end = NOW + timedelta(days=20, milliseconds=250)
        oauth = {"refreshTokenExpiresAt": ms(end), "expiresAt": ms(NOW + timedelta(hours=8)), "refreshToken": "r"}
        self.assertEqual(signin_end(oauth, NOW), end)
        self.assertEqual(signin_end_text(oauth, NOW), "2026-12-09T12:00:00Z")
        self.assertEqual(signin_end({"refreshTokenExpiresAt": float(ms(end))}, NOW), end)

    def test_unusable_values_give_no_end(self):
        for value in (None, True, False, "1790000000000", float("nan"), float("inf"), 0, -5, ms(NOW + timedelta(days=3651)), [1]):
            self.assertIsNone(signin_end({"refreshTokenExpiresAt": value}, NOW), value)
        self.assertEqual(signin_end({"refreshTokenExpiresAt": ms(NOW + timedelta(days=3650))}, NOW), NOW + timedelta(days=3650))
        for oauth in (None, "text", [], {}, {"expiresAt": ms(NOW)}):
            self.assertIsNone(signin_end(oauth, NOW))
            self.assertIsNone(signin_end_text(oauth, NOW))

    def test_an_access_key_more_than_3_days_past_the_end_makes_it_stale(self):
        end = NOW + timedelta(days=1)
        edge = {"refreshTokenExpiresAt": ms(end), "expiresAt": ms(end + timedelta(days=3))}
        self.assertEqual(signin_end(edge, NOW), end)
        stale = {"refreshTokenExpiresAt": ms(end), "expiresAt": ms(end + timedelta(days=3)) + 1}
        self.assertIsNone(signin_end(stale, NOW))
        self.assertEqual(signin_end({"refreshTokenExpiresAt": ms(end), "expiresAt": "soon"}, NOW), end)

    def test_an_end_already_passed_is_still_reported(self):
        end = NOW - timedelta(days=2)
        self.assertEqual(signin_end({"refreshTokenExpiresAt": ms(end)}, NOW), end)


class SigninEndRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.json"

    def test_the_registry_keeps_the_signin_end_and_reads_old_files(self):
        account = make(1)
        self.assertIsNone(account.signin_ends)
        Registry(self.path).save([Account(**dict(vars(account), signin_ends="2026-12-09T12:00:00Z"))])
        self.assertEqual(Registry(self.path).load()[0].signin_ends, "2026-12-09T12:00:00Z")
        old = dict(vars(make(2)))
        old.pop("signin_ends")
        self.path.write_text(json.dumps([old]))
        self.assertIsNone(Registry(self.path).load()[0].signin_ends)


LOGIN = "a1a1a1a1-1111-4111-8111-000000000001"
PERSONAL_ORG = "b2b2b2b2-2222-4222-8222-000000000002"
TEAM_ORG = "c3c3c3c3-3333-4333-8333-000000000003"
OTHER_LOGIN = "d4d4d4d4-4444-4444-8444-000000000004"
OTHER_ORG = "e5e5e5e5-5555-4555-8555-000000000005"


def snapshot(login, org=None):
    found = {"accountUuid": login, "emailAddress": "sam@example.invalid", "organizationName": "Some Organization"}
    if org is not None:
        found["organizationUuid"] = org
    return found


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.json"
        self.registry = Registry(self.path)

    def test_an_account_is_a_login_plus_its_organization(self):
        personal = make(1, uuid=LOGIN, oauth=snapshot(LOGIN, PERSONAL_ORG))
        personal.org_uuid = PERSONAL_ORG
        team = make(2, uuid=f"{LOGIN}-{TEAM_ORG}", oauth=snapshot(LOGIN, TEAM_ORG))
        team.org_uuid, team.login_uuid = TEAM_ORG, LOGIN
        self.registry.save([personal, team])
        loaded = self.registry.load()
        self.assertEqual([(a.uuid, a.login, a.org_uuid) for a in loaded], [(LOGIN, LOGIN, PERSONAL_ORG), (f"{LOGIN}-{TEAM_ORG}", LOGIN, TEAM_ORG)])
        self.assertEqual(self.registry.find(LOGIN, PERSONAL_ORG).key, 1)
        self.assertEqual(self.registry.find(LOGIN, TEAM_ORG).key, 2)
        self.assertIsNone(self.registry.find(LOGIN, OTHER_ORG))
        self.assertIsNone(self.registry.find(LOGIN, None))
        self.assertIsNone(self.registry.find(OTHER_LOGIN, PERSONAL_ORG))

    def test_a_new_account_gets_the_login_id_or_the_login_and_organization(self):
        personal = make(1, uuid=LOGIN, oauth=snapshot(LOGIN, PERSONAL_ORG))
        personal.org_uuid = PERSONAL_ORG
        self.assertEqual(new_id([], LOGIN, TEAM_ORG), LOGIN)
        self.assertEqual(new_id([personal], LOGIN, TEAM_ORG), f"{LOGIN}-{TEAM_ORG}")
        self.assertIsNone(new_id([personal], LOGIN, None))
        self.assertEqual(new_id([personal], OTHER_LOGIN, OTHER_ORG), OTHER_LOGIN)
        self.assertEqual(identity([personal], LOGIN, PERSONAL_ORG), LOGIN)
        self.assertEqual(identity([personal], LOGIN, TEAM_ORG), f"{LOGIN}-{TEAM_ORG}")
        self.assertIsNone(identity([personal], LOGIN, None))
        self.assertEqual(identity([personal], OTHER_LOGIN, None), OTHER_LOGIN)

    def test_an_entry_without_a_known_organization_matches_on_its_login_alone(self):
        unknown = make(1, uuid=LOGIN, oauth=snapshot(LOGIN))
        self.assertIsNone(unknown.org_uuid)
        self.assertIs(resolve([unknown], LOGIN, TEAM_ORG), unknown)
        self.assertIs(resolve([unknown], LOGIN, None), unknown)
        team = make(2, uuid=f"{LOGIN}-{TEAM_ORG}", oauth=snapshot(LOGIN, TEAM_ORG))
        team.org_uuid, team.login_uuid = TEAM_ORG, LOGIN
        self.assertIs(resolve([unknown, team], LOGIN, TEAM_ORG), team)
        self.assertIs(resolve([unknown, team], LOGIN, PERSONAL_ORG), unknown)
        self.assertIsNone(resolve([unknown, team], LOGIN, None))

    def test_save_refuses_two_accounts_for_one_login_and_organization(self):
        first = make(1, uuid=LOGIN, oauth=snapshot(LOGIN, PERSONAL_ORG))
        twin = make(2, uuid=f"{LOGIN}-{PERSONAL_ORG}", oauth=snapshot(LOGIN, PERSONAL_ORG))
        twin.login_uuid = LOGIN
        with self.assertRaisesRegex(RegistryError, "^Two accounts have the same login and organization, "):
            self.registry.save([first, twin])
        self.assertFalse(self.path.exists())



class PlanLabelTests(unittest.TestCase):
    def test_a_plan_is_written_one_way(self):
        self.assertEqual(accounts.PLAN_LABELS, {"default_claude_max_5x": "Max 5x", "default_claude_max_20x": "Max 20x"})


if __name__ == "__main__":
    unittest.main()
