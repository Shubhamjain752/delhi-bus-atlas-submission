import tempfile
import unittest
import json
from pathlib import Path

import numpy as np

from transit.walking import Walking, project
from tools.build_walk import foot_directions, blocked_node, below_street_level


class WalkRoutingTest(unittest.TestCase):
    def test_unlevelled_pins_use_the_street_but_can_enter_a_tunnel_at_its_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            coords = [[28.60,77.20], [28.60,77.201], [28.6001,77.201],
                      [28.59998,77.2005], [28.59998,77.201], [28.59998,77.202]]
            np.savez(data/'walk.npz', coords=coords, edges=[[0,1],[1,2],[3,4],[4,5],[4,1]],
                     directions=[3]*5, factors=[1.]*5, delays=[0.]*5, barriers=[])
            (data/'walk-meta.json').write_text(json.dumps({'input_md5':'test'}))
            (data/'walk-surface.json').write_text(json.dumps({
                'source_md5':'test', 'edge_count':5,'below_grade_edges':[2,3,4],
                'ground_entrance_nodes':[1,5]
            }))
            walk = Walking(data)
            road = walk.route(coords[0], coords[3])
            self.assertEqual(road['distance_m'],51)
            self.assertGreater(road['destination_snap_m'],2)
            subway = walk.route(coords[0], coords[5])
            self.assertIn(coords[4],subway['geometry'])
            self.assertEqual(subway['destination_snap_m'],0)

    def test_subway_level_tags_and_building_passages(self):
        for tags in ({'level':'-1'}, {'level':'0;-1'}, {'location':'underground'},
                     {'tunnel':'yes'}):
            self.assertTrue(below_street_level(tags))
        for tags in ({}, {'level':'0'}, {'tunnel':'building_passage','level':'0'}, {'layer':'-1'}, {'bridge':'yes'}):
            self.assertFalse(below_street_level(tags))
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

    def test_only_estimated_ground_approaches_join_a_grade_separated_crossing(self):
        # A separate stairway under a divided road has two street-facing ends.
        a, b = [28.6,77.2], [28.6,77.201]
        c, d = [28.6004,77.2], [28.6004,77.201]
        e, f = [28.60004,77.201], [28.60008,77.20095]
        g, h = [28.60032,77.20095], [28.60036,77.201]
        folder = Path(self.work.name)
        np.savez(folder/'walk.npz', coords=[a,b,c,d,e,f,g,h],
                 edges=[[0,1],[2,3],[4,5],[5,6],[6,7]], directions=[3]*5,
                 factors=[1,1,1.5,1,1.5], delays=[0]*5,
                 barriers=[[28.6002,77.1999,28.6002,77.2011]])
        def link(ground, stair, street, next_street):
            return {'landing':ground,'steps_neighbour':stair,
                    'street':street,'street_neighbour':next_street}
        # A spurious direct link through the median must be rejected.
        links = [link(e,f,b,a),link(h,g,d,c),link(e,f,d,c)]
        (folder/'walk-crossings.json').write_text(json.dumps({'crossings':[{'approaches':links}]}))
        trip = Walking(folder,use_crossing_access=True).route(a,c)
        self.assertIsNotNone(trip)
        self.assertTrue(trip['uses_steps'])
        self.assertEqual(trip['inferred_crossing_access_m'],9)
        self.assertIn(g,trip['geometry'])
        self.assertIn(f,trip['geometry'])
        self.assertIsNone(Walking(folder).route(a,c))

    def test_approach_builder_rejects_mapped_obstacle_and_other_floor(self):
        from shapely.geometry import Polygon
        from tools.build_crossing_access import approach

        # The upper pavement has a disconnected bridge at the same coordinates;
        # source neighbour coordinates identify the intended street level.
        landing = [28.6002,77.2019]
        street, neighbour = self.walk.coords[3].tolist(), self.walk.coords[2].tolist()
        self.assertEqual(self.walk.mapped_node(street,neighbour),3)
        self.assertEqual(self.walk.mapped_node(street,self.walk.coords[5]),4)
        candidate = {'landing':landing,'steps_neighbour':landing,'landing_node':10,
                     'steps_way':11,'graph':4}
        target = [(12,street,neighbour,13)]
        self.assertIsNotNone(approach(self.walk,candidate,target,[]))
        building = Polygon([(28.60019,77.20194),(28.60021,77.20194),
                            (28.60021,77.20196),(28.60019,77.20196)])
        self.assertIsNone(approach(self.walk,candidate,target,[building]))


class DelhiGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data_dir = Path(__file__).resolve().parents[1]/'data'
        if not (data_dir/'walk.npz').is_file():
            raise unittest.SkipTest('Build the offline Delhi graph to run real-location checks')
        cls.walk = Walking(data_dir,use_crossing_access=True)

    def test_aiims_uses_stairs_and_accounts_for_same_side_approaches(self):
        if len(self.walk.edges)==self.walk.mapped_edge_count:
            self.skipTest('Install the walking crossing evidence to run the crossing check')
        trip=self.walk.route((28.568914,77.213693),(28.569232,77.212737),max_distance_m=1100)
        self.assertIsNotNone(trip)
        self.assertTrue(300<trip['distance_m']<450)
        self.assertTrue(trip['uses_steps'])
        self.assertTrue(50<=trip['inferred_crossing_access_m']<=65)
        # Check both sides of the physical crossing, not just a short estimate.
        self.assertIn([28.5689344,77.2117954],trip['geometry'])
        self.assertIn([28.5693055,77.211831],trip['geometry'])

    def test_shivaji_stadium_to_rajiv_gate_and_surface_pin(self):
        stop = (28.6323,77.2165)
        gate = (28.6323385,77.2183526)
        park = (28.632986,77.219374)
        gate_walk = self.walk.route(stop,gate)
        park_walk = self.walk.route(stop,park)
        self.assertTrue(320 <= gate_walk['distance_m'] <= 345)
        self.assertEqual(gate_walk['inferred_crossing_access_m'],26)
        self.assertIn([28.6321017,77.2181956],gate_walk['geometry'])  # Radial Road 2 crossing
        self.assertTrue(640 <= park_walk['distance_m'] <= 665)
        self.assertTrue(20 <= park_walk['destination_snap_m'] <= 25)
        self.assertNotIn([28.632589,77.2195934],park_walk['geometry'])  # Underground fare gate

    def test_unconfirmed_nehru_place_street_estimate(self):
        a = (28.54857609786973,77.24913317314967)
        b = (28.54755,77.249444)
        for start, end in ((a,b),(b,a)):
            with self.subTest(start=start):
                self.assertIsNone(self.walk.route(start,end))
                walk = self.walk.estimated_route(start,end,max_distance_m=350)
                self.assertEqual((walk['distance_m'], walk['seconds']), (319,255))
                self.assertEqual((walk['geometry'][0], walk['geometry'][-1]), (list(start),list(end)))
                self.assertGreater(len(walk['geometry']), 6)
                self.assertIn('FOSSGIS', walk['source'])
                self.assertIn('crossing', walk['warning'])
                self.assertIsNone(self.walk.estimated_route(start,end,max_distance_m=310))
        self.assertIsNotNone(self.walk.estimated_route((28.54858,77.24913),b))
        # Reflect 2559 over the foot router's road line. The two-metre move is
        # within the pin tolerance but reverses the side of the carriageway.
        self.assertIsNone(self.walk.estimated_route((28.54856990213027,77.24911882685033),b))
        self.assertIsNone(self.walk.estimated_route((a[0]+.0001,a[1]),b))
        self.assertIsNone(self.walk.estimated_route(a,(28.54732266056635,77.2538969382967)))
        self.assertIsNone(self.walk.estimated_route((28.669372,77.227137),b))

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
