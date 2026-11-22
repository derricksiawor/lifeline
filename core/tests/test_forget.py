"""Remove and Reset: one account, or all of Lifeline, forgotten here and on the other computer.

Neither ever touches Claude Code's own sign-in, ~/.claude.json or its status line setting.
"""

import copy
import json
import threading
import unittest
from datetime import timedelta
from unittest import mock

from lifeline import cli, devbox, recovery, server
from lifeline.history import History
from tests.test_cli import FakeService
from tests.test_cli import World as CommandWorld
from tests.test_devbox import DevboxWorld
from lifeline import switch as switching
from tests.test_server import ALEX, NAMES, NEWCOMER, ORDER, ORG, PERSONAL, R_WORK, START, WORK, FakeLink, World

STAMP = "2026-11-19-154100"
EVENT = "limit:{}:2026-11-19T10:00:00+00:00"
WORK_IDLE = "Lifeline removes Work Account, its saved sign-in and its history, here and on the other computer."
ALEX_IN_USE = (
    "Lifeline removes Alex's Account, its saved sign-in and its history, here and on the other computer, and Claude Code keeps using it."
)
RESET_CONFIRM = (
    "Lifeline saves a copy of its history and settings, then removes every account, sign-in and setting here and on the other computer. "
    "Claude Code keeps the account it uses."
)
REMOVED_BOTH = "Lifeline removed Work Account here and on the other computer."
REMOVED_LATER = "Lifeline removed Work Account here, and removes it on the other computer when the other computer next answers."


class ForgetLink(FakeLink):
    """Another computer that takes remove and reset while it is up, and notes every change in the order it came."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.status = 200
        self.said = {}
        self.pushed = []

    def request(self, method, route, body=None):
        if not self.up:
            raise devbox.LinkUnreached()
        self.calls.append((method, route))
        return self.status, dict(self.said)

    def start_signin(self, route, body):
        raise AssertionError("a sign-in started on the other computer")

    def push_registry(self, accounts, known=None):
        self.calls.append(("POST", "/registry"))
        self.pushed.append([item["uuid"] for item in accounts])
        return self.up


def mac(case, up=True):
    world = World(case)
    world.link = ForgetLink()
    world.link.up = up
    service = world.service()
    service.tick()
    world.link.calls.clear()
    return world, service


def seed(world, service, uuid):
    world.usage_reading(uuid, 30.0, R_WORK)
    world.history.add_event(EVENT.format(uuid), world.clock.now)
    recovery.write(recovery.folder_for(world.lock_path), uuid, world.issue(uuid))
    service.report(f"{NAMES[uuid][0]} is out", kind="limit", key=EVENT.format(uuid), uuid=uuid, keep=True)


def claude_side(world):
    return copy.deepcopy(world.slot.item), world.slot.writes, (world.home / ".claude.json").read_bytes()


def refusal(action):
    try:
        action()
    except server.Refusal as refused:
        return refused.status, refused.sentence
    return None


class RemoveTests(unittest.TestCase):
    def test_remove_forgets_one_account_here_and_on_the_devbox(self):
        world, service = mac(self)
        for uuid in (WORK, PERSONAL):
            seed(world, service, uuid)
        service.update_settings({"placement": {WORK: "mac", PERSONAL: "mac"}})
        before = claude_side(world)
        reply = service.remove(WORK)
        self.assertEqual(reply, {"message": REMOVED_BOTH})
        self.assertEqual(world.link.calls, [("POST", f"/remove/{WORK}")])
        self.assertIsNone(world.registry.by_uuid(WORK))
        self.assertIsNone(world.store.get(WORK))
        self.assertEqual(world.history.readings(WORK, START - timedelta(days=1)), [])
        self.assertIsNone(world.history.event_at(EVENT.format(WORK)))
        self.assertEqual(recovery.accounts(recovery.folder_for(world.lock_path)), [PERSONAL])
        self.assertNotIn(WORK, json.loads((world.folder / "settings.json").read_text())["placement"])
        state = service.state()
        self.assertEqual([a["uuid"] for a in state["accounts"]], [ALEX, PERSONAL, ORG])
        self.assertEqual([n["uuid"] for n in state["notifications"]], [PERSONAL])
        self.assertIsNotNone(world.store.get(PERSONAL))
        self.assertTrue(world.history.readings(PERSONAL, START - timedelta(days=1)))
        self.assertIsNotNone(world.history.event_at(EVENT.format(PERSONAL)))
        self.assertEqual(claude_side(world), before)
        service.tick()
        self.assertIsNone(world.store.get(WORK), "the reader brought its sign-in back")
        self.assertEqual(world.link.pushed[-1], [ALEX, PERSONAL, ORG])
        self.assertNotIn(("POST", f"/remove/{WORK}"), world.link.calls[1:], "a change the other computer took is sent again")
        world.assert_clean()

    def test_removing_the_account_in_use_leaves_claude_code_on_it_and_offers_the_first_run_add(self):
        world, service = mac(self)
        self.assertEqual(service.watcher.chosen_uuid, ALEX)
        before = claude_side(world)
        self.assertEqual(service.remove(ALEX)["message"], "Lifeline removed Alex's Account here and on the other computer.")
        self.assertIsNone(service.watcher)
        self.assertIsNone(world.registry.by_uuid(ALEX))
        service.tick()
        self.assertEqual(claude_side(world), before)
        state = service.state()
        live = state["mac"]["live"]
        self.assertEqual((live["uuid"], live["added"], live["adopt_ready"]), (ALEX, False, True))
        self.assertEqual(state["mac"]["action_kind"], "adopt")
        job = service.start_job("adopt")["job"]
        self.assertTrue(world.wait_for(lambda: service.state()["jobs"][0]["state"] != "running", 10))
        self.assertEqual(service.state()["jobs"][0]["state"], "done", job)
        self.assertIsNotNone(world.registry.by_uuid(ALEX))
        world.assert_clean()

    def test_while_the_devbox_is_down_it_says_so_and_catches_up_after_a_restart(self):
        world, service = mac(self, up=False)
        self.assertEqual(service.remove(WORK)["message"], REMOVED_LATER)
        self.assertEqual(world.link.calls, [])
        again = world.service()
        world.link.up = True
        world.link.status = 409
        again.tick()
        self.assertEqual(world.link.calls[0], ("POST", f"/remove/{WORK}"))
        world.link.calls.clear()
        world.link.status = 200
        again.tick()
        self.assertEqual(world.link.calls[:2], [("POST", f"/remove/{WORK}"), ("POST", "/registry")])
        world.link.calls.clear()
        again.tick()
        self.assertNotIn(("POST", f"/remove/{WORK}"), world.link.calls)
        self.assertEqual(json.loads((world.folder / server.FORGET_NAME).read_text()), [])

    def test_a_registry_push_in_flight_goes_first_and_the_remove_follows_it(self):
        world, service = mac(self)
        with mock.patch.object(server, "FORGET_WAIT", 0.05):
            with service._link_lock:
                reply = service.remove(WORK)
        self.assertEqual(reply["message"], REMOVED_LATER)
        self.assertEqual(world.link.calls, [])
        service.tick()
        self.assertEqual(world.link.calls[:2], [("POST", f"/remove/{WORK}"), ("POST", "/registry")])

    def test_the_route_answers_and_an_unknown_account_changes_nothing(self):
        world, service = mac(self)
        world.run(service)
        self.assertEqual(world.call("POST", f"/remove/{WORK}"), (200, {"message": REMOVED_BOTH}))
        before = world.registry.path.read_bytes()
        self.assertEqual(world.call("POST", f"/remove/{WORK}"), (404, {"error": server.UNKNOWN_ACCOUNT}))
        self.assertEqual(world.registry.path.read_bytes(), before)


class ResetTests(unittest.TestCase):
    def test_reset_goes_back_to_the_first_run_and_keeps_a_copy_beside_the_data_folder(self):
        world, service = mac(self)
        for uuid in (WORK, PERSONAL):
            seed(world, service, uuid)
        service.update_settings({"auto_switch": False, "alerts": {"week": False}})
        server.write_last_switch(service.last_switch_path, WORK, ALEX, world.clock.now)
        known = world.folder / "known_accounts.json"
        known.write_text(json.dumps([{"name": "Work Account", "short": "Work", "full": "Work", "email": "c@example.invalid", "organization": "personal"}]))
        claude_settings = world.home / ".claude" / "settings.json"
        claude_settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        accounts_before = world.registry.path.read_bytes()
        before = claude_side(world)
        reply = service.reset()
        saved = world.root / f"lifeline-before-reset-{STAMP}"
        self.assertEqual(
            reply["message"],
            f"Lifeline is back to its first run here and on the other computer. It saved a copy of its history and settings in {saved}.",
        )
        self.assertEqual(world.link.calls, [("POST", "/reset")])
        self.assertEqual(world.registry.load(), [])
        self.assertEqual([uuid for uuid in ORDER if world.store.get(uuid) is not None], [])
        self.assertEqual([uuid for uuid in ORDER if world.history.readings(uuid, START - timedelta(days=1))], [])
        self.assertIsNone(world.history.event_at(EVENT.format(WORK)))
        self.assertEqual(recovery.accounts(recovery.folder_for(world.lock_path)), [])
        for name in ("settings.json", "last_switch.json", "accounts.json"):
            self.assertFalse((world.folder / name).exists(), name)
        state = service.state()
        self.assertEqual((state["accounts"], state["notifications"]), ([], []))
        self.assertEqual(state["settings"]["auto_switch"], True)
        self.assertEqual(set(state["settings"]["alerts"].values()), {True})
        self.assertTrue(known.exists())
        self.assertEqual(claude_settings.read_text(), json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        self.assertEqual(claude_side(world), before)
        self.assertEqual((saved / "accounts.json").read_bytes(), accounts_before)
        self.assertFalse(json.loads((saved / "settings.json").read_text())["auto_switch"])
        self.assertTrue((saved / "last_switch.json").exists())
        self.assertTrue(History(saved / "history.db").readings(WORK, START - timedelta(days=1)))
        self.assertFalse((saved / "recovery").exists())
        for path in saved.rglob("*"):
            data = path.read_bytes()
            for secret in world.secrets():
                self.assertNotIn(secret.encode(), data, f"{path.name} holds a sign-in")
        service.tick()
        self.assertEqual(service.state()["mac"]["action_kind"], "adopt")
        self.assertEqual([uuid for uuid in ORDER if world.store.get(uuid) is not None], [])
        self.assertEqual(world.link.pushed[-1], [])
        world.assert_clean()

    def test_reset_while_the_devbox_is_down_waits_for_it_and_replaces_a_waiting_remove(self):
        world, service = mac(self, up=False)
        service.remove(WORK)
        reply = service.reset()
        self.assertTrue(
            reply["message"].startswith(
                "Lifeline is back to its first run here, and resets the other computer when the other computer next answers. It saved a copy"
            ),
            reply["message"],
        )
        again = world.service()
        world.link.up = True
        again.tick()
        self.assertEqual(world.link.calls[:2], [("POST", "/reset"), ("POST", "/registry")])
        self.assertNotIn(("POST", f"/remove/{WORK}"), world.link.calls)

    def test_a_damaged_list_of_accounts_still_clears_every_saved_sign_in(self):
        world, service = mac(self)
        world.store.put(NEWCOMER, world.issue(NEWCOMER))
        self.assertEqual(world.store.names(), {WORK, PERSONAL, ORG, NEWCOMER})
        world.registry.path.write_text("not a list")
        self.assertTrue(service.reset()["message"].startswith("Lifeline is back to its first run here and on the other computer."))
        self.assertEqual(world.registry.load(), [])
        self.assertEqual((world.store.names(), sorted(world.store.folder.glob("*.json"))), (set(), []))
        self.assertEqual((world.root / f"lifeline-before-reset-{STAMP}" / "accounts.json").read_text(), "not a list")

    def test_a_reset_that_cannot_list_its_sign_ins_changes_nothing(self):
        world, service = mac(self)
        world.registry.path.write_text("not a list")
        with mock.patch.object(world.store, "names", side_effect=OSError("no list")):
            self.assertEqual(refusal(service.reset), (500, server.NO_RESET_SIGNINS))
        self.assertEqual(world.store.names(), {WORK, PERSONAL, ORG})
        self.assertFalse(list(world.root.glob("lifeline-before-reset-*")))
        self.assertEqual(world.link.calls, [])

    def test_a_reset_that_stops_partway_says_some_sign_ins_may_be_gone(self):
        world, service = mac(self)
        real = world.store.remove

        def remove(uuid):
            if uuid == PERSONAL:
                raise OSError("keychain")
            real(uuid)

        with mock.patch.object(world.store, "remove", remove):
            status, sentence = refusal(service.reset)
        saved = world.root / f"lifeline-before-reset-{STAMP}"
        self.assertEqual(
            (status, sentence),
            (500, f"Lifeline could not finish the reset, and some saved sign-ins may already be gone. Try again. The copy it saved is in {saved}."),
        )
        self.assertEqual(json.loads((world.folder / server.FORGET_NAME).read_text()), [])

    def test_a_copy_that_cannot_be_made_changes_nothing(self):
        world, service = mac(self)
        (world.root / f"lifeline-before-reset-{STAMP}").write_text("in the way")
        before = world.registry.path.read_bytes()
        self.assertEqual(refusal(service.reset), (500, server.NO_RESET_COPY))
        self.assertEqual(world.registry.path.read_bytes(), before)
        self.assertEqual(world.link.calls, [])


class FixRoundOneTests(unittest.TestCase):
    def test_the_devbox_change_is_saved_before_anything_is_deleted(self):
        world, service = mac(self, up=False)
        queued = []
        real = world.store.remove

        def remove(uuid):
            queued.append(json.loads((world.folder / server.FORGET_NAME).read_text()))
            real(uuid)

        with mock.patch.object(world.store, "remove", remove):
            service.remove(WORK)
        self.assertEqual(queued, [[f"/remove/{WORK}"]])
        world.link.up = True
        world.link.status = 404
        world.link.said = {"error": server.UNKNOWN_ACCOUNT}
        again = world.service()
        again.tick()
        self.assertEqual(world.link.calls[0], ("POST", f"/remove/{WORK}"))
        self.assertEqual(json.loads((world.folder / server.FORGET_NAME).read_text()), [], "another computer that no longer has it counts as done")

    def test_a_remove_that_fails_partway_leaves_nothing_the_reader_can_write_back(self):
        world, service = mac(self, up=False)
        seed(world, service, WORK)
        with mock.patch.object(world.history, "forget", side_effect=OSError("database is locked")):
            self.assertEqual(refusal(lambda: service.remove(WORK)), (500, "Lifeline could not finish removing Work Account, so it is still listed. Try again."))
        self.assertEqual(recovery.accounts(recovery.folder_for(world.lock_path)), [])
        self.assertIsNone(world.store.get(WORK))
        self.assertIsNotNone(world.registry.by_uuid(WORK), "a retry needs it listed")
        self.assertEqual(json.loads((world.folder / server.FORGET_NAME).read_text()), [], "a remove that did not finish is not sent")
        service.tick()
        self.assertIsNone(world.store.get(WORK), "the reader wrote its sign-in back")
        self.assertEqual(service.remove(WORK)["message"], REMOVED_LATER)

    def test_with_no_devbox_nothing_is_queued_and_no_sentence_names_it(self):
        world = World(self)
        (world.folder / server.FORGET_NAME).write_text(json.dumps(["/reset", f"/remove/{ORG}"]))
        service = world.service()
        self.assertFalse((world.folder / server.FORGET_NAME).exists(), "a stale change waits for a computer added later")
        service.tick()
        state = service.state()
        texts = {account["uuid"]: account["remove_text"] for account in state["accounts"]}
        self.assertEqual(texts[WORK], "Lifeline removes Work Account, its saved sign-in and its history.")
        self.assertEqual(texts[ALEX], "Lifeline removes Alex's Account, its saved sign-in and its history, and Claude Code keeps using it.")
        self.assertEqual(
            state["settings_text"]["reset"],
            "Lifeline saves a copy of its history and settings, then removes every account, sign-in and setting. Claude Code keeps the account it uses.",
        )
        self.assertEqual(service.remove(WORK)["message"], "Lifeline removed Work Account.")
        self.assertTrue(service.reset()["message"].startswith("Lifeline is back to its first run. It saved a copy"))
        self.assertFalse((world.folder / server.FORGET_NAME).exists())
        world.link = ForgetLink()
        world.link.up = True
        later = world.service()
        later.tick()
        self.assertEqual([call for call in world.link.calls if call != ("POST", "/registry")], [])

    def test_no_sign_in_starts_while_a_remove_or_reset_runs(self):
        world, service = mac(self)
        refused = []

        def try_both(*args):
            for start in (lambda: service.start_job("add"), lambda: service.start_devbox_job("adopt", {}), service.reset):
                refused.append(refusal(start))

        with mock.patch.object(world.history, "forget", side_effect=try_both):
            service.remove(WORK)
            service.reset()
        busy = (409, switching.LIFELINE_BUSY)
        self.assertEqual(refused, [busy] * 6)
        self.assertEqual(service.state()["jobs"], [])

    def test_an_exchange_during_the_delete_holds_the_change_back(self):
        world, service = mac(self)
        sent = []

        def exchange(*args):
            service._send_devbox(world.link)
            sent.extend(world.link.calls)

        with mock.patch.object(world.history, "forget", side_effect=exchange):
            service.remove(WORK)
        self.assertEqual(sent, [], "the other computer heard of a remove before this Mac finished it")
        self.assertEqual(world.link.calls, [("POST", f"/remove/{WORK}")])

    def test_the_devbox_takes_no_sign_in_while_it_removes(self):
        world = DevboxWorld(self)
        service = world.service()
        service._forgetting = True
        self.assertEqual(refusal(lambda: service.start_job("add", None, {"uuid": WORK})), (409, switching.LIFELINE_BUSY))


class BusyTests(unittest.TestCase):
    def test_neither_runs_during_a_switch_or_a_sign_in(self):
        world, service = mac(self)
        before = (world.registry.path.read_bytes(), world.store.get(WORK))
        service._switch_target = WORK
        for action in (lambda: service.remove(WORK), service.reset):
            self.assertEqual(refusal(action), (409, server.SWITCH_BUSY))
        service._switch_target = None
        held, done = threading.Event(), threading.Event()

        def switching():
            with service._switching:
                held.set()
                done.wait(5)

        thread = threading.Thread(target=switching)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            with mock.patch.object(server, "FORGET_WAIT", 0.05):
                for action in (lambda: service.remove(WORK), service.reset):
                    self.assertEqual(refusal(action), (409, server.SWITCH_BUSY))
        finally:
            done.set()
            thread.join(5)
        service._jobs["j"] = {"id": "j", "kind": "devbox-add", "uuid": WORK, "state": "running", "finished_at": None}
        for action in (lambda: service.remove(WORK), service.reset):
            self.assertEqual(refusal(action), (409, server.SIGNIN_BUSY))
        self.assertEqual((world.registry.path.read_bytes(), world.store.get(WORK)), before)
        self.assertEqual(world.link.calls, [])
        self.assertFalse(list(world.root.glob("lifeline-before-reset-*")))


class DevboxSideTests(unittest.TestCase):
    def test_the_devbox_removes_an_account_and_its_sign_in_and_never_claude_codes(self):
        world = DevboxWorld(self)
        service = world.service()
        service.tick()
        live = (world.slot_path.read_bytes(), (world.home / ".claude.json").read_bytes())
        world.run(service)
        self.assertEqual(world.tcp("POST", f"/remove/{ALEX}"), (200, {"message": "Lifeline removed Alex's Account."}))
        self.assertIsNone(world.registry.by_uuid(ALEX))
        self.assertFalse((world.store.folder / f"{ALEX}.json").exists())
        self.assertEqual(world.tcp("POST", f"/remove/{WORK}")[0], 200)
        self.assertIsNone(world.registry.by_uuid(WORK))
        self.assertEqual((world.slot_path.read_bytes(), (world.home / ".claude.json").read_bytes()), live)
        self.assertEqual(world.local("POST", f"/remove/{ORG}"), (403, {"error": devbox.MAC_FORGETS}))
        self.assertEqual(world.local("POST", "/reset"), (403, {"error": devbox.MAC_FORGETS}))
        self.assertIsNotNone(world.registry.by_uuid(ORG))
        world.assert_clean()

    def test_the_devbox_reset_keeps_its_program_the_macs_address_and_claude_codes_sign_in(self):
        world = DevboxWorld(self)
        program = world.folder / "app" / "current" / "lifeline.txt"
        program.parent.mkdir(parents=True)
        program.write_text("the installed program")
        service = world.service()
        service.tick()
        live = (world.slot_path.read_bytes(), (world.home / ".claude.json").read_bytes())
        world.run(service)
        service._jobs["j"] = {"id": "j", "kind": "add", "uuid": ORG, "state": "running", "finished_at": None}
        self.assertEqual(world.tcp("POST", "/reset"), (409, {"error": server.SIGNIN_BUSY}))
        service._jobs.clear()
        status, reply = world.tcp("POST", "/reset")
        self.assertEqual(
            (status, reply["message"]),
            (200, f"Lifeline is back to its first run. It saved a copy of its history and settings in ~/.local/share/com.example.lifeline-before-reset-{STAMP}."),
        )
        self.assertEqual(world.registry.load(), [])
        self.assertEqual(sorted(world.store.folder.glob("*.json")), [])
        self.assertTrue((world.home / ".local" / "share" / f"com.example.lifeline-before-reset-{STAMP}" / "accounts.json").exists())
        self.assertEqual(program.read_text(), "the installed program")
        self.assertTrue(world.pairing_path.exists())
        self.assertEqual((world.slot_path.read_bytes(), (world.home / ".claude.json").read_bytes()), live)
        world.assert_clean()


class PairingTests(unittest.TestCase):
    def test_reset_keeps_the_pairing_key_and_a_new_pairing_drops_old_changes(self):
        world, service = mac(self, up=False)
        world.store.put(server.COMPUTER_ACCOUNT, world.issue(ORG))
        service.reset()
        self.assertEqual(world.store.names(), {server.COMPUTER_ACCOUNT}, "the pairing key shares the store's keychain service")
        self.assertEqual(service._devbox_todo, [server.RESET_ROUTE])
        service._use_computer(None, server.NullLink())
        self.assertEqual(service._devbox_todo, [])
        self.assertFalse((world.folder / server.FORGET_NAME).exists())


class ConfirmTextTests(unittest.TestCase):
    def test_state_carries_each_confirm_sentence(self):
        world, service = mac(self)
        state = service.state()
        texts = {account["uuid"]: account["remove_text"] for account in state["accounts"]}
        self.assertEqual((texts[ALEX], texts[WORK]), (ALEX_IN_USE, WORK_IDLE))
        self.assertEqual(state["settings_text"]["reset"], RESET_CONFIRM)


class HistoryForgetTests(unittest.TestCase):
    def test_forget_drops_one_account_or_every_one_and_copy_keeps_them(self):
        world = World(self)
        for uuid in (WORK, PERSONAL):
            world.usage_reading(uuid, 30.0, R_WORK)
            world.history.add_event(EVENT.format(uuid), world.clock.now)
        world.history.add_event("devbox-report-1", world.clock.now)
        kept = world.root / "kept.db"
        world.history.copy_to(kept)
        world.history.forget(WORK)
        since = START - timedelta(days=1)
        self.assertEqual((world.history.readings(WORK, since), world.history.event_at(EVENT.format(WORK))), ([], None))
        self.assertTrue(world.history.readings(PERSONAL, since))
        self.assertIsNotNone(world.history.event_at("devbox-report-1"))
        world.history.forget()
        self.assertEqual((world.history.readings(PERSONAL, since), world.history.event_at("devbox-report-1")), ([], None))
        self.assertEqual(len(History(kept).readings(WORK, since)), 1)


class CommandTests(unittest.TestCase):
    def test_remove_and_reset_go_through_lifeline_in_the_background(self):
        world = CommandWorld(self)
        fake = FakeService({
            ("POST", f"/remove/{WORK}"): [(200, {"message": REMOVED_BOTH}), (409, {"error": server.SWITCH_BUSY})],
            ("POST", "/reset"): (200, {"message": "Lifeline is back to its first run here and on the other computer."}),
        })
        self.assertEqual(world.run("remove", "2", service=lambda: fake), (0, REMOVED_BOTH + "\n", ""))
        self.assertEqual(fake.calls[-1], ("POST", f"/remove/{WORK}", None, cli.CHANGE_SECONDS))
        self.assertEqual(world.run("remove", "2", service=lambda: fake), (1, "", server.SWITCH_BUSY + "\n"))
        self.assertEqual(world.run("reset", service=lambda: fake), (0, "Lifeline is back to its first run here and on the other computer.\n", ""))
        self.assertEqual(fake.calls[-1], ("POST", "/reset", None, cli.CHANGE_SECONDS))
        self.assertIsNotNone(world.registry.by_uuid(WORK))

    def test_without_lifeline_in_the_background_nothing_changes(self):
        world = CommandWorld(self)
        before = world.registry.path.read_bytes()
        for argv in (("remove", "2"), ("reset",)):
            self.assertEqual(world.run(*argv, service=lambda: None), (1, "", cli.NEEDS_SERVICE + "\n"))
        self.assertEqual(world.registry.path.read_bytes(), before)

    def test_remove_takes_one_number_and_reset_takes_none(self):
        world = CommandWorld(self)
        self.assertEqual(world.run("remove"), (2, "", "Give the number of the account to remove, as in lifeline remove 2.\n"))
        self.assertEqual(world.run("reset", "2"), (2, "", "Lifeline did not expect 2 after reset. Run lifeline help to see its commands.\n"))
        self.assertEqual(world.run("remove", "9", service=lambda: FakeService({})), (1, "", "There is no account number 9 in Lifeline, so nothing was changed.\n"))


if __name__ == "__main__":
    unittest.main()
