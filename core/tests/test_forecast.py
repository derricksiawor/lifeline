import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

from lifeline.forecast import Forecast, forecast, learn
from lifeline.history import Reading

UTC = timezone.utc
NOW = datetime(2026, 11, 19, 15, 5, tzinfo=UTC)
RESET = datetime(2026, 11, 19, 17, 0, tzinfo=UTC)


def rd(at, five, uuid="a"):
    return Reading(uuid, "usage", five, None, None, None, at)


def series(end, points, step=5):
    n = len(points)
    return [rd(end - timedelta(minutes=step * (n - 1 - i)), p) for i, p in enumerate(points)]


class ForecastTests(unittest.TestCase):
    def test_flat_use_lasts_to_reset(self):
        r = forecast(series(NOW, [40, 40, 40, 40, 40]), RESET, NOW)
        self.assertEqual(r.basis, "recent")
        self.assertEqual(r.pace_per_min, 0.0)
        self.assertEqual(r.at_reset_pct, 40.0)
        self.assertIsNone(r.runs_out_at)

    def test_steep_use_runs_out_before_reset(self):
        r = forecast(series(NOW, [72, 76, 80, 84, 88]), RESET, NOW)
        self.assertAlmostEqual(r.pace_per_min, 0.8)
        self.assertAlmostEqual(r.at_reset_pct, 88 + 0.8 * 115)
        self.assertEqual(r.runs_out_at, NOW + timedelta(minutes=15))
        self.assertLess(r.runs_out_at, RESET)

    def test_slow_climb_does_not_run_out(self):
        r = forecast(series(NOW, [10, 11, 12, 13, 14]), RESET, NOW)
        self.assertAlmostEqual(r.at_reset_pct, 14 + 0.2 * 115)
        self.assertIsNone(r.runs_out_at)

    def test_learned_pace_used_after_7_days(self):
        learned = learn(history_days(7, 0.5), UTC)
        self.assertIn(15, learned)
        r = forecast([rd(NOW - timedelta(minutes=10), 20)], RESET, NOW, learned)
        self.assertEqual(r.basis, "learned")
        self.assertAlmostEqual(r.pace_per_min, learned[15])
        self.assertAlmostEqual(r.at_reset_pct, 20 + learned[15] * 125)

    def test_blended_pace(self):
        r = forecast(series(NOW, [72, 76, 80, 84, 88]), RESET, NOW, {15: 0.2})
        self.assertEqual(r.basis, "blended")
        self.assertAlmostEqual(r.pace_per_min, 0.7 * 0.8 + 0.3 * 0.2)

    def test_extrapolates_from_latest_reading_time(self):
        data = series(NOW - timedelta(minutes=15), [72, 76, 80, 84, 88])
        r = forecast(data, RESET, NOW)
        self.assertAlmostEqual(r.at_reset_pct, 88 + 0.8 * 130)
        self.assertEqual(r.runs_out_at, NOW)

    def test_runs_out_measured_from_latest_reading(self):
        data = series(NOW - timedelta(minutes=5), [60, 62, 64, 66, 68])
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.runs_out_at, NOW - timedelta(minutes=5) + timedelta(minutes=80))

    def test_flat_recent_pace_not_pushed_by_learned(self):
        r = forecast(series(NOW, [40, 40, 40, 40, 40]), RESET, NOW, {15: 2.0})
        self.assertEqual((r.basis, r.pace_per_min, r.at_reset_pct), ("recent", 0.0, 40.0))
        self.assertIsNone(r.runs_out_at)

    def test_falling_recent_pace_not_pushed_by_learned(self):
        r = forecast(series(NOW, [50, 49.8, 49.6, 49.4, 49.2]), RESET, NOW, {15: 2.0})
        self.assertEqual((r.basis, r.pace_per_min), ("recent", 0.0))

    def test_near_zero_positive_pace_does_not_overflow(self):
        r = forecast([rd(NOW - timedelta(minutes=10), 20)], RESET, NOW, {15: 1e-12})
        self.assertEqual(r.basis, "learned")
        self.assertIsNone(r.runs_out_at)
        data = series(NOW, [40, 40 + 1e-9, 40 + 2e-9, 40 + 3e-9, 40 + 4e-9])
        r = forecast(data, RESET, NOW)
        self.assertGreater(r.pace_per_min, 0)
        self.assertIsNone(r.runs_out_at)

    def test_hour_argument_selects_learned_hour(self):
        data = series(NOW, [72, 76, 80, 84, 88])
        r = forecast(data, RESET, NOW, {9: 0.0, 15: 5.0}, hour=9)
        self.assertAlmostEqual(r.pace_per_min, 0.7 * 0.8)

    def test_learned_ignored_when_latest_reading_stale(self):
        r = forecast([rd(NOW - timedelta(minutes=30), 20)], RESET, NOW, {15: 0.5})
        self.assertEqual(r.basis, "none")
        self.assertEqual(r.at_reset_pct, 20.0)
        self.assertIsNone(r.runs_out_at)
        self.assertIsNone(r.pace_per_min)

    def test_learned_used_just_under_30_minutes(self):
        r = forecast([rd(NOW - timedelta(minutes=29), 20)], RESET, NOW, {15: 0.5})
        self.assertEqual(r.basis, "learned")

    def test_single_reading_without_learning(self):
        r = forecast([rd(NOW, 33)], RESET, NOW)
        self.assertEqual((r.basis, r.at_reset_pct, r.runs_out_at), ("none", 33.0, None))

    def test_readings_one_minute_apart_have_no_recent_pace(self):
        data = [rd(NOW - timedelta(minutes=1), 30), rd(NOW, 40)]
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.basis, "none")
        self.assertEqual(r.at_reset_pct, 40.0)

    def test_three_minutes_apart_gives_recent_pace(self):
        data = [rd(NOW - timedelta(minutes=3), 30), rd(NOW, 33)]
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.basis, "recent")
        self.assertAlmostEqual(r.pace_per_min, 1.0)

    def test_negative_slope_counts_as_zero(self):
        data = series(NOW, [50, 50.5, 50, 49.5, 49.2])
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.pace_per_min, 0.0)
        self.assertEqual(r.at_reset_pct, 49.2)

    def test_reset_inside_readings_uses_only_after_it(self):
        data = series(NOW, [90, 95, 2, 4, 6, 8, 10])
        r = forecast(data, RESET, NOW)
        self.assertAlmostEqual(r.pace_per_min, 0.4)
        self.assertAlmostEqual(r.at_reset_pct, 10 + 0.4 * 115)

    def test_drop_of_one_point_is_not_a_reset(self):
        data = series(NOW, [50, 49, 51, 53])
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.basis, "recent")
        self.assertGreater(r.pace_per_min, 0)

    def test_readings_outside_window_ignored(self):
        old = [rd(RESET - timedelta(hours=6), 99), rd(RESET - timedelta(hours=5, minutes=1), 98)]
        future = [rd(NOW + timedelta(minutes=5), 97)]
        data = old + series(NOW, [10, 10, 10]) + future
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.at_reset_pct, 10.0)
        self.assertIsNone(r.runs_out_at)

    def test_reading_exactly_at_window_start_counts(self):
        start = RESET - timedelta(hours=5)
        now = start + timedelta(minutes=10)
        data = [rd(start, 5), rd(now, 15)]
        r = forecast(data, RESET, now)
        self.assertEqual(r.basis, "recent")
        self.assertAlmostEqual(r.pace_per_min, 1.0)

    def test_none_five_ignored(self):
        data = series(NOW, [10, 12, 14])
        data.insert(1, Reading("a", "usage", None, None, None, None, NOW - timedelta(minutes=7)))
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.at_reset_pct, 14 + 0.4 * 115)

    def test_unsorted_readings_handled(self):
        data = list(reversed(series(NOW, [72, 76, 80, 84, 88])))
        self.assertAlmostEqual(forecast(data, RESET, NOW).pace_per_min, 0.8)

    def test_already_at_100_reports_first_reading_at_100(self):
        data = series(NOW, [90, 95, 100, 100, 100])
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.runs_out_at, NOW - timedelta(minutes=10))
        self.assertGreaterEqual(r.at_reset_pct, 100)

    def test_already_at_100_flat(self):
        data = series(NOW, [100, 100, 100])
        r = forecast(data, RESET, NOW)
        self.assertEqual(r.runs_out_at, NOW - timedelta(minutes=10))
        self.assertEqual(r.at_reset_pct, 100.0)

    def test_no_reset_at(self):
        r = forecast(series(NOW, [10, 20, 30]), None, NOW)
        self.assertEqual((r.at_reset_pct, r.runs_out_at, r.basis, r.pace_per_min), (30.0, None, "none", None))

    def test_no_reset_at_and_no_readings(self):
        r = forecast([], None, NOW)
        self.assertEqual((r.at_reset_pct, r.runs_out_at, r.basis), (0.0, None, "none"))

    def test_no_readings_in_window(self):
        r = forecast([], RESET, NOW)
        self.assertEqual((r.at_reset_pct, r.runs_out_at, r.pace_per_min, r.basis), (None, None, None, "none"))

    def test_reset_already_passed_adds_nothing(self):
        now = RESET + timedelta(minutes=5)
        r = forecast(series(now, [10, 12, 14]), RESET, now)
        self.assertEqual(r.at_reset_pct, 14.0)

    def test_runs_out_after_reset_is_none(self):
        data = series(NOW, [10, 10.5, 11, 11.5, 12])
        r = forecast(data, NOW + timedelta(minutes=30), NOW)
        self.assertIsNone(r.runs_out_at)

    def test_only_last_20_minutes_set_the_pace(self):
        data = series(NOW, [10, 10, 10, 10, 10, 10, 10, 20, 30, 40, 50], step=5)
        r = forecast(data, RESET, NOW)
        self.assertAlmostEqual(r.pace_per_min, 2.0)

    def test_forecast_is_frozen(self):
        r = forecast([rd(NOW, 1)], RESET, NOW)
        with self.assertRaises(FrozenInstanceError):
            r.basis = "x"

    def test_naive_datetimes_refused(self):
        naive = datetime(2026, 11, 19, 15, 5)
        data = [rd(NOW, 1)]
        with self.assertRaises(ValueError):
            forecast(data, RESET, naive)
        with self.assertRaises(ValueError):
            forecast(data, naive, NOW)

    def test_invalid_hour_refused(self):
        with self.assertRaises(ValueError):
            forecast([rd(NOW, 1)], RESET, NOW, {}, hour=24)

    def test_default_hour_is_local_hour_of_now(self):
        tz = timezone(timedelta(hours=2))
        now = NOW.astimezone(tz)
        r = forecast([rd(NOW, 20)], RESET, now, {17: 0.5})
        self.assertEqual(r.basis, "learned")


def history_days(days, rate, hour=15, end=None, uuid="a"):
    end = end or NOW
    out = []
    for d in range(days):
        base = (end - timedelta(days=d)).replace(hour=hour, minute=0)
        for i in range(4):
            out.append(rd(base + timedelta(minutes=5 * i), 10 + 5 * rate * i))
    return sorted(out, key=lambda x: x.at)


class WeekForecastTests(unittest.TestCase):
    def week_rd(self, at, week):
        return Reading("a", "usage", None, None, week, None, at)

    def test_a_week_forecast_uses_the_week_and_the_last_day_of_pace(self):
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        reset = now + timedelta(days=2)
        readings = [self.week_rd(now - timedelta(hours=h), 80.0 - h) for h in range(6, -1, -1)]
        found = forecast(readings, reset, now, value="week", window=timedelta(days=7), recent=timedelta(hours=24), min_span=timedelta(hours=1))
        self.assertAlmostEqual(found.pace_per_min, 1 / 60)
        self.assertEqual(found.runs_out_at, now + timedelta(hours=20))
        self.assertAlmostEqual(found.at_reset_pct, 80.0 + 48 * 60 / 60)
        self.assertEqual(found.basis, "recent")

    def test_a_week_that_will_last_has_no_run_out(self):
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        reset = now + timedelta(hours=10)
        readings = [self.week_rd(now - timedelta(hours=h), 50.0 - h) for h in range(6, -1, -1)]
        found = forecast(readings, reset, now, value="week", window=timedelta(days=7), recent=timedelta(hours=24), min_span=timedelta(hours=1))
        self.assertIsNone(found.runs_out_at)
        self.assertAlmostEqual(found.at_reset_pct, 60.0)

    def test_five_hour_figures_are_ignored_by_a_week_forecast(self):
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        readings = [Reading("a", "usage", 99.0, None, None, None, now - timedelta(minutes=m)) for m in (10, 5, 0)]
        found = forecast(readings, now + timedelta(days=1), now, value="week", window=timedelta(days=7))
        self.assertEqual(found, Forecast(None, None, None, "none"))


class StaleReadingTests(unittest.TestCase):
    def test_interleaved_stale_week_rows_keep_the_pace(self):
        reset = NOW + timedelta(days=1)
        data = []
        for i in range(24):
            at = NOW - timedelta(minutes=10 * (23 - i))
            data.append(Reading("p", "usage", None, None, 70.0 + 0.5 * i, reset, at))
            data.append(Reading("p", "statusline", None, None, 64.0, reset, at + timedelta(minutes=5)))
        from lifeline.forecast import WEEK_MIN_SPAN, WEEK_RECENT, WEEK_WINDOW

        r = forecast(data, reset, NOW, value="week", window=WEEK_WINDOW, recent=WEEK_RECENT, min_span=WEEK_MIN_SPAN)
        self.assertEqual(r.basis, "recent")
        self.assertAlmostEqual(r.pace_per_min, 0.05, places=3)
        self.assertIsNotNone(r.runs_out_at)

    def test_a_later_reset_time_starts_a_new_window(self):
        first, second = NOW + timedelta(minutes=30), NOW + timedelta(hours=4)
        data = [Reading("a", "usage", v, first, None, None, NOW - timedelta(minutes=50 - 5 * i)) for i, v in enumerate((80, 85, 90))]
        data += [Reading("a", "usage", v, second, None, None, NOW - timedelta(minutes=30 - 5 * i)) for i, v in enumerate((2, 4, 6, 8, 10, 12, 14))]
        r = forecast(data, second, NOW)
        self.assertAlmostEqual(r.pace_per_min, 0.4)
        self.assertAlmostEqual(r.at_reset_pct, 14 + 0.4 * 240)

    def test_a_new_reset_time_starts_a_new_window_without_any_drop(self):
        first, second = NOW + timedelta(minutes=10), NOW + timedelta(hours=4)
        data = [Reading("a", "usage", v, first, None, None, NOW - timedelta(minutes=40 - 5 * i)) for i, v in enumerate((2, 6, 10))]
        data += [Reading("a", "usage", v, second, None, None, NOW - timedelta(minutes=15 - 5 * i)) for i, v in enumerate((10, 11, 12, 13))]
        r = forecast(data, second, NOW)
        self.assertAlmostEqual(r.pace_per_min, 0.2)

    def test_a_drop_with_the_same_reset_time_is_not_a_reset(self):
        data = [
            Reading("a", "statusline" if v == 40 else "usage", v, RESET, None, None, NOW - timedelta(minutes=5 * (6 - i)))
            for i, v in enumerate((60, 62, 40, 66, 68, 40, 72))
        ]
        r = forecast(data, RESET, NOW)
        self.assertAlmostEqual(r.pace_per_min, 0.4)
        self.assertAlmostEqual(r.at_reset_pct, 72 + 0.4 * 115)
        self.assertIsNone(r.restarted_at)

    def test_a_usage_drop_with_the_same_reset_time_is_an_early_reset(self):
        early = NOW - timedelta(minutes=15)
        data = [Reading("a", "usage", v, RESET, None, None, NOW - timedelta(minutes=40 - 5 * i)) for i, v in enumerate((70, 75, 80, 85))]
        data += [Reading("a", "usage", v, RESET, None, None, early + timedelta(minutes=5 * i)) for i, v in enumerate((0, 2, 4, 6))]
        r = forecast(data, RESET, NOW)
        self.assertAlmostEqual(r.pace_per_min, 0.4)
        self.assertAlmostEqual(r.at_reset_pct, 6 + 0.4 * 115)
        self.assertEqual(r.restarted_at, early)
        self.assertIsNone(forecast(data[:4], RESET, NOW).restarted_at)


class UsageOnlyResetTests(unittest.TestCase):
    def test_a_usage_reading_below_live_status_line_posts_is_not_an_early_reset(self):
        start = NOW - timedelta(minutes=10)
        data = [Reading("u", "usage", 60.0, RESET, None, None, start)]
        data += [Reading("u", "statusline", v, RESET, None, None, start + timedelta(minutes=1 + i)) for i, v in enumerate((62.0, 64.0, 66.0, 68.0, 70.0))]
        data.append(Reading("u", "usage", 68.5, RESET, None, None, start + timedelta(minutes=5, seconds=30)))
        data.append(Reading("u", "statusline", 71.0, RESET, None, None, start + timedelta(minutes=6)))
        r = forecast(data, RESET, NOW)
        self.assertIsNone(r.restarted_at)
        self.assertEqual(r.basis, "recent")
        self.assertIsNotNone(r.pace_per_min)
        self.assertGreater(r.pace_per_min, 0)

    def test_a_usage_drop_below_earlier_usage_is_still_an_early_reset(self):
        start = NOW - timedelta(minutes=30)
        data = [Reading("u", "usage", 80.0, RESET, None, None, start)]
        data += [Reading("u", "statusline", 85.0, RESET, None, None, start + timedelta(minutes=2))]
        early = start + timedelta(minutes=5)
        data += [Reading("u", "usage", v, RESET, None, None, early + timedelta(minutes=5 * i)) for i, v in enumerate((0, 2, 4, 6))]
        self.assertEqual(forecast(data, RESET, NOW).restarted_at, early)


class BindingTests(unittest.TestCase):
    def test_the_binding_window_is_the_one_with_the_least_room(self):
        from lifeline.forecast import binding

        five_reset, week_reset = NOW + timedelta(hours=2), NOW + timedelta(days=6)
        cases = (
            ((42.0, five_reset, 18.0, week_reset), "five"),
            ((0.0, five_reset, 100.0, week_reset), "week"),
            ((10.0, five_reset, 95.0, week_reset), "week"),
            ((60.0, five_reset, 60.0, week_reset), "five"),
            ((100.0, five_reset, 100.0, week_reset), "week"),
            ((104.0, five_reset, 100.0, week_reset), "week"),
            ((100.0, five_reset, 100.4, week_reset), "week"),
            ((104.0, five_reset, 99.0, week_reset), "five"),
            ((None, None, 57.0, week_reset), "week"),
            ((42.0, five_reset, None, None), "five"),
            ((None, None, None, None), "five"),
            ((0.0, None, 30.0, week_reset), "week"),
            ((10.0, five_reset, 100.0, NOW - timedelta(minutes=1)), "five"),
            ((100.0, NOW - timedelta(minutes=1), 40.0, week_reset), "week"),
        )
        for (five, five_at, week, week_at), expected in cases:
            with self.subTest(five=five, week=week, week_at=week_at, five_at=five_at):
                self.assertEqual(binding(five, five_at, week, week_at, NOW), expected)

    def test_the_room_is_on_the_fullest_window_whose_reset_is_ahead(self):
        from lifeline.forecast import fullest

        five_reset, week_reset = NOW + timedelta(hours=2), NOW + timedelta(days=6)
        self.assertEqual(fullest(0.0, five_reset, 100.0, week_reset, NOW), 100.0)
        self.assertEqual(fullest(10.0, five_reset, 95.0, week_reset, NOW), 95.0)
        self.assertEqual(fullest(10.0, five_reset, 100.0, NOW, NOW), 10.0)
        self.assertEqual(fullest(None, None, 57.0, week_reset, NOW), 57.0)
        self.assertIsNone(fullest(None, None, None, None, NOW))


class LearnTests(unittest.TestCase):
    def test_seven_days_kept_and_value_is_average_climb(self):
        got = learn(history_days(7, 0.5), UTC)
        self.assertEqual(list(got), [15])
        self.assertAlmostEqual(got[15], 0.5)

    def test_six_days_left_out(self):
        self.assertEqual(learn(history_days(6, 0.5), UTC), {})

    def test_hours_judged_separately(self):
        data = history_days(7, 0.5, hour=15) + history_days(6, 0.9, hour=9)
        got = learn(sorted(data, key=lambda x: x.at), UTC)
        self.assertEqual(list(got), [15])

    def test_data_older_than_28_days_ignored(self):
        old = history_days(10, 2.0, end=NOW - timedelta(days=40))
        self.assertEqual(learn(old + history_days(3, 0.5), UTC), {})
        mixed = learn(old + history_days(7, 0.5), UTC)
        self.assertAlmostEqual(mixed[15], 0.5)

    def test_days_just_inside_28_days_count(self):
        data = history_days(7, 0.5, end=NOW - timedelta(days=21))
        data += [rd(NOW, 10)]
        self.assertIn(15, learn(sorted(data, key=lambda x: x.at), UTC))

    def test_reset_pair_not_counted_as_climb(self):
        data = history_days(7, 0.5)
        extra = []
        for d in range(7):
            base = (NOW - timedelta(days=d)).replace(hour=15, minute=30)
            extra += [rd(base - timedelta(minutes=10), 20), rd(base - timedelta(minutes=5), 21), rd(base, 1), rd(base + timedelta(minutes=5), 2)]
        got = learn(sorted(data + extra, key=lambda x: x.at), UTC)
        self.assertLess(got[15], 0.6)
        self.assertGreater(got[15], 0.3)

    def test_gap_over_30_minutes_not_counted(self):
        data = []
        for d in range(7):
            base = (NOW - timedelta(days=d)).replace(hour=15, minute=0)
            data += [rd(base, 10), rd(base + timedelta(minutes=40), 90)]
        self.assertEqual(learn(sorted(data, key=lambda x: x.at), UTC), {})

    def test_local_hour_follows_timezone(self):
        tz = timezone(timedelta(hours=3))
        got = learn(history_days(7, 0.5), tz)
        self.assertEqual(list(got), [18])

    def test_cutoff_measured_from_now_at_exactly_28_days(self):
        data = history_days(6, 0.5, end=NOW - timedelta(days=1))
        edge = NOW - timedelta(days=28)
        data += [rd(edge + timedelta(minutes=5 * i), 10 + 2.5 * i) for i in range(4)]
        data = sorted(data, key=lambda x: x.at)
        self.assertIn(15, learn(data, UTC, now=NOW))

    def test_cutoff_measured_from_now_at_29_days(self):
        data = history_days(6, 0.5, end=NOW - timedelta(days=1))
        edge = NOW - timedelta(days=29)
        data += [rd(edge + timedelta(minutes=5 * i), 10 + 2.5 * i) for i in range(4)]
        data = sorted(data, key=lambda x: x.at)
        self.assertEqual(learn(data, UTC, now=NOW), {})

    def test_now_later_than_newest_reading_ages_data_out(self):
        data = history_days(7, 0.5)
        self.assertIn(15, learn(data, UTC))
        self.assertEqual(learn(data, UTC, now=NOW + timedelta(days=30)), {})

    def test_learn_naive_now_refused(self):
        with self.assertRaises(ValueError):
            learn([], UTC, now=datetime(2026, 11, 19))

    def test_empty(self):
        self.assertEqual(learn([], UTC), {})

    def test_negative_climb_counts_as_zero(self):
        data = []
        for d in range(7):
            base = (NOW - timedelta(days=d)).replace(hour=15, minute=0)
            data += [rd(base, 50), rd(base + timedelta(minutes=5), 49.5)]
        got = learn(sorted(data, key=lambda x: x.at), UTC)
        self.assertEqual(got[15], 0.0)

    def test_same_time_readings_skipped(self):
        data = history_days(7, 0.5)
        data.append(rd(data[0].at, 99))
        got = learn(sorted(data, key=lambda x: x.at), UTC)
        self.assertIn(15, got)

    def days_of(self, build):
        data = []
        for d in range(7):
            base = (NOW - timedelta(days=d)).replace(hour=15, minute=0)
            data += build(base, lambda minutes: base + timedelta(minutes=minutes))
        return learn(sorted(data, key=lambda x: x.at), UTC)

    @staticmethod
    def seen(source, at, five, reset):
        return Reading("a", source, five, reset, None, None, at)

    def test_a_stale_status_line_value_never_splits_a_window(self):
        def build(base, at):
            reset = base + timedelta(hours=3)
            return [
                self.seen("usage", at(0), 10, reset),
                self.seen("usage", at(5), 12.5, reset),
                self.seen("statusline", at(7), 1, reset),
                self.seen("usage", at(10), 15, reset),
            ]

        self.assertAlmostEqual(self.days_of(build)[15], 0.5)

        def unreset(base, at):
            return [
                self.seen("usage", at(0), 10, None),
                self.seen("statusline", at(3), 1, None),
                self.seen("usage", at(5), 12.5, None),
                self.seen("usage", at(10), 15, None),
            ]

        self.assertAlmostEqual(self.days_of(unreset)[15], 0.5)

    def test_a_reset_that_moves_forward_splits_the_window_even_without_a_drop(self):
        def build(base, at):
            first = base + timedelta(minutes=7)
            second = first + timedelta(hours=5)
            return [
                self.seen("usage", at(0), 10, first),
                self.seen("usage", at(5), 12.5, first),
                self.seen("usage", at(10), 30, second),
                self.seen("usage", at(15), 32.5, second),
            ]

        self.assertAlmostEqual(self.days_of(build)[15], 0.5)

    def test_an_early_reset_seen_by_usage_readings_splits_the_window(self):
        def build(base, at):
            reset = base + timedelta(hours=3)
            return [
                self.seen("usage", at(0), 40, reset),
                self.seen("usage", at(5), 42.5, reset),
                self.seen("usage", at(10), 2, reset),
                self.seen("usage", at(15), 4.5, reset),
            ]

        self.assertAlmostEqual(self.days_of(build)[15], 0.5)

    def test_a_value_from_an_older_window_is_left_out(self):
        def build(base, at):
            reset = base + timedelta(hours=3)
            return [
                self.seen("usage", at(0), 10, reset),
                self.seen("statusline", at(3), 80, reset - timedelta(hours=5)),
                self.seen("usage", at(5), 12.5, reset),
                self.seen("usage", at(10), 15, reset),
            ]

        self.assertAlmostEqual(self.days_of(build)[15], 0.5)


if __name__ == "__main__":
    unittest.main()
