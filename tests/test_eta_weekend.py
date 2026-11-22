import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from transit.eta import EtaModel, IST


class CalendarEtaTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.sat = int(datetime(2026,10,17,8,tzinfo=IST).timestamp())
        for folder, ratio, train, holdout, replay in [
            (self.root,1.2,'2026-10-13','2026-10-15',[]),
            (self.root/'weekend',2.,'2026-10-11','2026-10-17',
             [[self.sat-7,self.sat-10,'1','weekend-run','a']])]:
            folder.mkdir(exist_ok=True)
            model = {'pairs':{},'routes':{'1':[ratio,{}]},
                     'metadata':{'train':[train],'holdout':holdout,'coverage':{train:{'segments':10}}}}
            with gzip.open(folder/'eta_model.json.gz','wt') as out:
                json.dump(model,out)
            with gzip.open(folder/'eta_replay.json.gz','wt') as out:
                json.dump({'segments':[],'passages':replay},out)
        self.model = EtaModel.load(self.root)

    def tearDown(self):
        self.work.cleanup()

    def test_ist_day_dispatch_and_dated_coverage(self):
        self.assertEqual(self.model.duration('1','a','b',100,50,self.sat),(100,'gps_historical'))
        self.assertEqual(self.model.duration('1','a','b',100,50,self.sat-86400),(60,'gps_historical'))
        # Friday 20:00 UTC is already Saturday in Delhi.
        utc = datetime(2026,10,16,20,tzinfo=timezone.utc)
        self.assertEqual(self.model.duration('1','a','b',100,50,utc)[0],100)
        self.assertEqual(self.model.metadata['day_types']['weekend']['holdout'],'2026-10-17')

    def test_requested_service_day_is_separate_from_replay_clock(self):
        friday = self.sat-86400
        self.assertEqual(self.model.duration('1','a','b',100,50,friday,service_date=date(2026,10,17))[0],100)
        self.assertEqual(self.model.duration('1','a','b',100,50,self.sat,service_date=date(2026,10,19))[0],60)
        self.assertEqual(self.model.recent_passages(self.sat)[0]['trip_id'],'weekend-run')
        self.assertEqual(self.model.recent_passages(self.sat-15),[])

    def test_training_cutoff_applies_to_requested_service_date_without_asof(self):
        self.assertEqual(self.model.duration('1','a','b',100,50,service_date=date(2026,10,10)),(50,'timetable'))
        self.assertEqual(self.model.duration('1','a','b',100,50,service_date=date(2026,10,17))[0],100)
