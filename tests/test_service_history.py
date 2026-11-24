import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from transit.arrivals import platform_estimate
from transit.contract import Itinerary, PlatformEstimate
from transit.eta import EtaModel, IST
from transit.replay import CapturedOverlay
from transit.router import Router
from test_disruptions import add_services
from test_router import feed


class ServiceHistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.feed = self.directory / "feed.zip"
        feed(self.feed,add_services)
        sha = hashlib.sha256(self.feed.read_bytes()).hexdigest()
        self.header = {"version":1, "feed_sha256":sha, "available_from":"2026-10-01"}
        self.cell = {"scheduled":9,"observed":11,"observed_days":3,
                     "gap_count":5,"gap_days":3,"median_gap_seconds":220}
        self.profile = {**self.header,"training_start":"2026-09-01","training_end":"2026-09-30",
                        "buckets":{"line|X|Y|weekday|4":self.cell,"r1|A|B|weekend|0":self.cell},
                        "platform_gaps":{"line|Y|Z|weekday|4":[6,4,240,420],
                                         "ambiguous|X|Y|weekday|4":[8,4,300,460]}}
        self.named = {**self.header,"buckets":{"line|X|Y|weekday|4":{
            "scheduled":6,"matched":0,"observed_dates":["2026-09-15","2026-09-16"]}}}
        self.when = datetime(2026,10,15,8,2,tzinfo=IST)

    def router(self):
        for path, payload in ((self.directory/"origin-service-history.json.gz",self.profile),
                              (self.directory/"from-2026-11/scheduled-start-check.json.gz",self.named)):
            path.parent.mkdir(exist_ok=True)
            with gzip.open(path,"wt", encoding="utf-8") as file:
                json.dump(payload,file)
        router = Router(self.feed,self.directory,None,None)
        overlay = CapturedOverlay(EtaModel(),self.directory,int(self.when.timestamp())).bind(router)
        return router,overlay

    def first_bus(self, router, overlay, origin="Y", destination="Z", **options):
        journey = router.journeys(origin,destination,self.when,max_transfers=0,max_walk_m=0,
                                  eta=overlay,**options)["journeys"][0]
        Itinerary.model_validate(journey)
        return next(leg for leg in journey["legs"] if leg["mode"]=="bus")

    def test_reports_separate_platform_origin_and_matched_scheduled_counts(self):
        router, overlay = self.router()
        bus = self.first_bus(router,overlay)
        self.assertEqual(bus["origin_departure"]["status"],"unconfirmed")
        self.assertEqual(bus["service_history"]["origin"]["id"],"X")
        self.assertEqual(bus["service_history"]["observed"],11)
        self.assertEqual(bus["service_history"]["scheduled"],9)
        self.assertEqual(bus["scheduled_start_history"]["matched"],0)
        self.assertEqual(bus["scheduled_start_history"]["scheduled"],6)
        self.assertEqual(bus["platform_gap_history"]["platform"]["id"],"Y")
        board = platform_estimate(router,overlay,"Y","line",self.when)
        PlatformEstimate.model_validate(board)
        self.assertEqual(board["platform_gap_history"],bus["platform_gap_history"])
        self.assertEqual(board["wait_seconds"],540)
        self.assertNotIn("platform_gap_history",platform_estimate(router,overlay,"X","ambiguous",self.when))

    def test_sparse_wrong_feed_and_future_data_do_not_supply_a_gap(self):
        self.cell.update(gap_days=2)
        self.profile["platform_gaps"]["line|Y|Z|weekday|4"] = [200,2,20,44]
        router, overlay = self.router()
        bus = self.first_bus(router,overlay)
        self.assertNotIn("median_gap_seconds",bus["service_history"])
        self.assertNotIn("platform_gap_history",bus)
        early = self.first_bus(router,overlay,asof=self.when-timedelta(days=30))
        self.assertNotIn("service_history",early)
        self.assertNotIn("scheduled_start_history",early)
        self.profile["feed_sha256"] = self.named["feed_sha256"] = "other"
        router,overlay = self.router()
        other = self.first_bus(router,overlay)
        self.assertNotIn("service_history",other)
        self.assertNotIn("scheduled_start_history",other)

    def test_overnight_civil_window_keeps_original_service_date(self):
        self.when = self.when.replace(day=17,hour=0,minute=0)
        router,overlay = self.router()
        bus = self.first_bus(router,overlay,"A","B")
        self.assertEqual(bus["origin_departure"]["service_date"],"2026-10-16")
        self.assertEqual(bus["origin_departure"]["scheduled_at"],"2026-10-17T00:05:00+05:30")
        self.assertEqual(bus["service_history"]["day_type"],"weekend")
