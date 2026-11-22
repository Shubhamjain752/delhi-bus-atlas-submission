import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from test_router import feed
from tools.near_miss_peak import token
from tools.replay_peak_router import (
    BeforeExitVeto, CapturedOverlay, MonthRouter, load_prospective_profiles, matched_trip, month_counts,
    prepared_rows, replay_one,
)
from tools.train_eta import open_db
from transit.eta import EtaModel, IST
from transit.router import Router


class MonthlyProductionRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.db_path = self.directory/'day.db'
        self.day = '2026-10-15'
        self.issue = int(datetime(2026,10,15,8,13,tzinfo=IST).timestamp())
        self.raw_bus, self.raw_trip = 'bus-1','22_08_10_7'
        self.row = dict(day=self.day,peak='AM',route_id='22',upstream='A',stop='B',next_stop='C',
                        ready=self.issue,gap=3,forecast=self.issue-3,origin_gps=self.issue-55,
                        forecast_receipt=self.issue-55,bus=token(self.raw_bus,'bus'),
                        run=token(self.day,self.raw_bus,self.raw_trip),outcome='unverified')
        def edit(content):
            for name in ('trips','routes'):
                for fields in content[name][1:]:
                    if fields[0]=='r2':
                        fields[0]='22'
            for fields in content['stop_times'][1:]:
                if fields[0]=='early':
                    fields[2]+=1
            content['stop_times'].append(['early','A',0,'08:10:00','08:10:00'])
        feed(self.directory/'s.zip',edit)
        self.evaluator = MonthRouter.__new__(MonthRouter)
        self.evaluator.data = self.directory
        self.evaluator.model = EtaModel({'metadata':{'train':['2026-10-13']}})
        self.evaluator.model.weekend = EtaModel({'metadata':{'train':['2026-10-11']}})
        self.evaluator.router = Router(self.directory/'s.zip',self.directory,None,self.evaluator.model)
        self.evaluator.registry = CapturedOverlay(self.evaluator.model,self.directory,0).bind(self.evaluator.router)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('CREATE TABLE vehicle_feed (vehicle_id TEXT,trip_id TEXT,route_id INTEGER,'
                       'timestamp INTEGER,time TEXT,lat REAL,lng REAL)')

    def put(self, observed, receipt, longitude, vehicle=None, raw_trip=None, route=22):
        clock = datetime.fromtimestamp(self.issue+receipt,IST).strftime('%H:%M:%S')
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('INSERT INTO vehicle_feed VALUES(?,?,?,?,?,?,?)',
                       (vehicle or self.raw_bus,raw_trip or self.raw_trip,route,self.issue+observed,clock,28.6,longitude))

    def evaluate(self, rows=None):
        return self.evaluator.evaluate_day(self.day,rows if rows is not None else [self.row],self.db_path)

    def test_every_candidate_counted_and_two_real_policies_equal_the_optimized_pair(self):
        self.put(-55,-55,77.2187)
        self.put(-25,-25,77.21995)
        for t,expected in ((None,True),(-5,False)):
            if t is not None:
                self.put(t,t,77.2208)
            with self.subTest(confirmed_exit=t is not None):
                report=self.evaluate([self.row,dict(self.row,bus='different'),dict(self.row,gap=31,forecast=self.issue-31)])
                self.assertEqual(report['diagnostics']['all_signed_gap_opportunities'],3)
                self.assertEqual(report['overall']['attempted'],2)
                self.assertEqual(report['overall']['exclusions'],{'raw_anchor_not_unique':1})
                case=next(r for r in report['rows'] if 'static_trip' in r)
                with open_db(self.db_path) as db:
                    observations=prepared_rows(db,[self.row])[0][2][1]
                args=(self.evaluator.router,self.evaluator.registry,self.evaluator.model,self.directory,
                      self.row,'early',observations)
                old=replay_one(*args,BeforeExitVeto)
                new=replay_one(*args,CapturedOverlay)
                self.assertTrue(old['conditional_emitted'])
                self.assertEqual(case['baseline_conditional_emitted'],old['conditional_emitted'])
                self.assertEqual(case['conditional_emitted'],new['conditional_emitted'])
                self.assertEqual(case['conditional_emitted'],expected)
                self.assertEqual(case['strict_seconds'],old['strict_seconds'])
                self.assertEqual(report['diagnostics']['extra_baseline_queries'],int(not expected))
                self.assertEqual(report['overall']['outcomes_by_stage']['suppressed_by_exit']['unverified'],int(not expected))

    def test_only_claims_observed_and_received_by_issue_can_affect_matching(self):
        self.put(-55,-55,77.2187)
        self.put(-15,-10,77.21995)
        self.put(-5,2,77.2208)
        self.put(-5,3,77.2198,vehicle='other')
        case=self.evaluate()['rows'][0]
        self.assertTrue(case['conditional_emitted'])
        self.assertEqual(case['position_reports_received'],2)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM vehicle_feed WHERE time>'08:13:00'")
        self.put(-5,-1,77.2198,vehicle='other')
        self.assertEqual(self.evaluate()['rows'][0]['excluded'],'multiple_recent_vehicles_claim_clock')
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE vehicle_feed SET route_id=23,vehicle_id=?,trip_id='23_08_12' WHERE vehicle_id='other'",
                       (self.raw_bus,))
        self.assertEqual(self.evaluate()['rows'][0]['excluded'],'vehicle_reassigned_before_issue')

    def test_report_cannot_use_future_receipt_and_preserves_wrong_suppressions(self):
        bad=dict(self.row,forecast_receipt=self.issue+1)
        self.assertEqual(self.evaluate([bad])['overall']['exclusions'],{'invalid_issue_or_receipt_clock':1})
        rows=[dict(self.row,outcome='approaching',baseline_conditional_emitted=True,conditional_emitted=False),
              dict(self.row,outcome='gone',baseline_conditional_emitted=False,conditional_emitted=False)]
        summary=month_counts(rows)
        self.assertEqual(summary['outcomes_by_stage']['not_offered']['approaching'],1)
        self.assertEqual(summary['outcomes_by_stage']['suppressed_by_exit']['approaching'],1)
        with self.assertRaises(ValueError):
            self.evaluate([dict(self.row,day='2026-10-16')])

    def test_same_minute_with_two_plausible_official_trips_or_wrong_service_cannot_match(self):
        registry = self.evaluator.registry
        pair = ('22',8*60+10)
        router = self.evaluator.router
        self.assertEqual(matched_trip(router,registry,self.row,pair[1]),(None,'early'))
        self.assertEqual(matched_trip(router,registry,dict(self.row,day='2026-10-17'),pair[1])[0],
                         'no_active_trip_at_start_minute')
        self.assertEqual(matched_trip(router,registry,dict(self.row,next_stop='A'),pair[1])[0],
                         'ordered_stop_pattern_mismatch')
        ambiguous = SimpleNamespace(by_clock={pair:list(registry.by_clock[pair])*2})
        self.assertEqual(matched_trip(router,ambiguous,self.row,pair[1])[0],'ambiguous_static_pattern')

    def test_learned_profiles_are_date_gated_and_contain_no_held_out_receipts(self):
        model=load_prospective_profiles(Path(__file__).resolve().parents[1]/'data')
        for profile,training in ((model,'2026-10-13'),(model.weekend,'2026-10-11')):
            self.assertEqual(profile.last_train_day,date.fromisoformat(training))
            self.assertFalse(profile.passages or profile.recent)
            route,origin,dest=next(iter(profile.pairs)).split('|')
            day=date.fromisoformat(training)
            issue=datetime(day.year,day.month,day.day,23,59,tzinfo=IST)
            self.assertEqual(model.duration(route,origin,dest,43200,300,asof=issue,service_date=day),
                             (300,'timetable'))
