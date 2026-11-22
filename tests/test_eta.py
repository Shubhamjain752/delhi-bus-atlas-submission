import tempfile
import unittest
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

    def test_untrained_route_and_reversed_edge_back_off(self):
        self.assertEqual(self.model.duration("1","b","a",8*3600,120), (120,"timetable"))
        with tempfile.TemporaryDirectory() as path:
            self.assertEqual(EtaModel.load(Path(path)).duration("unknown","a","b",0,50),
                             (50,"timetable"))


if __name__ == "__main__":
    unittest.main()
