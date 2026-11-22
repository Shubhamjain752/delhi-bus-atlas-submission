from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.month_boarding_report import MONTH, date_group, summarize
from tools.month_census import scan_census, sql_census


class MonthEvidenceTests(unittest.TestCase):
    def test_training_and_prior_audit_dates_do_not_enter_primary_fresh_result(self):
        groups = {group: [day[-2:] for day in MONTH if date_group(day) == group]
                  for group in ["fresh", "training", "previous", "pretraining"]}
        self.assertEqual(groups["fresh"], "14 16 18 19 20 21 24 25 27 28 30 31".split())
        self.assertEqual(groups["training"], ["10", "11", "12", "13"])
        self.assertEqual(groups["previous"], ["15", "17", "22", "23", "26", "29"])
        self.assertEqual(sum(map(len, groups.values())), 31)

    def test_removed_approach_and_unknown_are_visible_in_result(self):
        base = dict(route_id="1", bus="example", forecast_source="timetable", static_trip="trip",
                    baseline_conditional_emitted=True, conditional_emitted=False, forecast_accepted_at_issue=True)
        attempts = [{**base, "outcome": v} for v in ["approaching", "unverified", "gone"]]
        summary = summarize(attempts, attempts)
        self.assertEqual(summary["policies"]["suppressed"],
                         dict(offered=3, approaching=1, uncertain=0, gone=1, unverified=1))
        self.assertEqual(summary["approaching_without_offer"], 1)

    def test_census_includes_duplicates_missing_ids_and_unordered_receipt_hours(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "day.sqlite"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE vehicle_feed(route_id REAL, time TEXT)")
                db.executemany("INSERT INTO vehicle_feed(rowid,route_id,time) VALUES(?,?,?)",
                               [(1, 12, "23:59:59"), (3, 12, "23:59:59"), (4, None, "00:00:00")])
                db.commit()
            expected = {"raw_records": 3, "by_hour": {"00": 1, "23": 2}, "by_route": {"12": 2, "missing": 1},
                        "first_receipt_clock": "00:00:00", "last_receipt_clock": "23:59:59"}
            self.assertEqual(scan_census(source), expected)
            self.assertEqual(sql_census(source), expected)
            with closing(sqlite3.connect(source)) as db:
                db.execute("INSERT INTO vehicle_feed VALUES(3.5,NULL)")
                db.commit()
            result = scan_census(source)
            self.assertEqual(result, sql_census(source))
            self.assertEqual(result["by_hour"]["unknown"], 1)
            self.assertEqual(result["by_route"]["3.5"], 1)
