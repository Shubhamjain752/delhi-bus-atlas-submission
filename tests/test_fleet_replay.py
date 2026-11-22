import gzip
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tools.replay_fleet_boarding import IST, TimetableMatcher, identity, output_days, stratify
from tools.fleet_positions import received_positions


class FleetReplay(unittest.TestCase):
    def test_daywise_export_reconciles_counts_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/"cases.json.gz"
            with output_days(target,{"screened":2}) as write:
                write([])
                write([{"hour":1}])
                write([{"hour":3}])
            with gzip.open(target,"rt") as stream:
                original=json.load(stream)
            self.assertEqual(original["cases"],[{"hour":1},{"hour":3}])
            with self.assertRaises(ValueError), output_days(target,{"screened":2}) as write:
                write([{"hour":22}])
            with gzip.open(target,"rt") as stream:
                self.assertEqual(json.load(stream),original)

    def test_identical_rows_in_a_poll_are_one_fix_and_conflicts_remain_together(self):
        fix = (100,90,28.6,77.2)
        next_fix = (100,91,28.61,77.2)
        repeated_poll = (110,90,28.6,77.2)
        self.assertEqual(list(received_positions([fix,fix,next_fix,repeated_poll])),[
            (100,(fix[1:],next_fix[1:])), (110,(repeated_poll[1:],))])

    def test_midnight_trip_uses_previous_service_date_and_keeps_ambiguous_calls_out(self):
        def pattern(start, trip):
            return SimpleNamespace(service="daily", starts=[start], departures=[0, 600, 900],
                                   stops=["a", "b", "c"], pickups=[0]*3, drops=[0]*3, trips=[trip])
        router = SimpleNamespace(patterns=[pattern(23*3600+55*60, "late")],
                                 _active=lambda day: {"daily"})
        registry = SimpleNamespace(by_clock={("r", 1435): [(0, 0)]})
        row = dict(day="2026-10-02", route_id="r", upstream="a", stop="b", next_stop="c", start_minute=1435,
                   forecast=int(datetime(2026, 10, 2, 0, 5, tzinfo=IST).timestamp()))
        self.assertEqual(TimetableMatcher(router, registry).match(row), (None, "2026-10-01", "late"))
        # Both today's 00:05 and yesterday's 24:05 are equally credible for this provider clock.
        router.patterns = [pattern(300, "today"), pattern(86700, "yesterday")]
        registry.by_clock = {("r", 5): [(0, 0)], ("r", 1445): [(1, 0)]}
        row.update(start_minute=5, forecast=row["forecast"]+600)
        self.assertEqual(TimetableMatcher(router, registry).match(row)[0], "ambiguous_dated_trip_or_pattern")
        row["start_minute"] = 1445
        self.assertEqual(TimetableMatcher(router, registry).match(row), (None, "2026-10-01", "yesterday"))

    def test_selection_covers_rare_routes_hours_and_splits_without_labels(self):
        rows = [dict(day=f"2026-10-{day:02}", route_id=route, hour=hour, ready=1000+hour, stop="b", eligibility=None)
                for day in [1,2] for route in ["rare","busy","diagnostic"] for hour in [1,2,16]]
        rows.append(dict(rows[0], route_id="unmatched", eligibility="ambiguous"))
        names = {"diagnostic":"502DOWN"}
        selected = stratify(rows, names)
        self.assertEqual(selected, stratify([dict(r, outcome="gone") for r in reversed(rows)], names))
        for key in {r["route_id"] for r in rows if not r["eligibility"]}:
            self.assertEqual({h<6 for d,route,h in selected if route==key}, {True, False})
        self.assertEqual({(d,h) for d,_,h in selected}, {(r["day"],r["hour"]) for r in rows})
        self.assertEqual({h for d,route,h in selected if route=="diagnostic"}, {1,2,16})
        self.assertNotIn(identity(rows[-1]), selected)
