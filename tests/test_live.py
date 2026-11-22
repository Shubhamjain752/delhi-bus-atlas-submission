from datetime import datetime
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from transit.eta import EtaModel
from transit.live import LiveOverlay, protobuf
from transit.router import IST, Router
from test_router import feed


class LiveUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        p=Path(self.tmp.name)
        feed(p/"s.zip")
        self.r=Router(p/"s.zip",p,None,None)
        self.model=LiveOverlay(EtaModel(),p).bind(self.r)
        self.now=int(datetime(2026,10,15,8,1,tzinfo=IST).timestamp())
        self.date=datetime.fromtimestamp(self.now,IST).date()

    def tearDown(self):
        self.tmp.cleanup()

    def empty(self,now):
        f=protobuf().FeedMessage()
        f.header.gtfs_realtime_version="2.0"
        f.header.timestamp=now
        return f

    def add_update(self,f,trip_id="r1_08_00"):
        entity=f.entity.add(id=str(len(f.entity)))
        update=entity.trip_update
        update.trip.trip_id=trip_id
        update.trip.route_id="r1"
        update.trip.start_date="20261015"
        update.timestamp=self.now
        row=update.stop_time_update.add(stop_id="B",stop_sequence=1)
        row.arrival.time=self.now+300

    def test_unique_trip_clock_stops_and_asof(self):
        f=self.empty(self.now)
        self.add_update(f)
        self.assertEqual(self.model.apply_trip_updates(f,self.now)["matched_stop_updates"],1)
        with patch("transit.live.time.time",return_value=self.now):
            self.assertEqual(self.model.departure("first","B",self.date,self.now+500,self.now),self.now+300)
            self.assertIsNone(self.model.departure("first","B",self.date,self.now+500,self.now-10))
            self.assertTrue(self.model.status()["usable"])
        with patch("transit.live.time.time",return_value=self.now+500):
            self.assertIsNone(self.model.departure("first","B",self.date,self.now+500,self.now+500))
            self.assertEqual(self.model.status()["state"],"stale")
        self.add_update(f)
        self.assertEqual(self.model.apply_trip_updates(f,self.now)["matched_stop_updates"],0)

    def test_foreign_route_and_wrong_order(self):
        f=self.empty(self.now)
        self.add_update(f,"first")
        f.entity[0].trip_update.trip.route_id="r2"
        self.assertEqual(self.model.apply_trip_updates(f,self.now)["matched_trips"],0)
        f.entity[0].trip_update.trip.route_id="r1"
        u=f.entity[0].trip_update.stop_time_update.add(stop_id="A",stop_sequence=2)
        u.arrival.time=self.now+400
        self.assertEqual(self.model.apply_trip_updates(f,self.now)["matched_trips"],0)
        self.assertEqual(self.model.apply_trip_updates(f,self.now+500)["state"],"stale")

    def test_official_positions_need_adjacent_progress(self):
        def position(stamp,s):
            f=self.empty(stamp)
            v=f.entity.add(id="V").vehicle
            v.vehicle.id="V"
            v.trip.route_id="r1"
            v.trip.trip_id="inferred_23"
            v.timestamp=stamp
            v.position.latitude=self.r.stops[s]["lat"]
            v.position.longitude=self.r.stops[s]["lon"]
            return f
        a=self.model.apply_positions(position(self.now,"A"),self.now)
        self.assertEqual(a["segments_available"],0)
        b=self.model.apply_positions(position(self.now+180,"B"),self.now+180)
        self.assertEqual(b["segments_available"],1)
        self.assertEqual(self.model.apply_positions(position(self.now+180,"B"),self.now+180)["new_segment_samples"],0)
        with patch("transit.live.time.time",return_value=self.now+180):
            sec,source=self.model.duration("r1","A","B",8*3600,600,
                                          datetime.fromtimestamp(self.now+180,IST))
            self.assertEqual(source,"gps_live_segment")
            self.assertLess(sec,600)
