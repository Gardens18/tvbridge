import unittest
from datetime import date, datetime, time, timedelta, timezone

from tvbridge import clock

UTC = timezone.utc


class UtcNowTests(unittest.TestCase):
    def tearDown(self):
        clock.set_clock(None)

    def test_real_clock_is_aware_utc(self):
        now = clock.utcnow()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(0))
        self.assertLess(abs((now - datetime.now(UTC)).total_seconds()), 5)

    def test_freeze_and_restore(self):
        frozen = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)
        clock.set_clock(lambda: frozen)
        self.assertEqual(clock.utcnow(), frozen)
        self.assertEqual(clock.utcnow(), frozen)
        clock.set_clock(None)
        self.assertNotEqual(clock.utcnow(), frozen)

    def test_naive_provider_is_treated_as_utc(self):
        clock.set_clock(lambda: datetime(2026, 10, 1, 9, 56))
        self.assertEqual(clock.utcnow(), datetime(2026, 10, 1, 9, 56, tzinfo=UTC))

    def test_non_utc_provider_converted(self):
        tz3 = timezone(timedelta(hours=3))
        clock.set_clock(lambda: datetime(2026, 10, 1, 12, 0, tzinfo=tz3))
        now = clock.utcnow()
        self.assertEqual(now, datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(now.utcoffset(), timedelta(0))


class ServerTimeTests(unittest.TestCase):
    def test_to_server_gmt3(self):
        s = clock.to_server(datetime(2026, 10, 1, 20, 59, tzinfo=UTC), 3.0)
        self.assertEqual((s.year, s.month, s.day, s.hour, s.minute), (2026, 10, 1, 23, 59))
        self.assertEqual(s.utcoffset(), timedelta(hours=3))

    def test_server_date_across_midnight_gmt3(self):
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 20, 59, 59, tzinfo=UTC), 3), date(2026, 10, 1))
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 21, 0, tzinfo=UTC), 3), date(2026, 10, 2))
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 20, 59, tzinfo=UTC), 3.0), date(2026, 10, 1))

    def test_server_date_negative_and_fractional_offsets(self):
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 3, 0, tzinfo=UTC), -5), date(2026, 9, 30))
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 18, 29, tzinfo=UTC), 5.5), date(2026, 10, 1))
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 18, 30, tzinfo=UTC), 5.5), date(2026, 10, 2))

    def test_server_date_naive_input_assumed_utc(self):
        self.assertEqual(clock.server_date(datetime(2026, 10, 1, 21, 0), 3), date(2026, 10, 2))

    def test_server_midnight_utc(self):
        m = clock.server_midnight_utc(date(2026, 10, 2), 3.0)
        self.assertEqual(m, datetime(2026, 10, 1, 21, 0, tzinfo=UTC))
        self.assertEqual(m.utcoffset(), timedelta(0))
        self.assertEqual(clock.server_midnight_utc(date(2026, 10, 2), 0), datetime(2026, 10, 2, tzinfo=UTC))
        self.assertEqual(clock.server_midnight_utc(date(2026, 10, 2), -4), datetime(2026, 10, 2, 4, tzinfo=UTC))

    def test_server_midnight_consistent_with_server_date(self):
        d = date(2026, 3, 1)
        m = clock.server_midnight_utc(d, 3)
        self.assertEqual(clock.server_date(m, 3), d)
        self.assertEqual(clock.server_date(m - timedelta(microseconds=1), 3), date(2026, 2, 28))


class ParseHHMMTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(clock.parse_hhmm("00:05"), time(0, 5))
        self.assertEqual(clock.parse_hhmm("23:50"), time(23, 50))
        self.assertEqual(clock.parse_hhmm("9:30"), time(9, 30))
        self.assertEqual(clock.parse_hhmm(" 22:00 "), time(22, 0))
        self.assertEqual(clock.parse_hhmm("22:00:30"), time(22, 0, 30))

    def test_invalid(self):
        for bad in ("24:00", "12:60", "1200", "", "ab:cd", "12:5", None, 1200):
            with self.assertRaises(ValueError, msg=repr(bad)):
                clock.parse_hhmm(bad)


class ParseTvTimeTests(unittest.TestCase):
    BASE = datetime(2026, 10, 1, 9, 56, 0, tzinfo=UTC)

    def assertUtc(self, dt, expected):
        self.assertEqual(dt, expected)
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def test_z_suffix(self):
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00Z"), self.BASE)
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00z"), self.BASE)

    def test_fractional_seconds(self):
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00.5Z"), self.BASE.replace(microsecond=500000))
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00.123Z"), self.BASE.replace(microsecond=123000))
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00.123456Z"), self.BASE.replace(microsecond=123456))
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00.123456789Z"), self.BASE.replace(microsecond=123456))

    def test_offsets(self):
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00+00:00"), self.BASE)
        self.assertUtc(clock.parse_tv_time("2026-10-01T12:56:00+03:00"), self.BASE)
        self.assertUtc(clock.parse_tv_time("2026-10-01T12:56:00+0300"), self.BASE)
        self.assertUtc(clock.parse_tv_time("2026-10-01T05:56:00-04:00"), self.BASE)
        self.assertUtc(clock.parse_tv_time("2026-10-01T12:56:00.250+03:00"), self.BASE.replace(microsecond=250000))

    def test_naive_space_separated_assumed_utc(self):
        self.assertUtc(clock.parse_tv_time("2026-10-01 09:56:00"), self.BASE)
        self.assertUtc(clock.parse_tv_time("2026-10-01T09:56:00"), self.BASE)
        self.assertUtc(clock.parse_tv_time("  2026-10-01 09:56  "), self.BASE)

    def test_epoch_seconds_and_millis(self):
        epoch = int(self.BASE.timestamp())
        self.assertUtc(clock.parse_tv_time(epoch), self.BASE)
        self.assertUtc(clock.parse_tv_time(float(epoch)), self.BASE)
        self.assertUtc(clock.parse_tv_time(epoch * 1000), self.BASE)
        self.assertUtc(clock.parse_tv_time(str(epoch)), self.BASE)
        self.assertUtc(clock.parse_tv_time(str(epoch * 1000)), self.BASE)
        self.assertUtc(clock.parse_tv_time(epoch * 1000 + 250), self.BASE.replace(microsecond=250000))
        self.assertUtc(clock.parse_tv_time("%d.5" % epoch), self.BASE.replace(microsecond=500000))

    def test_datetime_passthrough(self):
        self.assertUtc(clock.parse_tv_time(datetime(2026, 10, 1, 9, 56)), self.BASE)
        tz3 = timezone(timedelta(hours=3))
        self.assertUtc(clock.parse_tv_time(datetime(2026, 10, 1, 12, 56, tzinfo=tz3)), self.BASE)

    def test_invalid_values(self):
        for bad in (None, "", "   ", "garbage", "2026-13-01T00:00:00Z", "2026-10-01T25:00:00Z",
                    "10/01/2026 09:56", True, False, float("nan"), float("inf"), [], {}, "1e9"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                clock.parse_tv_time(bad)


class IsoTests(unittest.TestCase):
    def test_iso_z_suffix(self):
        self.assertEqual(clock.iso(datetime(2026, 10, 1, 9, 56, tzinfo=UTC)), "2026-10-01T09:56:00Z")

    def test_iso_microseconds(self):
        self.assertEqual(clock.iso(datetime(2026, 10, 1, 9, 56, 0, 123456, tzinfo=UTC)),
                         "2026-10-01T09:56:00.123456Z")

    def test_iso_converts_to_utc_and_naive_assumed_utc(self):
        tz3 = timezone(timedelta(hours=3))
        self.assertEqual(clock.iso(datetime(2026, 10, 1, 12, 56, tzinfo=tz3)), "2026-10-01T09:56:00Z")
        self.assertEqual(clock.iso(datetime(2026, 10, 1, 9, 56)), "2026-10-01T09:56:00Z")

    def test_round_trip(self):
        for dt in (datetime(2026, 10, 1, 9, 56, tzinfo=UTC), datetime(2026, 10, 1, 9, 56, 0, 7, tzinfo=UTC)):
            back = clock.from_iso(clock.iso(dt))
            self.assertEqual(back, dt)
            self.assertEqual(back.utcoffset(), timedelta(0))

    def test_from_iso_variants(self):
        base = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)
        self.assertEqual(clock.from_iso("2026-10-01T09:56:00+00:00"), base)
        self.assertEqual(clock.from_iso("2026-10-01T09:56:00"), base)
        self.assertEqual(clock.from_iso("2026-10-01T09:56:00.000000Z"), base)
        self.assertEqual(clock.from_iso(base), base)
        with self.assertRaises(ValueError):
            clock.from_iso("nope")


class UsDstTests(unittest.TestCase):
    """account.server_utc_offset_hours = "auto" (brokers whose day ends at the New York close)."""

    def test_us_dst_boundaries(self):
        # 2026: Sun 8 Mar 02:00 EST (07:00 UTC) .. Sun 1 Nov 02:00 EDT (06:00 UTC)
        self.assertFalse(clock.us_dst_active(datetime(2026, 3, 8, 6, 59, tzinfo=UTC)))
        self.assertTrue(clock.us_dst_active(datetime(2026, 3, 8, 7, 0, tzinfo=UTC)))
        self.assertTrue(clock.us_dst_active(datetime(2026, 11, 1, 5, 59, 59, tzinfo=UTC)))
        self.assertFalse(clock.us_dst_active(datetime(2026, 11, 1, 6, 0, tzinfo=UTC)))
        # 2027: 14 Mar .. 7 Nov
        self.assertFalse(clock.us_dst_active(datetime(2027, 3, 13, 12, 0, tzinfo=UTC)))
        self.assertTrue(clock.us_dst_active(datetime(2027, 11, 6, 12, 0, tzinfo=UTC)))
        self.assertFalse(clock.us_dst_active(datetime(2027, 11, 7, 12, 0, tzinfo=UTC)))

    def test_ny_close_offset(self):
        self.assertEqual(clock.ny_close_offset_hours(datetime(2026, 10, 1, 9, 0, tzinfo=UTC)), 3.0)
        self.assertEqual(clock.ny_close_offset_hours(datetime(2026, 12, 1, 9, 0, tzinfo=UTC)), 2.0)


if __name__ == "__main__":
    unittest.main()
