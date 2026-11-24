"""Check observed movements away from a route origin against scheduled, dated trips.

Needs the official timetable, original daily SQLite archive and corresponding month_eta_labels.
Intermediate daily checkpoints contain only counts. A missing match is unknown.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.month_boarding_scan import ARCHIVE, MONTH, PREVIOUS_EXTRACTS, archive_members, daily_database
from tools.train_eta import format_clock, open_db
from transit.live import LiveOverlay
from transit.router import IST, Router, approximate_m, coords

RADIUS = 45
MIN_SEPARATION = 2*RADIUS
EARLY = 5*60
LATE = 60*60


FIX_COLUMNS = "SELECT vehicle_id,trip_id,lat,lng FROM vehicle_feed"
FIX_FILTER = "timestamp=? AND route_id=? AND time=?"
RANGE_SQL = FIX_COLUMNS + " NOT INDEXED WHERE rowid BETWEEN ? AND ? AND " + FIX_FILTER


def receipt_ranges(db, block_size=32768):
    """Read the table in bounded SQLite batches; verify exact spans even when receipt clocks reverse."""
    first=db.execute("SELECT min(rowid) FROM vehicle_feed").fetchone()[0]
    last=db.execute("SELECT max(rowid) FROM vehicle_feed").fetchone()[0]
    spans=defaultdict(list)
    if first is None:
        return spans
    previous=object()
    for start in range(first,last+1,block_size):
        end=start+block_size
        groups=sorted(db.execute(
            "SELECT min(rowid),max(rowid),time FROM vehicle_feed NOT INDEXED "
            "WHERE rowid>=? AND rowid<? GROUP BY time",(start,end)))
        if any(a[1]>=b[0] for a,b in zip(groups,groups[1:])):
            # A was seen, then B, then A again in this block. Split exactly at
            # each transition. Normal monotonically appended packets avoid this Python scan.
            groups=((i,i,t) for i,t in db.execute(
                "SELECT rowid,time FROM vehicle_feed WHERE rowid>=? AND rowid<? ORDER BY rowid",(start,end)))
        for lo,hi,clock in groups:
            if previous==clock:
                lo=spans[clock][-1][0]
                spans[clock][-1]=(lo,hi)
            else:
                spans[clock].append((lo,hi))
            previous=clock
    return spans


def captured_periods(spans):
    periods = set()
    for clock in spans:
        if not isinstance(clock,str) or len(clock)!=8:
            continue
        try:
            h,m,s = map(int,clock.split(":"))
        except ValueError:
            continue
        if 0<=h<24 and 0<=m<60 and 0<=s<60:
            periods.add((h*3600+m*60+s)//300)
    return periods


def fix_identities(db, route, observed, received, point, midnight, spans=None):
    if not 0<=received-observed<=90 or not 0<=received-midnight<86400:
        return set()
    clock = format_clock(received-midnight)
    params = (str(observed),route,clock)
    if spans is None:
        rows = db.execute(FIX_COLUMNS + " WHERE " + FIX_FILTER,params)
    else:
        rows = (row for start,end in spans.get(clock,())
                for row in db.execute(RANGE_SQL,(start,end,*params)))
    vehicles = defaultdict(set)
    for bus,run,lat,lon in rows:
        if bus and run and lat and lon:
            vehicles[bus].add((run,float(lat),float(lon)))
    result = set()
    for vehicle,claims in vehicles.items():
        if len(claims)==1:
            raw,lat,lon = next(iter(claims))
            if approximate_m((lat,lon),point)<=RADIUS:
                result.add((vehicle,raw))
    return result


def match_slot(raw, route, observed_second, exact, clocks, edge):
    slots = exact.get((route,raw))
    if slots is None:
        minute = LiveOverlay.provider_clock(str(route),raw)
        if minute is None:
            return None
        slots = clocks.get((route,minute%1440),())
    # A near-time run may use a previous GTFS service day. Never resolve
    # duplicate start clocks by selecting whichever vehicle happened to be nearest.
    eligible = [s for s in slots if -EARLY <= observed_second-s[0] <= LATE]
    if len(eligible)==1 and eligible[0][2]==edge:
        return eligible[0]


def count_matched(witnesses):
    return Counter(s[2]+(s[0]//7200,) for s,vehicles in witnesses.items() if len(vehicles)==1)


def origin_candidates(eligible, routes, first, second):
    """Match complete ordered edges, including Delhi's seven-digit stop IDs."""
    dtype=[("route","<i4"),("first","<i4"),("second","<i4")]
    labels=np.empty(len(routes),dtype=dtype)
    labels["route"],labels["first"],labels["second"]=routes,first,second
    return np.isin(labels,np.array(list(eligible),dtype=dtype))


def schedule_index(router, day):
    exact, clocks, slots = defaultdict(list), defaultdict(list), set()
    for shift in (-1,0,1):
        services = router._active(day+timedelta(days=shift))
        for p in router.patterns:
            if p.service not in services or len(p.stops)<2:
                continue
            route=int(p.route)
            a,b=p.stops[:2]
            edge=(route,int(a),int(b))
            valid=(p.pickups[0]==0 and p.stops.count(a)==p.stops.count(b)==1
                   and approximate_m(coords(router.stops[a]),coords(router.stops[b]))>MIN_SEPARATION)
            for trip,start in zip(p.trips,p.starts):
                sec=shift*86400+start+p.departures[0]
                if not -LATE <= sec < 86400+EARLY:
                    continue
                # Ineligible patterns may still share the same route and clock.
                slot=(sec,trip,edge if valid else None)
                exact[route,trip].append(slot)
                clocks[route,(sec//60)%1440].append(slot)
                if valid and 0<=sec<86400:
                    slots.add(slot)
    return exact,clocks,slots


def process_day(day, options, router, members, seven, feed_hash, code_hash):
    label=options.labels/(day+".npz")
    manifest=options.manifests[day]
    if manifest["source_member_crc32"]!=members[day]["crc32"]:
        raise ValueError("Different GPS archive for "+day)
    with label.open("rb") as source:
        label_hash=hashlib.file_digest(source,"sha256").hexdigest()
    if label_hash!=manifest["labels_sha256"]:
        raise ValueError("Changed GPS movement labels for "+day)
    signature=[feed_hash,code_hash,label_hash,members[day]["crc32"]]
    cache=options.scratch/(day+".json")
    if cache.exists():
        previous=json.loads(cache.read_bytes())
        if previous["signature"]==signature:
            return previous
    civil=date.fromisoformat(day)
    midnight=int(datetime.combine(civil,time(),IST).timestamp())
    exact,clocks,slots=schedule_index(router,civil)
    all_edges={s[2] for s in slots}
    with np.load(label,allow_pickle=False) as values:
        keep=origin_candidates(all_edges,values["route"],values["from_stop"],values["to_stop"])
        keys=("route","from_stop","to_stop","start","end","issue_received","received")
        records=np.column_stack([values[key][keep] for key in keys])
    witnesses=defaultdict(set)
    conflicting=set()
    with daily_database(seven,ARCHIVE,members[day],options.extracts,options.scratch) as (path,source_kind):
        if source_kind=="existing_read_only":
            crc=0
            with path.open("rb") as original:
                for block in iter(lambda:original.read(8*1024**2),b""):
                    crc=zlib.crc32(block,crc)
            if f"{crc:08X}"!=members[day]["crc32"]:
                raise ValueError("Existing GPS database differs from source archive for "+day)
        with open_db(path) as db:
            # Exclude scheduled five-minute periods for which no receiver packet exists.
            spans=receipt_ranges(db)
            captured=captured_periods(spans)
            eligible={s for s in slots if s[0]//300 in captured}
            plan=db.execute("EXPLAIN QUERY PLAN "+FIX_COLUMNS+" WHERE "+FIX_FILTER,
                            ("0",0,"00:00:00")).fetchall()
            lookup_spans=None if any("SEARCH" in r[-1] and "timestamp=?" in r[-1] for r in plan) else spans
            for row in records:
                r,a,b,start,end,heard,confirmed=map(int,row)
                if not midnight<=start<end<midnight+86400 or not 5<=end-start<=1200:
                    continue
                first=fix_identities(db,r,start,heard,coords(router.stops[str(a)]),midnight,lookup_spans)
                if not first:
                    continue
                second=fix_identities(db,r,end,confirmed,coords(router.stops[str(b)]),midnight,lookup_spans)
                for bus,raw in first & second:
                    slot=match_slot(raw,r,start-midnight,exact,clocks,(r,a,b))
                    if slot not in eligible:
                        continue
                    if len(first & second)!=1:
                        conflicting.add(slot)
                    witnesses[slot].add(bus)
    for slot in conflicting:
        witnesses.pop(slot,None)
    scheduled=Counter(s[2]+(s[0]//7200,) for s in eligible)
    matched=count_matched(witnesses)
    assert not matched.keys()-scheduled.keys()
    rows=[[*key,n,matched[key]] for key,n in sorted(scheduled.items())]
    result=dict(signature=signature,day=day,rows=rows,captured_five_minute_periods=len(captured),
                candidate_origin_movements=len(records),multiple_origin_claims=len(conflicting|{s for s,v in witnesses.items() if len(v)>1}))
    tmp=cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(result,separators=(",",":")),encoding="utf-8")
    tmp.replace(cache)
    print(day,len(records),"origin movements,",sum(matched.values()),"matched of",sum(scheduled.values()),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels",required=True,type=Path)
    parser.add_argument("--label-manifest",type=Path,help="Fingerprint manifest created by month_eta_labels --verify-checkpoints")
    parser.add_argument("--scratch",required=True,type=Path)
    parser.add_argument("--extracts",type=Path,default=PREVIOUS_EXTRACTS)
    parser.add_argument("--seven",default=shutil.which("7z") or "C:/Program Files/7-Zip/7z.exe")
    parser.add_argument("--dates",nargs="+",choices=MONTH,default=MONTH)
    parser.add_argument("--workers",type=int,choices=(1,2,3,4),default=1)
    parser.add_argument("--output",type=Path,default=ROOT/"data/from-2026-11/scheduled-start-check.json.gz")
    options=parser.parse_args()
    options.scratch.mkdir(parents=True,exist_ok=True)
    manifest_path=options.label_manifest or options.labels.parent/"complete-label-manifest.json"
    options.manifests=json.loads(manifest_path.read_bytes())["dates"]
    members=archive_members(options.seven,ARCHIVE)
    feed=ROOT/"delhi_buses_static_gtfs_v1.zip"
    with feed.open("rb") as f:
        digest=hashlib.file_digest(f,"sha256").hexdigest()
    if json.loads((options.labels/"protocol.json").read_bytes())["official_gtfs_sha256"]!=digest:
        raise ValueError("Timetable differs from traversal label training")
    code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    router=Router(feed,ROOT/"data",None,None)
    dates=sorted(set(options.dates))
    buckets={}
    work_order=sorted(dates,key=lambda d: not (options.extracts/members[d]["member"]).is_file())
    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        jobs=[pool.submit(process_day,d,options,router,members,options.seven,digest,code_hash) for d in work_order]
        for job in jobs:
            day=job.result()
            typ="weekend" if date.fromisoformat(day["day"]).weekday()>=5 else "weekday"
            for route,a,b,band,scheduled,matched in day["rows"]:
                key=f"{route}|{a}|{b}|{typ}|{band}"
                row=buckets.setdefault(key,dict(scheduled=0,matched=0,observed_dates=[]))
                row["observed_dates"].append(day["day"])
                row["scheduled"]+=scheduled
                row["matched"]+=matched
                assert 0<=row["matched"]<=row["scheduled"]
    for row in buckets.values():
        row["observed_dates"].sort()
    output=dict(version=1,available_from="2026-11-01",feed_sha256=digest,dates=dates,buckets=buckets)
    encoded=json.dumps(output,separators=(",",":"),sort_keys=True).encode()
    dest=options.output
    tmp=dest.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(encoded,compresslevel=6,mtime=0))
    tmp.replace(dest)
    print(len(buckets),"buckets;",sum(r["matched"] for r in buckets.values()),"matched of",
          sum(r["scheduled"] for r in buckets.values()),"scheduled;",dest.stat().st_size,"bytes",flush=True)


if __name__=="__main__":
    main()
