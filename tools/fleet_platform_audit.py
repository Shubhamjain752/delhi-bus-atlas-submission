"""Evaluate the production GPS platform rules on every named route/date/hour case."""

import argparse
from collections import defaultdict
from datetime import date
from functools import lru_cache
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.replay_fleet_boarding import BASELINE, CASE_DIR, LABELS, OUTPUT, TimetableMatcher
from tools.replay_fleet_boarding import baseline_package, dump, fingerprint, read_day
from tools.fleet_positions import received_positions
from transit.replay import CapturedOverlay
from transit.router import Router

OUTPUT_DAYS = ROOT / "data/fleet-platform"
EVENTS = ("before_exit","after_exit","new_exit","lost_exit","revised_approach","unconfirmed","exit_overrides_approach")
FLOAT_PAIR = struct.Struct("ff")


class PlatformStudy:
    def __init__(self, baseline_type):
        self.router = Router(ROOT/"delhi_buses_static_gtfs_v1.zip",ROOT/"data",None,None)
        registry = CapturedOverlay(None,ROOT/"data",0).bind(self.router)
        self.matcher = TimetableMatcher(self.router, registry)
        self.ids = registry.ids
        self.baseline_type = baseline_type
        # Store the exact immutable geometry returned by the router, so subsequent
        # days need neither every Delhi shape in RAM nor another expensive stop alignment.
        table = ROOT/"data"/".fleet-shape-cache.sqlite"
        self.cache = sqlite3.connect(table,timeout=90)
        self.cache.execute("CREATE TABLE IF NOT EXISTS shapes(signature TEXT,pid INTEGER,value BLOB,PRIMARY KEY(signature,pid))")
        namespace = hashlib.sha256((ROOT/"delhi_buses_static_gtfs_v1.zip").read_bytes()+
                                   (ROOT/"transit/router.py").read_bytes()).hexdigest()
        original = self.router._geometry
        @lru_cache(maxsize=125)
        def shape(pid):
            stored = self.cache.execute("SELECT value FROM shapes WHERE signature=? AND pid=?", (namespace,pid)).fetchone()
            if stored:
                return json.loads(gzip.decompress(stored[0]))
            value = original(pid)
            self.cache.execute("INSERT OR IGNORE INTO shapes VALUES(?,?,?)",(namespace,pid,gzip.compress(
                json.dumps(value,separators=(",",":")).encode(),compresslevel=1,mtime=0)))
            return value
        self.router._geometry = shape

    def score(self,row,service_day,trip):
        issue, bus = row["ready"], row["bus"]
        pid,_ = self.ids[trip]
        index = self.router.patterns[pid].stops.index(row["stop"])
        before = self.baseline_type(None,ROOT/"data",issue)
        after = CapturedOverlay(None,ROOT/"data",issue)
        before.router = after.router = self.router
        for receipt,positions in received_positions(row["observations"]):
            if not all(issue-180 <= observed <= receipt <= issue for observed,_,_ in positions):
                raise ValueError("Invalid causal GPS position")
            # The older adapter kept the last witness in a poll. The revised
            # adapter clears the track for a vehicle carrying conflicting fixes.
            observed,lat,lon = positions[-1]
            location = FLOAT_PAIR.unpack(FLOAT_PAIR.pack(lat,lon))
            witness = {bus:(service_day.replace("-",""),str(row["route_id"]),trip,observed,location,None)}
            before.update_exit_tracks(witness,receipt)
            after.update_exit_tracks(witness if len(positions)==1 else {bus:None},receipt)
            for overlay in (before,after):
                overlay.position_received = receipt
                overlay.position_snapshot = receipt,{}
        args = date.fromisoformat(service_day),pid,index,(bus,trip),issue
        age, approach = after.boarding_position(*args)
        return [bool(before.passed_platform(*args)),bool(after.passed_platform(*args)),bool(approach),age]

    def score_day(self,day,manifest,signature):
        path = OUTPUT_DAYS/(day+".json.gz")
        if path.is_file():
            with gzip.open(path,"rt") as f:
                saved = json.load(f)
            if saved.get("signature")==signature:
                return
        cases = sorted(read_day(day,manifest),key=lambda row:(row["route_id"],row["stop"],row["hour"]))
        result = []
        for row in cases:
            why,service,trip = self.matcher.match(row)
            if (why is None) != row["eligible_named_case"]:
                raise ValueError("Dated match disagreement")
            if why is None:
                score = self.score(row,service,trip)
                result.append([row["route_id"],row["hour"],row["ready"],row["outcome"],*score])
        dump(path, {"signature":signature,"day":day,"rows":result})
        self.cache.commit()
        print(day,len(result),"matched platform checks",flush=True)


def counts():
    return {"matched":0, **{key:dict.fromkeys(sorted(LABELS),0) for key in EVENTS}}


def evaluate(groups, label, before, after, approach):
    groups["matched"] += 1
    states = (before,after,after and not before,before and not after,approach and not after,
              not after and not approach,after and approach)
    for name,active in zip(EVENTS,states):
        if active:
            groups[name][label] += 1


def publish(signature):
    pairs = {}
    matched = 0
    for split in ("dev","holdout"):
        with gzip.open(OUTPUT/(split+".json.gz"),"rt") as f:
            rows = json.load(f)["cases"]
        matched += sum(row["eligibility"] is None for row in rows)
        for row in rows:
            revised = row.get("revised",{})
            if revised.get("offered"):
                pairs[row["ready"],row["route_id"]] = revised["vehicle_approaching"],revised["gps_age_seconds"]
    groups, times, routes = defaultdict(counts),defaultdict(counts),defaultdict(counts)
    checked, seen = 0,set()
    for day_no in range(1,32):
        day=f"2026-10-{day_no:02}"
        with gzip.open(OUTPUT_DAYS/(day+".json.gz"),"rt") as f:
            result=json.load(f)
        if result["day"]!=day or result["signature"]!=signature:
            raise ValueError("Incompatible GPS audit inputs")
        for route,hour,issue,label,before,after,approach,age in result["rows"]:
            key=day,route,hour
            if key in seen:
                raise ValueError("Duplicate matched route/date/hour")
            seen.add(key)
            subsets=["all","holdout" if day_no>=24 else "development"]
            if hour<6:
                subsets.append("overnight")
                if day_no>=24:
                    subsets.append("holdout_overnight")
            for table in [routes[route],times[hour],*(groups[name] for name in subsets)]:
                evaluate(table,label,before,after,approach)
            if (issue,route) in pairs:
                if after or pairs[issue,route]!=(approach,age):
                    raise ValueError("Local GPS classification differs from full production API offer")
                checked+=1
    if checked!=len(pairs) or len(seen)!=matched:
        raise ValueError("The full matched cohort or emitted offer parity check is incomplete")
    report={"complete":True,"signature":signature,"matched":matched,"actual_offer_parity_checks":checked,
            "groups":groups,"by_hour":times,"by_route":routes,
            "rule_scope":"Independent platform departure and approach predicates, evaluated before itinerary search.",
            "fields":["route_id","hour","ready","outcome","before_exit","after_exit","vehicle_approaching","gps_age_seconds"]}
    out=ROOT/"data/fleet-platform.json"
    pending=out.with_suffix(".pending")
    pending.write_text(json.dumps(report,separators=(",",":")),encoding="utf-8")
    pending.replace(out)
    print(matched,"matched route-date-hours;",checked,"full-offer parity checks",flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days",nargs="+",help="Complete days to score from the GPS collector")
    parser.add_argument("--wait",action="store_true",help="Process complete days as their GPS cases arrive")
    parser.add_argument("--publish",action="store_true")
    args=parser.parse_args()
    raw=(CASE_DIR/"manifest.json").read_bytes()
    manifest=json.loads(raw)
    freeze=fingerprint()
    if freeze!=json.loads((ROOT/"docs/fleet-policy-freeze.json").read_text()):
        raise ValueError("Frozen policy changed")
    sources=(raw,BASELINE.read_bytes(),Path(__file__).read_bytes(),(ROOT/"tools/fleet_positions.py").read_bytes(),
             json.dumps(freeze,sort_keys=True).encode())
    signature=hashlib.sha256(b"".join(sources)).hexdigest()
    if args.publish:
        publish(signature)
        return
    with baseline_package() as package:
        study=PlatformStudy(importlib.import_module(package+".replay").CapturedOverlay)
        pending = args.days or [f"2026-10-{n:02}" for n in range(1,32)]
        while pending:
            available = [day for day in pending if (CASE_DIR/(day+".json.gz")).is_file()]
            if not available:
                if not args.wait:
                    raise FileNotFoundError("GPS case files are not complete for "+", ".join(pending))
                time.sleep(20)
                continue
            for day in available:
                study.score_day(day,manifest,signature)
                pending.remove(day)
        study.cache.close()


if __name__=="__main__":
    main()
