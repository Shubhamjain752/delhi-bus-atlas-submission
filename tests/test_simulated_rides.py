import unittest

from tools.score_simulated_rides import crossing_reference, crossed, epoch, ride_case, stamp


class SimulatedRidesTest(unittest.TestCase):
    def setUp(self):
        self.t = epoch("2026-11-22T15:00:00+05:30")
        self.plan = {"departure":stamp(self.t), "legs":[{"board":"A","alight":"B"}]}

    def witness(self, vehicle, board, alight=None):
        def hit(limits):
            lo,hi = limits
            return {"lower":self.t+lo, "upper":self.t+hi, "received":self.t+hi+12,"evidence":"outside_to_inside_gps"}
        return {"run_token":vehicle,"route":"R","pattern_status":"serves","board":hit(board),
                "alight":hit(alight) if alight else None}

    def test_cannot_replace_unobserved_first_vehicle_with_convenient_second(self):
        first = self.witness("first",(15,25))
        second = self.witness("second",(40,50),(100,110))
        answer=ride_case(self.plan,[[first,second]],[0,0],1.25,0,[["R"]])
        self.assertEqual(answer["status"],"same_bus_alighting_not_witnessed")
        self.assertEqual(answer["events"][-1]["run_token"],"first")

    def test_readiness_inside_entry_bracket_is_unresolved(self):
        first = self.witness("first",(15,25),(70,80))
        answer=ride_case(self.plan,[[first]],[20,0],1,0,[["R"]])
        self.assertEqual(answer["status"],"boarding_interval_overlaps_readiness")

    def test_walk_wait_and_ride_reconcile_for_same_vehicle(self):
        first=self.witness("first",(15,25),(70,80))
        answer=ride_case(self.plan,[[first]],[10,20],1,0,[["R"]])
        self.assertEqual(answer["status"],"complete_simulation")
        self.assertEqual((answer["walking_seconds"],answer["waiting_seconds"],answer["riding_seconds"]),(30,10,55))
        self.assertEqual(answer["arrival_interval_ist"],[stamp(self.t+90),stamp(self.t+100)])

    def test_shape_crossing_requires_forward_aligned_evidence(self):
        target=(28.55,77.20)
        path=crossing_reference([(28.55,77.19),(28.55,77.21)],target)
        a,b=(28.55,77.199),(28.55,77.201)
        self.assertTrue(crossed([path],a,b))
        self.assertFalse(crossed([path],b,a))
        self.assertFalse(crossed([path],(28.551,77.199),b))
