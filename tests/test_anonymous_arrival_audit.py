import gzip
import json
import sqlite3
from contextlib import closing
from datetime import date,datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from collections import defaultdict

from tools.fleet_anonymous_arrivals import Audit,ReplayRouter,cases,summarize
from tools.partition_route_observations import empty_day,issue_day,move_counts
from transit.router import IST,Router


class AnonymousMovementAudit(unittest.TestCase):
    def test_platform_clock_crosses_confirmation_cutoff_before_the_boarding_clock(self):
        midnight=int(datetime(2026,10,24,tzinfo=IST).timestamp())
        prior,today=issue_day(midnight-6),issue_day(midnight+13)
        self.assertEqual((prior,today),('2026-10-23','2026-10-24'))
        counts=defaultdict(empty_day)
        counts[today]['coverage'].update(route_hour_candidates=2,unresolved_clock=1,movement_hint=1)
        move_counts(counts,today,prior,'movement_hint')
        move_counts(counts,today,prior,None)
        self.assertEqual(counts[prior]['coverage'],{'route_hour_candidates':2,'unresolved_clock':1,'movement_hint':1})
        self.assertFalse(any(counts[today]['coverage'].values()))

    def test_replay_geometry_cache_reuses_exact_gtfs_and_fallback_vertices(self):
        stops={'a':{'lat':28.6,'lon':77.2},'b':{'lat':28.61,'lon':77.2}}
        points=[[28.6,77.2],[28.605,77.2],[28.61,77.2]]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'geometry.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.execute('create table shapes(id text primary key,points text)')
                db.execute('insert into shapes values (?,?)',('aligned',json.dumps(points)))
                db.commit()
            anchors={}
            for _ in range(2):
                r=ReplayRouter.__new__(ReplayRouter)
                r.geometry_anchors,r.stops,r.cache_path=anchors,stops,path
                r.patterns=[SimpleNamespace(shape=k,stops=('a','b')) for k in ('aligned','missing')]
                for i in range(2):
                    self.assertEqual(r._geometry(i),Router._geometry(r,i))
            self.assertEqual(len(anchors),2)

    def test_unseen_fixes_do_not_supply_direction_and_duplicates_add_no_evidence(self):
        row={'forecast_receipt':100,'origin_gps':95,'observations':[
            [83,80,28.6,77.19],[100,95,28.6,77.191],[101,99,28.6,77.192]]}
        self.assertEqual(Audit.causal_pair(row),((80,83,(28.6,77.19)),(95,100,(28.6,77.191))))
        row['observations']=[[99,95,28.6,77.191],[100,95,28.6,77.191],[101,99,28.6,77.192]]
        self.assertIsNone(Audit.causal_pair(row))

    def test_requested_downstream_platform_cannot_select_between_anonymous_patterns(self):
        a=Audit.__new__(Audit)
        patterns=[SimpleNamespace(route='one',service='all',stops=stops,pickups=[0]*len(stops))
                  for stops in [('A','B','C'),('A','D')]]
        a.router=SimpleNamespace(patterns=patterns,at={'A':[(0,0),(1,0)]},_active=lambda day:{'all'})
        self.assertFalse(a.paths('one','A','B',(date(2026,10,1),)))
        a.router.at={'A':[(0,0)]}
        self.assertTrue(a.paths('one','A','B',(date(2026,10,2),)))

    def test_case_stream_and_unresolved_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'day.gz'
            with gzip.open(path,'wt') as stream:
                json.dump({'hours':'0'*65540,'cases':[{'one':1},{'two':2}]},stream,separators=(',',':'))
            self.assertEqual(list(cases(path)),[{'one':1},{'two':2}])
        rows=[{'wait_s':60,'timetable_link_wait_s':90,'ordinary_wait_s':None,'lower_s':50,'upper_s':70},
              {'wait_s':90,'timetable_link_wait_s':100,'ordinary_wait_s':600}]
        out=summarize(rows)
        self.assertEqual((out['hints'],out['verified'],out['unverified'],out['estimate']['within_60s']),(2,1,1,1))
