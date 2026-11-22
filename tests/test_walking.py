import tempfile
import unittest
from pathlib import Path

import numpy as np

from transit.walking import Walking, project
from tools.build_walk import foot_directions, blocked_node


class WalkRoutingTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        # Two parallel pavements joined at the east end by a marked crossing.
        # Node 4 shares coordinates with 3 but belongs to a disconnected bridge.
        coords = [[28.6,77.20], [28.6,77.202], [28.6002,77.20],
                  [28.6002,77.202], [28.6002,77.202], [28.6015,77.203]]
        np.savez(Path(self.work.name) / 'walk.npz', coords=coords,
                 edges=[[0,1],[2,3],[1,3],[4,5]], directions=[3,3,3,1],
                 factors=[1.,1.,1.,1.], delays=[0.,0.,20.,0.],
                 barriers=np.array([[28.6001,77.1999,28.6001,77.2021]]))
        self.walk = Walking(Path(self.work.name))

    def tearDown(self):
        self.work.cleanup()

    def test_route_uses_crossing_with_delay(self):
        trip = self.walk.route((28.6,77.2001), (28.6002,77.2001))
        self.assertGreater(trip['distance_m'], 360)
        self.assertGreater(trip['seconds'], trip['distance_m'] / 1.25 + 18)
        self.assertIn([28.6,77.202], trip['geometry'])

    def test_partial_edge_and_unreachable(self):
        trip = self.walk.route((28.6,77.2008), (28.6,77.201))
        self.assertAlmostEqual(trip['distance_m'], 19.5, delta=1)
        self.assertIsNone(self.walk.route((28.60,77.20), (29.0,77.20)))
        self.assertIsNone(self.walk.route((28.60,77.20), (28.6015,77.203)))
        self.assertIsNone(self.walk.route((28.6,77.2001),(28.6002,77.2001),max_distance_m=100))

    def test_endpoint_connector_cannot_pass_through_a_wall(self):
        p, q = project((28.6,77.2003)), project((28.6002,77.2003))
        self.assertTrue(self.walk._crosses_barrier(p,q,np.linalg.norm(p-q)))

    def test_same_pin_has_zero_walk_even_off_mapped_edge(self):
        p = (28.60003,77.2003)
        self.assertEqual(self.walk.route(p,p)['seconds'],0)

    def test_access(self):
        self.assertEqual(foot_directions({'highway':'footway','oneway':'yes'}), 3)
        self.assertEqual(foot_directions({'highway':'steps','oneway:foot':'-1'}), 2)
        self.assertEqual(foot_directions({'highway':'service','access':'private'}), 0)
        self.assertEqual(foot_directions({'highway':'primary','sidewalk':'separate'}), 0)
        self.assertEqual(foot_directions({'highway':'primary','bridge':'yes'}), 0)
        self.assertEqual(foot_directions({'highway':'secondary','oneway':'yes'}), 0)
        self.assertEqual(foot_directions({'highway':'secondary'}), 3)
        self.assertTrue(blocked_node({'barrier':'wall'}))


class DelhiGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data_dir = Path(__file__).resolve().parents[1]/'data'
        if not (data_dir/'walk.npz').is_file():
            raise unittest.SkipTest('Build the offline Delhi graph to run real-location checks')
        cls.walk = Walking(data_dir)

    def test_kashmere_gate_and_nehru_place(self):
        # Published stop coordinates, correct to rounding within a metre.
        pairs = [((28.668535,77.226647),(28.666960,77.227456)),
                 ((28.5485761,77.2491332),(28.547959,77.255166))]
        for start, end in pairs:
            with self.subTest(start=start,end=end):
                route = self.walk.route(start,end,max_distance_m=3000)
                self.assertIsNotNone(route)
                self.assertGreater(len(route['geometry']),3)
                self.assertEqual(route['geometry'][0],list(start))
                self.assertEqual(route['geometry'][-1],list(end))
                self.assertGreaterEqual(route['distance_m'],np.linalg.norm(project(end)-project(start))-1)
                self.assertGreater(route['seconds'],0)
        self.assertIsNone(self.walk.route((19.076,72.8777),pairs[0][0]))


if __name__ == '__main__':
    unittest.main()
