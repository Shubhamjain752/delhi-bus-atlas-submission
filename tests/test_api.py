import unittest
from datetime import datetime
from transit.api import ist_departure


class DepartureParsing(unittest.TestCase):
    def test_user_clock_is_delhi_even_if_server_zone_differs(self):
        self.assertEqual(ist_departure("2026-10-15T08:45").isoformat(), "2026-10-15T08:45:00+05:30")
        self.assertEqual(ist_departure("2026-10-15T03:15:00Z"), ist_departure("2026-10-15T08:45"))

    def test_invalid_or_partial_timestamps_are_rejected(self):
        for value in ("yesterday", "2026-10-15", "08:30", "2026-02-30T19:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ist_departure(value)
