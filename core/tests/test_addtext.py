import json
import unittest
from datetime import datetime, timezone

from lifeline import addtext

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 11, 30, tzinfo=UTC)
ENDS = datetime(2026, 12, 16, 9, 0, tzinfo=UTC)


def base(**changes):
    state = {
        "signin_browser": "Safari",
        "mac": {"rows": [{"uuid": "j", "name": "Alex's Account", "is_active": True}, {"uuid": "c", "name": "Work Account", "is_active": False}]},
        "accounts": [
            {"uuid": "j", "name": "Alex's Account", "plan": "Max 5x", "signin_ends": ENDS, "organization": "j@example.invalid's Organization"},
            {"uuid": "c", "name": "Work Account", "plan": "Max 20x", "signin_ends": None, "organization": "Example Team"},
        ],
        "known_accounts": [
            {"name": "Personal Account", "short": "Personal", "email": "sam@example.invalid", "organization": "personal"},
            {"name": "Team Account", "short": "Team", "email": "sam@example.invalid", "organization": "Example Org"},
            {"name": "Bare Account", "short": "Bare", "email": "", "organization": ""},
        ],
        "jobs": [],
    }
    state.update(changes)
    return state


def decorated(**changes):
    state = base(**changes)
    addtext.decorate(state, NOW, UTC)
    return state


class AddTextTests(unittest.TestCase):
    """every sentence on the add, waiting, naming and result screens comes from the core."""

    def test_the_add_sheet_sentences(self):
        text = decorated()["add_text"]
        self.assertEqual(text["title"], "Add an account")
        self.assertEqual(text["lead"], "Sign in to the account once. After that, switching to it never asks you to sign in.")
        self.assertEqual(
            text["explain"],
            "Lifeline opens Claude's sign-in in Safari, your default browser. Sign in there as the account you are adding. "
            "If Claude asks you to choose an organization, choose the one you are adding, then press Authorize.",
        )
        self.assertEqual(text["twice"], "The other computer gets its own sign-in too, so you press Authorize twice.")
        self.assertEqual(text["devbox_away"], "Lifeline on the other computer is not answering, so accounts are added to this Mac.")
        self.assertEqual(text["stays"], "Alex's Account stays signed in and in use on this Mac while you do this.")
        self.assertEqual(text["names_shown"].format(menu="Work Account", bar="Work"), "The menu shows Work Account, and the menu bar shows Work.")
        self.assertEqual(text["added"].format(name="Work Account"), "Work Account is added.")
        self.assertEqual(text["slow_answer"].format(seconds=15), "Lifeline did not answer within 15 seconds, so the result is not known yet.")
        for key in ("for_devbox", "page_not_opened", "page_not_opened_above", "failed", "lost", "cancelling", "cancelled", "names_shown_hidden", "unreadable"):
            self.assertTrue(text[key], key)

    def test_no_browser_and_no_account_in_use(self):
        state = base(signin_browser=None)
        state["mac"]["rows"][0]["is_active"] = False
        addtext.decorate(state, NOW, UTC)
        self.assertTrue(state["add_text"]["explain"].startswith("Lifeline opens Claude's sign-in in your default browser. "))
        self.assertIsNone(state["add_text"]["stays"])

    def test_each_known_account_has_its_pick_line_and_steps(self):
        known = {k["name"]: k for k in decorated()["known_accounts"]}
        self.assertEqual(known["Personal Account"]["pick_line"], "It is the personal account for sam@example.invalid.")
        self.assertEqual(
            known["Personal Account"]["explain"],
            "Lifeline opens Claude's sign-in in Safari, your default browser. Sign in there as sam@example.invalid. "
            "If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize.",
        )
        self.assertEqual(known["Team Account"]["pick_line"], "It signs in as sam@example.invalid and belongs to the Example Org organization.")
        self.assertTrue(known["Team Account"]["explain"].endswith("choose Example Org, then press Authorize."))
        self.assertIsNone(known["Bare Account"]["pick_line"])
        self.assertTrue(known["Bare Account"]["explain"].endswith("Sign in there as Bare Account. If Claude asks you to choose an organization, choose the one you are adding, then press Authorize."))

    def test_a_running_add_job_says_what_to_do(self):
        job = {"id": "1", "kind": "add", "uuid": None, "state": "running", "result": "", "browser": "Safari", "opened": True,
               "known": "Personal Account", "organization": "personal", "guidance": addtext.choose("sam@example.invalid's Organization")}
        state = decorated(jobs=[job])
        job = state["jobs"][0]
        steps = "Sign in there as sam@example.invalid. If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize."
        self.assertEqual(job["guidance"], "If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize.")
        self.assertEqual(job["waiting"], "Waiting for you to finish the sign-in in Safari.")
        self.assertEqual(job["compact"], "Finish the sign-in using Safari. This stays here until it is done.")
        self.assertEqual(job["lines"], [steps, "Alex's Account stays signed in and in use on this Mac while you do this."])
        self.assertEqual(job["card_lines"], [steps])
        self.assertEqual(job["label"], "Add an account")
        self.assertNotIn("title", job)

    def test_a_devbox_add_and_a_sign_in_again(self):
        devbox = {"id": "2", "kind": "devbox-add", "uuid": "c", "state": "running", "result": "", "browser": None, "opened": False,
                  "guidance": "If Claude asks you to choose an organization, choose Example Team, then press Authorize."}
        resign = {"id": "3", "kind": "resign", "uuid": "j", "state": "running", "result": "", "browser": "Safari", "opened": True,
                  "guidance": addtext.choose("j@example.invalid's Organization")}
        jobs = {j["id"]: j for j in decorated(jobs=[devbox, resign])["jobs"]}
        self.assertEqual(
            jobs["2"]["lines"],
            [
                "Sign in there as the account you are adding. If Claude asks you to choose an organization, choose Example Team, then press Authorize.",
                "This sign-in is for the other computer, so choose the same account again.",
                "Lifeline could not open the sign-in page by itself, so press Open the sign-in page.",
            ],
        )
        self.assertEqual(jobs["2"]["waiting"], "Waiting for you to finish the sign-in in your default browser.")
        self.assertEqual(
            jobs["3"]["lines"],
            ["Sign in there as Alex's Account. If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize."],
        )
        self.assertEqual(jobs["3"]["card_lines"], ["If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize."])
        self.assertEqual(jobs["3"]["label"], "Sign in again to Alex's Account")
        self.assertEqual((jobs["2"]["compact_title"], jobs["3"]["compact_title"]), ("Work Account", "Alex's Account"))
        self.assertNotIn("title", jobs["3"])

    def test_finished_jobs_give_a_head_and_the_rest(self):
        done = {"id": "4", "kind": "add", "uuid": "c", "state": "done", "result": "Lifeline added Work Account as number 2.", "suggested_name": "Work Account"}
        already = {"id": "5", "kind": "add", "uuid": None, "state": "failed",
                   "result": "Lifeline already has Personal Account. Safari signed in as sam@example.invalid, so nothing changed. "
                             "To add a different account, sign out of claude.ai in Safari or choose the other account, then press Try again."}
        cancelled = {"id": "6", "kind": "add", "uuid": None, "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}
        adopted = {"id": "7", "kind": "adopt", "uuid": "c", "state": "done", "result": "Lifeline added Work Account as number 2. Claude Code uses it now."}
        jobs = {j["id"]: j for j in decorated(jobs=[done, already, cancelled, adopted])["jobs"]}
        self.assertEqual((jobs["4"]["title"], jobs["4"]["result_head"], jobs["4"]["result_rest"]), ("Lifeline added Work Account", "Lifeline added Work Account.", ""))
        self.assertEqual(jobs["4"]["added_head"], "Work Account is added.")
        self.assertEqual(jobs["5"]["result_head"], "Lifeline already has Personal Account.")
        self.assertTrue(jobs["5"]["result_rest"].startswith("Safari signed in as sam@example.invalid, so nothing changed."))
        self.assertEqual((jobs["6"]["title"], jobs["6"]["result_head"], jobs["6"]["result_rest"]),
                         ("Lifeline cancelled the sign-in", "You cancelled the sign-in.", "Nothing changed."))
        self.assertEqual(jobs["7"]["added_head"], "Work Account is added.")
        self.assertNotIn("added_head", jobs["6"])

    def test_the_naming_screen_line_for_each_target(self):
        accounts = {a["uuid"]: a for a in decorated()["accounts"]}
        self.assertEqual(
            accounts["j"]["added_text"],
            {
                "mac": "It is signed in on this Mac. It is on the Max 5x plan. You will not need to sign in again until 16 December.",
                "both": "It is signed in on this Mac and on the other computer. It is on the Max 5x plan. You will not need to sign in again until 16 December.",
                "devbox": "It is signed in on the other computer, and this Mac will not use it. It is on the Max 5x plan.",
            },
        )
        self.assertEqual(accounts["c"]["added_text"]["mac"], "It is signed in on this Mac. It is on the Max 20x plan.")

    def test_a_title_appears_only_when_the_job_has_ended(self):
        jobs = [
            {"id": "r", "kind": "add", "uuid": None, "state": "running", "result": ""},
            {"id": "s", "kind": "resign", "uuid": "j", "state": "running", "result": ""},
            {"id": "d", "kind": "add", "uuid": "c", "state": "done", "result": "Lifeline added Work Account as number 2."},
            {"id": "f", "kind": "add", "uuid": None, "state": "failed", "result": "Lifeline could not finish adding the account."},
            {"id": "c", "kind": "resign", "uuid": "j", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."},
        ]
        found = {j["id"]: j for j in decorated(jobs=jobs)["jobs"]}
        self.assertNotIn("title", found["r"])
        self.assertNotIn("title", found["s"])
        self.assertEqual(found["d"]["title"], "Lifeline added Work Account")
        self.assertEqual(found["f"]["title"], "Lifeline did not add the account")
        self.assertEqual(found["c"]["title"], "Lifeline cancelled the sign-in")
        self.assertEqual(found["r"]["label"], "Add an account")

    def test_the_compact_title_follows_the_contract(self):
        jobs = [
            {"id": "k", "kind": "add", "uuid": None, "state": "running", "result": "", "known": "Team Account"},
            {"id": "a", "kind": "add", "uuid": "c", "state": "done", "result": "Lifeline added Work Account."},
            {"id": "n", "kind": "add", "uuid": None, "state": "running", "result": ""},
        ]
        found = {j["id"]: j["compact_title"] for j in decorated(jobs=jobs)["jobs"]}
        self.assertEqual(found, {"k": "Team Account", "a": "Work Account", "n": "Add an account"})

    def test_a_name_with_a_full_stop_stays_whole_in_the_head(self):
        state = base()
        state["accounts"].append({"uuid": "d", "name": "Dr. Lee Account", "plan": "", "signin_ends": None, "organization": "Lee Clinic"})
        state["jobs"] = [{"id": "x", "kind": "add", "uuid": None, "state": "failed",
                          "result": "Lifeline already has Dr. Lee Account. Safari signed in as lee@example.invalid, so nothing changed."}]
        addtext.decorate(state, NOW, UTC)
        job = state["jobs"][0]
        self.assertEqual((job["result_head"], job["result_rest"]), ("Lifeline already has Dr. Lee Account.", "Safari signed in as lee@example.invalid, so nothing changed."))

    def test_the_guidance_comes_from_one_place(self):
        given = "If Claude asks you to choose an organization, choose Example Team, then press Authorize."
        jobs = [
            {"id": "1", "kind": "resign", "uuid": "c", "state": "running", "result": "", "guidance": given, "opened": True},
            {"id": "2", "kind": "resign", "uuid": "j", "state": "done", "result": "Done.", "guidance": None},
        ]
        found = {j["id"]: j for j in decorated(jobs=jobs)["jobs"]}
        self.assertEqual(found["1"]["guidance"], given)
        self.assertEqual(found["1"]["card_lines"], [given])
        self.assertIsNone(found["2"]["guidance"])
        self.assertEqual(addtext.choose("j@example.invalid's Organization"), addtext.CHOOSE_PERSONAL)
        self.assertEqual(addtext.choose("personal"), addtext.CHOOSE_PERSONAL)
        self.assertEqual(addtext.choose("Example Team"), given)

    def test_a_second_step_gets_its_own_title(self):
        jobs = [
            {"id": "c", "kind": "devbox-add", "uuid": "c", "state": "cancelled", "step": [2, 2], "result": "Work Account is on this Mac. You cancelled the other computer step, so the other computer has no sign-in for it yet."},
            {"id": "f", "kind": "devbox-adopt", "uuid": "c", "state": "failed", "step": [2, 2], "result": "Work Account is on this Mac. Lifeline stopped, so it cancelled the other computer step. Lifeline does not manage the other computer's own sign-in for it yet."},
            {"id": "o", "kind": "devbox-add", "uuid": "c", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."},
        ]
        found = {j["id"]: j["title"] for j in decorated(jobs=jobs)["jobs"]}
        self.assertEqual(found, {"c": "You cancelled the other computer step", "f": "The other computer step did not finish", "o": "Lifeline cancelled the sign-in"})

    def test_every_sentence_is_plain(self):
        job = {"id": "1", "kind": "add", "uuid": None, "state": "running", "result": "", "browser": "Safari", "opened": False,
               "guidance": addtext.choose("sam@example.invalid's Organization")}
        state = decorated(jobs=[job])
        said = list(state["add_text"].values())
        said += [k[key] for k in state["known_accounts"] for key in ("pick_line", "explain")]
        said += [state["jobs"][0][key] for key in ("guidance", "waiting", "compact", "label")] + state["jobs"][0]["lines"]
        said += [line for a in state["accounts"] for line in a["added_text"].values()]
        text = json.dumps([s for s in said if s])
        for word in ("uuid", "token", "registry", " job", "route", "'s Organization", " may ", "about to"):
            self.assertNotIn(word, text, word)


if __name__ == "__main__":
    unittest.main()


class SettingsTextTests(unittest.TestCase):
    """Every Settings title, description and note comes from the core."""

    def settings(self, answers=True, statusline=None, name="build-box"):
        state = base(
            devbox={"answers": answers, "name": name, "address": "192.0.2.10:7433"},
            statusline=statusline or {"on": True, "ours": True, "other_present": False},
            settings_text={"auto_switch": {"title": "Kept", "description": "Kept as the service sent it."}},
        )
        addtext.decorate(state, NOW, UTC)
        return state["settings_text"]

    def test_the_page_and_the_accounts_note(self):
        text = self.settings()
        self.assertEqual(text["page"], {"title": "Alerts and settings", "lead": "Alerts reach you as notifications on this Mac."})
        self.assertEqual(text["accounts_note"], "Press an account's number in the menu to switch this Mac to it.")
        self.assertEqual(text["auto_switch"], {"title": "Kept", "description": "Kept as the service sent it."})

    def test_every_row_has_a_title_and_a_short_description(self):
        text = self.settings()
        rows = ("will_run_out", "limit", "back", "week", "signin_ends", "mac", "computer", "shortcut", "login", "show_name", "statusline")
        for key in rows:
            with self.subTest(key):
                self.assertTrue(text[key]["title"])
                description = text[key]["description"]
                self.assertLessEqual(description.count(". ") + 1, 2, description)
                self.assertLessEqual(len(description), 125, description)
        notes = [*text["mac"]["notes"].values(), *text["shortcut"]["notes"].values(), *text["login"]["notes"].values()]
        self.assertEqual(set(text["mac"]["notes"]), {"unavailable", "banners", "list_only", "refused", "failed"})
        self.assertEqual(set(text["shortcut"]["notes"]), {"taken", "refused"})
        self.assertEqual(set(text["login"]["notes"]), {"not_installed", "needs_approval", "failed"})
        for note in notes:
            self.assertLessEqual(len(note), 125, note)

    def test_the_rows_that_follow_the_service_state(self):
        text = self.settings()
        self.assertEqual(
            text["computer"]["description"],
            "This Mac reads build-box at 192.0.2.10:7433, and every request between them is signed with the key they share.",
        )
        self.assertEqual(self.settings(answers=False)["computer"]["description"], "This Mac is paired with build-box at 192.0.2.10:7433, but build-box is not answering now.")
        self.assertTrue(self.settings(name=None)["computer"]["description"].startswith("Lifeline can also read the accounts"))
        self.assertIsNone(text["statusline"]["note"])
        other = self.settings(statusline={"on": True, "ours": False, "other_present": True})
        self.assertEqual(other["statusline"]["note"], "Another status line is set there, so Lifeline leaves it alone.")
        unknown = self.settings(statusline={"on": None, "ours": None, "other_present": None})
        self.assertEqual(unknown["statusline"]["note"], "Lifeline could not read this setting just now.")
