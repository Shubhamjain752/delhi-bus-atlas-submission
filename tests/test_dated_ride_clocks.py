import unittest
from tools.score_dated_rides import ride_case
from tools.score_simulated_rides import epoch, stamp


class DatedClocks(unittest.TestCase):
    def test_half_second_passages_and_odd_walks_reconcile(self):
        dep = "2026-11-22T15:00:00+05:30"
        start = epoch(dep)
        def passage(t, width):
            return {"lower":start+t, "upper":start+t+width, "received":start+t+width+10, "evidence":"outside_to_inside_gps"}
        def run(bus, board, alight):
            return {"board":passage(board,2), "alight":passage(alight,1), "run_token":bus, "route":"R", "pattern_status":"serves"}
        p = {"departure":dep, "legs":[{"board":"A","alight":"B"},{"board":"C","alight":"D"}]}
        candidates = [[run("first",10,20)],[run("second",26,40)]]
        result = ride_case(p,candidates,[0,1,1],1,0,[["R"],["R"]])
        self.assertEqual(result["arrival_interval_ist"], [stamp(start+41),stamp(start+42)])
        self.assertEqual(result["events"][4]["at"], stamp(start+21))
        self.assertEqual((result["walking_seconds"],result["waiting_seconds"],result["riding_seconds"]),(2,17,22))
        self.assertEqual(result["door_to_door_seconds_proxy"], 41)
        self.assertEqual([e["run_token"] for e in result["events"] if e["kind"]=="board"],["first","second"])
