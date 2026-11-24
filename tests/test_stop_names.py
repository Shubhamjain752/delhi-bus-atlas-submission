from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from test_router import feed
from transit.router import IST, Router, _Query
from transit.endpoints import nearby_alighting_option, plan_with_stop_names, suggestions


def extra_platforms(data):
    data["stops"].extend([["Q", "Clock Tower", 28.6002, 77.24], ["F", "Clock Tower", 28.65, 77.24]])
    # Q serves more routes than platform C, but only as their first stop.
    for i in range(5):
        route = f"q{i}"
        data["routes"].append([route, route, "DTC"])
        data["trips"].append([route, "s", route, ""])
        data["stop_times"].extend([[route, "Q", 0, "08:00:00", "08:00:00"],
                                   [route, "A", 1, "08:12:00", "08:12:00"]])


class StopNamesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        feed(root / "feed.zip", extra_platforms)
        self.router = Router(root / "feed.zip", root, None, None)
        self.at = datetime(2026, 10, 15, 7, 57, tzinfo=IST)

    def test_free_text_keeps_a_served_same_name_platform(self):
        self.assertEqual(self.router.search("clock tower")[0]["id"], "Q")
        result = plan_with_stop_names(self.router, "A", "Clock Tower", self.at)
        self.assertTrue(result["journeys"])
        self.assertEqual(result["destination"]["id"], "C")
        self.assertTrue(any("stop C." in warning for warning in result["warnings"]))
        self.assertEqual(suggestions(self.router, "Q", 1)[0]["via_stop"], "Alpha")
        self.assertNotIn("via_stop", self.router.stops["Q"])

    def test_exact_platform_or_pin_stays_fixed_and_shows_partial_ride(self):
        for destination in ("Q", "28.6002,77.24"):
            with self.subTest(destination=destination):
                result = plan_with_stop_names(self.router, "A", destination, self.at)
                self.assertEqual(result["journeys"], [])
                option = nearby_alighting_option(self.router, result, self.at)
                self.assertEqual(option["destination"], result["destination"])
                self.assertEqual(option["alighting_stop"]["id"], "C")
                self.assertEqual(option["journey"]["legs"][-1]["mode"], "bus")
                self.assertEqual(option["journey"]["legs"][-1]["arrival"], option["journey"]["arrival"])
                self.assertAlmostEqual(option["straight_line_distance_m"], 22, delta=1)
                self.assertNotIn("estimated_walk", option)
        far = plan_with_stop_names(self.router, "A", "F", self.at)
        self.assertFalse(far["journeys"])
        self.assertIsNone(nearby_alighting_option(self.router, far, self.at))

    def test_inaccessible_stops_do_not_use_up_transfer_search_places(self):
        query = _Query(self.router, self.at, {})
        for i in range(15):
            key = f"S{i}"
            self.router.stops[key] = {"id": key, "name": key, "lat": 28.6001, "lon": 77.20}
        self.router._nearby = lambda point, radius: [(i, f"S{i}") for i in range(15)]
        query.walk = lambda a, b: {"seconds": 25, "distance_m": 30} if b["id"] == "S14" else None
        self.assertIn("S14", query.endpoint(self.router.stops["A"], True))
