import unittest
from pathlib import Path

from tools.verify_sample_passages import inspect


class SavedVehiclePassagesTest(unittest.TestCase):
    def test_consecutive_stop_entries_on_a_single_dated_604_run(self):
        root=Path(__file__).resolve().parents[1]
        folder=root/'data/nov21-samples'
        if len(list(folder.glob('positions-*.pb.gz')))<15:
            self.skipTest('The November 21 ground-truth collection is required')
        review=inspect(folder,root)
        candidates=[c for c in review['candidates'] if c['official_trip']=='2106_FULL_00_40'
                    and [x['id'] for x in c['events']]==['8065','3470','8066','3472']]
        self.assertEqual(len(candidates),1)
        c=candidates[0]
        self.assertTrue(c['consecutive_static_stops'])
        self.assertLess(max(c['available_pre_origin_receipts']),c['events'][0]['gps_time'])
        self.assertEqual(c['service_date'],'20261121')
        self.assertEqual(c['elapsed_seconds'],154)
        self.assertEqual(c['max_ping_gap'],33)
        for e in c['events']:
            self.assertLessEqual(e['offset_m'],45)
            self.assertGreater(e['before_offset_m'],45)
            self.assertLessEqual(e['gps_time'],e['receipt'])
