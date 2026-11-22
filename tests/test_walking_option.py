import tempfile
import unittest
from pathlib import Path

import numpy as np

from transit.walking import Walking
from transit.walking_option import walking_alternative


class WalkingAlternativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.a = {"id": "a", "name": "A", "lat": 28.6, "lon": 77.2}
        self.b = {"id": "b", "name": "B", "lat": 28.6, "lon": 77.21}
        self.c = {"id": "c", "name": "C", "lat": 28.6002, "lon": 77.2}
        np.savez(root / "walk.npz",
                 coords=[[28.6, 77.2], [28.6, 77.21], [28.6002, 77.2], [28.6002, 77.21]],
                 edges=[[0, 1], [2, 3]], directions=[1, 3], factors=[1, 1], delays=[0, 0],
                 centerlines=[True, False], barriers=[[28.6001, 77.1999, 28.6001, 77.2101]])
        self.walk = Walking(root)

    def tearDown(self):
        self.directory.cleanup()

    def result(self, start, end, durations=()):
        return {"origin": start, "destination": end, "departure": "2026-11-21T23:55:00+05:30",
                "journeys": [{"duration_seconds": d, "legs": [{"mode": "bus"}]} for d in durations]}

    def test_faster_walk_is_separate_and_retains_path_information(self):
        result = self.result(self.a, self.b, [1800, 1200])
        walk = walking_alternative(self.walk, result)
        self.assertEqual(walk["reason"], "faster_than_bus")
        self.assertEqual(walk["time_saved_seconds"], 1200 - walk["seconds"])
        self.assertEqual(walk["from"], self.a)
        self.assertEqual(walk["to"], self.b)
        self.assertTrue(walk["arrival"].startswith("2026-11-22T00:"))
        self.assertEqual(walk["assumed_pace_m_s"], 1.25)
        self.assertTrue(walk["includes_arterial_without_mapped_sidewalk"])
        self.assertGreater(walk["distance_m"], 950)
        self.assertEqual(len(result["journeys"]), 2)

    def test_equal_or_faster_bus_suppresses_walk(self):
        walk = walking_alternative(self.walk, self.result(self.a, self.b))
        for seconds in (1, walk["seconds"]):
            result = self.result(self.a, self.b, [9999, seconds])
            self.assertIsNone(walking_alternative(self.walk, result))

    def test_no_bus_and_midnight(self):
        result = self.result(self.a, self.b)
        walk = walking_alternative(self.walk, result)
        self.assertEqual(walk["reason"], "no_bus_within_limits")
        self.assertIsNone(walk["time_saved_seconds"])
        self.assertEqual(result["journeys"], [])

    def test_one_way_barrier_cap_and_same_location(self):
        far = {**self.b, "lon": 77.23}
        for a,b in [(self.b,self.a),(self.a,self.c),(self.a,far),(self.a,self.a),
                    (self.a,{**self.a, "id": "different-platform"})]:
            with self.subTest(origin=a["id"], destination=b["id"]):
                self.assertIsNone(walking_alternative(self.walk, self.result(a,b)))
