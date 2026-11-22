import unittest
from datetime import datetime
import json
from pathlib import Path

import numpy as np

from tools.analyze_gps_speed import project_stop, recent_before, neighbor_prediction


class SpeedAnalysisTests(unittest.TestCase):
    def test_distance_follows_polyline_and_rejects_ambiguous_loop(self):
        line = np.array([[0.,0.],[0.,200.],[200.,200.]])
        self.assertEqual(project_stop(line, np.array([100.,200.])), (300.,0.))
        loop = np.array([[0.,0.],[0.,200.],[200.,200.],[0.,0.],[0.,200.]])
        self.assertIsNone(project_stop(loop,np.array([0.,100.])))
        self.assertIsNone(project_stop(line,np.array([300.,300.])))

    def test_completed_and_received_before_forecast(self):
        # Tuples are receipt, GPS arrival, duration and prior.
        observations = [(900,860,40,30),(950,901,48,30),(999,998,65,30),(1000,990,55,30),(1010,995,65,30)]
        self.assertEqual(recent_before(observations,1000,120), observations[1:3])

    def test_normalized_neighbor_delay_adjusts_only_historical_share(self):
        self.assertEqual(neighbor_prediction(100,100,0,[1.5,1.5],2),100)
        self.assertEqual(neighbor_prediction(100,100,0,[1.5]*8,3),125)
        self.assertLess(neighbor_prediction(100,100,10,[1.5]*8,3),125)
        self.assertEqual(neighbor_prediction(100,100,0,[1.5]*8,1),100)

    def test_published_comparisons_and_examples(self):
        path = Path(__file__).resolve().parents[1]/'data/gps-speed.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(set(report['profiles']),{'weekday','weekend','confirmation'})
        for profile in report['profiles'].values():
            self.assertLess(max(profile['train']),profile['holdout'])
            for metrics in profile['metrics'].values():
                self.assertEqual(metrics['n'],profile['coverage']['distance_qualified_segments'])
        for example in report['examples']:
            start = datetime.fromisoformat(example['forecast_at_ist'])
            end = datetime.fromisoformat(example['observed_arrival_ist'])
            for field in ('last_neighbor_receipt_ist','same_segment_last_receipt_ist'):
                self.assertLess(datetime.fromisoformat(example[field]),start)
            self.assertEqual((end-start).total_seconds(),example['observed_seconds'])
            self.assertAlmostEqual(3.6*example['shape_distance_m']/example['observed_seconds'],
                                   example['observed_speed_kmh'],2)
        sample = report['raw_gps']['first_qualifying_interval']
        self.assertEqual(len(sample['bus_run_alias']),16)
        self.assertAlmostEqual(3.6*sample['chord_m']/sample['seconds'],sample['derived_kmh'],1)
