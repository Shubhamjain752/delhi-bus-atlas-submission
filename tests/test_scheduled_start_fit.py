import sqlite3
import unittest
import numpy as np

from tools import fit_scheduled_starts as fit


class ScheduledStartMatching(unittest.TestCase):
    def test_unindexed_receipts_preserve_full_scan_on_unordered_batches(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE vehicle_feed(route_id REAL,timestamp TEXT,time TEXT,vehicle_id TEXT,trip_id TEXT,lat REAL,lng REAL)")
        fixtures = [
            (2,7,"100","00:01:45","A","one",28.7,77.1),
            (4,7,"100","00:01:45","B","two",28.7,77.1),
            (7,7,"100","00:05:01","C","three",28.7,77.1),
            (8,8,"100","00:01:45","D","four",28.7,77.1),
            (9,7,"100","00:01:45","B","conflict",28.7,77.1),
            (10,7,"100","00:01:45","E","five",28.7,77.1),
            (11,7,"130","00:02:15","A","one",28.71,77.1),
            (12,7,"130","00:02:15","B","two",28.71,77.1),
        ]
        db.executemany("INSERT INTO vehicle_feed(rowid,route_id,timestamp,time,vehicle_id,trip_id,lat,lng) VALUES(?,?,?,?,?,?,?,?)",fixtures)
        spans=fit.receipt_ranges(db)
        self.assertEqual(spans["00:01:45"],[(2,4),(8,10)])
        self.assertEqual(fit.receipt_ranges(db,block_size=3),spans)
        self.assertEqual(fit.captured_periods(spans),{0,1})
        plan=db.execute("EXPLAIN QUERY PLAN "+fit.RANGE_SQL,(2,4,"100",7,"00:01:45")).fetchall()
        self.assertTrue(any("SEARCH" in row[-1] and "INTEGER PRIMARY KEY" in row[-1] for row in plan),plan)
        queries=[(7,100,105,(28.7,77.1),0),(7,130,135,(28.71,77.1),0),(7,100,110,(28.7,77.1),0)]
        witnesses=[fit.fix_identities(db,*args,spans) for args in queries]
        self.assertEqual(witnesses,[{("A","one"),("E","five")},{("A","one"),("B","two")},set()])
        for schema in ("unindexed","indexed"):
            if schema=="indexed":
                db.execute("CREATE INDEX observed_idx ON vehicle_feed(timestamp)")
            self.assertEqual(witnesses,[fit.fix_identities(db,*args) for args in queries])

    def test_large_stop_ids_never_alias_another_edge(self):
        rows=np.array([[1,20,1374322],[1,40,635602],[1,21,1308786]],dtype=np.int32)
        keep=fit.origin_candidates({(1,20,1374322)},*rows.T)
        self.assertEqual(keep.tolist(),[True,False,False])

    def test_requires_same_fresh_vehicle_and_run_at_both_stops(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE vehicle_feed(route_id REAL, timestamp TEXT, time TEXT, vehicle_id TEXT, trip_id TEXT, lat TEXT, lng TEXT)")
        for t, receipt, bus, raw, lat in [
            (100, "00:01:45", "A", "7_00_00", "28.70"),
            (100, "00:01:45", "B", "7_00_00", "28.70"),
            (130, "00:02:15", "B", "7_00_00", "28.71"),
            (130, "00:02:15", "A", "7_01_00", "28.71"),
            (130, "00:02:15", "A", "7_00_00", "28.78")]:
            db.execute("INSERT INTO vehicle_feed VALUES(7,?,?,?,?,?,'77.1')",(str(t),receipt,bus,raw,lat))
        a = fit.fix_identities(db,7,100,105,(28.70,77.1),0)
        b = fit.fix_identities(db,7,130,135,(28.71,77.1),0)
        self.assertEqual(a & b, {("B","7_00_00")})
        self.assertEqual(fit.fix_identities(db,7,100,195,(28.70,77.1),0),set())

    def test_route_clock_requires_one_scheduled_service_and_one_witness(self):
        slot = (43200,"7_FULL_12_00",(7,20,21))
        dup = (43200,"7_SHORT_12_00",(7,20,21))
        self.assertEqual(fit.match_slot("7_12_00",7,43225,{}, {(7,720):[slot]}, (7,20,21)), slot)
        self.assertIsNone(fit.match_slot("7_12_00",7,43225,{}, {(7,720):[slot,dup]}, (7,20,21)))
        self.assertIsNone(fit.match_slot("7_12_00",7,43225,{}, {(7,720):[slot]}, (7,30,31)))
        self.assertIsNone(fit.match_slot("7_12_00",7,43225+8000,{}, {(7,720):[slot]}, (7,20,21)))
        # A raw ID reused by two buses cannot contribute a uniquely identified start.
        self.assertEqual(fit.count_matched({slot:{"A","B"}}),{})
        self.assertEqual(fit.count_matched({slot:{"A"}}),{slot[2]+(6,):1})

    def test_previous_service_day_and_clock_after_midnight(self):
        slot=(300,"7_FULL_24_05",(7,20,21))
        rows={(7,5):[slot]}
        self.assertEqual(fit.match_slot("7_24_05",7,360,{},rows,slot[2]),slot)
        self.assertEqual(fit.match_slot("7_00_05",7,360,{},rows,slot[2]),slot)
