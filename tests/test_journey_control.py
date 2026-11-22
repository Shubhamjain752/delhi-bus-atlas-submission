from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import score_journey_control as control
from tools.score_simulated_rides import epoch, ride_case, stamp


class ControlTests(unittest.TestCase):
    def test_registered_google_event_has_label_when_boarding_is_witnessed(self):
        scenarios = control.load_scenarios()
        for name, expected in (("google","938A"),("google-159","159")):
            scenario = scenarios[name]
            t = epoch(scenario["departure"])
            def hit(offset):
                return {"lower":t+offset, "upper":t+offset+10, "received":t+offset+15,
                        "evidence":"outside_to_inside_gps"}
            witness = {"board":hit(30), "alight":hit(360), "pattern_status":"serves",
                       "run_token":"fixture", "route":expected}
            simulation = ride_case(scenario, [[witness]], [10,24], 1.25, 0, [[expected]])
            self.assertEqual(simulation["status"], "complete_simulation")
            self.assertEqual(simulation["events"][2]["route_label"], expected)
            self.assertEqual(simulation["events"][3]["route_label"], expected)

    def test_interval_includes_endpoints_and_signs(self):
        t = epoch("2026-11-22T15:10:00+05:30")
        bounds = [stamp(t+40), stamp(t+90)]
        self.assertEqual(control.interval_error(stamp(t+40), bounds), "Within arrival interval")
        self.assertEqual(control.interval_error(stamp(t+100), bounds), "10–60 s late")
        self.assertEqual(control.interval_error(stamp(t+10), bounds), "30–80 s early")

    def test_staged_receipts_and_final_cutoff(self):
        t = epoch("2026-11-22T15:10:00+05:30")
        with TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "raw"
            source.mkdir()
            sim = base / "simulation"
            sim.mkdir()
            inputs = {name:{"departure":stamp(t), "arm":name} for name in control.ARMS}
            for name in inputs:
                (sim / f"{name}-scenario.json").write_text(json.dumps(inputs[name]))
            def receipt(sec):
                path = source / f"positions-{t+sec}-{t+sec}.pb.gz"
                path.write_bytes(b"test position feed")
                return path
            first = receipt(0)
            receipt(6000)  # Beyond the control horizon.
            def score(scenario, stage, horizon):
                files = {p.name:control.fingerprint(p) for p in stage.iterdir()}
                receipt(500)  # Existing sample stays sealed while this file arrives.
                return {"scenario":scenario, "source":{"files":files,"captured_snapshots":len(files)},
                        "simulations":[]}
            with (patch.object(control,"CONTROL",base), patch.object(control,"run",side_effect=score),
                  patch.object(control,"update_report"), redirect_stdout(io.StringIO())):
                control.scan(inputs,source,t+660,t+5400)
                for name in inputs:
                    partial = control.read(sim / f"{name}-interim.json")
                    self.assertEqual(list(partial["source"]["files"]),[first.name])
                    self.assertEqual(partial["observation"]["state"],"interim")
                    self.assertFalse((sim / f"{name}-result.json").exists())
                control.scan(inputs,source,t+5500,t+5400)
                for name in inputs:
                    final = control.read(sim / f"{name}-result.json")
                    self.assertEqual(len(final["source"]["files"]),2)
                    self.assertEqual(final["observation"]["state"],"horizon_exhausted")
                    self.assertEqual(final["observation"]["receipt_cutoff_ist"],stamp(t+5400))
                first.unlink()
                with self.assertRaisesRegex(ValueError,"omits or changes"):
                    control.scan(inputs,source,t+5600,t+5400)
