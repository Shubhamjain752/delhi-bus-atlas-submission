import unittest
import gzip
import json
from pathlib import Path
from collections import Counter
from datetime import datetime
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from tools.near_miss_peak import IST, PeakStudy, chronological_indices, evaluate, fresh_approach, nominal_start_minute, ready_clock, target_outcome, token


class PeakNearMissTests(unittest.TestCase):
    def test_late_gps_cannot_retroactively_create_an_upstream_anchor(self):
        observed = np.array([10,20,30,40])
        receipt = np.array([12,50,31,44])
        np.testing.assert_array_equal(chronological_indices(observed,receipt),[0,2,3])

    def test_three_fix_anchor_remains_eligible_without_another_future_report(self):
        day = '2026-10-23'
        vehicle = next(str(i) for i in range(100) if int(token(day,str(i),'peak'),16)%2 == 0)
        start = int(datetime.fromisoformat(day+'T08:10:00').replace(tzinfo=IST).timestamp())
        scale = np.array([111000,97000])
        points = np.array([[28.6,77.2+x] for x in (0,.002,.004,.006)])
        stops = ['prior','anchor','board','next']
        frame = pd.DataFrame([{'route':1,'vehicle':vehicle,'trip':'1_08_00_123','lat':28.6,'lon':lon,
                               'observed':start+offset,'receipt':start+offset+1}
                              for offset,lon in [(0,77.1992),(20,77.2),(40,77.202)]])
        study = PeakStudy.__new__(PeakStudy)
        study.routes = ['1']
        study.patterns = {'1':(stops,[90]*3,Counter(stops),cKDTree(points*scale),scale)}
        study.model = Mock()
        study.model.duration.return_value = 90,'gps_historical'
        study.router = Mock()
        study.router.routes = {'1':('TestUP','agency')}
        study.onward_trips = Mock(return_value=[(32000,32100,'trip',460)])
        study.fallback = Mock(return_value=600)
        with patch('tools.near_miss_peak.read_window',return_value=(frame,3)):
            rows,_ = study.scan(Path('unused'),day,'AM')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['outcome'],'unverified')

    def test_fixed_platform_grid_has_both_early_and_late_readiness(self):
        offsets = [ready_clock('here', t)-t for t in range(120)]
        self.assertEqual(set(offsets), set(range(-59,61)))
        self.assertEqual(len({ready_clock('here', t)%120 for t in range(240)}),1)

    def test_evidence_at_readiness_excludes_late_received_fixes(self):
        xy = np.array([[0.,0.],[70.,0.],[130.,0.]])
        obs = np.array([100,120,140])
        received = np.array([102,152,144])
        split = np.array([True,False,False])
        for eligible, ready in [(False,135),(True,150)]:
            self.assertEqual(fresh_approach(xy,obs,received,split,0,xy[0],np.array([300.,0.]),ready),eligible)

    def test_dwell_uncertainty_does_not_become_a_false_suggestion(self):
        xy = np.array([[0.,0.],[130.,0.],[190.,0.],[205.,0.],[280.,0.]])
        stops = np.array([[0.,0.],[200.,0.],[400.,0.]])
        time = np.array([100,150,170,190,205])
        accepted = np.array([0,-1,1,1,-1])
        broken = np.array([True,False,False,False,False])
        for ready, expected in [(140,'approaching'),(160,'uncertain'),(195,'uncertain'),(206,'gone')]:
            label, bracket = target_outcome(xy,time,accepted,broken,0,1,stops,ready)
            self.assertEqual(label,expected)
            self.assertEqual(bracket,[150,170,205])

    def test_no_observed_label_and_false_suggestions_stay_in_offer_denominator(self):
        rows = [{'gap':3, 'fresh_approach':True, 'fallback_gain_s':400, 'outcome':label}
                for label in ['approaching','gone','uncertain','unverified']]
        metrics = evaluate(rows)['thresholds']['fallback_5min']
        self.assertEqual(metrics['0']['offered'],0)
        self.assertEqual(metrics['5']['offered'],4)
        self.assertEqual(metrics['5']['precision_among_definitive'],0.5)
        self.assertEqual(metrics['5']['minimum_supported_share_of_all_offers'],0.25)

    def test_archive_trip_minute_can_exclude_its_own_timetable(self):
        self.assertEqual(nominal_start_minute('3210','3210_23_25_3223'),1405)
        self.assertIsNone(nominal_start_minute('3210','999_23_25_3223'))

    def test_published_rows_reproduce_primary_holdout_tables(self):
        directory = Path(__file__).resolve().parents[1]/'data'
        with gzip.open(directory/'near-miss-opportunities.json.gz','rt') as src:
            rows = json.load(src)
        summary = json.loads((directory/'near-miss-peak.json').read_text())
        self.assertEqual(len(set((r['day'],r['bus']) for r in rows)),len(rows))
        for r in rows:
            self.assertLess(r['forecast_receipt'],r['ready'])
            self.assertLessEqual(r['origin_gps'],r['forecast_receipt'])
            self.assertEqual(r['forecast']+r['gap'],r['ready'])
            self.assertNotEqual(r['route'],'502UP')
            if r['outcome']=='approaching':
                self.assertLessEqual(r['ready'],r['gps_entry_exit'][0])
            if r['outcome']=='gone':
                self.assertGreaterEqual(r['ready'],r['gps_entry_exit'][2])
        held_out = [r for r in rows if r['day'] in summary['test_dates']]
        self.assertEqual(evaluate(held_out),summary['held_out'])
