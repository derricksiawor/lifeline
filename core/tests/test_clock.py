import unittest
from datetime import datetime, timedelta, timezone

from lifeline import clock

UTC = timezone.utc


class ClockTests(unittest.TestCase):
    def test_times_round_to_the_nearest_minute(self):
        cases = (
            (datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC), "02:00"),
            (datetime(2026, 11, 26, 1, 59, 30, tzinfo=UTC), "02:00"),
            (datetime(2026, 11, 26, 1, 59, 29, 999999, tzinfo=UTC), "01:59"),
            (datetime(2026, 11, 26, 2, 0, tzinfo=UTC), "02:00"),
        )
        for moment, text in cases:
            with self.subTest(moment=moment):
                self.assertEqual(clock.hhmm(moment, UTC), text)

    def test_the_day_follows_the_rounded_time(self):
        late = datetime(2026, 11, 25, 23, 59, 45, tzinfo=UTC)
        wednesday = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)
        self.assertEqual(clock.when(late, wednesday, UTC), "tomorrow at 00:00")
        self.assertEqual(clock.when(datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC), wednesday, UTC), "tomorrow at 02:00")

    def test_the_zone_is_applied_before_rounding(self):
        moment = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)
        self.assertEqual(clock.hhmm(moment, timezone(timedelta(hours=2))), "04:00")

    def test_a_naive_local_time_is_rounded_as_it_is(self):
        self.assertEqual(clock.hhmm(datetime(2026, 11, 26, 9, 14, 31)), "09:15")

    def test_future_times_name_their_day_by_local_calendar_date(self):
        now = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
        cases = (
            (datetime(2026, 11, 18, 23, 59, 29, tzinfo=UTC), "today at 23:59"),
            (datetime(2026, 11, 18, 23, 59, 45, tzinfo=UTC), "tomorrow at 00:00"),
            (datetime(2026, 11, 19, 23, 59, tzinfo=UTC), "tomorrow at 23:59"),
            (datetime(2026, 11, 19, 23, 59, 45, tzinfo=UTC), "on Friday at 00:00"),
            (datetime(2026, 11, 24, 23, 59, tzinfo=UTC), "on Tuesday at 23:59"),
            (datetime(2026, 11, 24, 23, 59, 45, tzinfo=UTC), "next Wednesday at 00:00"),
            (datetime(2026, 11, 25, 23, 59, tzinfo=UTC), "next Wednesday at 23:59"),
            (datetime(2026, 11, 25, 23, 59, 45, tzinfo=UTC), "on 26 November at 00:00"),
            (datetime(2027, 1, 2, 9, 5, tzinfo=UTC), "on 2 January at 09:05"),
        )
        for moment, text in cases:
            with self.subTest(moment=moment):
                self.assertEqual(clock.when(moment, now, UTC), text)

    def test_rounding_is_on_the_absolute_time_across_a_clock_change(self):
        from zoneinfo import ZoneInfo

        london, new_york = ZoneInfo("Europe/London"), ZoneInfo("America/New_York")
        cases = (
            (datetime(2026, 10, 25, 0, 59, 59, 678000, tzinfo=UTC), datetime(2026, 10, 24, 22, 0, tzinfo=UTC), london, "tomorrow at 01:00"),
            (datetime(2026, 3, 29, 0, 59, 59, 678000, tzinfo=UTC), datetime(2026, 3, 28, 22, 0, tzinfo=UTC), london, "tomorrow at 02:00"),
            (datetime(2026, 11, 1, 5, 59, 59, 678000, tzinfo=UTC), datetime(2026, 10, 31, 22, 0, tzinfo=UTC), new_york, "tomorrow at 01:00"),
        )
        for moment, now, zone, text in cases:
            with self.subTest(moment=moment, zone=zone):
                self.assertEqual(clock.when(moment, now, zone), text)
                self.assertEqual(clock.nearest_minute(moment, zone), (moment + timedelta(seconds=0.322)).astimezone(zone))

    def test_the_day_today_is_the_real_one_even_seconds_before_midnight(self):
        now = datetime(2026, 11, 18, 23, 59, 45, tzinfo=UTC)
        self.assertEqual(clock.when(datetime(2026, 11, 19, 2, 0, tzinfo=UTC), now, UTC), "tomorrow at 02:00")
        self.assertEqual(clock.when(datetime(2026, 11, 18, 23, 59, 50, tzinfo=UTC), now, UTC), "tomorrow at 00:00")

    def test_the_live_week_reset_reads_next_thursday(self):
        from zoneinfo import ZoneInfo

        reykjavik = ZoneInfo("Atlantic/Reykjavik")
        now = datetime(2026, 11, 19, 16, 35, tzinfo=reykjavik)
        reset = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)
        self.assertLess(reset - now, timedelta(days=7))
        self.assertEqual(clock.when(reset, now, reykjavik), "next Thursday at 02:00")
        self.assertEqual(clock.when(datetime(2026, 11, 26, 2, 0, tzinfo=reykjavik), now, reykjavik), "next Thursday at 02:00")

    def test_one_in_the_morning_tomorrow_reads_tomorrow_with_under_a_day_to_go(self):
        now = datetime(2026, 11, 19, 16, 35, tzinfo=UTC)
        moment = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
        self.assertLess(moment - now, timedelta(hours=24))
        self.assertEqual(clock.when(moment, now, UTC), "tomorrow at 01:00")

    def test_after_until_or_by_the_day_has_no_on(self):
        now = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
        cases = (
            (datetime(2026, 11, 18, 22, 0, tzinfo=UTC), "today at 22:00"),
            (datetime(2026, 11, 19, 1, 0, tzinfo=UTC), "tomorrow at 01:00"),
            (datetime(2026, 11, 21, 2, 0, tzinfo=UTC), "Saturday at 02:00"),
            (datetime(2026, 11, 25, 2, 0, tzinfo=UTC), "next Wednesday at 02:00"),
            (datetime(2026, 11, 27, 2, 0, tzinfo=UTC), "27 November at 02:00"),
        )
        for moment, text in cases:
            with self.subTest(moment=moment):
                self.assertEqual(clock.when(moment, now, UTC, on=False), text)

    def test_days_are_counted_in_the_local_zone(self):
        plus_two = timezone(timedelta(hours=2))
        now = datetime(2026, 11, 19, 23, 30, tzinfo=UTC)
        self.assertEqual(clock.when(datetime(2026, 11, 20, 10, 0, tzinfo=UTC), now, plus_two), "today at 12:00")
        self.assertEqual(clock.when(datetime(2026, 11, 20, 10, 0, tzinfo=UTC), now, UTC), "tomorrow at 10:00")
        self.assertEqual(clock.when(datetime(2026, 11, 20, 1, 0), datetime(2026, 11, 19, 16, 35)), "tomorrow at 01:00")

    def test_a_past_time_names_its_day_by_the_day_rules(self):
        now = datetime(2026, 11, 19, 16, 35, tzinfo=UTC)
        self.assertEqual(clock.when(datetime(2026, 11, 18, 2, 0, tzinfo=UTC), now, UTC), "yesterday at 02:00")
        self.assertEqual(clock.when(datetime(2026, 11, 11, 2, 0, tzinfo=UTC), now, UTC), "on 11 November at 02:00")

    def test_a_day_alone_follows_the_same_rules(self):
        today = datetime(2026, 11, 19).date()
        cases = ((0, "today"), (1, "tomorrow"), (2, "on Saturday"), (6, "on Wednesday"), (7, "next Thursday"), (8, "on 27 November"))
        for ahead, text in cases:
            with self.subTest(ahead=ahead):
                self.assertEqual(clock.day(today + timedelta(days=ahead), today), text)
        self.assertEqual(clock.day(today + timedelta(days=2), today, on=False), "Saturday")

    def test_gaps_round_to_the_nearest_minute(self):
        cases = (
            (timedelta(seconds=29), "less than a minute"),
            (timedelta(seconds=30), "1 minute"),
            (timedelta(seconds=89), "1 minute"),
            (timedelta(seconds=150), "3 minutes"),
            (timedelta(hours=1), "1 hour"),
            (timedelta(hours=1, minutes=1), "1 hour 1 minute"),
            (timedelta(hours=1, minutes=59, seconds=30), "2 hours"),
            (timedelta(hours=2, minutes=39, seconds=45), "2 hours 40 minutes"),
            (timedelta(days=1, hours=3, minutes=20), "1 day 3 hours"),
            (timedelta(days=2), "2 days"),
        )
        for delta, text in cases:
            with self.subTest(delta=delta):
                self.assertEqual(clock.gap(delta), text)


class EveryShownTimeTests(unittest.TestCase):
    def test_every_module_that_shows_a_time_rounds_it(self):
        from lifeline import cli
        from lifeline import reader as reading
        from lifeline.history import Reading

        moment = datetime(2026, 11, 26, 1, 59, 59, 678000, tzinfo=UTC)
        local = clock.nearest_minute(moment)
        before = moment - timedelta(minutes=5)
        day = "today" if before.astimezone().date() == local.date() else "tomorrow"
        self.assertEqual(reading._clock_text(moment, before), f"{day} at {local:%H:%M}")
        self.assertTrue(cli._moment(moment, UTC).startswith("02:00 on 26 "))
        now = datetime(2026, 11, 19, 15, 41, tzinfo=UTC)
        lines = cli._reading_lines(Reading("u", "usage", None, None, 57.0, moment, now), now, UTC)
        self.assertIn("Its week resets next Thursday at 02:00.", lines[-1])

    def test_the_alert_and_the_menu_say_the_same_gap(self):
        from lifeline import alerts, viewmodel

        for delta in (timedelta(seconds=20), timedelta(seconds=150), timedelta(hours=2, minutes=39, seconds=45), timedelta(days=1, minutes=1)):
            with self.subTest(delta=delta):
                self.assertEqual(alerts.format_gap(delta), clock.gap(delta))
                self.assertEqual(viewmodel._gap(delta), clock.gap(delta))



class PastDayTests(unittest.TestCase):
    def test_a_past_day_follows_the_day_rules(self):
        from datetime import date

        today = date(2026, 11, 20)
        self.assertEqual(clock.past_day(today, today), "today")
        self.assertEqual(clock.past_day(date(2026, 11, 19), today), "yesterday")
        self.assertEqual(clock.past_day(date(2026, 11, 16), today), "on Monday")
        self.assertEqual(clock.past_day(date(2026, 11, 13), today), "on 13 November")

    def test_every_time_in_the_past_follows_the_day_rules(self):
        from datetime import datetime, timezone

        now = datetime(2026, 11, 20, 11, 30, tzinfo=timezone.utc)
        self.assertEqual(clock.when(datetime(2026, 11, 20, 9, 5, tzinfo=timezone.utc), now, timezone.utc), "today at 09:05")
        self.assertEqual(clock.when(datetime(2026, 11, 19, 13, 5, tzinfo=timezone.utc), now, timezone.utc), "yesterday at 13:05")
        self.assertEqual(clock.when(datetime(2026, 11, 16, 13, 5, tzinfo=timezone.utc), now, timezone.utc), "on Monday at 13:05")
        self.assertEqual(clock.when(datetime(2026, 11, 16, 13, 5, tzinfo=timezone.utc), now, timezone.utc, on=False), "Monday at 13:05")


if __name__ == "__main__":
    unittest.main()
