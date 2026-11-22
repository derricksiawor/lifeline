import math
import re
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lifeline import history
from lifeline.history import History, Reading

CORE = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "data"
        self.path = self.dir / "history.db"
        self.h = History(self.path)

    def add(self, uuid="a", source="usage", five=0.1, week=0.2, at=T0, five_reset=None, week_reset=None):
        return self.h.add_reading(uuid, source, five, five_reset, week, week_reset, at)


class ReadingTests(HistoryCase):
    def test_add_returns_reading_and_latest_matches(self):
        r = self.add(five=0.5, week=0.25, five_reset=T0 + timedelta(hours=2), week_reset=T0 + timedelta(days=3))
        self.assertIsInstance(r, Reading)
        self.assertEqual(self.h.latest("a"), r)
        self.assertEqual(r.five_reset, T0 + timedelta(hours=2))
        self.assertEqual(r.week_reset.tzinfo, timezone.utc)

    def test_latest_none_when_empty(self):
        self.assertIsNone(self.h.latest("nobody"))

    def test_latest_prefers_newest_any_source(self):
        self.add(source="usage", five=0.1, at=T0)
        self.add(source="statusline", five=0.2, at=T0 + timedelta(minutes=5))
        self.add(source="cache", five=0.3, at=T0 + timedelta(minutes=1))
        got = self.h.latest("a")
        self.assertEqual(got.source, "statusline")
        self.assertEqual(got.five, 0.2)

    def test_latest_equal_timestamps_newest_insert_wins(self):
        self.add(source="usage", five=0.1)
        self.add(source="cache", five=0.2)
        self.add(source="statusline", five=0.3)
        self.assertEqual(self.h.latest("a").five, 0.3)

    def test_latest_is_per_account(self):
        self.add(uuid="a", five=0.1)
        self.add(uuid="b", five=0.9, at=T0 + timedelta(hours=1))
        self.assertEqual(self.h.latest("a").five, 0.1)

    def test_readings_since_inclusive_oldest_first(self):
        self.add(five=0.3, at=T0 + timedelta(minutes=10))
        self.add(five=0.1, at=T0)
        self.add(five=0.2, at=T0 + timedelta(minutes=5))
        got = self.h.readings("a", T0 + timedelta(minutes=5))
        self.assertEqual([r.five for r in got], [0.2, 0.3])
        self.assertEqual([r.five for r in self.h.readings("a", T0)], [0.1, 0.2, 0.3])
        self.assertEqual(self.h.readings("a", T0 + timedelta(hours=1)), [])
        self.assertEqual(self.h.readings("zzz", T0), [])

    def test_readings_equal_timestamps_keep_insertion_order(self):
        for v in (0.1, 0.2, 0.3):
            self.add(five=v)
        self.assertEqual([r.five for r in self.h.readings("a", T0)], [0.1, 0.2, 0.3])

    def test_since_with_microseconds_excludes_earlier_millisecond(self):
        self.add(at=T0)
        self.assertEqual(self.h.readings("a", T0 + timedelta(microseconds=500)), [])
        self.assertEqual(len(self.h.readings("a", T0)), 1)

    def test_none_values_roundtrip(self):
        r = self.h.add_reading("a", "cache", None, None, None, None, T0)
        got = self.h.latest("a")
        self.assertEqual(got, r)
        self.assertIsNone(got.five)
        self.assertIsNone(got.week_reset)

    def test_partial_none(self):
        self.add(five=None, week=0.4, week_reset=T0)
        got = self.h.latest("a")
        self.assertIsNone(got.five)
        self.assertEqual(got.week, 0.4)

    def test_integer_values_become_float(self):
        r = self.add(five=1, week=0)
        self.assertIsInstance(r.five, float)
        self.assertEqual(self.h.latest("a").week, 0.0)

    def test_aware_non_utc_is_converted_to_utc(self):
        local = timezone(timedelta(hours=5, minutes=30))
        r = self.add(at=datetime(2026, 11, 19, 15, 30, tzinfo=local))
        self.assertEqual(r.at, T0)
        self.assertEqual(r.at.utcoffset(), timedelta(0))

    def test_millisecond_precision_kept(self):
        at = T0 + timedelta(milliseconds=123)
        self.add(at=at)
        self.assertEqual(self.h.latest("a").at, at)

    def test_pre_epoch_and_far_future(self):
        old = datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc)
        self.add(at=old)
        self.assertEqual(self.h.latest("a").at, old)
        far = datetime(9999, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc)
        self.add(uuid="b", at=far)
        self.assertEqual(self.h.latest("b").at, far)
        self.assertEqual(len(self.h.readings("b", far)), 1)

    def test_overflow_becomes_value_error(self):
        east = timezone(timedelta(hours=5))
        edge = datetime.min.replace(tzinfo=east)
        with self.assertRaises(ValueError):
            self.add(at=edge)
        with self.assertRaises(ValueError):
            self.h.readings("a", edge)
        with self.assertRaises(ValueError):
            self.h.add_event("k", edge)
        self.assertIsNone(self.h.latest("a"))

    def test_reading_validates_numbers(self):
        for bad in (-0.1, math.nan, math.inf, "1", True):
            with self.assertRaises(ValueError):
                Reading("a", "usage", bad, None, None, None, T0)
            with self.assertRaises(ValueError):
                Reading("a", "usage", None, None, bad, None, T0)
        self.assertEqual(Reading("a", "usage", 0, None, 1.5, None, T0).week, 1.5)

    def test_negative_value_rejected_by_add(self):
        with self.assertRaises(ValueError):
            self.add(five=-0.01)
        self.assertIsNone(self.h.latest("a"))

    def test_naive_datetimes_rejected(self):
        naive = datetime(2026, 11, 19, 10, 0, 0)
        with self.assertRaises(ValueError):
            self.add(at=naive)
        with self.assertRaises(ValueError):
            self.add(five_reset=naive)
        with self.assertRaises(ValueError):
            self.add(week_reset=naive)
        with self.assertRaises(ValueError):
            self.h.readings("a", naive)
        with self.assertRaises(ValueError):
            self.h.add_event("k", naive)
        self.assertIsNone(self.h.latest("a"))
        self.assertTrue(self.h.add_event("k", T0))

    def test_non_datetime_rejected(self):
        with self.assertRaises(ValueError):
            self.add(at="2026-11-19")
        with self.assertRaises(ValueError):
            self.add(at=None)

    def test_unknown_source_rejected_and_nothing_stored(self):
        for bad in ("Usage", "", "api", None, 3):
            with self.assertRaises(ValueError):
                self.add(source=bad)
        self.assertIsNone(self.h.latest("a"))

    def test_all_three_sources_accepted(self):
        for s in ("usage", "statusline", "cache"):
            self.assertEqual(self.add(source=s).source, s)

    def test_bad_numbers_rejected(self):
        for bad in (math.nan, math.inf, -math.inf, "0.5"):
            with self.assertRaises(ValueError):
                self.add(five=bad)
            with self.assertRaises(ValueError):
                self.add(week=bad)
        self.assertEqual(self.h.readings("a", T0 - timedelta(days=1)), [])

    def test_bad_uuid_rejected(self):
        for bad in ("", None, 5):
            with self.assertRaises(ValueError):
                self.add(uuid=bad)

    def test_reading_is_frozen(self):
        r = self.add()
        with self.assertRaises(Exception):
            r.five = 0.9

    def test_reading_construction_validates(self):
        with self.assertRaises(ValueError):
            Reading("a", "bogus", None, None, None, None, T0)
        with self.assertRaises(ValueError):
            Reading("a", "usage", None, None, None, None, datetime(2026, 1, 1))

    def test_failed_insert_leaves_no_partial_row(self):
        real = sqlite3.connect

        def broken(*a, **k):
            conn = real(*a, **k)
            conn.execute("create trigger boom before insert on readings begin select raise(abort, 'boom'); end")
            return conn

        with mock.patch.object(history.sqlite3, "connect", broken):
            with self.assertRaises(sqlite3.DatabaseError):
                self.add()
        self.assertIsNone(self.h.latest("a"))
        con = sqlite3.connect(self.path)
        con.execute("drop trigger boom")
        con.commit()
        con.close()
        self.assertTrue(self.add())
        self.assertEqual(len(self.h.readings("a", T0)), 1)

    def test_keeps_every_row(self):
        for i in range(500):
            self.add(five=(i % 100) / 100, at=T0 + timedelta(minutes=i))
        self.assertEqual(len(self.h.readings("a", T0)), 500)

    def test_index_on_uuid_and_at(self):
        con = sqlite3.connect(self.path)
        self.addCleanup(con.close)
        found = False
        for row in con.execute("pragma index_list(readings)"):
            cols = [c[2] for c in con.execute(f"pragma index_info({row[1]})")]
            if cols[:2] == ["uuid", "at"]:
                found = True
        self.assertTrue(found)

    def test_busy_timeout_is_five_seconds(self):
        seen = []
        real = sqlite3.connect

        def spy(*a, **k):
            seen.append(k.get("timeout", a[1] if len(a) > 1 else None))
            return real(*a, **k)

        with mock.patch.object(history.sqlite3, "connect", spy):
            self.add()
        self.assertEqual(seen, [5.0])


class NewestTests(HistoryCase):
    def test_newest_by_source_and_by_field(self):
        self.add(source="usage", five=1.0, week=2.0, at=T0)
        self.add(source="statusline", five=None, week=3.0, at=T0 + timedelta(minutes=2))
        self.add(source="cache", five=4.0, week=None, at=T0 + timedelta(minutes=1))
        self.add(source="statusline", five=None, week=None, at=T0 + timedelta(minutes=3))
        self.add(uuid="b", source="usage", five=9.0, week=9.0, at=T0 + timedelta(minutes=4))
        self.assertEqual(self.h.newest("a", T0, sources=("usage", "cache")).five, 4.0)
        self.assertEqual(self.h.newest("a", T0, sources=("usage",)).at, T0)
        self.assertEqual(self.h.newest("a", T0, field="five").five, 4.0)
        self.assertEqual(self.h.newest("a", T0, field="week").week, 3.0)
        self.assertEqual(self.h.newest("a", T0).at, T0 + timedelta(minutes=3))
        self.assertIsNone(self.h.newest("a", T0 + timedelta(minutes=1, seconds=1), sources=("usage", "cache")))
        self.assertIsNone(self.h.newest("nobody", T0))
        with self.assertRaises(ValueError):
            self.h.newest("a", T0, field="five_reset; drop table readings")
        with self.assertRaises(ValueError):
            self.h.newest("a", T0, sources=("nonsense",))

    def test_newest_reads_newest_first_through_the_index_and_every_check_uses_an_index(self):
        plans = []
        real = self.h._connect

        def connect():
            conn = real()
            conn.set_trace_callback(plans.append)
            return conn

        self.add(source="usage", at=T0, five_reset=T0 + timedelta(hours=2), week_reset=T0 + timedelta(days=3))
        self.add(source="statusline", at=T0 + timedelta(minutes=1), five_reset=T0 + timedelta(hours=2), week_reset=T0 + timedelta(days=3))
        with mock.patch.object(self.h, "_connect", connect):
            self.assertEqual(self.h.newest("a", T0, sources=("usage", "cache")).source, "usage")
            self.assertEqual(self.h.newest("a", T0, field="five").source, "statusline")
        selects = [sql for sql in plans if sql.lower().startswith("select")]
        rows = [sql for sql in selects if sql.startswith("select id, uuid, source") and "order by at desc" in sql]
        checks = [sql for sql in selects if sql not in rows]
        self.assertEqual(len(rows), 2)
        self.assertTrue(checks)
        with sqlite3.connect(self.path) as conn:
            for sql in rows:
                self.assertIn("readings_uuid_at", " ".join(str(row) for row in conn.execute("explain query plan " + sql)))
            for sql in checks:
                plan = " ".join(str(row) for row in conn.execute("explain query plan " + sql))
                self.assertIn("INDEX readings_uuid_", plan, sql)
                self.assertNotIn("SCAN readings", plan.replace("USING", "").split("INDEX")[0], sql)

    def test_status_line_readings_keep_one_row_per_account_per_minute(self):
        base = T0 + timedelta(seconds=5)
        for seconds, five in ((0, 10.0), (20, 11.0), (50, 12.0)):
            self.h.add_reading("a", "statusline", five, None, 1.0, None, base + timedelta(seconds=seconds), per_minute=True)
        self.h.add_reading("a", "statusline", 9.0, None, 1.0, None, base + timedelta(seconds=10), per_minute=True)
        self.h.add_reading("a", "statusline", 13.0, None, 1.0, None, base + timedelta(seconds=60), per_minute=True)
        self.h.add_reading("b", "statusline", 30.0, None, 1.0, None, base, per_minute=True)
        self.h.add_reading("a", "usage", 14.0, None, 1.0, None, base + timedelta(seconds=61))
        self.h.add_reading("a", "usage", 15.0, None, 1.0, None, base + timedelta(seconds=62))
        rows = self.h.readings("a", T0)
        self.assertEqual([(r.source, r.five) for r in rows], [("statusline", 12.0), ("statusline", 13.0), ("usage", 14.0), ("usage", 15.0)])
        self.assertEqual(rows[0].at, base + timedelta(seconds=50))
        self.assertEqual(len(self.h.readings("b", T0)), 1)


class StaleTests(HistoryCase):
    def test_a_stale_post_is_not_stored(self):
        reset = T0 + timedelta(days=5)
        self.assertIsNotNone(self.h.add_reading("p", "usage", None, None, 81.0, reset, T0))
        self.assertIsNone(self.h.add_reading("p", "statusline", None, None, 75.0, reset + timedelta(milliseconds=300), T0 + timedelta(minutes=1)))
        self.assertIsNotNone(self.h.add_reading("p", "statusline", None, None, 80.2, reset, T0 + timedelta(minutes=2)))
        self.assertIsNone(self.h.add_reading("p", "statusline", None, None, 90.0, reset - timedelta(days=7), T0 + timedelta(minutes=3)))
        self.assertIsNotNone(self.h.add_reading("p", "usage", None, None, 4.0, reset + timedelta(days=7), T0 + timedelta(minutes=4)))
        self.assertIsNone(self.h.add_reading("p", "statusline", None, None, 82.0, reset, T0 + timedelta(minutes=5)))
        self.assertIsNotNone(self.h.add_reading("q", "statusline", None, None, 10.0, reset, T0 + timedelta(minutes=5)))
        self.assertEqual([r.week for r in self.h.readings("p", T0)], [81.0, 80.2, 4.0])

    def test_a_stale_post_never_replaces_the_row_of_its_minute(self):
        reset = T0 + timedelta(hours=3)
        self.h.add_reading("j", "statusline", 40.0, reset, 10.0, None, T0, per_minute=True)
        self.assertIsNone(self.h.add_reading("j", "statusline", 30.0, reset, 10.0, None, T0 + timedelta(seconds=20), per_minute=True))
        self.assertEqual([r.five for r in self.h.readings("j", T0)], [40.0])

    def test_rows_already_stored_are_filtered_on_read(self):
        reset = T0 + timedelta(days=5)
        older = T0 - timedelta(days=2)
        ms = lambda moment: int(moment.timestamp() * 1000)
        rows = []
        for i in range(10):
            rows.append(("p", "usage", None, None, 72.0 + i, ms(reset), ms(T0 + timedelta(minutes=10 * i))))
            rows.append(("p", "statusline", None, None, 70.0, ms(reset), ms(T0 + timedelta(minutes=10 * i + 5))))
        rows.append(("p", "statusline", None, None, 99.0, ms(older), ms(T0 + timedelta(minutes=96))))
        with sqlite3.connect(self.path) as conn:
            conn.executemany("insert into readings (uuid, source, five, five_reset, week, week_reset, at) values (?,?,?,?,?,?,?)", rows)
        self.assertEqual([r.week for r in self.h.readings("p", T0)], [72.0 + i for i in range(10)])
        self.assertEqual(self.h.latest("p").week, 81.0)
        self.assertEqual(self.h.newest("p", T0, sources=("statusline",)), None)
        self.assertEqual(self.h.newest("p", T0, field="week").week, 81.0)
        self.assertEqual(len(self.h.readings("p", T0, limit=3)), 3)


class EarlyResetTests(HistoryCase):
    def test_a_usage_drop_is_an_early_reset_that_starts_a_new_segment(self):
        reset = T0 + timedelta(days=5)
        self.assertIsNotNone(self.h.add_reading("p", "usage", None, None, 81.0, reset, T0))
        self.assertIsNotNone(self.h.add_reading("p", "usage", None, None, 80.5, reset, T0 + timedelta(minutes=5)))
        self.assertEqual(self.h.latest("p").week, 80.5)
        self.assertIsNotNone(self.h.add_reading("p", "usage", None, None, 0.0, reset, T0 + timedelta(minutes=10)))
        self.assertEqual(self.h.latest("p").week, 0.0)
        self.assertIsNone(self.h.add_reading("p", "statusline", None, None, 81.0, reset, T0 + timedelta(minutes=11)))
        self.assertIsNone(self.h.add_reading("p", "statusline", None, None, 80.0, reset, T0 + timedelta(minutes=12)))
        self.assertIsNotNone(self.h.add_reading("p", "statusline", None, None, 1.0, reset, T0 + timedelta(minutes=13)))
        self.assertIsNotNone(self.h.add_reading("p", "statusline", None, None, 1.8, reset, T0 + timedelta(minutes=14)))
        self.assertIsNone(self.h.add_reading("p", "statusline", None, None, 0.5, reset, T0 + timedelta(minutes=15)))
        self.assertEqual([r.week for r in self.h.readings("p", T0)], [81.0, 80.5, 0.0, 1.0, 1.8])
        self.assertEqual(self.h.latest("p").week, 1.8)

    def test_a_usage_reading_is_never_refused(self):
        reset = T0 + timedelta(days=5)
        self.h.add_reading("p", "statusline", None, None, 81.0, reset, T0)
        self.assertIsNotNone(self.h.add_reading("p", "usage", None, None, 40.0, reset - timedelta(days=7), T0 + timedelta(minutes=1)))
        self.assertIsNotNone(self.h.add_reading("p", "cache", None, None, 10.0, reset, T0 + timedelta(minutes=2)))

    def test_readings_take_their_baseline_from_the_start_of_the_window(self):
        reset = T0 + timedelta(days=3)
        ms = lambda moment: int(moment.timestamp() * 1000)
        rows = [
            ("p", "usage", None, None, 80.0, ms(reset), ms(T0 - timedelta(days=3))),
            ("p", "statusline", None, None, 70.0, ms(reset), ms(T0 - timedelta(days=2) + timedelta(minutes=1))),
            ("p", "usage", None, None, 82.0, ms(reset), ms(T0 - timedelta(days=1))),
        ]
        with sqlite3.connect(self.path) as conn:
            conn.executemany("insert into readings (uuid, source, five, five_reset, week, week_reset, at) values (?,?,?,?,?,?,?)", rows)
        self.assertEqual([r.week for r in self.h.readings("p", T0 - timedelta(days=2))], [82.0])
        self.assertEqual([r.week for r in self.h.readings("p", T0 - timedelta(days=2), fields=())], [70.0, 82.0])


class ConsistencyTests(HistoryCase):
    def test_a_stale_post_stored_before_an_earlier_stamped_usage_row_is_judged_like_the_walk(self):
        reset = T0 + timedelta(days=3)
        self.h.add_reading("u", "usage", None, None, 81.0, reset, T0)
        self.assertIsNotNone(self.h.add_reading("u", "statusline", None, None, 81.0, reset, T0 + timedelta(minutes=5, seconds=2)))
        self.h.add_reading("u", "usage", None, None, 0.0, reset, T0 + timedelta(minutes=5))
        latest = self.h.latest("u")
        self.assertEqual((latest.source, latest.week), ("usage", 0.0))
        self.assertEqual([(r.source, r.week) for r in self.h.readings("u", T0 - timedelta(hours=1))], [("usage", 81.0), ("usage", 0.0)])
        self.assertIsNotNone(self.h.add_reading("u", "statusline", None, None, 0.5, reset, T0 + timedelta(minutes=6)))
        self.assertIsNone(self.h.add_reading("u", "statusline", None, None, 81.0, reset, T0 + timedelta(minutes=6, seconds=5)))
        latest = self.h.latest("u")
        self.assertEqual((latest.source, latest.week), ("statusline", 0.5))
        self.assertEqual(self.h.newest("u", T0, field="week").week, 0.5)
        self.assertEqual([r.week for r in self.h.readings("u", T0 - timedelta(hours=1))], [81.0, 0.0, 0.5])

    def test_newest_for_one_field_judges_only_that_field(self):
        five_reset, week_reset = T0 + timedelta(hours=3), T0 + timedelta(days=3)
        self.h.add_reading("u", "usage", 10.0, five_reset, 50.0, week_reset, T0)
        ms = lambda moment: int(moment.timestamp() * 1000)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "insert into readings (uuid, source, five, five_reset, week, week_reset, at) values (?,?,?,?,?,?,?)",
                ("u", "statusline", 12.0, ms(five_reset), 30.0, ms(week_reset), ms(T0 + timedelta(minutes=1))),
            )
        self.assertEqual(self.h.latest("u").source, "usage")
        self.assertEqual((self.h.newest("u", T0, field="five").source, self.h.newest("u", T0, field="five").five), ("statusline", 12.0))
        self.assertEqual([r.five for r in self.h.readings("u", T0, fields=("five",))], [10.0, 12.0])
        self.assertEqual(self.h.newest("u", T0, field="week").source, "usage")

    def test_an_early_reset_is_judged_only_against_earlier_usage_readings(self):
        reset = T0 + timedelta(hours=3)
        self.h.add_reading("u", "usage", 60.0, reset, None, None, T0)
        for i, value in enumerate((62.0, 64.0, 66.0, 68.0, 70.0)):
            self.assertIsNotNone(self.h.add_reading("u", "statusline", value, reset, None, None, T0 + timedelta(minutes=1 + i)))
        self.h.add_reading("u", "usage", 68.5, reset, None, None, T0 + timedelta(minutes=5, seconds=30))
        self.assertIsNotNone(self.h.add_reading("u", "statusline", 71.0, reset, None, None, T0 + timedelta(minutes=6)))
        latest = self.h.latest("u")
        self.assertEqual((latest.source, latest.five), ("statusline", 71.0))
        self.assertEqual([r.five for r in self.h.readings("u", T0, fields=("five",))], [60.0, 62.0, 64.0, 66.0, 68.0, 70.0, 68.5, 71.0])

    def test_the_stale_check_reads_only_the_rows_of_its_own_window(self):
        reset = T0 + timedelta(hours=3)
        ms = lambda moment: int(moment.timestamp() * 1000)
        old = T0 - timedelta(days=20)
        with sqlite3.connect(self.path) as conn:
            conn.executemany(
                "insert into readings (uuid, source, five, five_reset, week, week_reset, at) values (?,?,?,?,?,?,?)",
                [("u", "statusline", 10.0, ms(old + timedelta(hours=1)), None, None, ms(old + timedelta(minutes=i))) for i in range(200)],
            )
        statements = []
        real = self.h._connect

        def connect():
            conn = real()
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(self.h, "_connect", connect):
            self.h.add_reading("u", "statusline", 20.0, reset, None, None, T0)
            self.h.latest("u")
            self.h.newest("u", T0 - timedelta(days=30), field="five")
        reads = [sql for sql in statements if sql.lower().startswith("select") and "from readings" in sql and "order by at desc, id desc" not in sql]
        self.assertTrue(reads)
        newest_old = ms(old + timedelta(minutes=199))
        for sql in reads:
            bounds = [int(value) for value in re.findall(r"\bat >= (\d+)", sql)]
            self.assertTrue(bounds, sql)
            self.assertTrue(all(bound > newest_old for bound in bounds), sql)


class EventTests(HistoryCase):
    def test_event_at_gives_the_time_an_event_was_first_recorded(self):
        self.assertIsNone(self.h.event_at("later:1"))
        self.h.add_event("later:1", T0)
        self.h.add_event("later:1", T0 + timedelta(hours=2))
        self.assertEqual(self.h.event_at("later:1"), T0)
        self.assertEqual(History(self.h.path).event_at("later:1"), T0)

    def test_event_key_unique(self):
        self.assertTrue(self.h.add_event("a:will-run-out:1", T0))
        self.assertFalse(self.h.add_event("a:will-run-out:1", T0 + timedelta(hours=1)))
        self.assertTrue(self.h.add_event("a:will-run-out:2", T0))

    def test_empty_or_non_string_key_rejected(self):
        for bad in ("", None, 4):
            with self.assertRaises(ValueError):
                self.h.add_event(bad, T0)

    def test_first_time_wins(self):
        self.h.add_event("k", T0)
        self.h.add_event("k", T0 + timedelta(days=1))
        con = sqlite3.connect(self.path)
        self.addCleanup(con.close)
        got = con.execute("select at from events where key = 'k'").fetchall()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], int(T0.timestamp() * 1000))

    def test_threads_racing_one_key_exactly_one_true(self):
        for round_ in range(5):
            key = f"race-{round_}"
            barrier = threading.Barrier(8)
            results = []

            def work():
                barrier.wait()
                results.append(self.h.add_event(key, T0))

            threads = [threading.Thread(target=work) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results), [False] * 7 + [True])

    def test_two_instances_same_file(self):
        other = History(self.path)
        self.assertTrue(self.h.add_event("k", T0))
        self.assertFalse(other.add_event("k", T0))

    def test_processes_racing_one_key_exactly_one_true(self):
        start = time.time() + 1.5
        code = (
            "import sys, time\n"
            "from datetime import datetime, timezone\n"
            "from pathlib import Path\n"
            "from lifeline.history import History\n"
            "h = History(Path(sys.argv[1]))\n"
            "time.sleep(max(0, float(sys.argv[2]) - time.time()))\n"
            "print(h.add_event('shared', datetime(2026, 11, 19, tzinfo=timezone.utc)))\n"
        )
        env = dict(os.environ, PYTHONPATH=str(CORE))
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.path), str(start)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            outs.append(out.strip())
        self.assertEqual(sorted(outs), ["False", "True"])

    def test_many_processes_opening_a_fresh_path_all_succeed(self):
        code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from lifeline.history import History\n"
            "time.sleep(max(0, float(sys.argv[2]) - time.time()))\n"
            "History(Path(sys.argv[1]))\n"
            "print('ok')\n"
        )
        env = dict(os.environ, PYTHONPATH=str(CORE))
        for round_ in range(4):
            path = Path(self._tmp.name) / f"fresh{round_}" / "h.db"
            start = time.time() + 1.5
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(path), str(start)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                for _ in range(10)
            ]
            for p in procs:
                out, err = p.communicate(timeout=90)
                self.assertEqual(p.returncode, 0, err)
                self.assertEqual(out.strip(), "ok")

    def test_threads_writing_readings_all_land(self):
        barrier = threading.Barrier(6)

        def work(i):
            barrier.wait()
            for j in range(20):
                self.add(uuid=f"u{i}", five=j / 100, at=T0 + timedelta(seconds=j))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for i in range(6):
            self.assertEqual(len(self.h.readings(f"u{i}", T0)), 20)

    def test_write_waits_for_a_busy_database(self):
        blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.addCleanup(blocker.close)
        blocker.execute("begin immediate")
        timer = threading.Timer(0.5, lambda: blocker.execute("commit"))
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        self.assertTrue(self.h.add_event("waited", T0))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)


class FileTests(HistoryCase):
    def test_modes_after_creation(self):
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(self.path), 0o600)

    def test_modes_with_permissive_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        path = Path(self._tmp.name) / "other" / "h.db"
        History(path).add_event("k", T0)
        self.assertEqual(mode(path.parent), 0o700)
        self.assertEqual(mode(path), 0o600)

    def test_wal_and_shm_modes_and_journal(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        path = Path(self._tmp.name) / "w" / "h.db"
        h = History(path)
        keeper = sqlite3.connect(path)
        self.addCleanup(keeper.close)
        keeper.execute("select count(*) from readings").fetchall()
        h.add_event("k", T0)
        found = [p for p in path.parent.iterdir() if p.name.startswith("h.db-")]
        self.assertTrue(found)
        for p in found:
            self.assertEqual(mode(p), 0o600, p.name)
        self.assertEqual(keeper.execute("pragma journal_mode").fetchone()[0], "wal")

    def test_existing_loose_file_is_tightened(self):
        self.h.add_event("k", T0)
        os.chmod(self.path, 0o644)
        again = History(self.path)
        again.add_event("j", T0)
        self.assertEqual(mode(self.path), 0o600)

    def test_default_folder_tightened_even_if_it_exists(self):
        folder = Path(self._tmp.name) / "default"
        folder.mkdir()
        os.chmod(folder, 0o755)
        with mock.patch.object(history, "default_path", return_value=folder / "h.db"):
            History()
        self.assertEqual(mode(folder), 0o700)
        self.assertEqual(mode(folder / "h.db"), 0o600)
        self.assertEqual(mode(folder / "h.db.lock"), 0o600)

    def test_existing_parent_folder_is_left_alone(self):
        folder = Path(self._tmp.name) / "shared"
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)
        History(folder / "h.db")
        self.assertEqual(mode(folder), 0o755)

    def test_reopen_keeps_everything(self):
        self.add(five=0.7, at=T0)
        self.h.add_event("k", T0)
        again = History(self.path)
        self.assertEqual(again.latest("a").five, 0.7)
        self.assertFalse(again.add_event("k", T0))
        self.assertEqual(len(again.readings("a", T0)), 1)

    def test_nested_missing_folders_created(self):
        path = Path(self._tmp.name) / "x" / "y" / "z" / "h.db"
        History(path).add_event("k", T0)
        self.assertTrue(path.exists())

    def test_default_path_mac(self):
        with mock.patch.object(history.ident.sys, "platform", "darwin"), mock.patch.object(history.Path, "home", return_value=Path("/h")):
            self.assertEqual(history.default_path(), Path("/h/Library/Application Support/com.example.lifeline/history.db"))

    def test_default_path_linux(self):
        with mock.patch.object(history.ident.sys, "platform", "linux"), mock.patch.object(history.Path, "home", return_value=Path("/h")):
            self.assertEqual(history.default_path(), Path("/h/.local/share/com.example.lifeline/history.db"))

    def test_default_used_when_no_path(self):
        target = Path(self._tmp.name) / "home" / "hist.db"
        with mock.patch.object(history, "default_path", return_value=target):
            History().add_event("k", T0)
        self.assertTrue(target.exists())


class LimitAndPruneTests(HistoryCase):
    def test_readings_take_a_limit_oldest_first(self):
        for minute in range(5):
            self.add(five=float(minute), at=T0 + timedelta(minutes=minute))
        got = self.h.readings("a", T0, limit=2)
        self.assertEqual([r.five for r in got], [0.0, 1.0])
        self.assertEqual(len(self.h.readings("a", T0)), 5)
        self.assertEqual(len(self.h.readings("a", T0, limit=None)), 5)
        for bad in (0, -1, True, 2.5, "3"):
            with self.assertRaises(ValueError, msg=bad):
                self.h.readings("a", T0, limit=bad)

    def test_prune_removes_only_rows_and_events_older_than_the_cut(self):
        cut = T0 - timedelta(days=90)
        self.add(uuid="a", five=1.0, at=cut - timedelta(milliseconds=1))
        self.add(uuid="b", five=2.0, at=cut - timedelta(days=30))
        self.add(uuid="a", five=3.0, at=cut)
        self.add(uuid="a", five=4.0, at=T0)
        self.h.add_event("old", cut - timedelta(seconds=1))
        self.h.add_event("edge", cut)
        self.h.add_event("new", T0)
        self.assertEqual(self.h.prune(cut), (2, 1))
        self.assertEqual([r.five for r in self.h.readings("a", cut - timedelta(days=365))], [3.0, 4.0])
        self.assertEqual(self.h.readings("b", cut - timedelta(days=365)), [])
        self.assertTrue(self.h.add_event("old", T0))
        self.assertFalse(self.h.add_event("edge", T0))
        self.assertFalse(self.h.add_event("new", T0))
        self.assertEqual(self.h.prune(cut), (0, 0))
        self.assertEqual(mode(self.path), 0o600)
        with self.assertRaises(ValueError):
            self.h.prune(datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
