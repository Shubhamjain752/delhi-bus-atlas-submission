from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

import numpy as np
from tools.fleet_short_links import Link, TerminalReplay, chunks, group, phase
from tools.short_link_prior import CUTOFF, fit, score_event, summary
from transit.eta import EtaModel, IST


class ShortPairs:
    def __init__(self,n=3):
        self.pair = Link(0,tuple('ABC'[:n]),tuple((28.6,77.2+d) for d in (0,0.004,0.008)[:n]),
                         90,n==3,'provider_clock')

    def link(self,r,m):
        return self.pair

    def service_day(self,r,m,l,at):
        return date(2026,10,15)

    def forward(self,link,i,prev,location):
        return (location[1]-prev[1])*98000>=20 and location[1]>link.locations[i][1]


class TerminalLinkStudy(unittest.TestCase):
    def setUp(self):
        # Choose a clock where the fixed two-minute platform grid falls 3 s after the forecast.
        base = int(datetime(2026,10,15,23,59,tzinfo=IST).timestamp())
        self.origin = base+(phase('B')-(base+90)-3)%120
        self.replay = TerminalReplay(ShortPairs(),EtaModel())

    def fix(self,second,lon,receipt=None,bus=10):
        stamp = self.origin+second
        self.replay.accept(1,bus,100,0,stamp,stamp+(2 if receipt is None else receipt),(28.6,77.2+lon))

    def departure(self):
        self.fix(-25,0)
        self.fix(-5,0.00005)
        self.fix(5,0.0006)

    def test_bracket_scoring_and_after_midnight_intermediate_offer_proxy(self):
        self.departure()
        self.fix(55,0.002)
        self.fix(95,0.0031)
        self.fix(108,0.0039)
        event, = self.replay.events
        self.assertEqual(event['entry_lower'],self.origin+95)
        self.assertEqual(event['entry_upper'],self.origin+108)
        self.assertEqual(event['boarding_label'],'approaching')
        self.assertEqual(event['near_ready'],self.origin+93)
        self.assertEqual(group([event])['model']['within_60s_of_midpoint'],1)

    def test_terminal_is_an_arrival_only_and_missing_gps_stays_in_denominator(self):
        self.replay = TerminalReplay(ShortPairs(2),EtaModel())
        self.departure()
        self.fix(90,0.003)
        event, = self.replay.events
        self.assertIsNone(event['near_ready'])
        self.assertNotIn('boarding_label',event)
        self.assertEqual(group([event])['model']['verified_entries'],0)
        self.assertEqual(event['end_reason'],'identity_or_continuity_lost')

    def test_destination_known_at_issue_and_single_origin_fix_cannot_generate_forecast(self):
        self.fix(-15,0)
        self.fix(0,0.0006)
        self.assertFalse(self.replay.tracks[10].get('event'))
        self.fix(20,0)
        self.fix(37,0)
        self.fix(55,0.004)
        self.assertEqual(self.replay.counts[1]['target_in_exit_receipt'],1)
        self.assertFalse(self.replay.tracks[10].get('event'))

    def test_confirmed_gone_intermediate_requires_exit_observed_before_readiness(self):
        self.departure()
        self.fix(35,0.0027)
        self.fix(55,0.004)
        self.fix(77,0.0046)
        self.assertEqual(self.replay.events[0]['boarding_label'],'gone')

    def test_bounded_reader_preserves_rows_and_dtype(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'hour.npy'
            rows = np.arange(65537,dtype='i4')
            np.save(path,rows,allow_pickle=False)
            parts = list(chunks(path))
            self.assertEqual(list(map(len,parts)),[65536,1])
            np.testing.assert_array_equal(np.concatenate(parts),rows)

    def test_short_link_prior_uses_only_pre_cutoff_receipts_and_timetable_fallback(self):
        def event(days,seconds):
            t = CUTOFF-days*86400
            return dict(route=10,from_stop='A',to_stop='B',issue=t+5,departure_lower=t-5,departure_upper=t+5,
                        entry_lower=t+seconds-5,entry_upper=t+seconds+5,entry_received=t+seconds+10,
                        schedule_forecast=t+120,forecast=t+120)
        day1,day2,day3 = [event(d,60) for d in (5,4,3)]
        late = dict(day3,entry_received=CUTOFF)
        self.assertFalse(fit([day1,day2,late,event(-1,5)]))
        profiles = fit([day1,day2,day3,event(-1,5)])
        query = event(-1,60)
        row = score_event(query,profiles)
        self.assertEqual(row['experimental_forecast']-row['schedule_forecast'],-30)
        unknown = score_event(dict(query,route=11),profiles)
        self.assertEqual(unknown['experimental_forecast'],unknown['schedule_forecast'])
        observed = summary([row,unknown])
        self.assertEqual(observed['paired_vs_timetable'],{'improved':1,'equal':1})
        self.assertEqual(observed['covered_verified'],1)
