import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.month_boarding_scan import MONTH, WINDOWS, MonthStudy, profile_info, route_groups
from tools.replay_peak_router import load_prospective_profiles
from datetime import date

ROOT = Path(__file__).resolve().parents[1]


class MonthScanTests(unittest.TestCase):
    def test_frozen_profiles_reject_same_and_later_training_days(self):
        model = load_prospective_profiles(ROOT/'data')
        for day,expected in [('2026-10-01','timetable_only'),('2026-10-10','timetable_only'),
                             ('2026-10-11','timetable_only'),('2026-10-13','timetable_only'),
                             ('2026-10-14','historical_if_supported'),('2026-10-17','historical_if_supported')]:
            info = profile_info(model,date.fromisoformat(day))
            self.assertEqual(info['mode'],expected)
            self.assertEqual(info['day_type'],'weekend' if date.fromisoformat(day).weekday()>=5 else 'weekday')

    def test_assignment_has_exactly_one_of_four_nonoverlapping_windows(self):
        sampler = MonthStudy.__new__(MonthStudy)
        sample = {sampler.assigned_peak(MONTH[0], str(v)) for v in range(200)}
        self.assertEqual(sample,set(WINDOWS))
        self.assertEqual(sampler.assigned_peak(MONTH[0],'a'),sampler.assigned_peak(MONTH[0],'a'))
        intervals = sorted(WINDOWS.values())
        self.assertTrue(all(b-a==90*60 for a,b in intervals))
        self.assertTrue(all(left[1] <= right[0] for left,right in zip(intervals,intervals[1:])))

    def test_extra_routes_selected_from_static_independently_of_original_cohort(self):
        ids = [str(i) for i in range(70)]
        names = {i:('Route'+i,'DTC') for i in ids}
        names['68'],names['69'] = ('502DOWN','DTC'),('502UP','DTC')
        def pick(order):
            return route_groups(SimpleNamespace(routes=names,patterns=[SimpleNamespace(route=i,stops=['a','b','c'])
                                                         for i in order]),ids[:40])
        first = pick(ids)
        self.assertEqual(first,pick(list(reversed(ids))))
        self.assertEqual(set(first['502_diagnostic']),{'68','69'})
        self.assertEqual(len({i for group in first.values() for i in group}),52)

    def test_written_days_cover_every_issued_boundary_case_once(self):
        folder = ROOT/'data'/'month-boarding'
        files = sorted(folder.glob('2026-10-*.json.gz'))
        if not files:
            self.skipTest('No monthly day has been published')
        manifest = json.loads((folder/'manifest.json').read_text())
        for file in files:
            with gzip.open(file,'rt',encoding='utf-8') as src:
                day = json.load(src)
            self.assertTrue(day['complete'])
            self.assertEqual(day['signature'],manifest['signature'])
            rows = day['opportunities']
            self.assertEqual(len(rows),len({r['bus'] for r in rows}))
            expected = {r['run'] for r in rows if 1 <= r['gap'] <= 30}
            actual = day['router']['rows']
            self.assertEqual(len(expected),len(actual))
            self.assertEqual(expected,{r['run'] for r in actual})
            self.assertEqual(sum(day['census']['by_hour'].values()),day['census']['raw_records'])
            self.assertEqual(sum(day['census']['by_route'].values()),day['census']['raw_records'])
