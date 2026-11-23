from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from transit.eta import EtaModel, IST
from transit.live import protobuf
from transit.replay import CapturedOverlay
from transit.router import Router
from test_router import feed


def add_corridor(data):
    data["trips"][0].append("direction_id")
    for n, stop in enumerate("WXYZ"):
        data["stops"].append([stop,stop,28.59 + n*.004,77.41])
    for route, trip, times in [
        ("red", "red0", ("08:00", "08:05", "08:08", "08:11")),
        ("blue", "blue0", ("07:59", "08:03", "08:06", "08:09")),
        ("red", "night", ("24:00", "24:05", "24:08", "24:11")),
        ("blue", "night-blue", ("00:00", "00:03", "00:06", "00:09")),
    ]:
        if not any(row[0] == route for row in data["routes"]):
            data["routes"].append([route,route,"DTC"])
        data["trips"].append([route, "s", trip, "", "1"])
        for n, (stop, clock) in enumerate(zip("WXYZ", times)):
            data["stop_times"].append([trip,stop,n+1,clock+":00",clock+":00"])


class JourneyPositionTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        feed(self.directory / "feed.zip", add_corridor)
        self.when = datetime(2026,10,15,8,1,tzinfo=IST)
        self.router = Router(self.directory / "feed.zip",self.directory,None,None)

    def gps(self, at=None, route="red", run="red0", service_date="20261015", age=0,
            receipt_delay=0, vehicles=("D1",), stop="X", direction=None):
        self.when = at or self.when
        issue = int(self.when.timestamp())
        self.overlay = CapturedOverlay(EtaModel(),self.directory,issue).bind(self.router)
        message = protobuf().FeedMessage()
        message.header.gtfs_realtime_version = "2.0"
        message.header.timestamp = issue - age
        for vehicle in vehicles:
            vp = message.entity.add(id=vehicle).vehicle
            vp.trip.route_id, vp.trip.trip_id, vp.trip.start_date = route, run, service_date
            if direction is not None:
                vp.trip.direction_id = direction
            vp.vehicle.id, vp.timestamp = vehicle, issue - age
            vp.position.latitude = self.router.stops[stop]["lat"]
            vp.position.longitude = self.router.stops[stop]["lon"]
        self.message = message
        self.overlay.apply_positions(message, issue + receipt_delay)

    def buses(self, **options):
        result = self.router.journeys("Y","Z",self.when,eta=self.overlay,max_walk_m=0,
                                      max_transfers=0,**options)
        return [next(l for l in j["legs"] if l["mode"]=="bus") for j in result["journeys"]]

    def test_upstream_receipt_changes_which_direct_line_arrives_first(self):
        self.gps()
        self.assertEqual(self.buses(use_position_board_times=False)[0]["route_id"],"blue")
        bus = self.buses()[0]
        self.assertEqual(bus["route_id"],"red")
        self.assertEqual(bus["calls"][0]["source"],"upstream_gps")
        self.assertEqual(bus["calls"][0]["departure"],"2026-10-15T08:04:00+05:30")
        self.assertEqual(bus["arrival"],"2026-10-15T08:07:00+05:30")
        self.assertEqual(bus["boarding_position"],{"observed_stop_id":"X",
                         "observed_at":self.when.isoformat(),"received_at":self.when.isoformat()})

    def test_prior_service_day_inferred_from_unique_stop_clock(self):
        for clock in ("24","00"):
            self.gps(at=self.when.replace(hour=0),run=f"red_{clock}_00_900",service_date="")
            bus = self.buses()[0]
            self.assertEqual((bus["trip_id"],bus["calls"][0]["source"]),("night","upstream_gps"))

    def test_missing_unsafe_and_unmatched_positions_preserve_ordinary_order(self):
        for changes in ({"age":181}, {"receipt_delay":1}, {"vehicles":("D1","D2")},
                        {"service_date":"20261013"}, {"route":"blue"},
                        {"run":"unknown"}, {"run":"red_02_12"}, {"direction":0}, {"age":-1}):
            with self.subTest(changes=changes):
                self.gps(**changes)
                self.assertEqual(self.buses()[0]["route_id"], "blue")

    def test_a_bus_already_seen_downstream_is_not_suggested_upstream(self):
        self.gps(stop="Z")
        self.assertNotIn("red0",[b["trip_id"] for b in self.buses()])

    def test_at_platform_keeps_prior_forecast_without_a_departure_claim(self):
        self.gps(stop="Y")
        self.assertEqual(self.buses(),self.buses(use_position_board_times=False))

    def test_newer_off_stop_fix_and_withdrawn_vehicle_retire_old_projection(self):
        for withdrawn in (False,True):
            self.gps(age=30)
            self.assertEqual(self.buses()[0]["route_id"],"red")
            self.message.header.timestamp += 30
            if withdrawn:
                self.message.ClearField("entity")
            else:
                latest = self.message.entity[0].vehicle
                latest.timestamp += 30
                latest.position.latitude += .0015
            self.overlay.apply_positions(self.message,int(self.when.timestamp()))
            self.assertEqual(self.buses(),self.buses(use_position_board_times=False))

    def test_backward_order_and_two_vehicle_aliases_do_not_supply_a_boarding_time(self):
        self.gps(age=30,stop="Z")
        self.message.header.timestamp += 30
        latest = self.message.entity[0].vehicle
        latest.timestamp += 30
        latest.position.latitude = self.router.stops["X"]["lat"]
        self.overlay.apply_positions(self.message,int(self.when.timestamp()))
        self.assertEqual(self.buses(),self.buses(use_position_board_times=False))
        self.gps()
        other = self.message.entity.add(id="other").vehicle
        other.CopyFrom(self.message.entity[0].vehicle)
        other.vehicle.id,other.trip.trip_id = "other","red_08_00_17"
        other.position.latitude += .001
        self.overlay.apply_positions(self.message,int(self.when.timestamp()))
        self.assertEqual(self.buses(),self.buses(use_position_board_times=False))

    def test_matched_live_forecast_retains_priority_and_its_source(self):
        self.gps()
        stamp = int(self.when.timestamp())
        feed_message = protobuf().FeedMessage()
        feed_message.header.gtfs_realtime_version = "2.0"
        feed_message.header.timestamp = stamp
        update = feed_message.entity.add(id="forecast").trip_update
        update.trip.trip_id, update.trip.route_id, update.trip.start_date = "red0","red","20261015"
        update.timestamp = stamp
        for stop, delta in (("Y",420),("Z",600)):
            call = update.stop_time_update.add(stop_id=stop)
            call.departure.time = stamp + delta
        self.overlay.apply_trip_updates(feed_message,stamp)
        red = next(b for b in self.buses() if b["route_id"]=="red")
        self.assertEqual(red["calls"][0]["source"],"live")
        self.assertNotIn("boarding_position",red)
