import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lifeline import alerts
from lifeline.alerts import AccountState, Alerts
from lifeline.forecast import Forecast
from lifeline.history import History

UTC = timezone.utc
TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
FIVE_RESET = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
WEEK_RESET = datetime(2026, 11, 23, 7, 30, tzinfo=UTC)

ALEX = "11111111-1111-1111-1111-111111111111"
WORK = "22222222-2222-2222-2222-222222222222"
SOLO = "33333333-3333-3333-3333-333333333333"
CREW = "44444444-4444-4444-4444-444444444444"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def state(uuid=ALEX, name="Alex's Account", **kwargs):
    base = dict(
        five=10.0,
        five_reset=FIVE_RESET,
        week=20.0,
        week_reset=WEEK_RESET,
        forecast=None,
        used_by=(),
        signin_ends=None,
        signin_machine=None,
    )
    base.update(kwargs)
    return AccountState(uuid, name, **base)


def limited(*args, **kwargs):
    kwargs.setdefault("used_by", ("this Mac",))
    return state(*args, **kwargs)


def running_out(runs_out_at=FIVE_RESET - timedelta(minutes=80), **kwargs):
    forecast = Forecast(120.0, runs_out_at, 1.0, "recent")
    return state(five=60.0, forecast=forecast, used_by=("this Mac",), **kwargs)


def others():
    return [
        state(WORK, "Work Account", five=30.0, week=10.0),
        state(SOLO, "Personal Account", five=0.0, week=50.0),
        state(CREW, "Team Account", five=0.0, week=40.0),
    ]


class AlertsTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.history = History(Path(self.folder.name) / "history.db")
        self.clock = Clock()

    def make(self, settings=None, history=None, clock=None):
        return Alerts(
            history or self.history,
            settings if settings is not None else {},
            TZ,
            clock or self.clock,
        )

    def one(self, states, **kwargs):
        found = self.make(**kwargs).evaluate(states)
        self.assertEqual(len(found), 1, found)
        return found[0]


def weekly(uuid=SOLO, name="Personal Account", **kwargs):
    base = dict(five=None, five_reset=None, binding="week")
    base.update(kwargs)
    return state(uuid, name, **base)


class WeekBindingTests(AlertsTestCase):
    def kinds(self, found, kind):
        return [alert for alert in found if alert.kind == kind]

    def test_a_week_binding_account_reaches_its_limit(self):
        found = self.make().evaluate([weekly(week=100.0, used_by=("this Mac",))] + others()[:1] + others()[2:])
        alert = self.kinds(found, "limit")[0]
        self.assertEqual(alert.key, "limit-week:%s:2026-11-16T07:30:00+00:00" % SOLO)
        self.assertEqual(alert.title, "Personal Account is out until Monday at 09:30")
        self.assertEqual(alert.body, "Work Account has the most room.")

    def test_a_week_binding_account_will_run_out(self):
        forecast = Forecast(130.0, WEEK_RESET - timedelta(hours=26), 0.01, "recent")
        found = self.make().evaluate([weekly(week=80.0, forecast=forecast, used_by=("this Mac",))] + others()[:1] + others()[2:])
        alert = self.kinds(found, "will_run_out")[0]
        self.assertEqual(alert.key, "will-run-out-week:%s:2026-11-16T07:30:00+00:00" % SOLO)
        self.assertEqual(alert.title, "Personal Account runs out on Sunday at 07:30")
        self.assertEqual(alert.body, "That is 1 day 2 hours before its week resets on Monday at 09:30. Work Account has the most room.")

    def test_a_week_binding_account_is_back(self):
        end = NOW - timedelta(minutes=20)
        self.history.add_reading(SOLO, "usage", None, None, 100.0, end, NOW - timedelta(hours=2))
        found = self.make().evaluate([weekly(week=3.0, week_reset=end + timedelta(days=7))])
        alert = self.kinds(found, "back")[0]
        self.assertEqual(alert.key, "back-week:%s:%s" % (SOLO, "2026-11-19T09:40:00+00:00"))
        self.assertEqual(alert.title, "Personal Account is back")
        self.assertEqual(alert.body, "It has 97% of its week left.")

    def test_a_week_binding_account_back_with_its_whole_week(self):
        end = NOW - timedelta(minutes=20)
        self.history.add_reading(SOLO, "usage", None, None, 100.0, end, NOW - timedelta(hours=2))
        found = self.make().evaluate([weekly(week=0.0, week_reset=end + timedelta(days=7))])
        alert = self.kinds(found, "back")[0]
        self.assertEqual(alert.body, "Its week has reset, so you can use it again.")

    def test_week_keys_stay_stable_across_a_split_second(self):
        keys = set()
        for reset in (WEEK_RESET - timedelta(milliseconds=322), WEEK_RESET + timedelta(milliseconds=300)):
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            engine = self.make(history=History(Path(folder.name) / "h.db"))
            found = engine.evaluate([weekly(week=100.0, week_reset=reset, used_by=("this Mac",))])
            keys.add(self.kinds(found, "limit")[0].key)
        self.assertEqual(len(keys), 1)

    def test_the_room_sentence_ranks_each_account_on_its_own_limit(self):
        subject = limited(ALEX, "Alex's Account", five=100.0)
        cases = (
            ([weekly(week=0.0), state(WORK, "Work Account", five=30.0)], "Personal Account has the most room."),
            ([weekly(week=20.0), state(WORK, "Work Account", five=30.0, week=10.0)], "Personal Account has the most room."),
            ([weekly(week=40.0), state(WORK, "Work Account", five=30.0, week=10.0)], "Work Account has the most room."),
            ([weekly(week=40.0), state(WORK, "Work Account", five=30.0, week=90.0)], "Personal Account has the most room."),
        )
        for rest, body in cases:
            with self.subTest(body=body):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                engine = self.make(history=History(Path(folder.name) / "h.db"))
                alert = self.kinds(engine.evaluate([subject] + rest), "limit")[0]
                self.assertEqual(alert.body, body)

    def test_an_account_reporting_a_full_5_hours_is_out_whatever_its_week(self):
        found = self.make().evaluate([weekly(week=10.0, five=100.0, five_reset=FIVE_RESET, used_by=("this Mac",))])
        self.assertEqual([(a.kind, a.key) for a in found], [("limit", "limit:%s:2026-11-19T07:00:00+00:00" % SOLO)])


class TestEvents(AlertsTestCase):
    def test_each_event_fires_once(self):
        engine = self.make()
        signin = state(CREW, "Team Account", signin_ends=NOW + timedelta(days=3), signin_machine="the other computer")
        states = [
            running_out(),
            limited(WORK, "Work Account", five=100.0),
            state(SOLO, "Personal Account", five=96.0, week=95.0),
            signin,
        ]
        first = engine.evaluate(states)
        self.assertEqual(sorted(a.kind for a in first), ["limit", "signin_ends", "week", "will_run_out"])
        self.assertEqual(engine.evaluate(states), [])
        self.assertEqual(self.make().evaluate(states), [])

    def test_will_run_out_text_and_key(self):
        alert = self.one([running_out()] + others())
        self.assertEqual(alert.kind, "will_run_out")
        self.assertEqual(alert.key, "will-run-out:%s:2026-11-19T07:00:00+00:00" % ALEX)
        self.assertEqual(alert.uuid, ALEX)
        self.assertEqual(alert.title, "Alex's Account runs out today at 12:40")
        self.assertEqual(
            alert.body,
            "That is 1 hour 20 minutes before its 5 hours reset today at 14:00. Work Account has the most room.",
        )

    def test_will_run_out_and_limit_need_an_account_in_use(self):
        idle = running_out()
        idle = AccountState(**{**idle.__dict__, "used_by": ()})
        self.assertEqual(self.make().evaluate([idle, state(WORK, "Work Account", five=100.0)]), [])
        for index, machine in enumerate(("this Mac", "the other computer")):
            used = AccountState(**{**idle.__dict__, "used_by": (machine,)})
            history = History(Path(self.folder.name) / ("use%d.db" % index))
            self.assertEqual(len(self.make(history=history).evaluate([used])), 1)
            hit = state(WORK, "Work Account", five=100.0, used_by=(machine,))
            history = History(Path(self.folder.name) / ("hit%d.db" % index))
            self.assertEqual(len(self.make(history=history).evaluate([hit])), 1)

    def test_will_run_out_needs_a_future_run_out_time(self):
        past = running_out(runs_out_at=NOW - timedelta(minutes=5))
        self.assertEqual(self.make().evaluate([past]), [])
        none = state(five=60.0, forecast=Forecast(80.0, None, 0.1, "recent"), used_by=("this Mac",))
        self.assertEqual(self.make().evaluate([none]), [])

    def test_limit_text_with_a_fresh_best_account(self):
        alert = self.one([limited(five=100.0)] + others()[:2] + [state(CREW, "Team Account", five=0.0, week=0.0)])
        self.assertEqual(alert.kind, "limit")
        self.assertEqual(alert.key, "limit:%s:2026-11-19T07:00:00+00:00" % ALEX)
        self.assertEqual(alert.title, "Alex's Account is out until today at 14:00")
        self.assertEqual(alert.body, "Team Account has all of its 5 hours.")

    def test_limit_text_when_best_is_not_fresh(self):
        rest = [
            state(WORK, "Work Account", five=30.0, week=10.0),
            state(SOLO, "Personal Account", five=12.0, week=50.0),
        ]
        alert = self.one([limited(five=104.0)] + rest)
        self.assertEqual(alert.body, "Work Account has the most room.")

    def test_best_breaks_ties_on_week_and_takes_an_account_the_devbox_uses(self):
        rest = [
            state(WORK, "Work Account", five=0.0, week=10.0, used_by=("the other computer",)),
            state(SOLO, "Personal Account", five=5.0, week=50.0),
            state(CREW, "Team Account", five=5.0, week=40.0),
        ]
        alert = self.one([limited(five=100.0)] + rest)
        self.assertEqual(alert.body, "Work Account has the most room.")
        alert = self.one([limited(five=100.0)] + rest[1:], history=History(Path(self.folder.name) / "tie.db"))
        self.assertEqual(alert.body, "Team Account has the most room.")

    def test_no_best_account_gives_the_menus_reason(self):
        alert = self.one([limited(five=100.0)])
        self.assertEqual(alert.body, "There is no other account to switch to.")
        busy = [state(WORK, "Work Account", five=1.0, used_by=("this Mac",))]
        again = History(Path(self.folder.name) / "busy.db")
        self.assertEqual(self.one([limited(five=100.0)] + busy, history=again).body, "No other account can take over right now.")
        alert = self.one([running_out()], history=History(Path(self.folder.name) / "run.db"))
        self.assertEqual(alert.body, "That is 1 hour 20 minutes before its 5 hours reset today at 14:00.")

    def read(self, five, at, reset=FIVE_RESET, uuid=ALEX):
        self.history.add_reading(uuid, "usage", five, reset, 20.0, WEEK_RESET, at)

    def test_back_after_a_limit_window_ends(self):
        self.read(100.0, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET - timedelta(minutes=1)
        self.assertEqual(self.make().evaluate([state(five=0.0)]), [])
        self.clock.now = FIVE_RESET + timedelta(minutes=2)
        engine = self.make()
        fresh = state(five=0.0, five_reset=FIVE_RESET + timedelta(hours=5))
        back = engine.evaluate([fresh])
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].kind, "back")
        self.assertEqual(back[0].key, "back:%s:2026-11-19T12:00:00+00:00" % ALEX)
        self.assertEqual(back[0].title, "Alex's Account is back")
        self.assertEqual(back[0].body, "It has all of its 5 hours again.")
        self.assertEqual(engine.evaluate([fresh]), [])
        self.assertEqual(self.make().evaluate([fresh]), [])

    def test_back_does_not_need_the_limit_alert(self):
        self.read(100.0, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET + timedelta(minutes=2)
        off = {"limit": False}
        self.assertEqual(self.make(settings=off).evaluate([limited(five=100.0, five_reset=FIVE_RESET)]), [])
        found = self.make(settings=off).evaluate([state(five=0.0, five_reset=FIVE_RESET + timedelta(hours=5))])
        self.assertEqual([a.kind for a in found], ["back"])

    def test_back_without_any_limit_reading(self):
        self.read(80.0, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET + timedelta(minutes=2)
        self.assertEqual(self.make().evaluate([state(five=0.0)]), [])

    def test_stale_limit_does_not_give_back(self):
        self.read(100.0, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET + timedelta(hours=3)
        self.assertEqual(self.make().evaluate([state(five=0.0)]), [])
        self.clock.now = FIVE_RESET + timedelta(minutes=61)
        self.assertEqual(self.make().evaluate([state(five=0.0)]), [])

    def test_back_waits_while_the_account_is_still_at_its_limit(self):
        self.read(100.0, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET + timedelta(minutes=1)
        self.assertEqual(self.make().evaluate([state(five=100.0)]), [])

    def test_back_part_used_text(self):
        self.read(100.0, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET + timedelta(minutes=5)
        found = self.make().evaluate([state(five=12.4, five_reset=FIVE_RESET + timedelta(hours=5))])
        self.assertEqual(found[0].body, "It has 88% of its 5 hours left.")

    def test_keys_round_the_reset_to_the_nearest_minute(self):
        for offset, expected in (
            (timedelta(seconds=29, microseconds=500000), "07:00:00"),
            (timedelta(seconds=-0.4), "07:00:00"),
            (timedelta(seconds=30), "07:01:00"),
            (timedelta(seconds=-30.5), "06:59:00"),
        ):
            history = History(Path(self.folder.name) / "rounding-{}.db".format(offset.total_seconds()))
            found = self.make(history=history).evaluate([limited(five=100.0, five_reset=FIVE_RESET + offset)])
            self.assertEqual(found[0].key, "limit:%s:2026-11-19T%s+00:00" % (ALEX, expected), offset)

    def test_one_reset_read_two_ways_gives_one_alert(self):
        engine = self.make()
        first = engine.evaluate([limited(five=100.0, five_reset=FIVE_RESET - timedelta(milliseconds=400))])
        second = engine.evaluate([limited(five=100.0, five_reset=FIVE_RESET)])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_week_keys_round_too(self):
        alert = self.one([state(five=95.0, week=93.4, week_reset=WEEK_RESET + timedelta(seconds=40))])
        self.assertEqual(alert.key, "week:%s:2026-11-23T07:31:00+00:00" % ALEX)

    def test_week_text_and_key(self):
        alert = self.one([state(five=95.0, week=93.4)])
        self.assertEqual(alert.kind, "week")
        self.assertEqual(alert.key, "week:%s:2026-11-23T07:30:00+00:00" % ALEX)
        self.assertEqual(alert.title, "Alex's Account has used 93% of its week")
        self.assertEqual(alert.body, "It resets on Monday at 09:30.")
        self.assertEqual(self.make().evaluate([state(week=89.9, five=1.0)]), [])

    def test_signin_text_and_days(self):
        machine = "this Mac"
        ends = NOW + timedelta(days=3)
        alert = self.one([state(signin_ends=ends, signin_machine=machine)])
        self.assertEqual(alert.kind, "signin_ends")
        self.assertEqual(alert.key, "signin-ends:%s:2026-11-22" % ALEX)
        self.assertEqual(alert.title, "A sign-in ends in 3 days")
        self.assertEqual(
            alert.body,
            "Alex's Account on this Mac needs a new sign-in by Sunday at 12:00. Press Sign in again, then Authorize in your browser.",
        )
        for days, title in ((2, "A sign-in ends in 2 days"), (1, "A sign-in ends tomorrow")):
            found = self.one(
                [state(signin_ends=NOW + timedelta(days=days), signin_machine=machine)],
                history=History(Path(self.folder.name) / ("h%d.db" % days)),
            )
            self.assertEqual(found.title, title)
        today = self.one(
            [state(signin_ends=NOW + timedelta(hours=3), signin_machine="the other computer")],
            history=History(Path(self.folder.name) / "today.db"),
        )
        self.assertEqual(today.title, "A sign-in ends today")

    def test_later_gives_one_reminder_a_day_before_the_end_even_after_a_restart(self):
        ends = NOW + timedelta(days=2)
        signin = state(signin_ends=ends, signin_machine="this Mac")
        first = self.one([signin])
        base = alerts.later_key(ALEX, ends)
        self.assertEqual(base, "signin-ends:%s:2026-11-21" % ALEX)
        self.clock.now = ends - timedelta(hours=23)
        self.assertEqual(self.make().evaluate([signin]), [])
        self.history.add_event(base + ":later", NOW + timedelta(hours=1))
        restarted = Alerts(History(self.history.path), {}, TZ, self.clock)
        self.clock.now = ends - timedelta(hours=24, minutes=1)
        self.assertEqual(restarted.evaluate([signin]), [])
        self.clock.now = ends - timedelta(hours=24)
        reminder = restarted.evaluate([signin])
        self.assertEqual([(a.kind, a.key, a.title) for a in reminder], [("signin_ends", base + ":reminder", "A sign-in ends tomorrow")])
        self.assertEqual(reminder[0].body, first.body.replace("by Saturday at", "by tomorrow at"))
        self.assertEqual(restarted.evaluate([signin]), [])

    def test_a_reminder_survives_a_change_of_time_zone(self):
        ends = datetime(2026, 11, 21, 10, 30, tzinfo=UTC)
        signin = state(signin_ends=ends, signin_machine="this Mac")
        self.history.add_event(alerts.later_key(ALEX, ends) + ":later", NOW)
        self.clock.now = ends - timedelta(hours=20)
        far_east = Alerts(self.history, {}, timezone(timedelta(hours=14)), self.clock)
        keys = [a.key for a in far_east.evaluate([signin])]
        self.assertIn("signin-ends:%s:2026-11-21:reminder" % ALEX, keys)
        self.assertIn("signin-ends:%s:2026-11-22" % ALEX, keys)

    def test_a_reminder_never_comes_sooner_than_an_hour_after_later(self):
        ends = NOW + timedelta(hours=10)
        signin = state(signin_ends=ends, signin_machine="this Mac")
        self.one([signin])
        base = alerts.later_key(ALEX, ends)
        self.history.add_event(base + ":later", NOW)
        self.clock.now = NOW + timedelta(minutes=59)
        self.assertEqual(self.make().evaluate([signin]), [])
        self.clock.now = NOW + timedelta(hours=1)
        self.assertEqual([a.key for a in self.make().evaluate([signin])], [base + ":reminder"])
        self.clock.now = ends + timedelta(minutes=1)
        self.assertEqual(self.make(history=History(Path(self.folder.name) / "after.db")).evaluate([signin]), [])

    def test_signin_without_a_machine_name(self):
        alert = self.one([state(signin_ends=NOW + timedelta(days=2), signin_machine=None)])
        self.assertEqual(alert.body, "Alex's Account needs a new sign-in by Saturday at 12:00. Press Sign in again, then Authorize in your browser.")

    def test_signin_title_uses_local_calendar_days(self):
        late = datetime(2026, 11, 19, 21, 30, tzinfo=UTC)
        self.clock.now = late
        alert = self.one([state(signin_ends=late + timedelta(hours=3), signin_machine="this Mac")])
        self.assertEqual(alert.title, "A sign-in ends tomorrow")

    def test_signin_not_yet_due_or_already_past(self):
        far = state(signin_ends=NOW + timedelta(days=3, minutes=1), signin_machine="this Mac")
        self.assertEqual(self.make().evaluate([far]), [])
        past = state(signin_ends=NOW - timedelta(minutes=1), signin_machine="this Mac")
        self.assertEqual(self.make().evaluate([past]), [])

    def test_missing_times_are_skipped(self):
        broken = limited(five=100.0, five_reset=None, week=95.0, week_reset=None)
        self.assertEqual(self.make().evaluate([broken]), [])


class TestSwitches(AlertsTestCase):
    def test_disabled_kinds_record_nothing(self):
        every = [
            running_out(),
            limited(WORK, "Work Account", five=100.0),
            state(SOLO, "Personal Account", five=96.0, week=95.0),
            state(CREW, "Team Account", signin_ends=NOW + timedelta(days=1), signin_machine="this Mac"),
        ]
        off = {k: False for k in ("will_run_out", "limit", "back", "week", "signin_ends")}
        self.assertEqual(self.make(settings=off).evaluate(every), [])
        self.assertEqual(len(self.make().evaluate(every)), 4)

    def test_one_kind_off_leaves_the_others(self):
        found = self.make(settings={"week": False}).evaluate([limited(week=95.0, five=100.0)])
        self.assertEqual([a.kind for a in found], ["limit"])
        self.assertEqual([a.kind for a in self.make().evaluate([limited(week=95.0, five=100.0)])], ["week"])

    def test_an_alert_is_a_mac_notification(self):
        alert = self.one([limited(five=100.0)] + others())
        self.assertEqual(alert.channels, ("mac",))

    def test_mac_notifications_off(self):
        home = self.make(settings={"mac": False}, history=History(Path(self.folder.name) / "c.db"))
        self.assertEqual(home.evaluate([limited(five=100.0)]), [])

    def test_alert_with_no_channels_is_recorded_not_returned(self):
        silent = {"mac": False}
        self.assertEqual(self.make(settings=silent).evaluate([limited(five=100.0)]), [])
        self.assertEqual(self.make().evaluate([limited(five=100.0)]), [])

    def test_alert_dedupes_across_machines(self):
        mac = self.make()
        devbox = self.make()
        states = [running_out(), limited(WORK, "Work Account", five=100.0)]
        total = mac.evaluate(states) + devbox.evaluate(states)
        self.assertEqual(len(total), 2)
        self.assertEqual(devbox.evaluate(states) + mac.evaluate(states), [])


LIVE_NOW = datetime(2026, 11, 19, 16, 35, tzinfo=UTC)
LIVE_RESET = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)


class BothWindowsAlertTests(AlertsTestCase):
    def test_the_5_hours_running_out_first_warns_though_the_week_binds(self):
        quick = Forecast(250.0, NOW + timedelta(minutes=35), 2.0, "recent")
        subject = limited(
            five=30.0, five_reset=NOW + timedelta(hours=3), week=50.0,
            forecast=Forecast(60.0, None, 0.001, "recent"), other_forecast=quick,
        )
        found = self.make().evaluate([subject])
        self.assertEqual([(a.kind, a.key) for a in found], [("will_run_out", "will-run-out:%s:2026-11-19T08:00:00+00:00" % ALEX)])
        self.assertEqual(found[0].title, "Alex's Account runs out today at 12:35")
        self.assertEqual(found[0].body, "That is 2 hours 25 minutes before its 5 hours reset today at 15:00.")

    def test_a_run_out_time_that_has_passed_warns_of_nothing(self):
        passed = Forecast(180.0, NOW - timedelta(minutes=1), 2.0, "recent")
        self.assertEqual(self.make().evaluate([limited(five=60.0, forecast=passed)]), [])
        found = self.make().evaluate([limited(five=30.0, week=50.0, forecast=Forecast(60.0, None, 0.001, "recent"), other_forecast=passed)])
        self.assertEqual(found, [])

    def test_both_windows_full_give_one_limit_alert_on_the_week(self):
        uneven = self.make(history=History(Path(self.folder.name) / "uneven.db")).evaluate([limited(five=104.0, week=100.0)])
        self.assertEqual([(a.kind, a.key) for a in uneven], [("limit", "limit-week:%s:2026-11-16T07:30:00+00:00" % ALEX)])
        found = self.make().evaluate([limited(five=100.0, week=100.0)])
        self.assertEqual([(a.kind, a.key, a.title) for a in found], [("limit", "limit-week:%s:2026-11-16T07:30:00+00:00" % ALEX, "Alex's Account is out until Monday at 09:30")])

    def test_no_week_warning_after_the_week_limit_alert_went_out(self):
        engine = self.make()
        in_use = weekly(week=100.0, used_by=("this Mac",))
        self.assertEqual([a.kind for a in engine.evaluate([in_use])], ["limit"])
        self.assertEqual(engine.evaluate([weekly(week=100.0)]), [])
        self.assertEqual(self.make().evaluate([weekly(week=100.0)]), [])


class MoreRoomAlertTests(AlertsTestCase):
    def test_the_room_sentence_names_only_an_account_with_more_room(self):
        fuller = state(SOLO, "Personal Account", five=86.0, week=10.0)
        found = self.make().evaluate([running_out(), fuller])
        self.assertEqual([a.kind for a in found], ["will_run_out"])
        self.assertTrue(found[0].body.endswith("before its 5 hours reset today at 14:00."), found[0].body)
        roomier = state(WORK, "Work Account", five=30.0, week=10.0)
        found = self.make(history=History(Path(self.folder.name) / "roomier.db")).evaluate([running_out(), roomier])
        self.assertTrue(found[0].body.endswith("Work Account has the most room."), found[0].body)


class EarlyResetAlertTests(AlertsTestCase):
    def test_an_early_reset_ends_the_limit_and_starts_new_keys(self):
        early = NOW - timedelta(minutes=20)
        self.history.add_reading(SOLO, "usage", None, None, 100.0, WEEK_RESET, NOW - timedelta(hours=2))
        self.history.add_reading(SOLO, "usage", None, None, 3.0, WEEK_RESET, early)
        restarted = Forecast(3.0, None, None, "none", early)
        found = self.make().evaluate([weekly(week=3.0, forecast=restarted)])
        self.assertEqual([(a.kind, a.key, a.body) for a in found], [("back", "back-week:%s:%s" % (SOLO, "2026-11-19T09:40:00+00:00"), "It has 97% of its week left.")])
        out = self.make().evaluate([weekly(week=100.0, used_by=("this Mac",), forecast=restarted)])
        self.assertEqual([a.key for a in out if a.kind == "limit"], ["limit-week:%s:2026-11-19T09:40:00+00:00" % SOLO])
        plain = self.make(history=History(Path(self.folder.name) / "plain.db")).evaluate([weekly(week=100.0, used_by=("this Mac",))])
        self.assertEqual([a.key for a in plain if a.kind == "limit"], ["limit-week:%s:2026-11-16T07:30:00+00:00" % SOLO])


class UsageOnlyBackTests(AlertsTestCase):
    def test_a_usage_reading_just_under_a_full_status_line_post_is_not_a_reset(self):
        reset = NOW + timedelta(hours=3)
        self.history.add_reading(SOLO, "usage", 97.0, reset, None, None, NOW - timedelta(minutes=6))
        self.history.add_reading(SOLO, "statusline", 100.0, reset, None, None, NOW - timedelta(minutes=5))
        self.history.add_reading(SOLO, "usage", 98.9, reset, None, None, NOW - timedelta(minutes=1))
        self.assertIsNone(alerts.ended_at_limit(self.history, SOLO, NOW, "five"))

    def test_a_usage_drop_from_a_full_usage_reading_is_still_a_reset(self):
        reset = NOW + timedelta(hours=3)
        self.history.add_reading(SOLO, "usage", 100.0, reset, None, None, NOW - timedelta(minutes=6))
        self.history.add_reading(SOLO, "usage", 3.0, reset, None, None, NOW - timedelta(minutes=1))
        self.assertEqual(alerts.ended_at_limit(self.history, SOLO, NOW, "five"), NOW - timedelta(minutes=1))


class PlainAlertTests(AlertsTestCase):

    def test_a_sign_in_alert_never_promises_one_click(self):
        ends = NOW + timedelta(days=2)
        found = self.make().evaluate([state(signin_ends=ends, signin_machine=None)])
        body = [a.body for a in found if a.kind == "signin_ends"][0]
        self.assertTrue(body.endswith("Press Sign in again, then Authorize in your browser."), body)
        self.assertNotIn("click", body)


class NoRoomAlertTests(AlertsTestCase):
    """with no room anywhere the alert names whichever account is back first, counting the one in use."""

    def limit_of(self, found):
        return next(a for a in found if a.kind == "limit")

    def test_the_account_in_use_can_be_back_first(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=100.0, week=50.0, five_reset=FIVE_RESET + timedelta(hours=1))
        found = self.limit_of(self.make().evaluate([alpha, bravo]))
        self.assertEqual((found.title, found.body), ("Alex's Account is out until today at 14:00", "No other account has room, and this one is back first."))
        self.assertEqual(alerts.room([alpha, bravo], ALEX, now=NOW, tz=TZ), "No other account has room, and this one is back first, today at 14:00.")

    def test_another_account_back_first_is_named(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=100.0, week=50.0, five_reset=FIVE_RESET - timedelta(minutes=30))
        found = self.limit_of(self.make().evaluate([alpha, bravo]))
        self.assertEqual(found.body, "No other account has room. Work Account is back today at 13:30.")

    def test_an_account_at_99_percent_is_never_the_room(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=99.5, week=50.0, five_reset=FIVE_RESET + timedelta(hours=1))
        found = self.limit_of(self.make().evaluate([alpha, bravo]))
        self.assertEqual(found.body, "No other account has room, and this one is back first.")
        charlie = state(CREW, "Team Account", five=98.9, week=50.0)
        found = self.limit_of(self.make(history=History(Path(self.folder.name) / "room.db")).evaluate([alpha, bravo, charlie]))
        self.assertEqual(found.body, "Team Account has the most room.")

    def test_a_warning_from_99_percent_with_no_room_says_so_too(self):
        bravo = state(WORK, "Work Account", five=100.0, week=50.0, five_reset=FIVE_RESET + timedelta(hours=1))
        near = state(five=99.5, forecast=Forecast(120.0, FIVE_RESET - timedelta(minutes=80), 1.0, "recent"), used_by=("this Mac",))
        found = next(a for a in self.make().evaluate([near, bravo]) if a.kind == "will_run_out")
        self.assertEqual(found.body, "That is 1 hour 20 minutes before its 5 hours reset today at 14:00. No other account has room, and this one resets first.")
        found = next(a for a in self.make(history=History(Path(self.folder.name) / "low.db")).evaluate([running_out(), bravo]) if a.kind == "will_run_out")
        self.assertEqual(found.body, "That is 1 hour 20 minutes before its 5 hours reset today at 14:00.")

    def test_an_account_lifeline_cannot_read_is_never_the_room(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        blind = state(CREW, "Team Account", five=1.0, week=1.0, cannot_read=True)
        other = state(WORK, "Work Account", five=30.0, week=10.0)
        self.assertEqual(self.limit_of(self.make().evaluate([alpha, blind, other])).body, "Work Account has the most room.")
        self.assertEqual(alerts.room([alpha, blind, other], ALEX, now=NOW, tz=TZ), "Work Account has the most room.")

    def test_an_account_used_on_the_devbox_gets_the_devbox_reason(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0, used_by=("the other computer",), on_devbox=True)
        bravo = state(WORK, "Work Account", five=10.0, week=10.0, on_devbox=False)
        found = self.limit_of(self.make().evaluate([alpha, bravo]))
        self.assertEqual(found.body, "No other account has a working sign-in on the other computer.")
        signed_out_here = state(WORK, "Work Account", five=100.0, week=10.0, on_devbox=True, needs_signin=True,
                                five_reset=FIVE_RESET + timedelta(hours=1))
        found = self.limit_of(self.make(history=History(Path(self.folder.name) / "dev.db")).evaluate([alpha, signed_out_here]))
        self.assertEqual(found.body, "No other account has room, and this one is back first.")

    def test_no_other_usable_account_gives_the_menus_reason(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=10.0, week=10.0, needs_signin=True)
        self.assertEqual(self.limit_of(self.make().evaluate([alpha, bravo])).body, "Every other account needs a new sign-in.")
        self.assertEqual(self.limit_of(self.make(history=History(Path(self.folder.name) / "alone.db")).evaluate([alpha])).body,
                         "There is no other account to switch to.")


class LeastRoomAlertTests(AlertsTestCase):
    def kinds(self, found):
        return sorted((a.kind, a.uuid) for a in found)

    def test_a_full_week_is_never_the_room(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=0.0, week=100.0)
        charlie = weekly(week=100.0)
        found = self.make().evaluate([alpha, bravo, charlie])
        self.assertEqual(self.kinds(found), [("limit", ALEX), ("week", WORK), ("week", SOLO)])
        self.assertEqual([a.body for a in found if a.kind == "limit"], ["No other account has room, and this one is back first."])

    def test_every_other_account_full_names_no_room(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=100.0, week=50.0)
        found = self.make().evaluate([alpha, bravo])
        self.assertEqual(found[0].body, "No other account has room, and this one is back first.")
        self.assertEqual(alerts.room([alpha, bravo], ALEX, now=NOW, tz=TZ), "No other account has room, and this one is back first, today at 14:00.")

    def test_an_account_that_needs_a_new_sign_in_is_never_the_room(self):
        alpha = limited(ALEX, "Alex's Account", five=100.0, week=40.0)
        bravo = state(WORK, "Work Account", five=0.0, week=0.0, needs_signin=True)
        charlie = weekly(week=40.0)
        found = self.make().evaluate([alpha, bravo, charlie])
        self.assertEqual(found[0].body, "Personal Account has the most room.")

    def test_an_account_bound_by_its_week_is_out_and_back_on_its_week(self):
        alpha = limited(ALEX, "Alex's Account", five=10.0, week=100.0)
        found = self.make().evaluate([alpha, state(CREW, "Team Account", five=0.0, week=0.0)])
        self.assertEqual(self.kinds(found), [("limit", ALEX)])
        self.assertEqual(found[0].key, "limit-week:%s:2026-11-16T07:30:00+00:00" % ALEX)
        self.assertEqual((found[0].title, found[0].body), ("Alex's Account is out until Monday at 09:30", "Team Account has all of its 5 hours."))

    def test_the_week_warning_comes_once_a_week_whatever_the_binding(self):
        found = self.make().evaluate([weekly(week=91.0)])
        self.assertEqual([(a.kind, a.title) for a in found], [("week", "Personal Account has used 91% of its week")])
        engine = self.make(history=History(Path(self.folder.name) / "five.db"))
        found = engine.evaluate([state(five=95.0, week=91.0)])
        self.assertEqual(self.kinds(found), [("week", ALEX)])
        self.assertEqual(engine.evaluate([state(five=95.0, week=93.0)]), [])
        engine = self.make(history=History(Path(self.folder.name) / "bound.db"))
        self.assertEqual(self.kinds(engine.evaluate([state(five=10.0, week=92.0)])), [("week", ALEX)])

    def test_a_full_week_gives_only_the_limit_alert(self):
        found = self.make().evaluate([weekly(week=100.0, used_by=("this Mac",))])
        self.assertEqual([a.kind for a in found], ["limit"])
        self.assertEqual(self.make().evaluate([weekly(week=100.0, used_by=("this Mac",))]), [])

    def test_the_week_percent_rounds_down_like_the_menu(self):
        found = self.make().evaluate([state(five=10.0, week=99.6)])
        self.assertEqual(found[0].title, "Alex's Account has used 99% of its week")
        end = NOW - timedelta(minutes=20)
        self.history.add_reading(SOLO, "usage", None, None, 100.0, end, NOW - timedelta(hours=2))
        found = self.make().evaluate([weekly(week=2.6, week_reset=end + timedelta(days=7))])
        self.assertEqual([a.body for a in found if a.kind == "back"], ["It has 98% of its week left."])

    def test_a_reset_that_has_passed_names_no_time(self):
        found = self.make().evaluate([limited(five=100.0, five_reset=NOW - timedelta(minutes=1))])
        self.assertEqual(found, [])

    def test_back_after_the_5_hours_reset_while_the_week_binds(self):
        self.history.add_reading(ALEX, "usage", 100.0, FIVE_RESET, 20.0, WEEK_RESET, FIVE_RESET - timedelta(hours=1))
        self.clock.now = FIVE_RESET + timedelta(minutes=2)
        found = self.make().evaluate([state(five=0.0, five_reset=None, week=30.0)])
        self.assertEqual([(a.kind, a.key, a.body) for a in found], [("back", "back:%s:2026-11-19T12:00:00+00:00" % ALEX, "It has all of its 5 hours again.")])


class FutureDayTests(AlertsTestCase):
    def make_live(self, now=LIVE_NOW):
        self.clock.now = now
        return Alerts(self.history, {}, UTC, self.clock)

    def test_the_live_week_limit_reads_next_thursday(self):
        found = self.make_live().evaluate([weekly(week=100.0, week_reset=LIVE_RESET, used_by=("this Mac",))])
        limit = [a for a in found if a.kind == "limit"][0]
        self.assertEqual(limit.title, "Personal Account is out until next Thursday at 02:00")

    def test_a_week_limit_days_away_has_no_on_after_until(self):
        sunday = datetime(2026, 11, 22, 2, 0, tzinfo=UTC)
        found = self.make_live().evaluate([weekly(week=100.0, week_reset=sunday, used_by=("this Mac",))])
        limit = [a for a in found if a.kind == "limit"][0]
        self.assertEqual(limit.title, "Personal Account is out until Sunday at 02:00")

    def test_a_week_warning_and_run_out_name_their_day(self):
        forecast = Forecast(130.0, datetime(2026, 11, 21, 9, 0, tzinfo=UTC), 1.0, "recent")
        found = self.make_live().evaluate([weekly(week=92.0, week_reset=LIVE_RESET, used_by=("this Mac",), forecast=forecast)])
        run = [a for a in found if a.kind == "will_run_out"][0]
        self.assertEqual(run.title, "Personal Account runs out on Saturday at 09:00")
        self.assertTrue(run.body.startswith("That is 4 days 17 hours before its week resets next Thursday at 02:00."), run.body)
        self.assertEqual(sorted(a.kind for a in found), ["week", "will_run_out"])
        history = History(Path(self.folder.name) / "week.db")
        found = Alerts(history, {}, UTC, self.clock).evaluate([state(five=95.0, week=92.0, week_reset=LIVE_RESET)])
        self.assertEqual([(a.kind, a.body) for a in found], [("week", "It resets next Thursday at 02:00.")])

    def test_five_hour_times_say_today_or_tomorrow(self):
        reset = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
        forecast = Forecast(140.0, datetime(2026, 11, 19, 23, 0, tzinfo=UTC), 1.0, "recent")
        found = self.make_live().evaluate([state(five=60.0, five_reset=reset, used_by=("this Mac",), forecast=forecast)])
        run = [a for a in found if a.kind == "will_run_out"][0]
        self.assertEqual(run.title, "Alex's Account runs out today at 23:00")
        self.assertEqual(run.body, "That is 2 hours before its 5 hours reset tomorrow at 01:00.")
        history = History(Path(self.folder.name) / "out.db")
        found = Alerts(history, {}, UTC, self.clock).evaluate([limited(five=100.0, five_reset=reset)])
        self.assertEqual(found[0].title, "Alex's Account is out until tomorrow at 01:00")

    def test_a_sign_in_end_names_its_day_without_on(self):
        found = self.make_live().evaluate([state(signin_ends=datetime(2026, 11, 21, 12, 0, tzinfo=UTC), signin_machine="this Mac")])
        self.assertEqual(found[0].body, "Alex's Account on this Mac needs a new sign-in by Saturday at 12:00. Press Sign in again, then Authorize in your browser.")
        history = History(Path(self.folder.name) / "today.db")
        found = Alerts(history, {}, UTC, self.clock).evaluate([state(signin_ends=datetime(2026, 11, 19, 20, 0, tzinfo=UTC))])
        self.assertEqual(found[0].body, "Alex's Account needs a new sign-in by today at 20:00. Press Sign in again, then Authorize in your browser.")


class TestGap(unittest.TestCase):
    def test_gap_formatting(self):
        cases = (
            (timedelta(minutes=1), "1 minute"),
            (timedelta(minutes=45), "45 minutes"),
            (timedelta(minutes=59), "59 minutes"),
            (timedelta(hours=1), "1 hour"),
            (timedelta(hours=1, minutes=1), "1 hour 1 minute"),
            (timedelta(hours=1, minutes=20), "1 hour 20 minutes"),
            (timedelta(hours=2), "2 hours"),
            (timedelta(hours=2, minutes=40), "2 hours 40 minutes"),
        )
        for gap, text in cases:
            self.assertEqual(alerts.format_gap(gap), text)



class EachSideBestTests(unittest.TestCase):
    def test_the_best_account_comes_from_the_subjects_own_side(self):
        mac_subject = state(ALEX, "Alex's Account", five=80.0, used_by=("this Mac",))
        on_devbox = state(WORK, "Work Account", five=10.0, used_by=("the other computer",))
        idle = state(SOLO, "Personal Account", five=40.0, week=20.0)
        self.assertEqual(alerts._best([mac_subject, on_devbox, idle], mac_subject).uuid, WORK)
        devbox_subject = state(WORK, "Work Account", five=80.0, used_by=("the other computer",))
        alex = state(ALEX, "Alex's Account", five=10.0, used_by=("this Mac",), on_devbox=True)
        no_devbox_signin = state(SOLO, "Personal Account", five=5.0, week=5.0)
        self.assertEqual(alerts._best([devbox_subject, alex, no_devbox_signin], devbox_subject).uuid, ALEX)
        self.assertIsNone(alerts._best([devbox_subject, no_devbox_signin], devbox_subject))


if __name__ == "__main__":
    unittest.main()
