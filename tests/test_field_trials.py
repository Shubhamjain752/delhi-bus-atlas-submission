import json
from pathlib import Path
import tempfile
import unittest

from transit.field_trials import field_results


class ProspectiveResults(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.query = {"departure":"2026-11-22T15:00:00+05:30"}
        self.save("plan.json", {"timezone":"Asia/Kolkata", "cases":{"a":self.query}})
        self.save("a/atlas/prediction.json", {"query":self.query, "case":"a", "provider":"atlas", "captured":"2026-11-22T14:00:00+05:30",
                                              "raw_sha256":"original", "earliest":{"arrival":"2026-11-22T15:30:00+05:30"}})
        self.payload = {"scenario":{"case":"a", "arm":"atlas", **self.query,"prediction_sha256":"original"},
                        "source":{"type":"captured_vehicle_positions"},"simulations":[{"status":"no_later_witnessed_boarding"}]}

    def save(self, name, value):
        path = self.data / "whole-journey" / name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value),encoding="utf-8")

    def test_diagnostic_success_never_enters_primary_result(self):
        self.payload["supplemental_directed_shape_crossings"] = {"simulations":[{"status":"complete_simulation"}]}
        self.save("a/simulation/atlas-result.json",self.payload)
        arm = field_results(self.data)["cases"][0]["providers"]["atlas"]
        self.assertEqual(arm["simulations"],[{"status":"no_later_witnessed_boarding"}])
        self.assertEqual(arm["diagnostics"]["shape_crossings"][0]["status"],"complete_simulation")

    def test_other_date_or_forecast_cannot_score_registered_case(self):
        for changed in ({"departure":"2026-10-25T15:00:00+05:30"},{"prediction_sha256":"new"}):
            self.save("a/simulation/atlas-result.json",{**self.payload,"scenario":{**self.payload["scenario"],**changed}})
            with self.assertRaises(ValueError):
                field_results(self.data)
        self.save("a/simulation/atlas-result.json",{**self.payload,"source":{"type":"official_october_daily_sqlite"}})
        with self.assertRaises(ValueError):
            field_results(self.data)

    def test_interim_observations_remain_separate_until_final(self):
        self.save("a/simulation/atlas-interim.json", self.payload)
        arm = field_results(self.data)["cases"][0]["providers"]["atlas"]
        self.assertEqual(arm["simulations"], [])
        self.assertEqual(arm["interim"]["simulations"], self.payload["simulations"])
        self.save("a/simulation/atlas-result.json", self.payload)
        arm = field_results(self.data)["cases"][0]["providers"]["atlas"]
        self.assertNotIn("interim", arm)
        self.assertEqual(arm["simulations"], self.payload["simulations"])

    def test_case_notes_need_two_completed_forecasts_for_the_same_journey(self):
        summary = {"case": "a", **self.query, "headline": "Both forecasts checked"}
        self.save("a/observed-legs.json", summary)
        self.save("a/witness-status.json", {"state": "scored"})
        self.save("a/simulation/atlas-result.json", self.payload)
        self.assertNotIn("observed_legs", field_results(self.data)["cases"][0])
        forecast = json.loads((self.data / "whole-journey/a/atlas/prediction.json").read_bytes())
        self.save("a/google/prediction.json", {**forecast, "provider": "google"})
        self.save("a/simulation/google-result.json", {
            **self.payload, "scenario": {**self.payload["scenario"], "arm": "google"}})
        self.assertEqual(field_results(self.data)["cases"][0]["observed_legs"], summary)
        self.save("a/observed-legs.json", {**summary, "departure": "2026-11-23T15:00:00+05:30"})
        case = field_results(self.data)["cases"][0]
        self.assertNotIn("observed_legs", case)
        self.assertEqual(case["providers"]["atlas"]["simulations"], self.payload["simulations"])

    def test_damaged_control_keeps_registered_cases_readable(self):
        folder = self.data / "whole-journey-walk-check"
        folder.mkdir()
        (folder / "control-registration.json").write_text("{}", encoding="utf-8")
        (folder / "control-registration-receipt.json").write_text('{"sha256":"mismatch"}', encoding="utf-8")
        result = field_results(self.data)
        self.assertEqual(result["supplemental_control"]["state"], "unverified")
        self.assertEqual(result["cases"][0]["providers"]["atlas"]["forecast"]["arrival"], "2026-11-22T15:30:00+05:30")

    def test_near_departure_requires_matching_query_and_predeparture_clock(self):
        ledger = {"query":self.query,"case":"a","kind":"separate_near_departure_forecast","state":"partial_or_failed",
                  "providers":{"atlas":{"state":"captured","captured_at":"2026-11-22T14:51:00+05:30","arrival":"2026-11-22T15:32:00+05:30"},
                               "google":{"state":"captured","captured_at":"2026-11-22T15:00:01+05:30"}}}
        self.save("near-departure/a/status.json",ledger)
        case = field_results(self.data)["cases"][0]
        self.assertEqual(case["near_departure"]["providers"]["google"],{"state":"outside_capture_window"})
        self.assertNotEqual(case["providers"]["atlas"]["forecast"]["arrival"],case["near_departure"]["providers"]["atlas"]["arrival"])
        self.save("near-departure/a/status.json",{**ledger,"case":"another"})
        self.assertEqual(field_results(self.data)["cases"][0]["near_departure"]["state"],"mismatched_query")
