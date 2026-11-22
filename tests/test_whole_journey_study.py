import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.whole_journey_study import classify, digest, equivalent, report, seal, simulation_report, summarize_events
from tools import capture_near_departure as refresh


def at(start, minutes):
    return (start + timedelta(minutes=minutes)).isoformat()


class JourneyStudyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.start = datetime.now(timezone.utc) + timedelta(hours=1)
        self.case = {"origin": [28.7,77.1], "destination": [28.6,77.2], "departure": at(self.start,0)}
        (self.root/"plan.json").write_text(json.dumps({"cases": {"trial": self.case}}))
        self.legs = [{"route": "7UP", "board": "A", "alight": "B"}]
        self.choice = {"arrival": at(self.start,18), "legs": self.legs}
        self.reading = {**self.case, "choices": [self.choice]}
        self.source = self.root/"google.txt"
        self.source.write_text("Displayed reading from fixture")
        self.record = self.root/"transcription.json"
        self.record.write_text(json.dumps(self.reading))
        self.capture_time = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()

    def events(self, simulated=False):
        records = [
            ("depart_origin",0,None,None,"scenario_clock"),
            ("ready_at_stop",3,"A",None,"walk_model"),
            ("board",5,"A","7UP","bus_gps"),
            ("alight",13,"B","7UP","bus_gps"),
            ("arrive_destination",17,None,None,"walk_model"),
        ]
        events = []
        for name,minute,stop,route,source in records:
            event = {"kind":name,"at":at(self.start,minute),"source":source if simulated else "rider_clock"}
            if stop:
                event["stop_id"] = stop
            if route:
                event["route"] = route
            if simulated and source=="bus_gps":
                event.update(lower=at(self.start,minute-.25),upper=at(self.start,minute+.25),evidence="test trace")
            events.append(event)
        return events

    def freeze(self):
        return seal("trial","google",self.source,self.capture_time,self.record,root=self.root)

    def test_forecast_seal_rejects_future_duplicate_or_retrospective(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError,"Already sealed"):
            self.freeze()
        with self.assertRaisesRegex(ValueError,"before the planned departure"):
            seal("trial","atlas",self.source,at(self.start,2),root=self.root)
        expired = {**self.case,"departure":at(self.start,-120)}
        (self.root/"plan.json").write_text(json.dumps({"cases":{"trial":expired}}))
        with self.assertRaisesRegex(ValueError,"before the planned departure"):
            self.freeze()

    def test_comparable_clocks_and_pins(self):
        for change in ({"departure": at(self.start,1)}, {"origin":self.case["destination"]}):
            with self.assertRaises(ValueError):
                classify(self.case,{**self.reading,**change})
        with self.assertRaisesRegex(ValueError,"UTC offset"):
            classify(self.case,{**self.reading,"departure":"2030-01-01T09:30"})

    def test_earliest_arrival_and_one_change_can_differ(self):
        transfer = {"arrival":at(self.start,20), "legs":self.legs*2}
        choices = classify(self.case,{**self.reading,"choices":[transfer,self.choice]})
        self.assertEqual((choices["earliest"],choices["one_change"]),(self.choice,transfer))

    def test_observed_and_simulated_scores_are_distinct(self):
        self.freeze()
        for simulated,kind in ((True,"simulation"),(False,"observed_passenger")):
            result = report("trial",self.events(simulated),self.root)
            self.assertEqual(result["predictions"]["google"]["earliest"]["predicted_minus_"+kind+"_arrival_seconds"],60)
            self.assertEqual([e["seconds"] for e in result["outcome"]["stages"]],[180,120,480,240])
        self.assertTrue(equivalent([{"route":"7","route_id":"7UP","board":"Stop A","board_id":"A","alight":"B"}],self.legs))

    def test_wrong_bus_or_overlapping_GPS_intervals_are_unscored(self):
        self.freeze()
        for ambiguous in (True,False):
            events = self.events(True)
            if ambiguous:
                events[2]["lower"] = at(self.start,2)
            else:
                events[2]["route"] = events[3]["route"] = "Another service"
            arm = report("trial", events,self.root)["predictions"]["google"]["earliest"]
            self.assertNotIn("predicted_minus_simulation_arrival_seconds",arm)

    def test_transfer_uncertainty_and_missing_event(self):
        events = self.events(True)
        with self.assertRaises(ValueError):
            summarize_events(events[:-1])
        second = [dict(event) for event in events[1:4]]
        second[0].update(at=at(self.start,14),stop_id="B")
        second[1].update(at=at(self.start,15),stop_id="B",lower=at(self.start,14.1),upper=at(self.start,16))
        second[2].update(at=at(self.start,17),stop_id="C",lower=at(self.start,16),upper=at(self.start,18))
        events[-1]["at"] = at(self.start,19)
        outcome = summarize_events(events[:-1] + second + events[-1:])
        self.assertEqual(outcome["simulated_boarding_checks"][1]["order"],"overlap")
        self.assertEqual([s["stage"] for s in outcome["stages"]],
                         ["access_walk","wait","ride","transfer_walk","wait","ride","egress_walk"])

    def test_refresh_only_in_window_and_does_not_overwrite_completed_provider(self):
        current = self.start-timedelta(minutes=10)
        successful = {"state":"captured","captured_at":current.isoformat(),"arrival":at(self.start,20)}
        with patch.object(refresh,"STUDY",self.root), patch.object(refresh,"verify_lock"), patch.object(refresh,"collect") as get:
            with patch.object(refresh,"now",return_value=self.start-timedelta(hours=1)):
                self.assertEqual(refresh.execute("trial","local")["state"],"pending")
                get.assert_not_called()
            with patch.object(refresh,"now",return_value=current):
                get.side_effect = lambda key,case,p,folder,base: successful if p=="atlas" else {"state":"failed"}
                self.assertEqual(refresh.execute("trial","local")["state"],"partial_or_failed")
                get.reset_mock(side_effect=True)
                get.return_value = successful
                result = refresh.execute("trial","local")
                self.assertEqual(result["state"],"complete")
                self.assertEqual([c.args[2] for c in get.call_args_list],["google"])
                self.assertEqual(refresh.execute("trial","local"),result)
                self.assertEqual(get.call_count,1)

    def test_refresh_does_not_recreate_past_predictions(self):
        with patch.object(refresh,"STUDY",self.root), patch.object(refresh,"verify_lock"), patch.object(refresh,"collect") as get:
            with patch.object(refresh,"now",return_value=self.start):
                self.assertEqual(refresh.execute("trial","local")["state"],"incomplete_after_departure")
                get.assert_not_called()
        for offset in (0,-13,1):
            with self.assertRaises(ValueError):
                refresh.check_capture(self.case,at(self.start,offset))

    def test_simulation_reader_keeps_partial_and_historical_results_out_of_accuracy(self):
        frozen = self.freeze()
        saved = self.root/"trial"/"simulation"
        saved.mkdir()
        capture = self.root/"trial"/"capture"
        capture.mkdir()
        marker = capture/"map-marker-readings.json"
        marker.write_text("{}")
        scenario = {**self.case, "case":"trial", "arm":"google", "prediction_sha256":frozen["raw_sha256"],
                    "marker_sha256":digest(marker)}
        (saved/"google-scenario.json").write_text(json.dumps(scenario))
        events = self.events(True)
        events[2].update(stop_id="map:A", stop_label="A", route="route-id", route_label="7UP")
        events[1]["stop_id"]="map:A"
        events[3].update(stop_id="map:B", stop_label="B", route="route-id")
        primary = {"walking_pace_mps":1.25, "transfer_allowance_seconds":0, "events":events,
                   "status":"complete_simulation", "arrival_interval_ist":[at(self.start,16.75),at(self.start,17.25)]}
        source = saved/"result.json"
        source.write_text(json.dumps({"scenario":scenario,"simulations":[primary]}))
        full = simulation_report("trial",source,self.root)["predictions"]["google"]["earliest"]
        self.assertEqual(full["predicted_minus_simulated_arrival_interval_seconds"],[45,75])
        primary.update(events=events[:2],status="insufficient_bus_gps",leg_reached=1)
        source.write_text(json.dumps({"scenario":scenario,"simulations":[primary]}))
        self.assertIsNone(simulation_report("trial",source,self.root)["outcome"])
        scenario["departure"] = at(self.start,-1440)
        source.write_text(json.dumps({"scenario":scenario,"simulations":[primary]}))
        with self.assertRaisesRegex(ValueError,"Historical analogues"):
            simulation_report("trial",source,self.root)
