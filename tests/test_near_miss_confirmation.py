import json
from pathlib import Path
import unittest

from tools.near_miss_confirmation import policies


class NearMissConfirmationTests(unittest.TestCase):
    def test_suppressed_cases_retain_their_truth_label(self):
        rows = [{'outcome':'uncertain','baseline_conditional_emitted':True,'conditional_emitted':False},
                {'outcome':'gone','baseline_conditional_emitted':True,'conditional_emitted':False},
                {'outcome':'approaching','baseline_conditional_emitted':True,'conditional_emitted':True},
                {'outcome':'unverified','excluded':'unmatched'}]
        result = policies(rows)
        self.assertEqual(result['baseline']['offered'],3)
        self.assertEqual(result['revised']['offered'],1)
        self.assertEqual(result['suppressed'],{'offered':2,'approaching':0,'gone':1,'uncertain':1,'unverified':0})
        with self.assertRaises(ValueError):
            policies([{'outcome':'gone','conditional_emitted':True}])

    def test_published_confirmation_has_an_untouched_date_and_complete_paired_counts(self):
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root/'data'/'near-miss-confirmation.json').read_text())
        replay = json.loads((root/'data'/'near-miss-router-confirmation.json').read_text())
        primary = json.loads((root/'data'/'near-miss-peak.json').read_text())
        self.assertEqual(report['date'],'2026-10-29')
        self.assertNotIn(report['date'],[*primary['test_dates'],primary['development_date']])
        self.assertEqual({r['day'] for r in replay['rows']},{report['date']})
        self.assertEqual(report['positive_gap_1_30'],len(replay['rows']))
        expected = policies(replay['rows'])
        self.assertEqual({k:report['router'][k] for k in expected},expected)
