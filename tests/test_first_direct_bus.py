import unittest
from types import SimpleNamespace

import numpy as np

from tools.check_first_direct_bus import winner, witnessed
from tools.check_passenger_wait_cost import wait_costs
from tools.fleet_gps import FIX
from transit.router import approximate_m, coords


class FirstDirectOutcomeTests(unittest.TestCase):
    def test_wait_cost_compares_two_tracked_choices_against_same_first_line(self):
        observed = {"complete": {"A": [100, 240], "B": [220, 360]}, "without_exit": []}
        decisions = {"before": {"line": "B"}, "after": {"line": "A"}}
        self.assertEqual(wait_costs(decisions, observed), (120, 0))
        decisions["after"] = {"line": "unobserved"}
        self.assertIsNone(wait_costs(decisions, observed))
        decisions["after"] = {"line": "A"}
        observed["without_exit"] = [["C", 80]]
        self.assertIsNone(wait_costs(decisions, observed))

    def test_earlier_rival_without_an_exit_and_close_observations_remain_unscored(self):
        trace = {"complete": {"A": [100, 220], "B": [180, 280]}, "without_exit": [["C", 142]]}
        self.assertEqual(winner(trace, 0), (None, "incomplete_early_rival"))
        trace["without_exit"] = []
        self.assertEqual(winner(trace, 0), ("A", None))
        trace["complete"]["B"][1] = 260
        self.assertEqual(winner(trace, 1), (None, "close_observations"))

    def test_one_line_can_supply_different_first_boarding_and_exit_vehicles(self):
        stops = {"one": {"lat": 28.60, "lon": 77.20}, "two": {"lat": 28.604, "lon": 77.20}}
        router = SimpleNamespace(stops=stops, routes={"5": ["A"], "9": ["B"]},
                                 at={s: [(0, 0), (1, 0)] for s in stops},
                                 patterns=[SimpleNamespace(route=r) for r in ["5", "9"]])
        router._nearby = lambda point, radius: sorted(
            (distance, s) for s, stop in stops.items()
            if (distance := approximate_m(coords(stop), point)) <= radius)
        rows = []
        for route, vehicle, board, exit_time in [(5, 1, 100, 480), (5, 2, 220, 300), (9, 3, 180, 390)]:
            for stop, t in [("one", board), ("two", exit_time)]:
                rows.append((vehicle, vehicle, route, 0, t, t, *coords(stops[stop])))
        result, = witnessed(router, [("one", "two", ["5", "9"])], np.array(rows, dtype=FIX), issue=90)
        self.assertEqual(result["complete"]["A"], [100, 300])
        self.assertEqual(winner(result, 1), ("A", None))
