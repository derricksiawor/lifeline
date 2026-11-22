import unittest
from dataclasses import replace

from lifeline import widgettext
from lifeline.forecast import Forecast
from lifeline.viewmodel import build
from tests.test_viewmodel import UTC, at, situation


def mac_model(name, **changes):
    state = situation(name)
    if changes:
        state = replace(state, **changes)
    return build(state, "dark")


class WidgetLinesTests(unittest.TestCase):
    def test_an_account_that_lasts_to_its_reset_says_so_plainly(self):
        out = mac_model("good")
        self.assertEqual(out["widget_line"], "Lasts until its reset at 17:00.")
        self.assertEqual(out["widget_head"], "Alex's Account lasts until its 5 hours reset at 17:00.")

    def test_a_run_out_before_the_reset_gives_its_time(self):
        out = mac_model("low")
        self.assertEqual(out["widget_line"], "Lasts until 15:40.")
        self.assertEqual(out["widget_head"], "Alex's Account lasts until 15:40.")

    def test_an_account_at_its_limit_is_out_until_it_is_back(self):
        out = mac_model("limit")
        self.assertEqual(out["widget_line"], "Out until 17:00.")
        self.assertEqual(out["widget_head"], "Alex's Account is out until 17:00.")

    def test_the_lines_follow_the_forecast_not_a_passing_moment(self):
        self.assertEqual(mac_model("auto")["widget_line"], "Out until 17:00.")
        self.assertNotIn("Switching", mac_model("auto")["widget_head"])
        self.assertNotIn("now uses", mac_model("switched")["widget_head"])

    def test_another_day_keeps_its_day(self):
        state = situation("low")
        alex = state.accounts[0]
        later = replace(alex, forecast=Forecast(140, at(15, 40).replace(day=12), None, "recent"), five_reset=at(17, 0).replace(day=12))
        out = build(replace(state, accounts=(later,) + state.accounts[1:]), "dark")
        self.assertEqual(out["widget_line"], "Lasts until tomorrow at 15:40.")

    def test_a_sign_in_or_a_missing_reading_is_said_plainly(self):
        state = situation("good")
        signed_out = replace(state.accounts[0], needs_signin=True)
        out = build(replace(state, accounts=(signed_out,) + state.accounts[1:]), "dark")
        self.assertEqual((out["widget_line"], out["widget_head"]), ("Needs a new sign-in.", "Alex's Account needs a new sign-in."))
        unread = replace(state.accounts[0], five=None, week=None, reading_at=None)
        out = build(replace(state, accounts=(unread,) + state.accounts[1:]), "dark")
        self.assertEqual((out["widget_line"], out["widget_head"]), ("No reading yet.", "Lifeline has no reading for Alex's Account yet."))

    def test_the_stale_line_names_the_reading_time_and_its_date(self):
        out = mac_model("low")
        self.assertEqual(out["widget_stale"], "Lifeline last updated this at 15:05 on 11 November.")
        self.assertEqual(build(replace(situation("good"), accounts=()), "dark")["widget_stale"], "Lifeline last updated this at 14:20 on 11 November.")

    def test_every_line_is_one_short_sentence(self):
        for name in ("good", "low", "limit", "auto", "switched", "back"):
            out = mac_model(name)
            for line in (out["widget_line"], out["widget_head"]):
                self.assertTrue(line.endswith("."), (name, line))
                self.assertEqual(line.count(". "), 0, (name, line))
            self.assertLessEqual(len(out["widget_line"]), 34, (name, out["widget_line"]))


class WidgetBlockTests(unittest.TestCase):
    def state(self, answers=True, dev_name="Work Account"):
        return {
            "mac": {"widget_line": "Lasts until 15:40.", "widget_head": "Alex's Account lasts until 15:40."},
            "dev": {"hero_name": dev_name},
            "devbox": {"answers": answers},
        }

    def test_the_block_carries_the_mac_lines_and_the_devbox_account(self):
        block = widgettext.extend(self.state())["widget"]
        self.assertEqual(block, {
            "small_line": "Lasts until 15:40.",
            "headline": "Alex's Account lasts until 15:40.",
            "sub": "The other computer uses Work Account.",
            "stale_line": "",
            "more": [],
        })

    def test_a_devbox_that_does_not_answer_is_said_plainly(self):
        self.assertEqual(widgettext.extend(self.state(answers=False))["widget"]["sub"], "Lifeline on the other computer is not answering.")
        self.assertEqual(widgettext.extend(self.state(dev_name=""))["widget"]["sub"], "")

    def test_the_block_carries_the_stale_line_and_a_line_for_each_left_out_count(self):
        state = self.state()
        state["mac"]["widget_stale"] = "Lifeline last updated this at 03:40 on 21 November."
        state["mac"]["rows"] = [{}] * 6
        block = widgettext.extend(state)["widget"]
        self.assertEqual(block["stale_line"], "Lifeline last updated this at 03:40 on 21 November.")
        self.assertEqual(block["more"], [
            "1 more account is in the menu.",
            "2 more accounts are in the menu.",
            "3 more accounts are in the menu.",
            "4 more accounts are in the menu.",
            "5 more accounts are in the menu.",
        ])
        self.assertEqual(widgettext.more_lines(1), [])
        self.assertEqual(widgettext.more_lines(0), [])


if __name__ == "__main__":
    unittest.main()
