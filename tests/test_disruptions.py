from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from transit.eta import EtaModel
from transit.live import LiveOverlay, protobuf
from transit.router import IST, Router
from test_router import feed


def add_services(content):
    for i,s in enumerate("XYZR"):
        content["stops"].append([s,s,28.61+i/50,77.4])
    for rt in ("line","link","ambiguous"):
        content["routes"].append([rt,rt,"DTC"])

    def trip(rt,name,places,hour,minute):
        content["trips"].append([rt,"s",name,""])
        for i,s in enumerate(places):
            m=minute+4*i
            t=f"{hour+m//60:02}:{m%60:02}:00"
            content["stop_times"].append([name,s,10+i*10,t,t])
    for n in range(7):
        trip("line",f"bus{n}","XYZ",8,7+3*n)
    trip("link","connector","YR",8,14)
    trip("link","later-connector","YR",8,40)
    trip("ambiguous","ambiguity-one","XYZ",10,30)
    trip("ambiguous","ambiguity-two","XZR",10,30)


class Disruptions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        p=Path(self.tmp.name)
        feed(p/"service.zip",add_services)
        self.router=Router(p/"service.zip",p,None,None)
        self.live=LiveOverlay(EtaModel(),p).bind(self.router)
        self.router.eta=self.live
        self.at=datetime(2026,10,15,8,1,tzinfo=IST)

    def tearDown(self):
        self.tmp.cleanup()

    def updates(self,*entries,at=None):
        dt=at or self.at
        self.feed=protobuf().FeedMessage()
        self.feed.header.gtfs_realtime_version="2.0"
        self.feed.header.timestamp=int(dt.timestamp())
        for trip,day,route,action in entries:
            u=self.feed.entity.add(id=str(len(self.feed.entity))).trip_update
            u.trip.trip_id=trip
            u.trip.route_id=route
            u.trip.start_date=day
            u.timestamp=int(dt.timestamp())
            if action=="cancel":
                u.trip.schedule_relationship=u.trip.CANCELED
            else:
                s=u.stop_time_update.add()
                if isinstance(action,int):
                    s.stop_sequence=action
                else:
                    s.stop_id=action
                s.schedule_relationship=s.SKIPPED
        return self.live.apply_trip_updates(self.feed,int(dt.timestamp()))

    def rides(self,origin,dest,dt=None,transfers=0,horizon=3600):
        dt=dt or self.at
        with patch("transit.live.time.time",return_value=dt.timestamp()):
            journeys=self.router.journeys(origin,dest,dt,max_transfers=transfers,max_walk_m=0,
                                          transfer_walk_m=0,horizon_seconds=horizon)["journeys"]
        return [l for l in journeys[0]["legs"] if l["mode"]=="bus"] if journeys else []

    def test_skipped_call_blocks_board_alight_and_transfer_but_permits_passage(self):
        result=self.updates(("bus0","20261015","line",20))
        self.assertEqual(result["skipped_stop_calls"],1)
        ride=self.rides("X","Z")[0]
        self.assertEqual(ride["trip_id"],"bus0")
        self.assertEqual([s["id"] for s in ride["stops"]],["X","Z"])
        self.assertEqual([s["id"] for s in ride["skipped_stops"]],["Y"])
        self.assertEqual(self.rides("X","Y")[0]["trip_id"],"bus1")
        self.assertEqual(self.rides("Y","Z")[0]["trip_id"],"bus1")
        self.assertEqual([l["trip_id"] for l in self.rides("X","R",transfers=1)],["bus1","later-connector"])

    def test_skipped_second_leg_alighting_uses_later_trip(self):
        self.updates(("connector","20261015","link","R"))
        self.assertEqual([l["trip_id"] for l in self.rides("X","R",transfers=1)],["bus0","later-connector"])

    def test_multiple_cancellations_fallback_and_refresh(self):
        blocked=[("line_08_07","20261015","line","cancel")]
        blocked.extend((f"bus{i}","20261015","line","cancel") for i in range(1,6))
        self.assertEqual(self.updates(*blocked)["cancelled_trips"],6)
        self.assertEqual(self.rides("X","Z")[0]["trip_id"],"bus6")
        self.assertEqual(self.rides("X","Z",self.at.replace(minute=5))[0]["trip_id"],"bus0")
        self.updates()
        self.assertEqual(self.rides("X","Z")[0]["trip_id"],"bus0")
        self.assertEqual(self.updates(("ambiguous_10_30","20261015","ambiguous","cancel"))["cancelled_trips"],0)

    def test_dated_cancellation_around_midnight(self):
        dt=datetime(2026,10,17,0,2,tzinfo=IST)
        self.updates(("overnight","20261016","r1","cancel"),at=dt)
        self.assertFalse(self.rides("A","B",dt))
        self.updates(("overnight","20261017","r1","cancel"),at=dt)
        self.assertEqual(self.rides("A","B",dt)[0]["trip_id"],"overnight")
        dt=datetime(2026,10,18,23,58,tzinfo=IST)
        self.updates(("newday","20261019","r2","cancel"),at=dt)
        self.assertFalse(self.rides("B","C",dt))
        self.updates(("newday","20261018","r2","cancel"),at=dt)
        self.assertEqual(self.rides("B","C",dt)[0]["trip_id"],"newday")
