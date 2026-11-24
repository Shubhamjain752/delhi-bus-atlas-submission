from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from tools.fleet_gps import FIX
from tools.month_eta_labels import COLUMNS, VERSION, DatedLabels, Matcher, index_stops, midnight, validate_checkpoint
from tools.train_eta import window_events


class MonthlyLabelsTest(unittest.TestCase):
    def test_late_duplicates_and_midnight_match_reference_across_small_batches(self):
        day = '2026-10-25'
        t = midnight(day)
        scale = np.array([111190., 111190. * np.cos(np.deg2rad(28.7))])
        locations = np.array([[28.7, 77.10], [28.7, 77.105], [28.7, 77.110]])
        stops = ['10', '20', '30']
        route = (stops, [70, 80], dict.fromkeys(stops, 1), cKDTree(locations * scale), scale)
        with patch('tools.month_eta_labels.gtfs_routes', return_value={'3': route}):
            model = index_stops(None, {'3'})
        # The 00:00:20 reading arrives after the 00:00:38 reading. The first
        # observation at -10 wins over a duplicate reporting another location.
        # A long outage is followed by a position inside a stop, not an entry.
        cases = [(-40, -38, 77.097), (-10, -8, 77.10), (-10, -5, 77.105),
                 (38, 40, 77.108), (20, 46, 77.105), (60, 70, 77.110),
                 (200, 201, 77.105), (209, 210, 77.110)]
        fixes = np.array([(8, 7, 3, -1, t + obs, t + receipt, 28.7, lon) for obs, receipt, lon in cases], dtype=FIX)
        # A second vehicle reusing the trip identifier never supplies a stop on the first vehicle's journey.
        other = np.array([(9, 7, 3, -1, t + 5, t + 6, 28.7, 77.105)], dtype=FIX)
        fixes = np.concatenate((fixes, other))
        fixes = fixes[np.argsort(fixes['recv'], kind='stable')]
        frame = pd.DataFrame({'route': fixes['route'], 'vehicle': fixes['bus'], 'trip': fixes['trip'],
                              'lat': fixes['lat'], 'lng': fixes['lon'], 'observed': fixes['obs'], 'received': fixes['recv']})
        original, _ = window_events(frame, {'3': route}, t)
        self.assertEqual([seg[5] for seg in original], [30, 40])
        expected = [(int(r), int(a), int(b), at-duration, at, received, duration, scheduled)
                    for received, at, r, a, b, duration, scheduled, _, _ in original]
        for batch in (1, 3, 20):
            matcher, extracted = Matcher(model), []
            for offset in range(0, len(fixes), batch):
                chunk = fixes[offset:offset+batch]
                extracted.append(matcher.advance(chunk, int(chunk['recv'][-1]) - 90))
            extracted.append(matcher.advance(np.empty(0, dtype=FIX), 2**31 - 1))
            rows = np.concatenate(extracted)
            self.assertEqual([tuple(r) for r in rows[:, [0, 1, 2, 3, 4, 6, 7, 8]]], expected)
            self.assertEqual(rows[:, 5].tolist(), [t-8, t+46])
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            writer = DatedLabels(directory, [day])
            writer.add(rows, day)
            hourly = {f'{h:02}': 9 if h == 0 else 0 for h in range(24)}
            writer.finalize(day, {'raw': 9, 'scan_seconds': 1, 'method': VERSION, 'day': day,
                                  'source_member_crc32': 'A1234567', 'hourly_raw': hourly})
            label_file = directory / (day + '.npz')
            with np.load(label_file) as labels:
                self.assertEqual(tuple(labels.files), COLUMNS)
                self.assertEqual(labels['start'][0], t-10)
                self.assertEqual(labels['end'][0], t+20)
            member = {'crc32': 'A1234567'}
            self.assertEqual(validate_checkpoint(directory, day, member)['labels'], 2)
            with label_file.open('ab') as target:
                target.write(b'invalid extra bytes')
            with self.assertRaisesRegex(ValueError, 'Changed label archive'):
                validate_checkpoint(directory, day, member)


if __name__ == '__main__':
    unittest.main()
