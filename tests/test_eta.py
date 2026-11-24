import tempfile
import unittest
import gzip
import json
from datetime import datetime
from pathlib import Path

from transit.eta import EtaModel, IST


class EtaTest(unittest.TestCase):
    def setUp(self):
        self.clock = int(datetime(2026, 10, 15, 8, tzinfo=IST).timestamp())
        self.fit = {"pairs":{"1|a|b":[[20, 100], {"4":[4,80]}]}, "routes":{},
                    "metadata":{"train":["2026-10-12","2026-10-13"]}}
        self.replay = {
            "segments":[[self.clock + 20, self.clock - 10, "1", "a","b", 200]],
            "passages":[[self.clock + 20, self.clock - 10, "1","run-1","b"]]}
        self.model = EtaModel(self.fit, self.replay)

    def test_future_receipt_cannot_change_estimate_or_position(self):
        history = self.model.duration("1","a","b",8*3600,120)
        self.assertEqual(self.model.duration("1","a","b",8*3600,120,self.clock),history)
        self.assertEqual(self.model.recent_passages(self.clock),[])
        later = self.model.duration("1","a","b",8*3600,120,self.clock+21)
        self.assertEqual(later[1],"gps_historical_recent")
        self.assertGreater(later[0],history[0])
        self.assertEqual(self.model.recent_passages(self.clock + 21)[0]["stop_id"],"b")
        self.assertEqual(self.model.recent_passages(self.clock + 181), [])
        self.assertEqual(self.model.duration("1","a","b",8*3600,120,self.clock+86400),history)

    def test_times_before_training_back_off(self):
        before = datetime(2026,10,2,8,tzinfo=IST)
        self.assertEqual(self.model.duration("1","a","b",8*3600,120,before),(120,"timetable"))

    def test_monthly_history_respects_forecast_clock_service_date_and_october_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix, training, ratio in (("", "2026-10-12", 1.5), ("from-2026-11", "2026-10-30", 2.0)):
                for offset, folder in ((0, root / prefix), (1, root / prefix / "weekend")):
                    folder.mkdir(parents=True, exist_ok=True)
                    meta = {"train": [training]}
                    if prefix:
                        meta["cutoff_exclusive"] = "2026-11-01"
                    fit = {"metadata": meta, "routes": {"1": [ratio + offset, {}]}}
                    (folder / "eta_model.json.gz").write_bytes(gzip.compress(json.dumps(fit).encode()))
            (root / "eta_replay.json.gz").write_bytes(gzip.compress(json.dumps(self.replay).encode()))
            model = EtaModel.load(root, include_forward=True)
            def seconds(issue, service):
                return model.duration("1", "a", "b", 0, 100, datetime.fromisoformat(issue).replace(tzinfo=IST), service)
            self.assertEqual(seconds("2026-11-02T08:00", "2026-11-02"), (200, "gps_historical"))
            self.assertEqual(seconds("2026-11-01T01:00", "2026-11-01"), (300, "gps_historical"))
            self.assertEqual(seconds("2026-11-01T01:00", "2026-10-31"), (250, "gps_historical"))
            self.assertEqual(seconds("2026-10-31T23:59", "2026-11-02"), (150, "gps_historical"))
            self.assertEqual(seconds("2026-10-05T09:00", "2026-10-05"), (100, "timetable"))
            self.assertEqual(seconds("2026-10-15T08:01", "2026-10-15")[1], "gps_historical_recent")

    def test_untrained_route_and_reversed_edge_back_off(self):
        self.assertEqual(self.model.duration("1","b","a",8*3600,120), (120,"timetable"))
        with tempfile.TemporaryDirectory() as path:
            self.assertEqual(EtaModel.load(Path(path)).duration("unknown","a","b",0,50),
                             (50,"timetable"))


if __name__ == "__main__":
    unittest.main()
