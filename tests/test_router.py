import csv
import io
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from transit.router import IST, Router


def feed(path,edit=None):
    content = {
        "stops": [
            ["stop_id","stop_name","stop_lat","stop_lon"],
            ["A","Alpha",28.6,77.20], ["B","Change West",28.6,77.22],
            ["C","Clock Tower",28.6,77.24],
        ],
        "routes": [["route_id","route_short_name","agency_id"],["r1","1","DTC"],["r2","2","DTC"]],
        "trips": [["route_id","service_id","trip_id","shape_id"],
                  ["r1","s","first","shape"],["r2","s","early",""],["r2","s","late",""],
                  ["r1","s","overnight","shape"],["r1","s","dwell","shape"],["r2","s","newday",""]],
        "calendar": [["service_id","monday","tuesday","wednesday","thursday","friday","saturday","sunday",
                      "start_date","end_date"],["s",1,1,1,1,1,1,1,"20261001","20261031"]],
        "calendar_dates": [["service_id","date","exception_type"],["s","20261017","2"]],
        "stop_times": [["trip_id","stop_id","stop_sequence","arrival_time","departure_time"]],
        "shapes": [["shape_id","shape_pt_lat","shape_pt_lon","shape_pt_sequence"],
                   ["shape",28.6,77.20,2],["shape",28.601,77.21,3],["shape",28.6,77.22,4]],
    }
    for trip, stops, times in [
        ("first","AB",["08:00:00","08:10:00"]),
        ("early","BC",["08:13:00","08:23:00"]),
        ("late","BC",["08:25:00","08:35:00"]),
        ("overnight","AB",["24:05:00","24:15:00"]),
        ("newday","BC",["00:11:00","00:19:00"]),
        ("dwell","AB",["09:00:00","09:10:00"]),
    ]:
        for i in (1,0):
            departure="09:02:00" if trip=="dwell" and i==0 else times[i]
            content["stop_times"].append([trip,stops[i],i,times[i],departure])
    if edit:
        edit(content)
    with zipfile.ZipFile(path,"w") as archive:
        for name,rows in content.items():
            text = io.StringIO()
            csv.writer(text).writerows(rows)
            archive.writestr(name+".txt",text.getvalue())


class Model:
    def duration(self,route,a,b,service_second,scheduled_seconds,asof=None,service_date=None):
        return (960,"gps_historical") if route=="r1" else (scheduled_seconds,"timetable")


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        feed(data/"sample.zip")
        self.router = Router(data/"sample.zip", data,None,None)
        self.query = datetime(2026,10,15,7,57,tzinfo=IST)

    def tearDown(self):
        self.temp.cleanup()

    def test_predicted_arrival_changes_connecting_bus(self):
        scheduled = self.router.journeys("A","C",self.query)["journeys"][0]
        self.assertEqual([l["trip_id"] for l in scheduled["legs"] if l["mode"]=="bus"],["first","early"])
        self.router.eta = Model()
        predicted = self.router.journeys("A","C",self.query)["journeys"][0]
        self.assertEqual([l["trip_id"] for l in predicted["legs"] if l["mode"]=="bus"],["first","late"])
        self.assertEqual(predicted["transfers"],1)
        self.assertEqual(predicted["duration_seconds"],38*60)
        self.assertEqual(sum(l["seconds"] for l in predicted["legs"]),predicted["duration_seconds"])
        self.assertEqual(predicted["legs"][1]["geometry_source"],"gtfs_shape")
        self.assertEqual(len(predicted["legs"][1]["geometry"]),3)
        self.assertEqual(self.router.search("clock")[0]["id"],"C")

    def test_direct_bus_outside_transfer_shortlist(self):
        def crowded(contents):
            # More than a dozen closer platforms serve unrelated routes. The
            # original boarding and exit stops stay within the walking radius.
            for prefix, lon in (("X", 77.198), ("Y", 77.222)):
                for n in range(13):
                    stop = f"{prefix}{n}"
                    contents["stops"].append([stop, stop, 28.6 + n * .00001, lon])
                    trip = f"noise{stop}"
                    contents["trips"].append(["r2", "s", trip, ""])
                    contents["stop_times"].extend([[trip,stop,0,"05:00:00","05:00:00"],
                                                   [trip,"C",1,"05:10:00","05:10:00"]])
        directory = Path(self.temp.name) / "crowded"
        directory.mkdir()
        feed(directory / "data.zip", crowded)
        class Walking:
            @staticmethod
            def route(a,b,max_distance_m=None):
                return {"distance_m":150,"seconds":120,"source":"osm","geometry":[a,b]}
        router = Router(directory / "data.zip", directory, Walking(), None)
        result = router.journeys("28.6,77.198","28.6,77.222",self.query,max_transfers=0)
        self.assertEqual([l["trip_id"] for l in result["journeys"][0]["legs"] if l["mode"]=="bus"],["first"])

    def test_unmapped_approach_lists_platform_departure_without_claiming_a_walk(self):
        query = self.router.journeys("28.6005,77.20","B",self.query)
        self.assertFalse(query["journeys"])
        item, = query["direct_from_nearby_platforms"]
        self.assertEqual((item["route_id"], item["boarding_stop"]["id"], item["alighting_stop"]["id"]),
                         ("r1", "A", "B"))
        self.assertEqual(item["departure_at"],"2026-10-15T08:00:00+05:30")
        self.assertEqual(item["alight_at"],"2026-10-15T08:10:00+05:30")
        self.assertNotIn("walk_seconds", item)
        self.assertNotIn("duration_seconds", item)
        self.assertFalse(self.router.journeys("28.605,77.20","B",self.query)["direct_from_nearby_platforms"])
        self.assertFalse(self.router.journeys("28.6005,77.20","B",self.query.replace(day=17))["direct_from_nearby_platforms"])

    def test_staying_on_same_bus_beats_unnecessary_change_to_it(self):
        def extend_second_route(contents):
            for row in contents["stop_times"][1:]:
                if row[0] == "early":
                    row[2] += 1
            contents["stop_times"].append(["early","A",0,"08:01:00","08:01:00"])
        directory = Path(self.temp.name) / "through-service"
        directory.mkdir()
        feed(directory / "data.zip", extend_second_route)
        router = Router(directory / "data.zip", directory, None, None)
        plans = router.journeys("A","C",self.query)["journeys"]
        trips = [[leg["trip_id"] for leg in plan["legs"] if leg["mode"] == "bus"] for plan in plans]
        self.assertIn(["early"], trips)
        self.assertNotIn(["first","early"], trips)

    def test_alternative_lines_use_the_same_two_stops_and_original_ready_time(self):
        def services(contents):
            contents["stops"].append(["D","Change East",28.6001,77.22])
            for route, trip, board_time, exit_stop in [
                    ("other","next","08:02:00","B"), ("other","following","08:05:00","B"),
                    ("past","gone","07:56:00","B"), ("wrong-side","wrong","08:03:00","D"),
                    ("too-late","sparse","08:28:00","B")]:
                if [route,route,"DTC"] not in contents["routes"]:
                    contents["routes"].append([route,route,"DTC"])
                contents["trips"].append([route,"s",trip,""])
                contents["stop_times"].extend([[trip,"A",0,board_time,board_time],
                                               [trip,exit_stop,1,"08:32:00","08:32:00"]])
        directory = Path(self.temp.name) / "other-services"
        directory.mkdir()
        feed(directory / "data.zip", services)
        router = Router(directory / "data.zip", directory, None, None)
        result = router.journeys("A","B",self.query)
        leg = next(leg for leg in result["journeys"][0]["legs"] if leg["mode"] == "bus")
        self.assertEqual(leg["trip_id"], "first")
        other, = leg["same_platform_services"]
        self.assertEqual((other["route_id"],other["trip_id"]), ("other","next"))
        self.assertEqual(other["departure_at"],"2026-10-15T08:02:00+05:30")
        day = self.query.date()
        class Cancelled(Model):
            def trip_exclusions(self, asof):
                return {(day,"next"):None}
        router.eta = Cancelled()
        revised = router.journeys("A","B",self.query)
        leg = next(leg for leg in revised["journeys"][0]["legs"] if leg["mode"] == "bus")
        self.assertEqual([s["trip_id"] for s in leg["same_platform_services"]], ["following"])

    def test_after_midnight_previous_service_and_exception(self):
        dt = datetime(2026,10,17,0,2,tzinfo=IST)
        journey = self.router.journeys("A","B",dt,max_transfers=0)["journeys"][0]
        self.assertEqual(journey["legs"][-1]["trip_id"],"overnight")
        dt = datetime(2026,10,17,7,57,tzinfo=IST)
        self.assertFalse(self.router.journeys("A","B",dt)["journeys"])

    def test_transfer_buffer_and_live_departure(self):
        result = self.router.journeys("A","C",self.query,transfer_buffer_seconds=240)
        self.assertEqual(result["journeys"][0]["legs"][-1]["trip_id"],"late")
        class Live:
            def duration(self,route,a,b,service_second,scheduled_seconds,asof=None,service_date=None):
                return scheduled_seconds,"timetable"
            def departure(self,trip,stop,date,scheduled_epoch,asof_epoch):
                return scheduled_epoch+600 if trip=="early" and stop=="B" else None
        self.router.eta = Live()
        result = self.router.journeys("B","C",self.query.replace(hour=8,minute=20),max_transfers=0)
        self.assertEqual(result["journeys"][0]["legs"][-1]["trip_id"],"early")
        self.assertEqual(result["journeys"][0]["legs"][0]["source"],"live")

    def test_coordinate_origin_requires_verified_walk(self):
        origin = "28.6001,77.2"
        self.assertFalse(self.router.journeys(origin,"B",self.query)["journeys"])
        class Walking:
            @staticmethod
            def route(a,b,max_distance_m=None):
                return {"seconds":120,"distance_m":100,"geometry":[a,b],"source":"osm"}
        self.router.walking = Walking()
        result = self.router.journeys(origin,"B",self.query,max_transfers=0)["journeys"][0]
        self.assertEqual(result["walk_seconds"],120)
        self.assertEqual(result["legs"][0]["mode"],"walk")

    def test_identical_stop_or_map_pin_has_zero_duration(self):
        for a,b in (("A","Alpha"),("28.6,77.2","28.600,77.200")):
            journey=self.router.journeys(a,b,self.query)["journeys"][0]
            self.assertEqual(journey["duration_seconds"],0)
            self.assertEqual(journey["legs"][0]["source"],"same_location")

    def test_midnight_crossing_and_operating_date(self):
        dates=[]
        class Clock:
            def duration(self,route,a,b,service_second,scheduled_seconds,asof=None,service_date=None):
                dates.append(service_date.isoformat())
                return scheduled_seconds,"schedule"
        self.router.eta=Clock()
        d=datetime(2026,10,16,23,58,tzinfo=IST)
        ride=self.router.journeys("A","B",d,max_transfers=0)["journeys"][0]
        self.assertEqual(ride["arrival"],"2026-10-17T00:15:00+05:30")
        self.assertIn("2026-10-16",dates)
        self.assertEqual(ride["duration_seconds"],17*60)
        dates.clear()
        d=datetime(2026,10,18,23,58,tzinfo=IST)
        ride=self.router.journeys("B","C",d,max_transfers=0)["journeys"][0]
        self.assertEqual(ride["legs"][-1]["trip_id"],"newday")
        self.assertEqual(ride["arrival"],"2026-10-19T00:19:00+05:30")
        self.assertIn("2026-10-19",dates)

    def test_passage_time_includes_upstream_dwell(self):
        self.router.eta=Model()
        journey=self.router.journeys("A","B",self.query.replace(hour=8,minute=59),max_transfers=0)["journeys"][0]
        self.assertEqual(journey["duration_seconds"],17*60)
        self.assertEqual(journey["wait_seconds"],3*60)
        self.assertEqual(journey["ride_seconds"],14*60)

    def test_early_live_calls_do_not_collapse_against_upstream_timetable(self):
        from transit.router import _Query
        p = next((p for p in self.router.patterns if "first" in p.trips))
        pid = self.router.patterns.index(p)
        midnight = int(self.query.replace(hour=0,minute=0).timestamp())
        class Early:
            def duration(self,route,a,b,service_second,scheduled_seconds,**kwargs):
                return scheduled_seconds, "timetable"
            def departure(self,trip,stop,date,scheduled,asof):
                return midnight + 7*3600 + 55*60 if trip == "first" and stop == "B" else None
        self.router.eta = Early()
        q = _Query(self.router, self.query, {})
        ride = q.timeline(pid, p.trips.index("first"), self.query.date(), 0)
        self.assertEqual(ride.arrivals[-1], 7*3600+55*60)
        self.assertEqual(ride.departures[0],7*3600+45*60)
        self.assertEqual(ride.board_source[0], "live_inferred")
        self.assertEqual(ride.arrivals[-1] - ride.departures[0],600)

    def test_stop_times_and_speed_contract_handles_missing_geometry(self):
        trip = self.router.journeys("A","C",self.query)["journeys"][0]
        from transit.contract import Itinerary
        Itinerary.model_validate(trip)
        first, second = [leg for leg in trip["legs"] if leg["mode"] == "bus"]
        self.assertIsNone(first["calls"][0]["preceding_segment_seconds"])
        call = first["calls"][-1]
        self.assertEqual(call["arrival"],first["arrival"])
        self.assertEqual(call["preceding_segment_seconds"],600)
        self.assertGreater(call["path_distance_m"],1900)
        self.assertAlmostEqual(call["estimated_speed_kmh"],3.6*call["path_distance_m"]/600,places=1)
        self.assertIsNone(second["calls"][-1]["estimated_speed_kmh"])
        self.assertEqual(trip["walk_seconds"],sum(trip[k] for k in ("access_walk_seconds","transfer_walk_seconds","egress_walk_seconds")))

    def test_same_name_different_platforms_require_real_walk(self):
        # A duplicated stop coordinate is insufficient evidence of a transfer path.
        from transit.router import _Query
        q=_Query(self.router,self.query,{})
        a,b=self.router.stops["A"].copy(),self.router.stops["A"].copy()
        b["id"]="other-platform"
        self.assertIsNone(q.walk(a,b))
