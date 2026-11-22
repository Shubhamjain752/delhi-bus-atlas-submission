from datetime import date, datetime, time
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.fleet_cases import evaluate_hour
from tools.fleet_gps import FIX
from tools.fleet_screen import Geometry
from transit.router import IST


class ClockMatcher:
    def match(self,row):
        return (None,'2026-10-21','trip') if row['start_minute']==1430 else ('unknown_clock',None,None)


class FleetScreenTests(unittest.TestCase):
    def test_midnight_case_uses_previous_file_and_prefix_only_identity(self):
        midnight = int(datetime.combine(date(2026,10,22),time(),IST).timestamp())
        # A,B,C,D are ~195m apart. Two completed, ordered entries A and B establish
        # an upstream forecast for C. The incident is owned by readiness on Oct 22.
        coords = [77+i*.002 for i in range(4)]
        desired_ready = midnight+28
        preferred = np.full(65536,-1,'i4')
        preferred[13] = 0
        g = Geometry(['A','B','C','D'],(
            np.array([0,4]),np.array([28.6]*4),np.array(coords),np.array([40,40,40,-1]),
            np.array([0,0,desired_ready%120,0]),np.array([True,True,False,False]),
            np.ones(4,'?'),preferred,np.array([131430]),np.array([0])),{131430:1})
        # A second earlier run has no matched start minute, and may not starve
        # the first calendar-eligible case on this route in the selected hour.
        fixes = []
        for bus,minute,offset in ((7,5,-1),(9,1430,0)):
            for secs,lon in ((-85,76.9993),(-65,77),(-35,77.0010),(-15,77.002),
                             (15,77.0029),(33,77.0034),(45,77.004),(60,77.0048)):
                t = midnight+secs+offset
                fixes.append((bus,bus,13,minute,t,t,28.6,lon))
        # Another claimant's pre-readiness physical observation only reaches us
        # after readiness; the production identity gate cannot use it at issue.
        fixes.append((99,99,13,1430,midnight+19,midnight+34,28.6,77.002))
        arr = np.array(fixes,dtype=FIX)
        arr = arr[np.argsort(arr['recv'],kind='stable')]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            np.save(p/'2026-10-21-23.npy',arr[arr['recv']<midnight])
            np.save(p/'2026-10-22-00.npy',arr[arr['recv']>=midnight])
            np.save(p/'2026-10-22-01.npy',np.empty(0,FIX))
            cells,cases = evaluate_hour(p,'2026-10-22',0,{},g,ClockMatcher())
        self.assertEqual(len(cases),1)
        case = cases[0]
        self.assertEqual(cells['13'][-1],2)
        self.assertEqual(case['first_candidate_exclusion'],'unknown_clock')
        self.assertEqual(case['candidates_checked'],2)
        self.assertTrue(case['eligible_named_case'])
        self.assertEqual(case['outcome'],'approaching')
        self.assertEqual(case['start_minute'],1430)
        self.assertEqual(case['ready'],desired_ready)
        self.assertLess(case['forecast_receipt'],midnight)
        self.assertGreater(case['gps_entry_exit'][0],desired_ready)
        self.assertTrue(all(obs<=recv<=desired_ready for recv,obs,_,_ in case['observations']))
