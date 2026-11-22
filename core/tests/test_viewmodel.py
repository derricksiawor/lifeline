import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from lifeline.forecast import Forecast
from lifeline.viewmodel import AccountView, MenuState, build

UTC = timezone.utc
DAY = datetime(2026, 11, 11, tzinfo=UTC)
WEEK_RESET = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def at(hour, minute=0, second=0):
    return DAY.replace(hour=hour, minute=minute, second=second)


def account(uuid, name, short, plan, key, five, week, reset, fore, runs_out=None, now=None):
    return AccountView(
        uuid, name, short, plan, key, five, week, reset, WEEK_RESET,
        Forecast(fore, runs_out, None, "recent"), False, False, None, now,
    )


def situation(name):
    now = {"good": at(14, 20), "low": at(15, 5), "limit": at(15, 41), "auto": at(15, 41),
           "switched": at(15, 42), "back": at(17, 0)}[name]
    r1 = {"alex": at(17, 0), "work": at(18, 30), "solo": at(19, 10), "crew": None}
    tables = {
        "good": ({"alex": (42, 18), "work": (12, 55), "solo": (4, 31), "crew": (0, 9)}, r1,
                 {"alex": 71, "work": 30, "solo": 15, "crew": 0}),
        "low": ({"alex": (88, 22), "work": (19, 56), "solo": (6, 31), "crew": (0, 9)}, r1,
                {"alex": 140, "work": 34, "solo": 16, "crew": 0}),
        "limit": ({"alex": (100, 24), "work": (21, 56), "solo": (7, 32), "crew": (0, 9)}, r1,
                  {"alex": 100, "work": 36, "solo": 17, "crew": 0}),
        "switched": ({"alex": (100, 24), "work": (21, 56), "solo": (7, 32), "crew": (1, 9)},
                     {"alex": at(17, 0), "work": at(18, 30), "solo": at(19, 10), "crew": at(20, 42)},
                     {"alex": 100, "work": 36, "solo": 17, "crew": 58}),
        "back": ({"alex": (0, 24), "work": (34, 57), "solo": (12, 33), "crew": (38, 14)},
                 {"alex": None, "work": at(18, 30), "solo": at(19, 10), "crew": at(20, 42)},
                 {"alex": 0, "work": 52, "solo": 20, "crew": 74}),
    }
    tables["auto"] = tables["limit"]
    use, reset, fore = tables[name]
    runs = {"alex": at(15, 40)} if name == "low" else {}
    info = [
        ("alex", "Alex's Account", "Alex", "Max 5x", 1),
        ("work", "Work Account", "Work", "Max 20x", 2),
        ("solo", "Personal Account", "Personal", "Max 20x", 3),
        ("crew", "Team Account", "Team", "Max 5x", 4),
    ]
    accounts = tuple(
        account(u, n, s, p, k, use[u][0], use[u][1], reset[u], fore[u], runs.get(u), now)
        for u, n, s, p, k in info
    )
    mac = "crew" if name in ("switched", "back") else "alex"
    return MenuState(
        accounts, mac, "work", "mac",
        ("mac", "alex", "crew") if name == "switched" else None,
        "crew" if name == "auto" else None,
        "alex" if name == "back" else None,
        3, 2, now, UTC,
        sessions_moved=3 if name == "switched" else 0,
        back_window="five" if name == "back" else "",
    )


THURSDAY = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)


def weekly(week=57.0, fore=None, runs_out=None, now=None, reset=THURSDAY, **changes):
    view = AccountView(
        "solo", "Personal Account", "Personal", "Max 20x", 3, None, week, None, reset,
        Forecast(fore, runs_out, None, "recent") if fore is not None else None, False, False, None, now,
        binding="week",
    )
    return replace(view, **changes) if changes else view


def mixed(solo, mac="solo", **changes):
    now = at(14, 20)
    alex = account("alex", "Alex's Account", "Alex", "Max 5x", 1, 42, 18, at(17, 0), 71, now=now)
    work = account("work", "Work Account", "Work", "Max 20x", 2, 12, 55, at(18, 30), 30, now=now)
    crew = account("crew", "Team Account", "Team", "Max 5x", 4, 0, 9, None, 0, now=now)
    solo = replace(solo, reading_at=now) if solo.reading_at is None and (solo.week is not None or solo.five is not None) else solo
    state = MenuState((alex, work, solo, crew), mac, "work", "mac", None, None, None, 1, 0, now, UTC)
    return replace(state, **changes) if changes else state


class WeekBindingTests(unittest.TestCase):
    def test_a_week_binding_account_in_use_states_its_week(self):
        out = build(mixed(weekly()), "dark")
        self.assertEqual(out["head"], "Personal Account lasts until its week resets on 26 November at 02:00.")
        self.assertEqual(out["sub"], "")
        self.assertEqual((out["binding"], out["hero_used"], out["hero_reset"], out["hero_w"]), ("week", "57% of its week used", "Resets on 26 November at 02:00", "57%"))
        self.assertEqual((out["week_text"], out["week_w"]), ("", "0%"))
        self.assertEqual((out["icon_name"], out["icon_pct"], out["icon_w"], out["icon_binding"]), ("Personal", "57%", "57%", "week"))
        row = out["rows"][2]
        self.assertEqual(
            {key: row[key] for key in ("binding", "pct", "five_w", "week_pct", "week_w", "where", "is_active")},
            {"binding": "week", "pct": "57%", "five_w": "57%", "week_pct": "", "week_w": "0%",
             "where": "Used by this Mac. Resets on 26 November at 02:00.", "is_active": True},
        )
        self.assertEqual(
            {key: out["rows"][0][key] for key in ("binding", "pct", "five_w", "week_pct", "week_w")},
            {"binding": "five", "pct": "42%", "five_w": "42%", "week_pct": "Week 18%", "week_w": "18%"},
        )
        self.assertNotIn("5 hours", out["head"] + out["sub"])

    def test_a_week_binding_account_at_its_limit_is_out_until_its_reset(self):
        out = build(mixed(weekly(week=100.0)), "dark")
        self.assertEqual(out["head"], "Personal Account is out until 26 November at 02:00.")
        self.assertEqual(out["sub"], "Team Account has the most room.")
        self.assertEqual((out["hero_reset"], out["hero_tone"], out["action_kind"], out["action_target"]), ("Back on 26 November at 02:00", "limit", "primary", "crew"))
        self.assertEqual(out["rows"][2]["where"], "Used by this Mac. Back on 26 November at 02:00.")
        self.assertEqual(out["rows"][2]["tone"], "limit")

    def test_a_week_binding_account_that_will_run_out_says_when(self):
        runs = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
        out = build(mixed(weekly(week=80.0, fore=130.0, runs_out=runs)), "dark")
        self.assertEqual(out["head"], "Personal Account runs out on 24 November at 14:00.")
        self.assertEqual(out["sub"], "That is 1 day 12 hours before its week resets on 26 November at 02:00. Team Account has the most room.")
        self.assertEqual(out["hero_tone"], "low")

    def test_no_reading_yet_says_so_and_never_claims_five_hours(self):
        for binding in ("five", "week"):
            with self.subTest(binding=binding):
                bare = weekly(week=None, reset=None, binding=binding, reading_at=None)
                out = build(mixed(bare), "dark")
                self.assertEqual(out["head"], "Lifeline has no reading for Personal Account yet.")
                self.assertEqual(out["sub"], "")
                self.assertEqual((out["icon_pct"], out["hero_used"], out["hero_reset"]), ("", "", ""))
                self.assertEqual(out["rows"][2]["pct"], "")
                self.assertEqual(out["rows"][2]["where"], "Used by this Mac.")
                hero = {key: value for key, value in out.items() if key != "rows"}
                self.assertNotIn("5 hours", json.dumps(hero) + json.dumps(out["rows"][2]))

    def test_an_account_with_no_reading_is_never_the_best_choice(self):
        bare = weekly(week=None, reset=None, reading_at=None)
        out = build(mixed(bare, mac="alex"), "dark")
        self.assertFalse(out["rows"][2]["is_best"])
        self.assertTrue(out["rows"][3]["is_best"])

    def test_a_reading_with_no_value_on_its_own_limit_is_never_the_best_choice(self):
        state = mixed(weekly(week=None, reset=None, reading_at=at(14, 19)), mac="alex")
        alex, work, solo, crew = state.accounts
        accounts = (replace(alex, five=100.0), replace(work, needs_signin=True), solo, replace(crew, needs_signin=True))
        out = build(replace(state, accounts=accounts), "dark")
        self.assertIsNotNone(solo.reading_at)
        self.assertFalse(any(row["is_best"] for row in out["rows"]))
        self.assertEqual((out["action_kind"], out["action_target"]), ("none", ""))
        self.assertEqual(out["sub"], "No other account can take over right now.")

    def test_an_account_at_its_limit_is_never_the_best_choice(self):
        state = situation("limit")
        full = tuple(replace(a, five=100.0) for a in state.accounts)
        out = build(replace(state, accounts=full), "dark")
        self.assertFalse(any(row["is_best"] for row in out["rows"]))
        self.assertEqual(out["action_target"], "")
        self.assertEqual(out["sub"], "No other account has room. Work Account is back today at 18:30.")

    def test_the_best_account_is_the_one_with_most_room_on_its_own_limit(self):
        state = mixed(weekly(week=5.0), mac="alex")
        crew = replace(state.accounts[3], five=30.0)
        out = build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")
        self.assertTrue(out["rows"][2]["is_best"])
        limited = replace(state.accounts[0], five=100.0)
        out = build(replace(state, accounts=(limited,) + state.accounts[1:3] + (crew,)), "dark")
        self.assertEqual(out["sub"], "Personal Account has the most room.")

    def test_week_sentences_after_a_switch_in_a_countdown_and_when_back(self):
        out = build(mixed(weekly(week=100.0), mac="alex", just_switched=("mac", "solo", "alex"), sessions_moved=1), "dark")
        self.assertEqual(out["sub"], "Your open Claude Code session moved with it. Personal Account is back on 26 November at 02:00.")
        out = build(mixed(weekly(week=100.0), auto_switch_target="crew"), "dark")
        self.assertEqual(out["sub"], "Personal Account is out until 26 November at 02:00.")
        out = build(mixed(weekly(week=0.0), mac="alex", back_uuid="solo"), "dark")
        self.assertEqual(out["sub"], "Its week has reset, so you can use it again. This Mac stays on Alex's Account until you switch.")

    def test_every_shown_time_rounds_to_the_nearest_minute(self):
        state = situation("good")
        late = replace(state.accounts[0], five_reset=datetime(2026, 11, 11, 16, 59, 59, 678000, tzinfo=UTC), week_reset=THURSDAY)
        out = build(replace(state, accounts=(late,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
        self.assertEqual(out["week_text"], "Its week is 18% used and resets on 26 November at 02:00.")
        self.assertEqual(out["rows"][0]["where"], "Used by this Mac. Resets today at 17:00.")


LIVE_NOW = datetime(2026, 11, 19, 16, 35, tzinfo=UTC)
LIVE_RESET = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)
NO_DOUBLE_WORDS = ("on today", "on tomorrow", "on next", "until on", "by on", "Back on today", "at today")


def live(solo, **changes):
    state = mixed(replace(solo, reading_at=LIVE_NOW))
    accounts = tuple(replace(a, reading_at=LIVE_NOW) for a in state.accounts)
    return replace(state, accounts=accounts, now=LIVE_NOW, **changes)


def three(mac, alpha=(10.0, 40.0), bravo=(0.0, 100.0), charlie=60.0, **changes):
    five_reset = LIVE_NOW + timedelta(hours=2)

    def view(uuid, name, key, five, week):
        return AccountView(
            uuid, name, name.split()[0], "Max", key, five, week, five_reset if five is not None else None,
            LIVE_RESET if week is not None else None, None, False, False, None, LIVE_NOW,
        )

    accounts = (
        view("a", "Alpha Account", 1, *alpha),
        view("b", "Bravo Account", 2, *bravo),
        view("c", "Charlie Account", 3, None, charlie),
    )
    return replace(MenuState(accounts, mac, None, "mac", None, None, None, 1, 0, LIVE_NOW, UTC), **changes)


def pair(mac, alex, personal):
    def view(uuid, name, short, key, week):
        return AccountView(uuid, name, short, "Max", key, None, week, None, LIVE_RESET, None, False, False, None, LIVE_NOW, binding="week")

    accounts = (view("alex", "Alex's Account", "Alex", 1, alex), view("solo", "Personal Account", "Personal", 3, personal))
    return MenuState(accounts, mac, None, "mac", None, None, None, 1, 0, LIVE_NOW, UTC)


class MoreRoomTests(unittest.TestCase):
    def test_no_badge_when_the_account_in_use_has_the_most_room(self):
        out = build(pair("alex", 54.0, 86.0), "dark")
        self.assertEqual([row["is_best"] for row in out["rows"]], [False, False])
        self.assertEqual(out["action_kind"], "none")
        out = build(pair("alex", 54.0, 54.0), "dark")
        self.assertEqual([row["is_best"] for row in out["rows"]], [False, False])

    def test_the_badge_goes_to_an_account_with_more_room(self):
        out = build(pair("solo", 54.0, 83.0), "dark")
        self.assertEqual([row["is_best"] for row in out["rows"]], [True, False])
        out = build(pair("solo", 54.0, 100.0), "dark")
        self.assertEqual((out["sub"], out["action_target"]), ("Alex's Account has the most room.", "alex"))


class LeastRoomTests(unittest.TestCase):
    def test_an_account_with_a_full_week_is_never_best_and_shows_its_week(self):
        out = build(three("a"), "dark")
        alpha, bravo, charlie = out["rows"]
        self.assertFalse(bravo["is_best"])
        self.assertFalse(charlie["is_best"])
        self.assertEqual((bravo["binding"], bravo["pct"], bravo["week_pct"], bravo["tone"]), ("week", "100%", "", "limit"))
        self.assertEqual(bravo["where"], "Not in use. Back next Thursday at 02:00.")
        self.assertEqual((alpha["binding"], alpha["display"], alpha["pct"], alpha["thin_label"]), ("week", "five", "10%", "Week 40%"))

    def test_the_mac_on_an_account_out_for_its_week_is_out_and_offers_a_switch(self):
        out = build(three("b", bravo=(10.0, 100.0)), "dark")
        self.assertEqual(out["head"], "Bravo Account is out until next Thursday at 02:00.")
        self.assertEqual((out["binding"], out["hero_tone"], out["hero_reset"]), ("week", "limit", "Back next Thursday at 02:00"))
        self.assertEqual((out["action_kind"], out["action_target"]), ("primary", "a"))
        self.assertEqual((out["week_text"], out["icon_pct"], out["icon_binding"]), ("", "100%", "week"))

    def test_a_week_fuller_than_the_five_hours_binds_on_the_week(self):
        out = build(three("a", alpha=(10.0, 95.0)), "dark")
        self.assertEqual(out["head"], "Alpha Account lasts until its week resets next Thursday at 02:00.")
        self.assertEqual((out["binding"], out["rows"][0]["pct"], out["hero_w"]), ("week", "95%", "95%"))
        out = build(three("a", alpha=(42.0, 18.0)), "dark")
        self.assertEqual((out["binding"], out["rows"][0]["pct"], out["rows"][0]["week_pct"]), ("five", "42%", "Week 18%"))

    def test_all_of_its_5_hours_only_when_its_week_is_unused_too(self):
        state = situation("limit")
        fresh = replace(state.accounts[3], week=0.0)
        out = build(replace(state, accounts=state.accounts[:3] + (fresh,)), "dark")
        self.assertEqual(out["sub"], "Team Account has all of its 5 hours.")
        self.assertEqual(build(state, "dark")["sub"], "Team Account has the most room.")

    def test_the_back_sentence_names_the_window_that_ended(self):
        state = three("c", alpha=(0.0, 30.0), back_uuid="a", back_window="five")
        out = build(state, "dark")
        self.assertEqual(out["sub"], "It has all of its 5 hours again. This Mac stays on Charlie Account until you switch.")
        out = build(replace(state, back_window="week"), "dark")
        self.assertEqual(out["sub"], "Its week has reset, so you can use it again. This Mac stays on Charlie Account until you switch.")


def both(five=30.0, week=50.0, fore=None, other=None, mac="c", **changes):
    view = AccountView(
        "c", "Work Account", "Work", "Max", 2, five, week, LIVE_NOW + timedelta(hours=3), LIVE_RESET,
        fore, False, False, None, LIVE_NOW, other_forecast=other,
    )
    view = replace(view, **changes) if changes else view
    return MenuState((view,), mac, None, "mac", None, None, None, 1, 0, LIVE_NOW, UTC)


class BothWindowsTests(unittest.TestCase):
    def test_the_earlier_run_out_drives_the_head_and_tone(self):
        fore = Forecast(60.0, None, 0.001, "recent")
        other = Forecast(250.0, LIVE_NOW + timedelta(minutes=35), 2.0, "recent")
        out = build(both(fore=fore, other=other), "dark")
        self.assertEqual(out["head"], "Work Account runs out today at 17:10.")
        self.assertEqual(out["sub"], "That is 2 hours 25 minutes before its 5 hours reset today at 19:35.")
        self.assertEqual((out["binding"], out["hero_used"], out["hero_w"], out["hero_tone"]), ("week", "30% of its 5 hours used", "30%", "low"))
        self.assertEqual(
            (out["second_window"], out["second_text"], out["second_w"]),
            ("week", "Its week is 50% used. It resets next Thursday at 02:00.", "50%"),
        )
        row = out["rows"][0]
        self.assertEqual((row["pct"], row["tone"], row["second_window"], row["second_w"]), ("30%", "low", "week", "50%"))
        self.assertEqual(row["second_text"], out["second_text"])

    def test_the_other_window_is_a_second_line(self):
        out = build(both(five=42.0, week=18.0), "dark")
        self.assertEqual((out["binding"], out["second_window"], out["second_w"]), ("five", "week", "18%"))
        self.assertEqual(out["second_text"], "Its week is 18% used. It resets next Thursday at 02:00.")
        out = build(both(), "dark")
        self.assertEqual((out["display"], out["second_window"]), ("five", "week"))
        self.assertEqual(out["second_text"], "Its week is 50% used. It resets next Thursday at 02:00.")
        out = build(both(five=30.0, week=85.0), "dark")
        self.assertEqual((out["display"], out["second_window"]), ("week", "five"))
        self.assertEqual(out["second_text"], "Its 5 hours are 30% used and reset today at 19:35.")
        out = build(both(five=None, five_reset=None), "dark")
        self.assertEqual((out["second_window"], out["second_text"], out["second_w"]), ("", "", "0%"))
        self.assertEqual((out["rows"][0]["second_window"], out["rows"][0]["second_text"]), ("", ""))

    def test_a_week_running_out_first_names_its_week(self):
        fore = Forecast(150.0, LIVE_NOW + timedelta(days=2), 0.01, "recent")
        out = build(both(five=10.0, week=60.0, fore=fore, other=Forecast(20.0, None, 0.01, "recent")), "dark")
        self.assertEqual(out["head"], "Work Account runs out on Saturday at 16:35.")
        self.assertTrue(out["sub"].startswith("That is 4 days 9 hours before its week resets next Thursday at 02:00."), out["sub"])

    def test_a_run_out_time_that_has_passed_with_100_or_more_is_about_to_run_out(self):
        passed = Forecast(390.0, LIVE_NOW - timedelta(minutes=5), 2.0, "recent")
        out = build(both(five=30.0, week=20.0, fore=passed), "dark")
        self.assertEqual((out["head"], out["sub"], out["hero_tone"]), ("Work Account runs out now.", "Lifeline last read 30% at 16:35.", "low"))
        self.assertNotIn("390", json.dumps(out))
        out = build(both(fore=Forecast(60.0, None, 0.001, "recent"), other=passed), "dark")
        self.assertEqual((out["head"], out["hero_tone"], out["rows"][0]["tone"]), ("Work Account runs out now.", "low", "low"))
        out = build(both(five=30.0, week=20.0, fore=Forecast(80.0, None, 0.1, "recent")), "dark")
        self.assertEqual(out["sub"], "At this pace it reaches 80% before its 5 hours reset.")

    def test_a_run_out_at_or_after_the_reset_uses_all_of_it_just_as_it_resets(self):
        for runs_out in (None, LIVE_NOW + timedelta(hours=3), LIVE_NOW + timedelta(hours=4)):
            with self.subTest(runs_out=runs_out):
                late = Forecast(100.6, runs_out, 0.5, "recent")
                out = build(both(five=30.0, week=20.0, fore=late), "dark")
                self.assertEqual(out["head"], "Work Account lasts until its 5 hours reset today at 19:35.")
                self.assertEqual(out["sub"], "At this pace it uses all of its 5 hours just as they reset.")
                self.assertEqual((out["hero_tone"], out["action_kind"]), ("low", "none"))
                self.assertNotIn("100.6", json.dumps(out))
        out = build(both(five=30.0, five_reset=LIVE_NOW + timedelta(hours=3), week=None, week_reset=None, fore=Forecast(100.0, None, 0.5, "recent")), "dark")
        self.assertEqual(out["sub"], "At this pace it uses all of its 5 hours just as they reset.")
        out = build(both(five=None, five_reset=None, week=60.0, fore=Forecast(100.0, None, 0.01, "recent")), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Work Account lasts until its week resets next Thursday at 02:00.", "At this pace it uses all of its week just as it resets."))
        out = build(both(week=60.0, fore=Forecast(100.0, None, 0.01, "recent")), "dark")
        self.assertEqual(out["sub"], "At this pace it uses all of its week just as it resets.")
        out = build(both(fore=Forecast(60.0, None, 0.001, "recent"), other=Forecast(100.4, None, 0.5, "recent")), "dark")
        self.assertEqual((out["head"], out["hero_tone"]), ("Work Account lasts until its 5 hours reset today at 19:35.", "low"))
        self.assertEqual(out["sub"], "At this pace it uses all of its 5 hours just as they reset.")
        out = build(both(week=85.0, fore=Forecast(60.0, None, 0.001, "recent"), other=Forecast(100.4, None, 0.5, "recent")), "dark")
        self.assertEqual(out["head"], "Work Account lasts until its week resets next Thursday at 02:00.")
        self.assertEqual(out["sub"], "At this pace it uses all of its 5 hours just as they reset.")

    def test_a_projection_of_101_or_more_never_says_it_uses_all_of_it_just_as_it_resets(self):
        for runs_out in (None, LIVE_NOW + timedelta(hours=3)):
            with self.subTest(runs_out=runs_out):
                out = build(both(five=30.0, week=20.0, fore=Forecast(101.0, runs_out, 0.5, "recent")), "dark")
                self.assertEqual((out["head"], out["sub"], out["hero_tone"]), ("Work Account lasts until its 5 hours reset today at 19:35.", "", "low"))
        out = build(both(five=30.0, week=20.0, fore=Forecast(100.9, None, 0.5, "recent")), "dark")
        self.assertEqual(out["sub"], "At this pace it uses all of its 5 hours just as they reset.")

    def test_about_to_run_out_only_while_the_newest_reading_is_under_20_minutes_old(self):
        passed = Forecast(390.0, LIVE_NOW - timedelta(minutes=30), 2.0, "recent")
        out = build(both(five=30.0, week=20.0, fore=passed, reading_at=LIVE_NOW - timedelta(minutes=19)), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Work Account runs out now.", "Lifeline last read 30% at 16:16."))
        out = build(both(five=30.0, week=20.0, fore=passed, reading_at=LIVE_NOW - timedelta(minutes=20)), "dark")
        self.assertEqual((out["head"], out["hero_tone"]), ("Work Account was due to run out today at 16:05.", "low"))
        self.assertTrue(out["sub"].startswith("Lifeline last read it at 16:15."), out["sub"])
        self.assertNotIn("Last read", out["sub"])
        out = build(both(five=30.0, week=20.0, fore=passed, reading_at=datetime(2026, 11, 18, 11, 0, tzinfo=UTC)), "dark")
        self.assertEqual(out["head"], "Work Account was due to run out today at 16:05.")
        self.assertTrue(out["sub"].startswith("Lifeline last read it yesterday at 11:00."), out["sub"])
        self.assertNotIn("Last read", out["sub"])

    def test_both_windows_full_are_out_until_the_week_resets(self):
        for five, week in ((100.0, 100.0), (104.0, 100.0), (100.0, 100.4)):
            with self.subTest(five=five, week=week):
                out = build(both(five=five, week=week), "dark")
                self.assertEqual(out["head"], "Work Account is out until next Thursday at 02:00.")
        out = build(both(five=100.0, week=100.0), "dark")
        self.assertEqual(out["head"], "Work Account is out until next Thursday at 02:00.")
        self.assertEqual((out["binding"], out["hero_reset"], out["rows"][0]["where"]), ("week", "Back next Thursday at 02:00", "Used by this Mac. Back next Thursday at 02:00."))

    def test_a_run_out_time_that_has_passed_is_none(self):
        passed = Forecast(180.0, LIVE_NOW - timedelta(hours=1), 0.01, "recent")
        out = build(both(five=None, five_reset=None, week=97.0, fore=passed), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Work Account runs out now.", "Lifeline last read 97% at 16:35."))
        self.assertNotIn("15:35", json.dumps(out))
        out = build(both(fore=Forecast(60.0, None, 0.001, "recent"), other=passed), "dark")
        self.assertEqual(out["head"], "Work Account runs out now.")
        self.assertEqual((out["display"], out["hero_used"]), ("five", "30% of its 5 hours used"))
        self.assertEqual(out["second_text"], "Its week is 50% used. It resets next Thursday at 02:00.")
        calm = Forecast(60.0, LIVE_NOW - timedelta(hours=1), 0.01, "recent")
        out = build(both(five=None, five_reset=None, week=50.0, fore=calm), "dark")
        self.assertEqual(out["head"], "Work Account lasts until its week resets next Thursday at 02:00.")


class PassedTimeTests(unittest.TestCase):
    def test_a_passed_reset_never_reads_today(self):
        state = live(weekly(week=61.0, reset=LIVE_RESET), mac_uuid="alex")
        alex = replace(state.accounts[0], five=40.0, five_reset=datetime(2026, 11, 19, 10, 0, tzinfo=UTC), week=None, week_reset=None)
        out = build(replace(state, accounts=(alex,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account has used 40% of its 5 hours.")
        self.assertNotIn("10:00", json.dumps(out))

    def test_last_read_names_the_day_when_it_is_not_today(self):
        state = live(weekly(week=61.0, reset=LIVE_RESET))
        solo = replace(state.accounts[2], reading_at=datetime(2026, 11, 18, 13, 20, tzinfo=UTC))
        out = build(replace(state, accounts=state.accounts[:2] + (solo,) + state.accounts[3:]), "dark")
        self.assertTrue(out["sub"].endswith("Last read yesterday at 13:20."), out["sub"])
        solo = replace(solo, reading_at=datetime(2026, 11, 19, 13, 20, tzinfo=UTC))
        out = build(replace(state, accounts=state.accounts[:2] + (solo,) + state.accounts[3:]), "dark")
        self.assertTrue(out["sub"].endswith("Last read at 13:20."), out["sub"])


class FutureDayTests(unittest.TestCase):
    def assertClean(self, out):
        text = json.dumps(out)
        for words in NO_DOUBLE_WORDS:
            self.assertNotIn(words, text)

    def test_the_live_week_reset_reads_next_thursday(self):
        out = build(live(weekly(week=61.0, reset=LIVE_RESET)), "dark")
        self.assertEqual(out["head"], "Personal Account lasts until its week resets next Thursday at 02:00.")
        self.assertEqual(out["sub"], "")
        self.assertEqual(out["hero_reset"], "Resets next Thursday at 02:00")
        self.assertEqual(out["rows"][2]["where"], "Used by this Mac. Resets next Thursday at 02:00.")
        self.assertClean(out)

    def test_a_week_account_out_until_next_thursday(self):
        out = build(live(weekly(week=100.0, reset=LIVE_RESET)), "dark")
        self.assertEqual(out["head"], "Personal Account is out until next Thursday at 02:00.")
        self.assertEqual(out["hero_reset"], "Back next Thursday at 02:00")
        self.assertEqual(out["rows"][2]["where"], "Used by this Mac. Back next Thursday at 02:00.")
        self.assertClean(out)

    def test_a_week_account_two_to_six_days_ahead_says_on_the_weekday(self):
        out = build(live(weekly(week=61.0, reset=datetime(2026, 11, 22, 2, 0, tzinfo=UTC))), "dark")
        self.assertEqual(out["head"], "Personal Account lasts until its week resets on Sunday at 02:00.")
        out = build(live(weekly(week=100.0, reset=datetime(2026, 11, 22, 2, 0, tzinfo=UTC))), "dark")
        self.assertEqual(out["head"], "Personal Account is out until Sunday at 02:00.")

    def test_a_five_hour_reset_today_and_tomorrow(self):
        state = live(weekly(week=61.0, reset=LIVE_RESET), mac_uuid="alex")
        alex = replace(state.accounts[0], five=40.0, five_reset=datetime(2026, 11, 19, 20, 0, tzinfo=UTC), forecast=Forecast(71, None, None, "recent"))
        out = build(replace(state, accounts=(alex,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 20:00.")
        self.assertEqual(out["hero_reset"], "Resets today at 20:00")
        late = replace(alex, five_reset=datetime(2026, 11, 20, 1, 0, tzinfo=UTC))
        out = build(replace(state, accounts=(late,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset tomorrow at 01:00.")
        full = replace(late, five=100.0)
        out = build(replace(state, accounts=(full,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account is out until tomorrow at 01:00.")
        self.assertEqual(out["hero_reset"], "Back tomorrow at 01:00")
        self.assertClean(out)

    def test_a_run_out_and_a_pause_name_their_day(self):
        state = live(weekly(week=61.0, reset=LIVE_RESET), mac_uuid="alex")
        alex = replace(
            state.accounts[0], five=80.0, five_reset=datetime(2026, 11, 20, 1, 0, tzinfo=UTC),
            forecast=Forecast(140, datetime(2026, 11, 19, 23, 59, 45, tzinfo=UTC), None, "recent"),
            paused_until=datetime(2026, 11, 19, 17, 5, tzinfo=UTC),
        )
        out = build(replace(state, accounts=(alex,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account runs out tomorrow at 00:00.")
        self.assertTrue(out["sub"].startswith("That is 1 hour before its 5 hours reset tomorrow at 01:00."), out["sub"])
        self.assertTrue(out["sub"].endswith("Lifeline could not take a reading, so readings are paused until today at 17:05."), out["sub"])
        self.assertClean(out)


class ViewModelTests(unittest.TestCase):
    def test_menu_sentences_for_each_situation(self):
        good = build(situation("good"), "dark")
        self.assertEqual(good["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
        self.assertEqual(good["sub"], "At this pace it reaches 71% before its 5 hours reset.")
        self.assertEqual(good["action_kind"], "none")
        self.assertEqual(good["hint"], "")
        self.assertEqual(good["view_label"], "This Mac")
        self.assertEqual(good["sessions"], "3 sessions open")
        self.assertEqual(good["hero_name"], "Alex's Account")
        self.assertEqual(good["hero_plan"], "Max 5x")
        self.assertEqual(good["hero_w"], "42%")
        self.assertEqual(good["fore_w"], "29%")
        self.assertEqual(good["hero_used"], "42% of its 5 hours used")
        self.assertEqual(good["hero_reset"], "Resets today at 17:00")
        self.assertEqual(good["week_w"], "18%")
        self.assertEqual(good["week_text"], "Its week is 18% used and resets on Sunday at 09:00.")
        self.assertEqual(good["hero_tone"], "calm")
        self.assertEqual((good["icon_name"], good["icon_pct"], good["icon_w"]), ("Alex", "42%", "42%"))
        self.assertEqual([r["where"] for r in good["rows"]], [
            "Used by this Mac. Resets today at 17:00.",
            "Used by the other computer. Resets today at 18:30.",
            "Not in use. Resets today at 19:10.",
            "Not in use.",
        ])
        self.assertEqual([r["binding"] for r in good["rows"]], ["five", "week", "week", "week"])
        self.assertEqual([r["display"] for r in good["rows"]], ["five", "five", "five", "five"])
        self.assertEqual([r["pct"] for r in good["rows"]], ["42%", "12%", "4%", "0%"])
        self.assertEqual([r["thin_label"] for r in good["rows"]], ["Week 18%", "Week 55%", "Week 31%", "Week 9%"])
        self.assertEqual([r["is_best"] for r in good["rows"]], [False, False, False, True])
        self.assertEqual((good["rows"][0]["week_pct"], good["rows"][1]["week_pct"]), ("Week 18%", "Week 55%"))
        self.assertEqual(good["rows"][3]["fore_w"], "0%")
        self.assertTrue(good["rows"][3]["unused"])

        low = build(situation("low"), "dark")
        self.assertEqual(low["head"], "Alex's Account runs out today at 15:40.")
        self.assertEqual(
            low["sub"],
            "That is 1 hour 20 minutes before its 5 hours reset today at 17:00. Team Account has the most room.",
        )
        self.assertEqual(low["action_kind"], "primary")
        self.assertEqual(low["action_text"], "Switch this Mac to Team Account")
        self.assertEqual(low["action_target"], "crew")
        self.assertEqual(low["hint"], "Or press 4.")
        self.assertEqual(low["hero_tone"], "low")
        self.assertEqual(low["fore_w"], "12%")

        limit = build(situation("limit"), "dark")
        self.assertEqual(limit["head"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(limit["sub"], "Team Account has the most room.")
        self.assertEqual(limit["action_kind"], "primary")
        self.assertEqual(limit["hint"], "Or press 4.")
        self.assertEqual(limit["hero_tone"], "limit")
        self.assertEqual(limit["hero_reset"], "Back today at 17:00")
        self.assertEqual(limit["rows"][0]["where"], "Used by this Mac. Back today at 17:00.")
        self.assertEqual(limit["fore_w"], "0%")
        self.assertFalse(limit["countdown"])

        auto = build(situation("auto"), "dark")
        self.assertEqual(auto["head"], "Switching this Mac to Team Account in 5 seconds.")
        self.assertEqual(auto["sub"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(auto["action_kind"], "cancel")
        self.assertEqual(auto["action_text"], "Cancel the switch")
        self.assertTrue(auto["countdown"])

        switched = build(situation("switched"), "dark")
        self.assertEqual(switched["head"], "This Mac now uses Team Account.")
        self.assertEqual(switched["sub"], "Your 3 open Claude Code sessions moved with it. Alex's Account is back today at 17:00.")
        self.assertEqual(switched["action_kind"], "undo")
        self.assertEqual(switched["action_text"], "Undo the switch")
        self.assertEqual(switched["icon_name"], "Team")
        self.assertEqual((switched["icon_w"], switched["icon_pct"], switched["icon_binding"]), ("4%", "1%", "week"))
        self.assertEqual(switched["rows"][3]["fore_w"], "0%")
        self.assertEqual(switched["rows"][3]["where"], "Used by this Mac. Resets today at 20:42.")

        back = build(situation("back"), "dark")
        self.assertEqual(back["head"], "Alex's Account is back.")
        self.assertEqual(
            back["sub"],
            "It has all of its 5 hours again. This Mac stays on Team Account until you switch.",
        )
        self.assertEqual(back["action_kind"], "quiet")
        self.assertEqual(back["action_text"], "Switch back to Alex's Account")
        self.assertEqual(back["hint"], "Or press 1.")
        self.assertEqual(back["action_target"], "alex")
        self.assertEqual(back["rows"][0]["where"], "Not in use.")

    def test_devbox_view_has_no_switch_action(self):
        for name in ("good", "low", "limit", "switched", "back"):
            state = replace(situation(name), view="dev")
            out = build(state, "dark")
            self.assertEqual(out["action_kind"], "none", name)
            self.assertEqual(out["action_text"], "", name)
            self.assertEqual(out["action_target"], "", name)
            self.assertEqual(out["hint"], "Lifeline switches only this Mac, so switch the other computer there.", name)
            self.assertFalse(out["countdown"], name)
            self.assertEqual(out["hero_name"], "Work Account")
            self.assertEqual(out["view_label"], "The other computer")
            self.assertEqual(out["sessions"], "2 sessions open")
            self.assertTrue(out["rows"][1]["is_active"])

    def test_devbox_runs_out_keeps_sentences_without_action(self):
        state = situation("low")
        work = replace(state.accounts[1], five=90.0, five_reset=at(18, 30),
                      forecast=Forecast(130.0, at(16, 0), 1.0, "recent"))
        state = replace(state, accounts=(state.accounts[0], work) + state.accounts[2:], view="dev")
        out = build(state, "dark")
        self.assertEqual(out["head"], "Work Account runs out today at 16:00.")
        self.assertTrue(out["sub"].startswith("That is 2 hours 30 minutes before its 5 hours reset today at 18:30."))
        self.assertEqual(out["action_kind"], "none")

    def test_stale_reading_adds_last_read(self):
        state = situation("good")
        old = replace(state.accounts[0], reading_at=state.now - timedelta(minutes=11))
        out = build(replace(state, accounts=(old,) + state.accounts[1:]), "dark")
        self.assertTrue(out["sub"].endswith(" Last read at 14:09."), out["sub"])
        fresh = replace(state.accounts[0], reading_at=state.now - timedelta(minutes=10))
        out = build(replace(state, accounts=(fresh,) + state.accounts[1:]), "dark")
        self.assertNotIn("Last read", out["sub"])

    def test_paused_readings(self):
        state = situation("good")
        paused = replace(state.accounts[0], paused_until=at(15, 10))
        out = build(replace(state, accounts=(paused,) + state.accounts[1:]), "dark")
        self.assertTrue(out["sub"].endswith(" Lifeline could not take a reading, so readings are paused until today at 15:10."), out["sub"])
        both = replace(paused, reading_at=state.now - timedelta(hours=1))
        out = build(replace(state, accounts=(both,) + state.accounts[1:]), "dark")
        self.assertTrue(out["sub"].endswith(" Last read at 13:20. Lifeline could not take a reading, so readings are paused until today at 15:10."), out["sub"])
        over = replace(paused, paused_until=state.now - timedelta(minutes=1))
        out = build(replace(state, accounts=(over,) + state.accounts[1:]), "dark")
        self.assertNotIn("paused", out["sub"])

    def test_needs_a_new_signin(self):
        state = situation("good")
        dead = replace(state.accounts[0], needs_signin=True)
        out = build(replace(state, accounts=(dead,) + state.accounts[1:]), "dark")
        self.assertEqual(out["head"], "Alex's Account needs a new sign-in.")
        self.assertEqual(out["action_kind"], "resign")
        self.assertEqual(out["action_target"], "alex")
        self.assertEqual(out["rows"][0]["where"], "Needs a new sign-in. Click it to sign in.")
        self.assertTrue(out["rows"][0]["needs_signin"])
        dev = build(replace(state, accounts=(dead,) + state.accounts[1:], view="dev"), "dark")
        self.assertEqual(dev["action_kind"], "none")
        self.assertEqual(dev["rows"][0]["where"], "Needs a new sign-in.")

    def test_dead_account_is_never_the_best_choice(self):
        state = situation("limit")
        dead = replace(state.accounts[3], needs_signin=True)
        out = build(replace(state, accounts=state.accounts[:3] + (dead,)), "dark")
        self.assertEqual(out["action_target"], "solo")
        self.assertEqual(out["sub"], "Personal Account has the most room.")

    def test_values_above_100(self):
        state = situation("limit")
        over = replace(state.accounts[0], five=137.9, week=104.2, forecast=Forecast(180.0, at(15, 0), 1.0, "recent"))
        out = build(replace(state, accounts=(over,) + state.accounts[1:]), "dark")
        self.assertEqual(out["binding"], "week")
        self.assertEqual(out["hero_used"], "104% of its week used")
        self.assertEqual(out["hero_w"], "100%")
        self.assertEqual(out["fore_w"], "0%")
        self.assertEqual((out["second_window"], out["second_w"]), ("five", "100%"))
        self.assertEqual(out["second_text"], "Its 5 hours are 137% used and reset today at 17:00.")
        self.assertEqual(out["rows"][0]["pct"], "104%")
        self.assertEqual(out["rows"][0]["five_w"], "100%")
        self.assertEqual(out["rows"][0]["second_w"], "100%")
        self.assertEqual(out["icon_pct"], "104%")
        self.assertEqual(out["icon_w"], "100%")

    def test_light_and_dark_give_the_same_text(self):
        for name in ("good", "low", "limit", "auto", "switched", "back"):
            for view in ("mac", "dev"):
                state = replace(situation(name), view=view)
                dark = build(state, "dark")
                light = build(state, "light")
                self.assertEqual(dark.pop("appearance"), "dark")
                self.assertEqual(light.pop("appearance"), "light")
                self.assertEqual(dark, light)
        with self.assertRaises(ValueError):
            build(situation("good"), "sepia")

    def test_no_account_used_by_q_is_excluded_from_best(self):
        state = situation("limit")
        crew = replace(state.accounts[3], five=60.0, week=40.0)
        accounts = state.accounts[:3] + (crew,)
        out = build(replace(state, accounts=accounts), "dark")
        self.assertEqual(out["action_target"], "solo")
        self.assertTrue(out["rows"][2]["is_best"])
        self.assertEqual(out["rows"][2]["where"], "Not in use. Resets today at 19:10.")
        self.assertEqual(out["sub"], "Personal Account has the most room.")

    def test_week_breaks_a_tie_on_five_hours(self):
        state = situation("limit")
        crew = replace(state.accounts[3], five=7.0, week=50.0)
        out = build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")
        self.assertEqual(out["action_target"], "solo")

    def test_an_account_the_devbox_uses_is_offered_only_with_more_room(self):
        state = replace(situation("limit"), devbox_uuid="work", mac_uuid="alex")
        only = replace(state, accounts=state.accounts[:2])
        out = build(only, "dark")
        self.assertEqual((out["action_kind"], out["action_target"]), ("primary", "work"))
        full = replace(only, accounts=(only.accounts[0], replace(only.accounts[1], five=100.0)))
        out = build(full, "dark")
        self.assertEqual(out["action_kind"], "none")
        self.assertEqual(out["sub"], "No other account has room, and this one is back first.")

    def test_times_use_the_menu_timezone(self):
        state = replace(situation("good"), tz=timezone(timedelta(hours=2)))
        out = build(state, "dark")
        self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 19:00.")

    def test_single_session_and_no_reset_text(self):
        state = replace(situation("good"), sessions_mac=1)
        self.assertEqual(build(state, "dark")["sessions"], "1 session open")
        fresh = replace(state.accounts[3], week=0.0)
        crew = replace(state, mac_uuid="crew", accounts=state.accounts[:3] + (fresh,))
        out = build(crew, "dark")
        self.assertEqual(out["head"], "Team Account has all of its 5 hours.")
        self.assertEqual(out["sub"], "Its 5 hours start when you use it.")
        self.assertEqual(out["hero_reset"], "Not started")
        out = build(replace(state, mac_uuid="crew"), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Team Account has all of its 5 hours.", "Its 5 hours start when you use it."))
        self.assertEqual((out["thin_label"], out["second_text"]), ("Week 9%", "Its week is 9% used and resets on Sunday at 09:00."))

    def test_cannot_read_and_unknown_numbers(self):
        state = situation("good")
        blind = replace(state.accounts[0], five=None, week=None, forecast=None, five_reset=None,
                        week_reset=None, cannot_read=True)
        out = build(replace(state, accounts=(blind,) + state.accounts[1:]), "dark")
        self.assertEqual((out["hero_used"], out["display"], out["head"]), ("", "", "Lifeline has no reading for Alex's Account yet."))
        self.assertIn("Anthropic does not let Lifeline read its usage. Lifeline tries again once you sign in to it again.", out["sub"])
        self.assertEqual(out["week_text"], "")

    def test_no_account_on_the_machine(self):
        out = build(replace(situation("good"), mac_uuid=None), "dark")
        self.assertEqual(out["head"], "No account is in use on this Mac.")
        self.assertEqual(out["action_kind"], "none")
        self.assertEqual(out["icon_pct"], "")

    def test_a_live_account_lifeline_does_not_know_yet(self):
        out = build(replace(situation("good"), mac_uuid="stranger", live_signed_in=True), "dark")
        self.assertEqual(out["head"], "This Mac uses an account Lifeline has not added yet.")
        self.assertEqual(out["sub"], "It takes one Authorize in your browser.")
        self.assertEqual((out["action_kind"], out["action_text"]), ("add", "Add this account"))
        self.assertEqual(out["icon_name"], "No account")
        self.assertFalse(any(row["is_active"] for row in out["rows"]))

    def test_a_live_signin_not_identified_yet(self):
        out = build(replace(situation("good"), mac_uuid=None, live_signed_in=True), "dark")
        self.assertEqual(out["head"], "Lifeline cannot tell yet which account Claude Code uses.")
        self.assertEqual(out["sub"], "It checks again within 5 minutes.")
        dev = build(replace(situation("good"), view="dev", devbox_uuid=None, live_signed_in=True), "dark")
        self.assertEqual(dev["head"], "No account is in use on the other computer.")

    def test_the_switch_sentence_counts_only_sessions_whose_readings_prove_it(self):
        cases = (
            (3, 0, "3 of your 5 open Claude Code sessions have moved so far. Alex's Account is back today at 17:00."),
            (0, 2, "2 open Claude Code sessions were still on Alex's Account at their last message. Alex's Account is back today at 17:00."),
            (
                1,
                1,
                "1 of your 5 open Claude Code sessions has moved so far. 1 open Claude Code session was still on Alex's Account at its last message. "
                "Alex's Account is back today at 17:00.",
            ),
            (0, 0, "Alex's Account is back today at 17:00."),
        )
        for moved, behind, sub in cases:
            with self.subTest(moved=moved, behind=behind):
                state = replace(situation("switched"), sessions_moved=moved, sessions_behind=behind, sessions_mac=5)
                out = build(state, "dark")
                self.assertEqual(out["head"], "This Mac now uses Team Account.")
                self.assertEqual(out["sub"], sub)
                self.assertEqual(out["sessions"], "5 sessions open")

    def test_after_a_switch_the_old_account_stays_ready_to_switch_back_to(self):
        state = replace(situation("switched"), sessions_moved=2)
        alex = replace(state.accounts[0], five=40.0)
        out = build(replace(state, accounts=(alex,) + state.accounts[1:]), "dark")
        self.assertEqual(out["sub"], "2 of your 3 open Claude Code sessions have moved so far. Undo takes you back to Alex's Account.")
        self.assertNotIn("keeps its place", json.dumps(out))

    def _with_first(self, name="good", **changes):
        state = situation(name)
        first = replace(state.accounts[0], **changes)
        return replace(state, accounts=(first,) + state.accounts[1:])

    def test_every_other_account_needs_a_new_signin(self):
        state = situation("limit")
        dead = tuple(replace(a, needs_signin=True) if a.uuid != "alex" else a for a in state.accounts)
        out = build(replace(state, accounts=dead, devbox_uuid=None), "dark")
        self.assertEqual(out["sub"], "Every other account needs a new sign-in.")
        self.assertEqual(out["action_kind"], "none")

    def test_being_in_use_on_the_devbox_is_never_the_reason(self):
        state = situation("limit")
        out = build(replace(state, accounts=state.accounts[:2]), "dark")
        self.assertEqual(out["sub"], "Work Account has the most room.")
        self.assertNotIn("in use", out["sub"])

    def test_a_single_account_has_no_one_to_switch_to(self):
        state = situation("limit")
        out = build(replace(state, accounts=state.accounts[:1], devbox_uuid=None), "dark")
        self.assertEqual(out["head"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(out["sub"], "There is no other account to switch to.")
        self.assertEqual(out["action_kind"], "none")

    def test_mixed_reasons_give_the_general_sentence(self):
        state = situation("limit")
        alex, work, solo = state.accounts[0], replace(state.accounts[1], needs_signin=True), replace(state.accounts[2], five=100.0)
        mixed = replace(state, accounts=(alex, work, solo), mac_uuid="alex", devbox_uuid="solo")
        out = build(mixed, "dark")
        self.assertEqual(out["sub"], "No other account can take over right now.")
        self.assertEqual(out["action_kind"], "none")

    def test_boundaries_of_the_limit_and_low_tones(self):
        out = build(self._with_first(five=99.9, forecast=Forecast(99.9, None, None, "none")), "dark")
        self.assertEqual(out["hero_used"], "99% of its 5 hours used")
        self.assertEqual(out["hero_tone"], "low")
        self.assertEqual((out["action_kind"], out["action_target"], out["sub"]), ("primary", "crew", "Team Account has the most room."))
        self.assertNotIn("out until", out["head"])
        out = build(self._with_first(five=79.9, forecast=Forecast(79.9, None, None, "none")), "dark")
        self.assertEqual(out["hero_tone"], "calm")
        out = build(self._with_first(five=80.0, forecast=Forecast(80.0, None, None, "none")), "dark")
        self.assertEqual(out["hero_tone"], "low")
        out = build(self._with_first(five=100.0), "dark")
        self.assertEqual(out["hero_tone"], "limit")
        self.assertEqual(out["head"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(out["rows"][0]["tone"], "limit")

    def test_negative_value_shows_as_zero(self):
        out = build(self._with_first(five=-5.0, week=-1.0), "dark")
        self.assertEqual(out["hero_used"], "0% of its 5 hours used")
        self.assertEqual(out["hero_w"], "0%")
        self.assertEqual(out["week_text"], "It has all of its week.")
        self.assertEqual(out["rows"][0]["pct"], "0%")

    def test_forecast_at_reset_of_exactly_100_makes_it_low_without_runout(self):
        out = build(self._with_first(forecast=Forecast(100.0, None, 1.0, "recent")), "dark")
        self.assertEqual(out["hero_tone"], "low")
        self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
        self.assertEqual(out["sub"], "At this pace it uses all of its 5 hours just as they reset.")
        out = build(self._with_first(forecast=Forecast(99.9, None, 1.0, "recent")), "dark")
        self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
        self.assertEqual(out["sub"], "At this pace it reaches 99% before its 5 hours reset.")

    def test_runout_not_before_the_reset_is_not_announced(self):
        for runs_out in (at(17, 0), at(17, 30)):
            out = build(self._with_first(forecast=Forecast(120.0, runs_out, 1.0, "recent")), "dark")
            self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
            self.assertNotIn("before it resets", out["sub"])
            self.assertEqual(out["action_kind"], "none")
            self.assertNotIn("120", json.dumps(out))
        out = build(self._with_first(forecast=Forecast(120.0, at(16, 59, 40), 1.0, "recent")), "dark")
        self.assertEqual(out["head"], "Alex's Account runs out today at 17:00.")
        self.assertIn("less than a minute before its 5 hours reset today at 17:00.", out["sub"])

    def test_use_without_a_reset_time_does_not_claim_all_5_hours(self):
        out = build(self._with_first(five_reset=None, forecast=None), "dark")
        self.assertEqual(out["head"], "Alex's Account has used 42% of its 5 hours.")
        self.assertNotIn("all of its 5 hours", out["head"] + out["sub"])

    def test_hero_labels(self):
        self.assertEqual(build(situation("good"), "dark")["hero_reset"], "Resets today at 17:00")
        self.assertEqual(build(situation("limit"), "dark")["hero_reset"], "Back today at 17:00")
        out = build(self._with_first(five=100.0, five_reset=None), "dark")
        self.assertEqual(out["hero_reset"], "Reset time unknown")
        self.assertNotIn("Limit reached", str(out))
        out = build(self._with_first(five=42.0, five_reset=None, forecast=None), "dark")
        self.assertEqual(out["hero_reset"], "Reset time unknown")
        out = build(self._with_first(five=0.0, five_reset=None, week=0.0, forecast=None), "dark")
        self.assertEqual(out["hero_reset"], "Not started")
        out = build(self._with_first(five=0.0, five_reset=None, forecast=None), "dark")
        self.assertEqual((out["binding"], out["display"], out["hero_reset"]), ("week", "five", "Not started"))
        out = build(self._with_first(five=0.0, five_reset=None, week=85.0, forecast=None), "dark")
        self.assertEqual((out["binding"], out["display"], out["hero_reset"]), ("week", "week", "Resets on Sunday at 09:00"))

    def _with_devbox(self, name="good", **changes):
        state = situation(name)
        work = replace(state.accounts[1], **changes)
        return replace(state, accounts=(state.accounts[0], work) + state.accounts[2:])

    def test_signin_ends_and_on_mac_default_and_close_the_fields(self):
        view = situation("good").accounts[0]
        self.assertIsNone(view.signin_ends)
        self.assertTrue(view.on_mac)
        fields = list(view.__dataclass_fields__)
        self.assertEqual(fields[-7:], ["signin_ends", "on_mac", "binding", "other_forecast", "on_devbox", "climbing", "pause_reason"])
        self.assertFalse(view.on_devbox)
        self.assertIsNone(view.climbing)
        self.assertIsNone(view.pause_reason)
        ends = at(20, 0)
        full = AccountView(*[getattr(view, f) for f in fields[:-7]], ends)
        self.assertEqual(full.signin_ends, ends)
        self.assertTrue(full.on_mac)
        self.assertEqual(full.binding, "five")

    def test_icon_dot_is_off_when_the_devbox_account_is_fine(self):
        for name in ("good", "low", "limit", "auto", "switched", "back"):
            for view in ("mac", "dev"):
                out = build(replace(situation(name), view=view), "dark")
                self.assertIs(out["icon_dot"], False, (name, view))

    def test_icon_dot_does_not_follow_the_mac_account(self):
        out = build(situation("limit"), "dark")
        self.assertEqual(out["icon_tone"], "limit")
        self.assertIs(out["icon_dot"], False)

    def test_icon_dot_when_the_devbox_account_is_low(self):
        out = build(self._with_devbox(five=80.0), "dark")
        self.assertIs(out["icon_dot"], True)
        out = build(self._with_devbox(five=79.9), "dark")
        self.assertIs(out["icon_dot"], False)

    def test_icon_dot_when_the_devbox_account_will_run_out(self):
        out = build(self._with_devbox(forecast=Forecast(100.0, None, 1.0, "recent")), "dark")
        self.assertIs(out["icon_dot"], True)
        out = build(self._with_devbox(forecast=Forecast(99.9, None, 1.0, "recent")), "dark")
        self.assertIs(out["icon_dot"], False)

    def test_icon_dot_when_the_devbox_account_is_at_its_limit(self):
        out = build(self._with_devbox(five=100.0), "dark")
        self.assertIs(out["icon_dot"], True)
        out = build(self._with_devbox(five=137.0), "light")
        self.assertIs(out["icon_dot"], True)

    def test_icon_dot_when_the_devbox_account_needs_a_new_signin(self):
        out = build(self._with_devbox(needs_signin=True), "dark")
        self.assertIs(out["icon_dot"], True)

    def test_icon_dot_when_the_devbox_signin_ends_within_3_days(self):
        now = situation("good").now
        for ends, expected in (
            (now + timedelta(days=3), True),
            (now + timedelta(days=3, seconds=1), False),
            (now + timedelta(days=2, hours=23), True),
            (now, True),
            (now - timedelta(days=1), True),
            (now + timedelta(days=30), False),
            (None, False),
        ):
            out = build(self._with_devbox(signin_ends=ends), "dark")
            self.assertIs(out["icon_dot"], expected, ends)
            dev = build(replace(self._with_devbox(signin_ends=ends), view="dev"), "dark")
            self.assertIs(dev["icon_dot"], expected, ends)

    def test_icon_dot_ignores_a_mac_signin_ending_soon(self):
        state = situation("good")
        soon = replace(state.accounts[0], signin_ends=state.now + timedelta(hours=1))
        out = build(replace(state, accounts=(soon,) + state.accounts[1:]), "dark")
        self.assertIs(out["icon_dot"], False)

    def test_icon_dot_with_no_devbox_account(self):
        state = replace(self._with_devbox(five=100.0, needs_signin=True), devbox_uuid=None)
        self.assertIs(build(state, "dark")["icon_dot"], False)
        state = replace(self._with_devbox(five=100.0), devbox_uuid="gone")
        self.assertIs(build(state, "dark")["icon_dot"], False)
        state = replace(self._with_devbox(five=100.0), mac_uuid=None)
        out = build(state, "dark")
        self.assertIs(out["icon_dot"], True)
        self.assertEqual(out["icon_pct"], "")

    def test_icon_dot_when_the_devbox_and_the_mac_share_an_account(self):
        state = replace(situation("good"), devbox_uuid="alex")
        self.assertIs(build(state, "dark")["icon_dot"], False)
        state = replace(situation("low"), devbox_uuid="alex")
        self.assertIs(build(state, "dark")["icon_dot"], True)

    def test_state_is_frozen(self):
        state = situation("good")
        with self.assertRaises(Exception):
            state.view = "dev"



class PlainWordingTests(unittest.TestCase):
    def test_a_key_hint_appears_only_for_the_first_nine_accounts(self):
        import dataclasses

        state = situation("limit")
        far = tuple(dataclasses.replace(a, key=12) if a.uuid == "crew" else a for a in state.accounts)
        out = build(dataclasses.replace(state, accounts=far), "dark")
        self.assertEqual((out["action_target"], out["hint"]), ("crew", ""))
        near = tuple(dataclasses.replace(a, key=9) if a.uuid == "crew" else a for a in state.accounts)
        self.assertEqual(build(dataclasses.replace(state, accounts=near), "dark")["hint"], "Or press 9.")
        back = situation("back")
        far = tuple(dataclasses.replace(a, key=10) if a.uuid == "alex" else a for a in back.accounts)
        self.assertEqual(build(dataclasses.replace(back, accounts=far), "dark")["hint"], "")

    def test_the_devbox_view_says_switching_it_is_not_here_yet(self):
        out = build(replace(situation("good"), view="dev"), "dark")
        self.assertEqual(out["hint"], "Lifeline switches only this Mac, so switch the other computer there.")

    def test_each_row_names_the_window_of_its_main_figure_and_shows_the_week_beside_the_5_hours(self):
        rows = {r["uuid"]: r for r in build(situation("good"), "dark")["rows"]}
        self.assertEqual((rows["alex"]["pct_window"], rows["alex"]["pct_label"], rows["alex"]["week_pct"]), ("5 hours", "5 hours 42%", "Week 18%"))
        self.assertEqual((rows["work"]["pct_window"], rows["work"]["pct_label"], rows["work"]["week_pct"]), ("5 hours", "5 hours 12%", "Week 55%"))
        solo = rows_of(build(mixed(weekly()), "dark"))["solo"]
        self.assertEqual((solo["pct_window"], solo["pct_label"], solo["week_pct"]), ("week", "Week 57%", ""))
        no_week = build(both(five=30.0, week=None, week_reset=None), "dark")["rows"][0]
        self.assertEqual((no_week["pct_label"], no_week["week_pct"]), ("5 hours 30%", ""))

    def test_the_hero_figure_names_its_window(self):
        self.assertEqual(build(situation("good"), "dark")["hero_used"], "42% of its 5 hours used")
        self.assertEqual(build(replace(situation("good"), view="dev"), "dark")["hero_used"], "12% of its 5 hours used")
        self.assertEqual(build(mixed(weekly()), "dark")["hero_used"], "57% of its week used")

    def test_a_week_bound_hero_says_its_reset_once(self):
        out = build(replace(situation("good"), view="dev"), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Work Account lasts until its 5 hours reset today at 18:30.", ""))
        state = replace(situation("good"), view="dev")
        heavy = replace(state.accounts[1], week=85.0, forecast=None)
        out = build(replace(state, accounts=(state.accounts[0], heavy) + state.accounts[2:]), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Work Account lasts until its week resets on Sunday at 09:00.", ""))
        self.assertEqual(out["hero_reset"], "Resets on Sunday at 09:00")
        climbing = build(mixed(weekly(week=57.0, fore=70.0), now=at(14, 20)), "dark")
        self.assertEqual(climbing["head"], "Personal Account lasts until its week resets on 26 November at 02:00.")
        self.assertEqual(climbing["sub"], "At this pace it reaches 70% before its week resets.")

    def test_an_unused_account_that_is_still_climbing_says_so(self):
        state = situation("good")
        crew = replace(state.accounts[3], five=5.0, forecast=Forecast(20.0, None, 0.1, "recent"))
        rows = {r["uuid"]: r for r in build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")["rows"]}
        self.assertEqual(rows["crew"]["where"], "In use elsewhere.")
        idle = {r["uuid"]: r for r in build(state, "dark")["rows"]}
        self.assertTrue(idle["crew"]["where"].startswith("Not in use."))

    def test_the_countdown_counts(self):
        out = build(situation("auto"), "dark")
        self.assertEqual((out["head"], out["sub"]), ("Switching this Mac to Team Account in 5 seconds.", "Alex's Account is out until today at 17:00."))
        self.assertEqual(out["countdown_left"], 5)
        self.assertEqual(build(replace(situation("auto"), countdown_left=1), "dark")["head"], "Switching this Mac to Team Account in 1 second.")

    def test_one_vocabulary_for_the_two_windows(self):
        good = build(situation("good"), "dark")
        self.assertEqual(good["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
        self.assertEqual(good["sub"], "At this pace it reaches 71% before its 5 hours reset.")
        self.assertEqual(good["week_text"], "Its week is 18% used and resets on Sunday at 09:00.")
        low = build(situation("low"), "dark")
        self.assertEqual(low["sub"], "That is 1 hour 20 minutes before its 5 hours reset today at 17:00. Team Account has the most room.")
        fresh = build(both(five=0.0, five_reset=None, week=None, week_reset=None, fore=None), "dark")
        self.assertEqual((fresh["head"], fresh["sub"]), ("Work Account has all of its 5 hours.", "Its 5 hours start when you use it."))
        late = build(both(five=30.0, five_reset=LIVE_NOW + timedelta(hours=3), week=None, week_reset=None, fore=Forecast(100.0, None, 0.5, "recent")), "dark")
        self.assertEqual(late["sub"], "At this pace it uses all of its 5 hours just as they reset.")

    def test_an_unused_window_has_all_of_it(self):
        state = situation("good")
        heavy = replace(state.accounts[3], week=85.0)
        rows = {r["uuid"]: r for r in build(replace(state, accounts=state.accounts[:3] + (heavy,)), "dark")["rows"]}
        self.assertEqual((rows["crew"]["second_window"], rows["crew"]["second_text"]), ("five", "It has all of its 5 hours."))

    def test_one_sentence_explains_the_amber_dot(self):
        state = situation("good")
        work = replace(state.accounts[1], needs_signin=True)
        out = build(replace(state, accounts=(state.accounts[0], work) + state.accounts[2:]), "dark")
        self.assertTrue(out["icon_dot"])
        self.assertEqual(out["devbox_note"], "The other computer needs you. Work Account needs a new sign-in.")
        work = replace(state.accounts[1], five=100.0, week=56.0)
        out = build(replace(state, accounts=(state.accounts[0], work) + state.accounts[2:]), "dark")
        self.assertEqual(out["devbox_note"], "The other computer needs you. Work Account is out until today at 18:30.")
        self.assertEqual(build(state, "dark")["devbox_note"], "")

    def test_a_row_with_an_old_reading_says_when_it_was_read(self):
        state = situation("good")
        old = replace(state.accounts[2], reading_at=at(13, 5))
        rows = {r["uuid"]: r for r in build(replace(state, accounts=state.accounts[:2] + (old,) + state.accounts[3:]), "dark")["rows"]}
        self.assertEqual((rows["solo"]["where"], rows["solo"]["read_note"], rows["solo"]["stale"]), ("Not in use. Resets today at 19:10.", "Last read at 13:05.", True))
        self.assertEqual((rows["alex"]["read_note"], rows["alex"]["stale"]), ("", False))

    def test_both_users_read_as_one_list(self):
        state = replace(situation("good"), devbox_uuid="alex")
        rows = {r["uuid"]: r for r in build(state, "dark")["rows"]}
        self.assertTrue(rows["alex"]["where"].startswith("Used by both machines."))

    def test_after_a_switch_undo_is_named_plainly(self):
        state = replace(situation("good"), just_switched=("mac", "work", "alex"))
        self.assertIn("Undo takes you back to Work Account.", build(state, "dark")["sub"])

    def test_the_head_holds_one_sentence(self):
        passed = Forecast(390.0, LIVE_NOW - timedelta(minutes=30), 2.0, "recent")
        out = build(both(five=30.0, week=20.0, fore=passed, reading_at=LIVE_NOW - timedelta(minutes=20)), "dark")
        self.assertEqual(out["head"], "Work Account was due to run out today at 16:05.")
        self.assertTrue(out["sub"].startswith("Lifeline last read it at 16:15."), out["sub"])

    def test_sessions_that_have_not_reported_are_not_claimed_to_have_moved(self):
        state = replace(situation("switched"), sessions_moved=1, sessions_behind=0, sessions_mac=3)
        self.assertIn("1 of your 3 open Claude Code sessions has moved so far.", build(state, "dark")["sub"])
        state = replace(situation("switched"), sessions_moved=3, sessions_behind=0, sessions_mac=3)
        self.assertIn("Your 3 open Claude Code sessions moved with it.", build(state, "dark")["sub"])

    def test_an_account_that_cannot_be_read_says_why_and_when_lifeline_tries_again(self):
        state = situation("good")
        alex = replace(state.accounts[0], cannot_read=True)
        out = build(replace(state, accounts=(alex,) + state.accounts[1:]), "dark")
        self.assertIn("Anthropic does not let Lifeline read its usage. Lifeline tries again once you sign in to it again.", out["sub"])



WORK_KNOWN = {"name": "Work Account", "short": "Work", "email": "hello@example.invalid"}


class SeenOnlyTests(unittest.TestCase):

    def test_climbing_names_only_what_lifeline_can_see(self):
        state = situation("good")
        crew = replace(state.accounts[3], five=5.0, forecast=Forecast(20.0, None, 0.1, "recent"))
        climbing = replace(state, accounts=state.accounts[:3] + (crew,))
        rows = {r["uuid"]: r for r in build(climbing, "dark")["rows"]}
        self.assertEqual(rows["crew"]["where"], "In use elsewhere.")
        rows = {r["uuid"]: r for r in build(replace(climbing, devbox_answers=False), "dark")["rows"]}
        self.assertEqual(rows["crew"]["where"], "In use elsewhere.")

    def test_while_the_devbox_does_not_answer_no_row_says_it_uses_an_account(self):
        rows = {r["uuid"]: r for r in build(replace(situation("good"), devbox_answers=False), "dark")["rows"]}
        self.assertFalse(rows["work"]["where"].startswith("Used by the other computer"), rows["work"]["where"])
        self.assertTrue(rows["alex"]["where"].startswith("Used by this Mac."))

    def test_the_devbox_names_a_login_lifeline_has_not_added(self):
        state = replace(situation("good"), view="dev", devbox_uuid="work-login-not-added", devbox_unadded_known=WORK_KNOWN)
        out = build(state, "dark")
        self.assertEqual((out["head"], out["sub"]), ("The other computer uses Work Account, which Lifeline has not added yet.", ""))
        self.assertEqual(out["unadded_known"], WORK_KNOWN)
        plain = build(replace(state, devbox_unadded_known=None), "dark")
        self.assertEqual(plain["head"], "The other computer uses an account Lifeline has not added yet.")
        self.assertIsNone(plain["unadded_known"])
        empty = build(replace(state, devbox_uuid=None, devbox_unadded_known=None), "dark")
        self.assertEqual(empty["head"], "No account is in use on the other computer.")
        self.assertIsNone(build(situation("good"), "dark")["unadded_known"])

    def test_this_mac_names_a_login_lifeline_has_not_added(self):
        known = {"name": "Team Account", "short": "Team", "email": "sam@example.invalid"}
        state = replace(situation("good"), mac_uuid="org-login-not-added", mac_unadded_known=known)
        out = build(state, "dark")
        self.assertEqual(
            (out["head"], out["sub"]),
            ("This Mac uses Team Account, which Lifeline has not added yet.", "It takes one Authorize in your browser."),
        )
        self.assertEqual(out["unadded_known"], known)
        plain = build(replace(state, mac_unadded_known=None), "dark")
        self.assertEqual(plain["head"], "This Mac uses an account Lifeline has not added yet.")
        self.assertIsNone(plain["unadded_known"])



class EachSideTests(unittest.TestCase):
    def side(self, devbox_signins=(), devbox=None, alex=59.0, work=None):
        now = at(14, 20)
        accounts = [
            replace(account("alex", "Alex's Account", "Alex", "Max 5x", 1, alex, 30.0, at(17, 0), None, now=now),
                    on_devbox="alex" in devbox_signins),
            replace(account("solo", "Personal Account", "Personal", "Max 20x", 3, 10.0, 93.0, at(19, 0), None, now=now),
                    on_devbox="solo" in devbox_signins),
        ]
        if work is not None:
            accounts.append(replace(account("work", "Work Account", "Work", "Max 5x", 2, work, 20.0, at(18, 0), None, now=now),
                                    on_devbox="work" in devbox_signins))
        return MenuState(tuple(accounts), "alex", devbox, "mac", None, None, None, 1, 0, now, UTC)

    def best(self, state, view):
        return [r["uuid"] for r in build(replace(state, view=view), "dark")["rows"] if r["is_best"]]

    def test_tonight_neither_side_badges_an_account(self):
        state = self.side()
        self.assertEqual((self.best(state, "mac"), self.best(state, "dev")), ([], []))

    def test_a_devbox_with_sign_ins_and_nothing_in_use_badges_the_most_room(self):
        state = self.side(devbox_signins=("alex", "solo"))
        self.assertEqual(self.best(state, "dev"), ["alex"])
        self.assertEqual(self.best(state, "mac"), [])

    def test_a_devbox_badge_needs_more_room_than_its_account_in_use(self):
        state = self.side(devbox_signins=("alex", "solo", "work"), devbox="work", work=20.0)
        self.assertEqual(self.best(state, "dev"), [])
        state = self.side(devbox_signins=("alex", "solo", "work"), devbox="work", work=70.0)
        self.assertEqual(self.best(state, "dev"), ["alex"])

    def test_the_devbox_says_when_no_other_account_has_a_sign_in_there(self):
        state = replace(self.side(devbox_signins=("work",), devbox="work", work=100.0), view="dev")
        out = build(state, "dark")
        self.assertEqual(out["sub"], "No other account has a working sign-in on the other computer.")

    def test_an_account_in_use_on_the_devbox_can_be_the_macs_best(self):
        state = self.side(devbox="work", work=5.0)
        self.assertEqual(self.best(state, "mac"), ["work"])



class OpenRowTests(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 11, 30, tzinfo=UTC)

    def signin(self, devbox, mac_needs=False, mac_ends=datetime(2026, 12, 18, 9, 0, tzinfo=UTC)):
        from lifeline import viewmodel

        return viewmodel.signin_text(self.NOW, UTC, mac_needs, mac_ends, devbox)

    def test_sign_in_sentences_name_only_the_sign_ins_that_exist(self):
        dec19 = datetime(2026, 12, 19, 9, 0, tzinfo=UTC)
        cases = (
            ({"held": False, "needs": False, "ends": None}, False, True, "It is signed in here until 18 December, and not yet on the other computer."),
            ({"held": True, "needs": False, "ends": dec19}, False, True, "It is signed in here until 18 December and on the other computer until 19 December."),
            ({"held": True, "needs": True, "ends": None}, False, True, "It is signed in here until 18 December, but the other computer needs a new sign-in."),
            (None, False, True, "It is signed in here until 18 December, but the other computer is not answering."),
            ({"held": False, "needs": False, "ends": None}, True, True, "It needs a new sign-in here, and the other computer has none yet."),
            ({"held": True, "needs": True, "ends": None}, True, True, "It needs a new sign-in here and on the other computer."),
            ({"held": True, "needs": False, "ends": dec19}, True, True, "It needs a new sign-in here, but it is signed in on the other computer until 19 December."),
            (None, True, True, "It needs a new sign-in here, and the other computer is not answering."),
            ({"held": True, "needs": False, "ends": None}, False, False, "It is signed in here and on the other computer."),
            ({"held": False, "needs": False, "ends": None}, False, False, "It is signed in here, and not yet on the other computer."),
        )
        for devbox, mac_needs, mac_dated, sentence in cases:
            with self.subTest(sentence):
                ends = datetime(2026, 12, 18, 9, 0, tzinfo=UTC) if mac_dated else None
                said = self.signin(devbox, mac_needs=mac_needs, mac_ends=ends)
                self.assertEqual(said, sentence)
                self.assertEqual(said.count("."), 1)

    def view(self, **changes):
        base = account("solo", "Personal Account", "Personal", "Max 20x", 3, 12.0, 76.0, at(19, 0), None)
        return replace(base, **changes)

    def test_the_usage_line_merges_both_windows(self):
        from lifeline import viewmodel

        now = at(14, 20)
        solo = replace(self.view(), forecast=None)
        self.assertEqual(viewmodel.usage_line(solo, now, UTC), "It has used 12% of its 5 hours and 76% of its week.")
        fresh = replace(self.view(five=0.0, week=0.0), forecast=None)
        self.assertEqual(viewmodel.usage_line(fresh, now, UTC), "It has all of its 5 hours and all of its week.")
        out = self.view(five=100.0, week=40.0, forecast=None)
        self.assertEqual(viewmodel.usage_line(out, now, UTC), "It is out until today at 19:00. It has used 40% of its week.")
        running = self.view(week=76.0, forecast=None, other_forecast=None)
        running = replace(running, forecast=Forecast(110.0, at(16, 16), 0.2, "recent"))
        line = viewmodel.usage_line(replace(running, binding="week"), now, UTC)
        self.assertTrue(line.endswith("At this pace its week runs out today at 16:16."), line)
        self.assertEqual(viewmodel.usage_line(self.view(needs_signin=True), now, UTC), "It needs a new sign-in. Sign in again to read its usage.")

    def test_a_model_limit_at_zero_is_left_out_and_the_rest_share_one_line(self):
        from lifeline import viewmodel

        now = at(14, 20)
        limits = [
            {"model": "Opus", "week": 12.4, "week_reset": WEEK_RESET},
            {"model": "Sonnet", "week": 0.0, "week_reset": WEEK_RESET},
            {"model": "Haiku", "week": 4.0, "week_reset": WEEK_RESET},
        ]
        self.assertEqual(
            viewmodel.models_line(limits, now, UTC),
            "Opus has used 12% and Haiku has used 4% of their own weekly limits. They reset on Sunday at 09:00.",
        )
        self.assertEqual(viewmodel.models_line(limits[1:2], now, UTC), "")
        self.assertEqual(
            viewmodel.models_line(limits[:1], now, UTC), "Opus has used 12% of its own weekly limit. It resets on Sunday at 09:00."
        )

    def test_the_settings_line_is_two_sentences(self):
        from lifeline import viewmodel

        ends = datetime(2026, 12, 16, 9, 0, tzinfo=UTC)

        def line(name, full, short, organization, email, shared, plan, needs=False, end=ends):
            return viewmodel.account_line(name, full, short, organization, email, shared, plan, needs, end, self.NOW, UTC)

        self.assertEqual(
            line("Alex's Account", "Alex's Account", "Alex", "j@example.invalid's Organization", "j@example.invalid", False, "Max 5x"),
            "The menu bar calls it Alex. It is on the Max 5x plan, signed in here until 16 December.",
        )
        self.assertEqual(
            line("Team Account", "Example Org Team Account", "Team", "Example Org", "sam@example.invalid", True, "Max 20x"),
            "Its full name is Example Org Team Account, and the menu bar calls it Team. It is on the Max 20x plan, signed in here until 16 December.",
        )
        self.assertEqual(
            line("Team Account", "", "Team", "Acme", "t@example.invalid", True, "Max 20x"),
            "The menu bar calls it Team. It is in the Acme organization, on the Max 20x plan, signed in here until 16 December.",
        )
        self.assertEqual(
            line("Personal Account", "Personal Account", "Personal", "sam@example.invalid's Organization", "sam@example.invalid", True, "Max 20x"),
            "The menu bar calls it Personal. It is a personal account, on the Max 20x plan, signed in here until 16 December.",
        )
        self.assertEqual(
            line("Work Account", "Work Account", "Work", "Example Team", "c@example.invalid", False, "Max 5x", needs=True),
            "The menu bar calls it Work. It is on the Max 5x plan and needs a new sign-in here.",
        )
        self.assertEqual(line("Work Account", "", "Work", None, None, False, "", end=None), "The menu bar calls it Work. It is signed in here.")
        for said in (line("Alex's Account", "", "Alex", None, None, False, "Max 5x"), line("Work Account", "", "Work", None, None, False, "", needs=True)):
            self.assertLessEqual(said.count("."), 2, said)

    def test_projects_share_one_line(self):
        from lifeline import viewmodel

        self.assertEqual(viewmodel.projects_line([{"cwd": "/home/d/projects/lifeline"}, {"cwd": "/home/d/kb", "name": "x"}]), "Open on this Mac: lifeline and kb.")
        self.assertEqual(viewmodel.projects_line([]), "")

    def test_a_climbing_account_names_its_run_out_in_its_closed_row(self):
        state = situation("good")
        crew = replace(state.accounts[3], five=5.0, week=60.0, climbing=True,
                      forecast=Forecast(110.0, at(16, 16), 0.2, "recent"), binding="week")
        rows = {r["uuid"]: r for r in build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")["rows"]}
        self.assertEqual(rows["crew"]["where"], "In use elsewhere. It runs out today at 16:16.")

    def test_climbing_comes_only_from_readings_after_it_left_use(self):
        state = situation("good")
        crew = replace(state.accounts[3], five=5.0, forecast=Forecast(20.0, None, 0.1, "recent"), climbing=False)
        rows = {r["uuid"]: r for r in build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")["rows"]}
        self.assertTrue(rows["crew"]["where"].startswith("Not in use."), rows["crew"]["where"])


SHOWN = ("display", "figure", "pct", "pct_window", "pct_label", "five_w", "thin_window", "thin_w", "thin_label", "week_pct", "week_w")


def shown(row):
    return {key: row[key] for key in SHOWN}


def rows_of(out):
    return {r["uuid"]: r for r in out["rows"]}


class DisplayWindowTests(unittest.TestCase):
    """the figure and the lines follow the display window, while room keeps the fuller window."""

    def test_a_row_shows_its_5_hours_with_its_week_as_the_thin_line(self):
        rows = rows_of(build(situation("good"), "dark"))
        self.assertEqual(shown(rows["work"]), {
            "display": "five", "figure": "12%", "pct": "12%", "pct_window": "5 hours", "pct_label": "5 hours 12%",
            "five_w": "12%", "thin_window": "week", "thin_w": "55%", "thin_label": "Week 55%", "week_pct": "Week 55%", "week_w": "55%",
        })
        self.assertEqual(rows["work"]["binding"], "week")
        self.assertEqual(rows["work"]["where"], "Used by the other computer. Resets today at 18:30.")
        self.assertEqual((rows["alex"]["display"], rows["alex"]["figure"], rows["alex"]["thin_label"]), ("five", "42%", "Week 18%"))
        self.assertEqual((rows["work"]["second_window"], rows["work"]["second_w"]), ("week", "55%"))

    def test_the_week_is_shown_once_it_reaches_80_percent(self):
        out = build(both(five=10.0, week=85.0), "dark")
        self.assertEqual(shown(out["rows"][0]), {
            "display": "week", "figure": "Week 85%", "pct": "85%", "pct_window": "week", "pct_label": "Week 85%",
            "five_w": "85%", "thin_window": "five", "thin_w": "10%", "thin_label": "5 hours 10%", "week_pct": "", "week_w": "0%",
        })
        self.assertEqual((out["display"], out["hero_used"], out["hero_w"]), ("week", "85% of its week used", "85%"))
        self.assertEqual(out["head"], "Work Account lasts until its week resets next Thursday at 02:00.")
        self.assertEqual((out["thin_window"], out["thin_w"], out["thin_label"]), ("five", "10%", "5 hours 10%"))
        self.assertEqual(out["second_text"], "Its 5 hours are 10% used and reset today at 19:35.")
        self.assertEqual((out["week_text"], out["week_w"]), ("", "0%"))
        below = build(both(five=10.0, week=79.0), "dark")
        self.assertEqual((below["display"], below["binding"], below["hero_used"]), ("five", "week", "10% of its 5 hours used"))
        self.assertEqual(below["head"], "Work Account lasts until its 5 hours reset today at 19:35.")
        self.assertEqual(below["hero_reset"], "Resets today at 19:35")
        self.assertEqual(below["second_text"], "Its week is 79% used. It resets next Thursday at 02:00.")
        self.assertEqual((below["week_text"], below["week_w"]), (below["second_text"], "79%"))

    def test_an_account_with_one_window_or_no_reading_has_no_thin_line(self):
        rows = rows_of(build(mixed(weekly()), "dark"))
        self.assertEqual(
            (rows["solo"]["display"], rows["solo"]["figure"], rows["solo"]["thin_window"], rows["solo"]["thin_w"], rows["solo"]["thin_label"]),
            ("week", "Week 57%", "", "0%", ""),
        )
        alone = build(both(five=30.0, week=None, week_reset=None), "dark")
        self.assertEqual((alone["rows"][0]["display"], alone["rows"][0]["figure"], alone["rows"][0]["thin_label"]), ("five", "30%", ""))
        self.assertEqual((alone["display"], alone["thin_window"], alone["second_text"]), ("five", "", ""))
        bare = rows_of(build(mixed(weekly(week=None, reset=None, reading_at=None)), "dark"))["solo"]
        self.assertEqual((bare["display"], bare["figure"], bare["pct_label"], bare["thin_label"]), ("", "", "", ""))

    def test_the_window_that_stops_it_first_is_shown(self):
        week_out = Forecast(150.0, LIVE_NOW + timedelta(days=2), 0.01, "recent")
        out = build(both(five=10.0, week=60.0, fore=week_out, other=Forecast(20.0, None, 0.01, "recent")), "dark")
        self.assertEqual(out["head"], "Work Account runs out on Saturday at 16:35.")
        self.assertEqual((out["display"], out["hero_used"], out["thin_label"]), ("week", "60% of its week used", "5 hours 10%"))
        self.assertEqual(out["rows"][0]["figure"], "Week 60%")
        five_out = Forecast(250.0, LIVE_NOW + timedelta(minutes=35), 2.0, "recent")
        out = build(both(five=30.0, week=50.0, fore=Forecast(60.0, None, 0.001, "recent"), other=five_out), "dark")
        self.assertEqual(out["head"], "Work Account runs out today at 17:10.")
        self.assertEqual((out["display"], out["hero_used"], out["hero_w"], out["thin_label"]), ("five", "30% of its 5 hours used", "30%", "Week 50%"))
        self.assertEqual(out["second_text"], "Its week is 50% used. It resets next Thursday at 02:00.")
        out = build(both(five=30.0, week=50.0, fore=week_out, other=five_out), "dark")
        self.assertEqual((out["head"], out["display"]), ("Work Account runs out today at 17:10.", "five"))
        self.assertEqual(out["second_text"], "At this pace its week runs out on Saturday at 16:35.")

    def test_a_window_at_its_limit_is_shown(self):
        out = build(both(five=100.0, week=85.0), "dark")
        self.assertEqual((out["display"], out["head"], out["hero_reset"], out["thin_label"]),
                         ("five", "Work Account is out until today at 19:35.", "Back today at 19:35", "Week 85%"))
        out = build(both(five=30.0, week=100.0), "dark")
        self.assertEqual((out["display"], out["head"], out["thin_label"]), ("week", "Work Account is out until next Thursday at 02:00.", "5 hours 30%"))
        out = build(both(five=99.5, week=85.0), "dark")
        self.assertEqual((out["display"], out["hero_used"]), ("five", "99% of its 5 hours used"))
        out = build(both(five=40.0, week=99.5), "dark")
        self.assertEqual((out["display"], out["hero_used"]), ("week", "99% of its week used"))

    def test_the_hero_and_the_menu_bar_show_the_same_window_after_a_switch(self):
        out = build(situation("switched"), "dark")
        self.assertEqual((out["display"], out["hero_used"], out["hero_w"]), ("five", "1% of its 5 hours used", "1%"))
        self.assertEqual((out["icon_display"], out["icon_pct"], out["icon_w"], out["icon_binding"]), ("five", "1%", "4%", "week"))
        self.assertEqual((out["thin_label"], out["second_text"]), ("Week 9%", "Its week is 9% used and resets on Sunday at 09:00."))
        back = build(situation("back"), "dark")
        alex = rows_of(back)["alex"]
        self.assertEqual((alex["display"], alex["figure"], alex["thin_label"]), ("five", "0%", "Week 24%"))

    def test_the_forecast_on_the_line_comes_from_the_shown_window(self):
        state = situation("switched")
        crew = replace(state.accounts[3], other_forecast=Forecast(58.0, None, 0.2, "recent"))
        out = build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")
        self.assertEqual((out["hero_w"], out["fore_w"]), ("1%", "57%"))
        self.assertEqual((rows_of(out)["crew"]["five_w"], rows_of(out)["crew"]["fore_w"]), ("1%", "57%"))
        week_shown = replace(crew, week=85.0, forecast=Forecast(95.0, None, 0.01, "recent"))
        out = build(replace(state, accounts=state.accounts[:3] + (week_shown,)), "dark")
        self.assertEqual((out["display"], out["hero_w"], out["fore_w"]), ("week", "85%", "10%"))

    def test_the_devbox_head_talks_about_the_window_it_shows(self):
        out = build(replace(situation("good"), view="dev"), "dark")
        self.assertEqual(out["head"], "Work Account lasts until its 5 hours reset today at 18:30.")
        self.assertEqual((out["display"], out["hero_used"], out["hero_reset"], out["hero_w"]),
                         ("five", "12% of its 5 hours used", "Resets today at 18:30", "12%"))
        self.assertEqual(out["second_text"], "Its week is 55% used and resets on Sunday at 09:00.")

    def test_the_open_row_names_the_shown_window_first(self):
        from lifeline import viewmodel

        work = situation("good").accounts[1]
        self.assertEqual(viewmodel.usage_line(replace(work, forecast=None), at(14, 20), UTC), "It has used 12% of its 5 hours and 55% of its week.")
        heavy = replace(work, five=10.0, week=85.0, forecast=None)
        self.assertEqual(viewmodel.usage_line(heavy, at(14, 20), UTC), "It has used 85% of its week and 10% of its 5 hours.")


class RowWhereTests(unittest.TestCase):
    """Gaps 4 and 37: one short where line, with no sentence said twice."""

    def climbing(self, **changes):
        state = situation("good")
        fields = {"five": 5.0, "five_reset": at(18, 0), "forecast": Forecast(20.0, None, 0.1, "recent"), **changes}
        crew = replace(state.accounts[3], **fields)
        return replace(state, accounts=state.accounts[:3] + (crew,))

    def test_a_climbing_row_says_it_is_in_use_elsewhere(self):
        rows = rows_of(build(self.climbing(), "dark"))
        self.assertEqual(rows["crew"]["where"], "In use elsewhere. Resets today at 18:00.")
        rows = rows_of(build(replace(self.climbing(), devbox_answers=False), "dark"))
        self.assertEqual(rows["crew"]["where"], "In use elsewhere. Resets today at 18:00.")

    def test_a_climbing_row_that_runs_out_says_it_once(self):
        state = self.climbing(week=60.0, climbing=True, forecast=Forecast(110.0, at(16, 16), 0.2, "recent"), binding="week")
        rows = rows_of(build(state, "dark"))
        self.assertEqual(rows["crew"]["where"], "In use elsewhere. It runs out today at 16:16.")
        self.assertEqual((rows["crew"]["display"], rows["crew"]["thin_label"]), ("week", "5 hours 5%"))
        state = self.climbing(week=94.0, five=70.0, climbing=True, forecast=Forecast(101.0, None, 0.01, "recent"),
                              other_forecast=Forecast(118.0, at(16, 20), 0.5, "recent"))
        rows = rows_of(build(state, "dark"))
        self.assertEqual(rows["crew"]["where"], "In use elsewhere. It runs out today at 16:20.")
        self.assertEqual((rows["crew"]["display"], rows["crew"]["figure"], rows["crew"]["thin_label"]), ("five", "70%", "Week 94%"))
        self.assertNotIn("runs out", rows["crew"]["second_text"])
        rows = rows_of(build(replace(state, devbox_answers=False), "dark"))
        self.assertEqual(rows["crew"]["where"], "In use elsewhere. It runs out today at 16:20.")


class ViewEdgeTests(unittest.TestCase):
    """Edge cases in the menu's words."""

    def test_most_room_ranks_by_the_fuller_window_not_by_what_is_shown(self):
        state = situation("limit")
        work = replace(state.accounts[1], five=12.0, week=55.0)
        solo = replace(state.accounts[2], five=30.0, week=20.0)
        crew = replace(state.accounts[3], five=60.0, week=70.0, five_reset=at(19, 0))
        out = build(replace(state, accounts=(state.accounts[0], work, solo, crew)), "dark")
        rows = rows_of(out)
        self.assertEqual((rows["work"]["figure"], rows["solo"]["figure"]), ("12%", "30%"))
        self.assertEqual([r["uuid"] for r in out["rows"] if r["is_best"]], ["solo"])
        self.assertEqual((out["action_target"], out["sub"]), ("solo", "Personal Account has the most room."))

    def test_a_stale_row_keeps_its_where_line_to_two_sentences(self):
        state = situation("good")
        old = tuple(replace(a, reading_at=state.now - timedelta(minutes=45)) for a in state.accounts)
        out = build(replace(state, accounts=old), "dark")
        for row in out["rows"]:
            with self.subTest(row=row["uuid"]):
                self.assertLessEqual(row["where"].count("."), 2, row["where"])
                self.assertNotIn("Last read", row["where"])
                self.assertEqual((row["stale"], row["read_note"]), (True, "Last read at 13:35."))
        self.assertTrue(out["sub"].endswith("Last read at 13:35."), out["sub"])
        fresh = rows_of(build(state, "dark"))["alex"]
        self.assertEqual((fresh["stale"], fresh["read_note"]), (False, ""))

    def test_a_reading_with_no_figures_shows_no_figure(self):
        state = situation("good")
        blank = replace(state.accounts[1], five=None, week=None, forecast=None, five_reset=None, week_reset=None)
        out = build(replace(state, accounts=(state.accounts[0], blank) + state.accounts[2:], view="dev"), "dark")
        row = rows_of(out)["work"]
        self.assertEqual((row["display"], row["figure"], row["pct"], row["pct_label"], row["unused"], row["thin_label"]), ("", "", "", "", False, ""))
        self.assertEqual((out["display"], out["hero_used"], out["head"]), ("", "", "Lifeline has no reading for Work Account yet."))

    def test_a_thin_run_out_fits_one_line(self):
        five_out = Forecast(250.0, LIVE_NOW + timedelta(minutes=35), 2.0, "recent")
        week_out = Forecast(150.0, LIVE_NOW + timedelta(days=2), 0.01, "recent")
        out = build(both(five=30.0, week=60.0, fore=week_out, other=five_out), "dark")
        self.assertEqual(out["second_text"], "At this pace its week runs out on Saturday at 16:35.")
        late = Forecast(150.0, datetime(2026, 11, 26, 1, 0, tzinfo=UTC), 0.01, "recent")
        out = build(both(five=30.0, week=60.0, fore=late, other=five_out), "dark")
        self.assertEqual(out["second_text"], "At this pace its week runs out next Thursday at 01:00.")
        out = build(both(five=10.0, week=18.0), "dark")
        self.assertEqual(out["second_text"], "Its week is 18% used. It resets next Thursday at 02:00.")
        near = build(both(five=10.0, week=18.0, week_reset=LIVE_NOW + timedelta(days=3)), "dark")
        self.assertEqual(near["second_text"], "Its week is 18% used and resets on Sunday at 16:35.")

    def test_an_unused_window_with_a_reset_has_all_of_it(self):
        out = build(both(five=0.0, week=85.0), "dark")
        self.assertEqual((out["thin_label"], out["second_text"]), ("5 hours 0%", "It has all of its 5 hours."))
        out = build(both(five=10.0, week=0.0), "dark")
        self.assertEqual(out["second_text"], "It has all of its week.")

    def test_each_step_of_the_display_rule_at_its_edge(self):
        week_out = Forecast(150.0, LIVE_NOW + timedelta(days=2), 0.01, "recent")
        passed = Forecast(390.0, LIVE_NOW - timedelta(minutes=30), 2.0, "recent")
        cases = (
            ("week at exactly 80%", both(five=10.0, week=80.0), "week"),
            ("week just under 80%", both(five=10.0, week=79.9), "five"),
            ("5 hours at exactly 99% beat a week at 85%", both(five=99.0, week=85.0), "five"),
            ("5 hours just under 99% give way to the week at 85%", both(five=98.9, week=85.0), "week"),
            ("an earlier run-out beats a window at 99%", both(five=99.5, week=60.0, fore=Forecast(99.5, None, 0.0, "none"), other=week_out), "week"),
            ("a passed run-out beats the week at 80%", both(five=50.0, week=85.0, fore=Forecast(90.0, None, 0.0, "none"), other=passed), "five"),
            ("both windows at 100% give the week", both(five=100.0, week=100.0), "week"),
            ("week-only at 80%", both(five=None, five_reset=None, week=80.0), "week"),
            ("week-only at 99%", both(five=None, five_reset=None, week=99.5), "week"),
        )
        for name, state, window in cases:
            with self.subTest(name):
                out = build(state, "dark")
                self.assertEqual((out["display"], out["rows"][0]["display"]), (window, window))
        over = build(both(five=137.0, week=104.0), "dark")
        self.assertEqual((over["display"], over["thin_w"], over["thin_label"]), ("week", "100%", "5 hours 137%"))
        state = replace(situation("good"), view="dev")
        heavy = replace(state.accounts[1], week=85.0, forecast=None)
        out = build(replace(state, accounts=(state.accounts[0], heavy) + state.accounts[2:]), "dark")
        self.assertEqual((out["display"], out["head"], out["hero_used"]), ("week", "Work Account lasts until its week resets on Sunday at 09:00.", "85% of its week used"))


class FirstRunEdgeTests(unittest.TestCase):
    """Edge cases on first run."""

    def test_an_unreadable_row_is_marked_and_never_offered(self):
        state = situation("limit")
        blind = replace(state.accounts[3], cannot_read=True)
        out = build(replace(state, accounts=state.accounts[:3] + (blind,)), "dark")
        rows = rows_of(out)
        self.assertEqual(
            {key: rows["crew"][key] for key in ("cannot_read", "figure", "pct", "pct_label", "five_w", "thin_label", "is_best")},
            {"cannot_read": True, "figure": "", "pct": "", "pct_label": "", "five_w": "0%", "thin_label": "", "is_best": False},
        )
        self.assertEqual(rows["crew"]["where"], "Lifeline cannot read it. Sign in again to fix it.")
        self.assertFalse(rows["solo"]["cannot_read"])
        self.assertEqual((out["action_target"], out["sub"]), ("solo", "Personal Account has the most room."))

    def test_a_row_used_by_both_machines_stays_short(self):
        state = replace(situation("good"), devbox_uuid="alex")
        self.assertEqual(rows_of(build(state, "dark"))["alex"]["where"], "Used by both machines. Resets today at 17:00.")
        blind = replace(state.accounts[0], cannot_read=True)
        out = build(replace(state, accounts=(blind,) + state.accounts[1:]), "dark")
        self.assertEqual(rows_of(out)["alex"]["where"], "Used by both machines. Lifeline cannot read it.")

    def test_this_one_is_first_only_when_every_other_reset_is_known(self):
        state = full_house(100.0)
        unknown = replace(state.accounts[1], five_reset=None)
        out = build(replace(state, accounts=(state.accounts[0], unknown) + state.accounts[2:]), "dark")
        self.assertEqual(out["sub"], "No other account has room. Personal Account is back today at 19:10.")
        nothing = tuple(replace(a, five_reset=None) for a in state.accounts[1:])
        out = build(replace(state, accounts=(state.accounts[0],) + nothing), "dark")
        self.assertEqual(out["sub"], "No other account has room.")

    def test_first_run_with_a_login_not_yet_known_offers_the_first_account(self):
        state = replace(situation("good"), accounts=(), mac_uuid=None, devbox_uuid=None, live_signed_in=True)
        out = build(state, "dark")
        self.assertEqual(out["head"], "Lifeline cannot tell yet which account Claude Code uses.")
        self.assertEqual(out["sub"], "It checks again within 5 minutes. Adding an account takes one Authorize in your browser.")
        self.assertEqual((out["action_kind"], out["action_text"]), ("add", "Add your first account"))

    def test_a_sentence_read_away_from_the_menu_never_says_click(self):
        state = situation("good")
        dead = replace(state.accounts[2], needs_signin=True)
        out = build(replace(state, accounts=state.accounts[:2] + (dead,) + state.accounts[3:], clickable=False), "dark")
        self.assertEqual(rows_of(out)["solo"]["where"], "Needs a new sign-in.")
        self.assertNotIn("Click", json.dumps(out))


class SentenceEdgeTests(unittest.TestCase):
    """Edge cases in the menu's sentences."""

    def test_a_devbox_end_date_never_reads_as_the_macs(self):
        from lifeline import viewmodel

        said = viewmodel.signin_text(at(14, 20), UTC, False, None, {"held": True, "needs": False, "ends": datetime(2026, 12, 16, 9, 0, tzinfo=UTC)})
        self.assertEqual(said, "It is signed in here, and on the other computer until 16 December.")

    def test_the_menu_bar_icon_has_a_plain_sentence_for_voiceover(self):
        self.assertEqual(build(situation("good"), "dark")["icon_label"], "Alex's Account has used 42% of its 5 hours.")
        state = situation("good")
        work = replace(state.accounts[1], needs_signin=True)
        out = build(replace(state, accounts=(state.accounts[0], work) + state.accounts[2:]), "dark")
        self.assertEqual(out["icon_label"], "Alex's Account has used 42% of its 5 hours, and the other computer needs you.")
        dev = build(replace(state, accounts=(state.accounts[0], work) + state.accounts[2:], view="dev"), "dark")
        self.assertEqual(dev["icon_label"], out["icon_label"])
        heavy = build(both(five=10.0, week=85.0), "dark")
        self.assertEqual(heavy["icon_label"], "Work Account has used 85% of its week.")
        bare = build(mixed(weekly(week=None, reset=None, reading_at=None)), "dark")
        self.assertEqual(bare["icon_label"], "Lifeline has no reading for Personal Account yet.")
        self.assertEqual(build(replace(situation("good"), mac_uuid=None), "dark")["icon_label"], "No account is in use on this Mac.")
        unadded = replace(situation("good"), mac_uuid="org-login-not-added", mac_unadded_known=MIO_KNOWN)
        self.assertEqual(build(replace(unadded, view="dev"), "dark")["icon_label"], "This Mac uses Team Account, which Lifeline has not added yet.")

    def test_the_pending_heads_come_from_the_core(self):
        out = build(situation("good"), "dark")
        rows = rows_of(out)
        self.assertEqual(rows["work"]["pending_head"], "Switching this Mac to Work Account.")
        self.assertEqual(rows["alex"]["pending_head"], "")
        self.assertEqual(out["pending_back"], "Switching this Mac back.")
        dev = build(replace(situation("good"), view="dev"), "dark")
        self.assertEqual((rows_of(dev)["crew"]["pending_head"], dev["pending_back"]), ("", ""))
        state = situation("good")
        nameless = replace(state.accounts[1], name="")
        out = build(replace(state, accounts=(state.accounts[0], nameless) + state.accounts[2:]), "dark")
        self.assertEqual(rows_of(out)["work"]["pending_head"], "Switching this Mac to the account.")


MIO_KNOWN = {"name": "Team Account", "short": "Team", "email": "sam@example.invalid"}
BROWSER_ONCE = "It takes one Authorize in your browser."


class FirstRunTests(unittest.TestCase):
    """an account Lifeline has not added, or no account at all, gets a button that adds it."""

    def test_a_known_login_lifeline_has_not_added_gets_an_add_button(self):
        state = replace(situation("good"), mac_uuid="org-login-not-added", mac_unadded_known=MIO_KNOWN)
        out = build(state, "dark")
        self.assertEqual(out["head"], "This Mac uses Team Account, which Lifeline has not added yet.")
        self.assertEqual((out["action_kind"], out["action_text"], out["action_target"], out["sub"]), ("add", "Add Team Account", "", BROWSER_ONCE))
        ready = build(replace(state, mac_adopt_ready=True), "dark")
        self.assertEqual((ready["action_kind"], ready["action_text"]), ("adopt", "Add Team Account"))
        self.assertEqual(ready["sub"], "Lifeline adds the sign-in this Mac already has, so no browser opens.")

    def test_an_unknown_login_gets_an_add_button(self):
        state = replace(situation("good"), mac_uuid="stranger", live_signed_in=True)
        out = build(state, "dark")
        self.assertEqual((out["action_kind"], out["action_text"], out["sub"]), ("add", "Add this account", BROWSER_ONCE))
        out = build(replace(state, mac_adopt_ready=True), "dark")
        self.assertEqual((out["action_kind"], out["action_text"]), ("adopt", "Add this account"))

    def test_no_account_at_all_offers_the_first_one(self):
        empty = replace(situation("good"), accounts=(), mac_uuid=None, devbox_uuid=None)
        out = build(empty, "dark")
        self.assertEqual(out["head"], "No account is in use on this Mac.")
        self.assertEqual((out["action_kind"], out["action_text"], out["sub"]), ("add", "Add your first account", BROWSER_ONCE))
        self.assertEqual(out["rows"], [])

    def test_accounts_to_switch_to_need_no_add_button(self):
        out = build(replace(situation("good"), mac_uuid=None), "dark")
        self.assertEqual((out["action_kind"], out["action_text"]), ("none", ""))

    def test_the_devbox_view_offers_no_add_button(self):
        state = replace(situation("good"), view="dev", devbox_uuid="work-login-not-added", devbox_unadded_known=WORK_KNOWN)
        out = build(state, "dark")
        self.assertEqual((out["action_kind"], out["action_text"]), ("none", ""))


class TroubleRowTests(unittest.TestCase):
    """Gaps 12 and 13: a row that cannot be read or needs a sign-in says so and says how to fix it."""

    def test_a_row_lifeline_cannot_read_says_so(self):
        state = situation("good")
        blind = replace(state.accounts[2], cannot_read=True, five=None, week=None, forecast=None, five_reset=None, week_reset=None, reading_at=None)
        rows = rows_of(build(replace(state, accounts=state.accounts[:2] + (blind,) + state.accounts[3:]), "dark"))
        self.assertEqual(rows["solo"]["where"], "Lifeline cannot read it. Sign in again to fix it.")
        used = replace(state, accounts=state.accounts[:2] + (blind,) + state.accounts[3:], devbox_uuid="solo")
        self.assertEqual(rows_of(build(used, "dark"))["solo"]["where"], "Used by the other computer. Lifeline cannot read it.")

    def test_a_row_that_needs_a_sign_in_says_a_click_signs_it_in(self):
        state = situation("good")
        dead = replace(state.accounts[2], needs_signin=True)
        out = build(replace(state, accounts=state.accounts[:2] + (dead,) + state.accounts[3:]), "dark")
        self.assertEqual(rows_of(out)["solo"]["where"], "Needs a new sign-in. Click it to sign in.")
        dev = build(replace(state, accounts=state.accounts[:2] + (dead,) + state.accounts[3:], view="dev"), "dark")
        self.assertEqual(rows_of(dev)["solo"]["where"], "Needs a new sign-in.")


def full_house(alex_five, alex_reset=None, others=99.0):
    state = situation("limit")
    alex = replace(state.accounts[0], five=alex_five, forecast=Forecast(alex_five, None, None, "none"),
                     **({"five_reset": alex_reset} if alex_reset else {}))
    rest = tuple(replace(a, five=others, five_reset=a.five_reset or at(21, 0)) for a in state.accounts[1:])
    return replace(state, accounts=(alex,) + rest)


class NoRoomTests(unittest.TestCase):
    """with no room anywhere, name whichever account is back first, from 99%."""

    def test_the_account_in_use_can_be_the_one_back_first(self):
        out = build(full_house(100.0), "dark")
        self.assertEqual(out["head"], "Alex's Account is out until today at 17:00.")
        self.assertEqual(out["sub"], "No other account has room, and this one is back first.")
        self.assertEqual(out["action_kind"], "none")

    def test_another_account_back_first_is_named(self):
        out = build(full_house(100.0, alex_reset=at(19, 30)), "dark")
        self.assertEqual(out["sub"], "No other account has room. Work Account is back today at 18:30.")

    def test_from_99_percent_the_menu_says_no_account_has_room(self):
        for used in (99.0, 99.5, 99.9):
            with self.subTest(used=used):
                out = build(full_house(used), "dark")
                self.assertEqual(out["head"], "Alex's Account lasts until its 5 hours reset today at 17:00.")
                self.assertEqual(out["sub"], "No other account has room, and this one resets first.")
                self.assertEqual(out["action_kind"], "none")
                late = build(full_house(used, alex_reset=at(19, 30)), "dark")
                self.assertEqual(late["sub"], "No other account has room. Work Account is back today at 18:30.")
        out = build(full_house(98.9), "dark")
        self.assertNotIn("No other account", out["sub"])

    def test_from_99_percent_the_menu_offers_an_account_with_room(self):
        state = full_house(99.5)
        crew = replace(state.accounts[3], five=10.0)
        out = build(replace(state, accounts=state.accounts[:3] + (crew,)), "dark")
        self.assertEqual(out["sub"], "Team Account has the most room.")
        self.assertEqual((out["action_kind"], out["action_target"], out["hint"]), ("primary", "crew", "Or press 4."))


class HedgeFreeHeadTests(unittest.TestCase):
    """a passed run-out is stated plainly, with no 'about to' and no 'may'."""

    def passed(self, read_at):
        fore = Forecast(390.0, LIVE_NOW - timedelta(minutes=30), 2.0, "recent")
        return build(both(five=97.0, week=20.0, fore=fore, reading_at=read_at), "dark")

    def test_a_fresh_reading_says_it_runs_out_now(self):
        out = self.passed(LIVE_NOW - timedelta(minutes=19))
        self.assertEqual((out["head"], out["sub"]), ("Work Account runs out now.", "Lifeline last read 97% at 16:16."))

    def test_an_old_reading_says_when_it_was_due(self):
        out = self.passed(LIVE_NOW - timedelta(minutes=20))
        self.assertEqual((out["head"], out["sub"]), ("Work Account was due to run out today at 16:05.", "Lifeline last read it at 16:15."))
        out = self.passed(datetime(2026, 11, 18, 11, 0, tzinfo=UTC))
        self.assertEqual(out["sub"], "Lifeline last read it yesterday at 11:00.")

    def test_no_head_hedges(self):
        for read_at in (LIVE_NOW, LIVE_NOW - timedelta(minutes=40), None):
            out = self.passed(read_at)
            text = json.dumps(out)
            self.assertNotIn("about to", text)
            self.assertNotIn(" may ", text)


class PauseReasonTests(unittest.TestCase):
    """a pause says why."""

    def paused(self, reason):
        state = situation("good")
        alex = replace(state.accounts[0], paused_until=at(15, 10), pause_reason=reason)
        return build(replace(state, accounts=(alex,) + state.accounts[1:]), "dark")["sub"]

    CAUSES = {
        "RateLimited": "Anthropic asked Lifeline to wait",
        "Unavailable": "Lifeline could not reach Anthropic",
        "UnexpectedReply": "Anthropic sent a reply Lifeline could not read",
        "CertificateFailed": "Lifeline could not confirm it was talking to Anthropic",
    }
    OWN = {
        "FAILED_ERROR": "Lifeline could not take a reading",
        "STORE_ERROR": "Lifeline could not open the sign-in it keeps for this account",
        "UNWRITABLE_ERROR": "Lifeline could not save the sign-in it keeps for this account",
        "NO_RECOVERY_ERROR": "Lifeline could not write to the folder where it keeps safe copies of sign-ins",
        "GUARD_ERROR": "Lifeline could not check that it is safe to renew the sign-in",
        "LOCK_ERROR": "Lifeline could not open one of its own files",
        "HISTORY_ERROR": "Lifeline could not save the last reading",
        "UNSAVED_ERROR": "Lifeline renewed the sign-in but could not save it yet",
        "DEAD_ERROR": "Anthropic refused its sign-in",
        "NOT_ALLOWED_ERROR": "Anthropic does not let Lifeline read its usage",
    }
    TAIL = ", so readings are paused until today at 15:10."
    RAW = ("429", "lock", "token", "address", "error", "uuid", "job", "route", "registry")

    def test_every_known_cause_has_its_own_plain_sentence(self):
        from lifeline import anthropic, reader

        expected = {}
        for name, clause in self.CAUSES.items():
            kind = getattr(anthropic, name)
            expected[reader.READ_ERRORS[kind]] = clause
            expected[reader.RENEW_ERRORS[kind]] = clause
        for name, clause in self.OWN.items():
            expected[getattr(reader, name)] = clause
        self.assertEqual(set(expected), set(reader.KNOWN_ERRORS))
        for raw, clause in expected.items():
            with self.subTest(raw=raw):
                sub = self.paused(raw)
                self.assertTrue(sub.endswith(" " + clause + self.TAIL), sub)
                for word in self.RAW:
                    self.assertNotIn(word, (clause + self.TAIL).lower())
                self.assertNotIn(raw, sub)

    def test_an_unknown_cause_gets_a_plain_general_sentence(self):
        for raw in (None, "", "HTTP 429 from https://api.example.invalid/usage, token expired"):
            with self.subTest(raw=raw):
                sub = self.paused(raw)
                self.assertTrue(sub.endswith(" Lifeline could not take a reading" + self.TAIL), sub)
                self.assertNotIn("429", sub)


class SessionsBehindTests(unittest.TestCase):
    """the sentence about sessions left behind says only what Lifeline saw."""

    def test_sessions_left_behind_are_described_not_promised(self):
        for behind, sentence in ((1, "1 open Claude Code session was still on Alex's Account at its last message."),
                                 (2, "2 open Claude Code sessions were still on Alex's Account at their last message.")):
            out = build(replace(situation("switched"), sessions_moved=0, sessions_behind=behind, sessions_mac=5), "dark")
            self.assertIn(sentence, out["sub"])
            self.assertNotIn("next message", out["sub"])


class HeroSecondLineTests(unittest.TestCase):
    """the line under the thin bar is one short sentence."""

    def test_the_week_line_is_one_sentence(self):
        out = build(situation("good"), "dark")
        self.assertEqual(out["second_text"], "Its week is 18% used and resets on Sunday at 09:00.")
        self.assertEqual((out["week_text"], out["week_w"]), ("Its week is 18% used and resets on Sunday at 09:00.", "18%"))

    def test_the_5_hour_line_is_one_sentence(self):
        out = build(both(five=1.0, week=85.0), "dark")
        self.assertEqual(out["second_text"], "Its 5 hours are 1% used and reset today at 19:35.")
        out = build(both(five=0.0, five_reset=None, week=85.0), "dark")
        self.assertEqual(out["second_text"], "It has all of its 5 hours.")


if __name__ == "__main__":
    unittest.main()
